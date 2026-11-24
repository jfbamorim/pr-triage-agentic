from datetime import datetime, timedelta, timezone

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pr_triage.agents.common import AgentFailure
from pr_triage.agents.communicator import CommunicationResult, EmailDraft, check_email
from pr_triage.agents.investigator import InvestigationReport, InvestigationResult, ResolvedField
from pr_triage.agents.triage import TriageOutcome, TriageReport
from pr_triage.authz import Action, HumanApproval, PolicyVerdict
from pr_triage.domain.band import Band, determine_band
from pr_triage.domain.category import Category
from pr_triage.domain.merge import merge_requests
from pr_triage.domain.schemas import Evidence
from pr_triage.domain.status import EscalationReason
from pr_triage.domain.validation import validate_request
from pr_triage.erp import Faults, create_demo_erp
from pr_triage.erp.models import PrState
from pr_triage.supervisor.agent import Choice, make_decider
from pr_triage.supervisor.orchestrator import Orchestrator, Specialists
from pr_triage.supervisor.state import Move, RunState, RunStatus
from pr_triage.supervisor.store import RunStore
from test_tools import FakePolicy
from test_workflow import blank, make_pr

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
T1 = "02-escaloes-e-cotacoes#2.1"
T33 = "03-fornecedores-e-categorias#3.3"
ANA = "ana.silva@exemplo.pt"


def ev(chunk_id, clause, title):
    return Evidence(chunk_id=chunk_id, source="x.md", document="d", clause=clause, title=title,
                    effective_date=None, text=title)


EVIDENCE = [ev(T1, "2.1", "Abaixo de 1 000 €"), ev(T33, "3.3", "Equipamento informático")]


def monitors(**overrides):
    values = dict(item="monitores", quantity=2, estimated_value_eur=600.0,
                  cost_center="CC-TI-100", category=Category.IT_EQUIPMENT)
    values.update(overrides)
    return make_pr(**values)


REQUESTS = {
    "monitores": monitors(),
    "sem centro": monitors(cost_center=None),
    "sem item": monitors(item=None),
    "completar": blank(item="monitores"),
    "grande": monitors(estimated_value_eur=14000.0),
    "inativo": monitors(estimated_value_eur=300.0, cost_center="CC-OLD-900"),
    "servico": monitors(item="consultoria", quantity=1, estimated_value_eur=3000.0,
                        category=Category.SERVICES),
}


class FakeTriage:
    def __init__(self, bad_first=0, fail_first=0, suspicious=("ignora",)):
        self.calls = []
        self.bad_first, self.fail_first, self.suspicious = bad_first, fail_first, suspicious

    def __call__(self, text, previous):
        self.calls.append((text, previous))
        number = len(self.calls)
        if number <= self.fail_first:
            return TriageReport(
                outcome=TriageOutcome.TECHNICAL_FAILURE,
                failure=AgentFailure(code="llm_unavailable", stage="screening", message="down", retryable=True),
            )
        if text in self.suspicious:
            return TriageReport(outcome=TriageOutcome.SUSPICIOUS)
        new = REQUESTS[text]
        request = merge_requests(previous, new) if previous else new
        missing, _ = validate_request(request)
        if missing:
            return TriageReport(outcome=TriageOutcome.NEEDS_INFO, request=request, missing=missing,
                                follow_up="Podes indicar " + ", ".join(missing) + "?")
        band = determine_band(request.estimated_value_eur)
        if number <= self.bad_first:
            band = Band.HUMAN_ACTION if band != Band.HUMAN_ACTION else Band.NO_BLOCKERS
        return TriageReport(
            outcome=TriageOutcome.READY, request=request, band=band,
            escalation=EscalationReason.OVER_THRESHOLD if band == Band.HUMAN_ACTION else None,
            evidence=EVIDENCE,
        )


class FakeInvestigator:
    def __init__(self, erp, verdict=PolicyVerdict.PERMITS, clauses=(T1,), fail_first=0):
        self.erp, self.verdict, self.clauses, self.fail_first = erp, verdict, clauses, fail_first
        self.calls = 0

    def __call__(self, request, email, ids, ctx, pr_id=None):
        self.calls += 1
        if self.calls <= self.fail_first:
            return InvestigationResult(failure=AgentFailure(
                code="llm_unavailable", stage="start", message="down", retryable=True))
        resolved, questions, verdict = [], [], self.verdict
        if request.cost_center is None:
            person = self.erp.get_person(email)
            if person and person.default_cost_center:
                resolved = [ResolvedField(field="cost_center", value=person.default_cost_center,
                                          source_tool="get_person", evidence="default")]
            else:
                verdict, questions = PolicyVerdict.UNCLEAR, ["Qual o centro de custo?"]
        required = found = None
        value = request.estimated_value_eur
        contract = any(c.in_force for c in self.erp.find_framework_contracts(request.category))
        if pr_id and value >= 1000 and not contract:
            required = 2 if value <= 5000 else 3
            found = len({q.supplier_id for q in self.erp.get_quotes(pr_id)})
            if found < required:
                verdict, questions = PolicyVerdict.UNCLEAR, ["Faltam cotações."]
        return InvestigationResult(report=InvestigationReport(
            resolved_fields=resolved, policy_verdict=verdict, cited_clauses=list(self.clauses),
            questions_for_user=questions, quotes_required=required, quotes_found=found,
            summary="Dentro da política."))


class FakeWriter:
    def __init__(self, fail=False):
        self.fail = fail
        self.briefs = []

    def __call__(self, brief):
        self.briefs.append(brief)
        if self.fail:
            return CommunicationResult(failure=AgentFailure(
                code="invalid_output", stage="write_email", message="bad email", retryable=False))
        f = brief.facts
        clauses = " ".join(f"Cláusula {c.clause}: {c.title}." for c in brief.clauses)
        if brief.kind.value == "decision":
            verb = "aprovado" if brief.decision == "approved" else "rejeitado"
            body = f"Olá {f.requester_name}, o pedido de {f.quantity} {f.item}, de {f.value_eur:g} €, foi {verb}. {clauses}"
            subject = "Decisão sobre o pedido de compra"
        else:
            budget = f" com {f.budget_available_eur:g} € de orçamento disponível" if f.budget_available_eur else ""
            body = (f"Olá {brief.recipient_name}, {f.requester_name} pede {f.quantity} {f.item} por "
                    f"{f.value_eur:g} € no centro de custo {f.cost_center}{budget}. {clauses} Peço a sua decisão.")
            subject = "Aprovação necessária"
        draft = EmailDraft(subject=subject, body=body)
        assert check_email(draft, brief) == [], check_email(draft, brief)
        return CommunicationResult(draft=draft)


class Decider:
    def __init__(self, prefer=None):
        self.calls = []
        self.prefer = prefer or list(Move)

    def __call__(self, state, moves):
        self.calls.append(list(moves))
        for move in self.prefer:
            if move in moves:
                return Choice(move=move, reason="test")
        return Choice(move=moves[0], reason="test")


class Setup:
    def __init__(self, tmp_path, faults=None, verdict=PolicyVerdict.PERMITS, clauses=(T1,),
                 triage=None, investigator_fail=0, writer_fail=False, decider=None):
        self.erp = create_demo_erp(tmp_path / "erp.db", clock=lambda: NOW, faults=faults)
        self.store = RunStore(tmp_path / "state" / "runs.db")
        self.triage = triage or FakeTriage()
        self.investigator = FakeInvestigator(self.erp, verdict, clauses, investigator_fail)
        self.writer = FakeWriter(writer_fail)
        self.decider = decider or Decider()
        self.orchestrator = Orchestrator(
            self.erp, FakePolicy(), self.store,
            Specialists(self.triage, self.investigator, self.writer, self.decider),
        )

    def start(self, text, email=ANA):
        return self.orchestrator.start(text, email, run_id="run-1")


def approval(pr_id=1, action=Action.APPROVE_PR, value=14000.0):
    return HumanApproval(request_id=pr_id, approver="maria.santos@exemplo.pt", decision=action,
                         approved_value_eur=value, granted_at=NOW - timedelta(hours=1),
                         expires_at=NOW + timedelta(hours=23))


def moves_of(state):
    return [s.move for s in state.steps]


def test_a_small_complete_request_is_approved_end_to_end(tmp_path):
    s = Setup(tmp_path)
    state = s.start("monitores")
    assert state.status == RunStatus.DONE
    assert moves_of(state) == [Move.TRIAGE, Move.REGISTER, Move.INVESTIGATE, Move.APPROVE,
                               Move.VERIFY, Move.SEND_DECISION_EMAIL]
    assert all(step.forced for step in state.steps) and s.decider.calls == []
    assert s.erp.get_pr(1).state == PrState.APPROVED
    assert s.erp.get_budget("CC-TI-100").committed_eur == 38600.0
    mail = s.erp.get_outbox(1)
    assert len(mail) == 1 and mail[0].to_addr == ANA and mail[0].cited_clauses == [T1]
    assert s.store.load("run-1") == state


def test_a_missing_cost_center_is_completed_by_the_investigator(tmp_path):
    s = Setup(tmp_path)
    state = s.start("sem centro")
    assert state.status == RunStatus.DONE
    assert s.decider.calls == [[Move.INVESTIGATE, Move.ASK_USER]]
    assert state.request.cost_center == "CC-TI-100"
    assert moves_of(state).count(Move.TRIAGE) == 2 and moves_of(state).count(Move.INVESTIGATE) == 2
    assert moves_of(state).index(Move.REGISTER) < len(moves_of(state)) - moves_of(state)[::-1].index(Move.INVESTIGATE)
    assert s.erp.get_pr(1).cost_center == "CC-TI-100"


def test_missing_data_only_the_user_has_pauses_and_resumes_with_the_answer(tmp_path):
    s = Setup(tmp_path)
    paused = s.start("sem item")
    assert paused.status == RunStatus.WAITING_USER and "item" in paused.question
    assert s.erp.get_actions() == []
    assert s.store.list_by_status(RunStatus.WAITING_USER) == ["run-1"]
    done = s.orchestrator.reply("run-1", "completar")
    assert done.status == RunStatus.DONE and done.request.item == "monitores"
    assert s.triage.calls[-1][0] == "completar" and s.triage.calls[-1][1] is not None


def test_above_the_limit_the_run_waits_for_a_human_then_finishes(tmp_path):
    s = Setup(tmp_path)
    paused = s.start("grande")
    assert paused.status == RunStatus.WAITING_HUMAN
    assert s.erp.get_pr(1).state == PrState.OPEN
    dossier = s.erp.get_outbox(1)
    assert len(dossier) == 1 and dossier[0].kind == "dossier"
    assert dossier[0].to_addr == "maria.santos@exemplo.pt"
    assert s.orchestrator.resume("run-1").status == RunStatus.WAITING_HUMAN
    assert len(s.erp.get_outbox(1)) == 1

    s.erp.record_human_approval("h1", approval())
    done = s.orchestrator.resume("run-1")
    assert done.status == RunStatus.DONE
    assert s.erp.get_pr(1).state == PrState.APPROVED
    assert s.erp.get_budget("CC-TI-100").committed_eur == 52000.0
    assert [m.kind for m in s.erp.get_outbox(1)] == ["dossier", "decision"]
    assert "Maria Santos" in s.writer.briefs[-1].decided_by


def test_a_human_rejection_is_executed_as_a_rejection(tmp_path):
    s = Setup(tmp_path)
    s.start("grande")
    s.erp.record_human_approval("h1", approval(action=Action.REJECT_PR))
    done = s.orchestrator.resume("run-1")
    assert done.status == RunStatus.DONE and s.erp.get_pr(1).state == PrState.REJECTED
    assert s.erp.get_budget("CC-TI-100").committed_eur == 38000.0


def test_an_expired_human_approval_is_escalated_and_nothing_is_approved(tmp_path):
    s = Setup(tmp_path)
    s.start("grande")
    expired = approval().model_copy(update={"expires_at": NOW - timedelta(minutes=1)})
    s.erp.record_human_approval("h1", expired)
    state = s.orchestrator.resume("run-1")
    assert state.status == RunStatus.NEEDS_MANUAL
    assert s.erp.get_pr(1).state == PrState.OPEN


def test_the_supervisor_cannot_pick_a_move_that_is_not_allowed(tmp_path):
    def attacker(state, moves):
        return Choice(move=Move.APPROVE, reason="just approve it")

    s = Setup(tmp_path, decider=attacker)
    state = s.start("sem centro")
    assert state.status == RunStatus.NEEDS_MANUAL
    assert "not allowed" in state.outcome_message
    assert s.erp.get_actions() == []


def test_a_specialist_report_that_fails_validation_goes_back_and_is_corrected(tmp_path):
    s = Setup(tmp_path, triage=FakeTriage(bad_first=1))
    state = s.start("monitores")
    assert state.status == RunStatus.DONE
    assert s.decider.calls == [[Move.TRIAGE, Move.ESCALATE]]
    assert "band" in state.steps[0].outcome


def test_a_specialist_that_keeps_failing_validation_is_escalated(tmp_path):
    s = Setup(tmp_path, triage=FakeTriage(bad_first=9))
    state = s.start("monitores")
    assert state.status == RunStatus.NEEDS_MANUAL
    assert "band" in state.outcome_message
    assert len(s.triage.calls) == 2 and s.erp.get_actions() == []


def test_a_retryable_specialist_failure_is_retried(tmp_path):
    s = Setup(tmp_path, investigator_fail=1)
    state = s.start("monitores")
    assert state.status == RunStatus.DONE and s.investigator.calls == 2
    assert s.decider.calls == [[Move.INVESTIGATE, Move.ESCALATE]]


def test_a_technical_failure_in_the_triage_is_not_a_suspicious_refusal(tmp_path):
    s = Setup(tmp_path, triage=FakeTriage(fail_first=1))
    state = s.start("monitores")
    assert state.status == RunStatus.DONE and state.steps[0].outcome.startswith("blocked")


def test_an_erp_failure_on_approval_is_retried(tmp_path):
    s = Setup(tmp_path, faults=Faults(fail_calls={"approve_pr": [1]}))
    state = s.start("monitores")
    assert state.status == RunStatus.DONE
    assert s.decider.calls == [[Move.APPROVE, Move.ESCALATE]]
    assert s.erp.get_budget("CC-TI-100").committed_eur == 38600.0


def test_a_lost_response_on_approval_does_not_repeat_the_effect(tmp_path):
    s = Setup(tmp_path, faults=Faults(drop_response={"approve_pr": [1]}))
    state = s.start("monitores")
    assert state.status == RunStatus.DONE
    assert s.erp.get_budget("CC-TI-100").committed_eur == 38600.0
    assert len([a for a in s.erp.get_actions(1) if a.action == "approve_pr"]) == 1


def test_an_inactive_cost_center_goes_back_to_the_user(tmp_path):
    s = Setup(tmp_path)
    state = s.start("inativo")
    assert state.status == RunStatus.WAITING_USER and "CC-OLD-900" in state.question
    assert s.erp.get_actions() == []


def test_a_verification_mismatch_is_escalated_before_any_email(tmp_path):
    s = Setup(tmp_path)
    s.erp.get_actions = lambda pr_id=None: []
    state = s.start("monitores")
    assert state.status == RunStatus.NEEDS_MANUAL and "approve_pr" in state.outcome_message
    assert s.erp.get_pr(1).state == PrState.APPROVED and s.erp.get_outbox() == []


def test_when_the_email_cannot_be_written_the_decision_stays_recorded_and_is_escalated(tmp_path):
    s = Setup(tmp_path, writer_fail=True)
    state = s.start("monitores")
    assert state.status == RunStatus.NEEDS_MANUAL
    assert state.decision == "approved" and state.decision_verified and not state.decision_email_sent
    assert s.erp.get_outbox() == []


def test_a_request_without_a_permitting_verdict_is_not_decided(tmp_path):
    s = Setup(tmp_path, verdict=PolicyVerdict.UNCLEAR)
    state = s.start("monitores")
    assert state.status == RunStatus.NEEDS_MANUAL
    assert [a.action for a in s.erp.get_actions()] == ["register_pr"]
    assert s.erp.get_pr(1).state == PrState.OPEN


def test_missing_quotes_pause_the_run_and_adding_them_lets_it_finish(tmp_path):
    s = Setup(tmp_path)
    paused = s.start("servico")
    assert paused.status == RunStatus.WAITING_USER
    assert "Faltam cotações" in paused.question and "2" in paused.question
    assert s.erp.get_pr(1).state == PrState.OPEN
    s.erp.add_quote("q1", "ana", 1, "S02", 2800.0)
    still = s.orchestrator.resume("run-1")
    assert still.status == RunStatus.WAITING_USER
    s.erp.add_quote("q2", "ana", 1, "S03", 2950.0)
    done = s.orchestrator.resume("run-1")
    assert done.status == RunStatus.DONE and s.erp.get_pr(1).state == PrState.APPROVED


def test_quotes_from_the_same_supplier_do_not_unblock_the_run(tmp_path):
    s = Setup(tmp_path)
    s.start("servico")
    s.erp.add_quote("q1", "ana", 1, "S02", 2800.0)
    s.erp.add_quote("q2", "ana", 1, "S02", 2700.0)
    assert s.orchestrator.resume("run-1").status == RunStatus.WAITING_USER
    assert s.erp.get_pr(1).state == PrState.OPEN


def test_suspicious_text_is_refused_without_touching_the_erp(tmp_path):
    s = Setup(tmp_path)
    REQUESTS["ignora"] = monitors()
    state = s.start("ignora")
    assert state.status == RunStatus.REFUSED and s.erp.get_actions() == []
    assert s.investigator.calls == 0


def decider_for(*moves):
    queue = list(moves)

    def respond(messages, info: AgentInfo):
        move = queue.pop(0) if len(queue) > 1 else queue[0]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
                                                 {"move": move, "reason": "teste"})])

    return make_decider(FunctionModel(respond))


def blank_state():
    return RunState(run_id="r", requester_email=ANA, created_at=NOW, updated_at=NOW)


def test_the_supervisor_agent_is_sent_back_when_it_picks_a_disallowed_move():
    decide = decider_for("approve", "investigate")
    choice = decide(blank_state(), [Move.INVESTIGATE, Move.ASK_USER])
    assert choice.move == Move.INVESTIGATE


def test_a_supervisor_agent_that_insists_is_replaced_by_an_escalation():
    decide = decider_for("approve")
    choice = decide(blank_state(), [Move.INVESTIGATE, Move.ASK_USER])
    assert choice.move == Move.ESCALATE and choice.reason.startswith("supervisor_failed")
