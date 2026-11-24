from datetime import datetime, timedelta, timezone

from pr_triage.agents.investigator import InvestigationReport, ResolvedField
from pr_triage.agents.triage import TriageOutcome, TriageReport
from pr_triage.authz import Action, HumanApproval, PolicyVerdict
from pr_triage.domain.band import Band, determine_band
from pr_triage.domain.status import EscalationReason
from pr_triage.supervisor.moves import MAX_ATTEMPTS, MAX_STEPS, allowed_moves, pause_reason, pending_roles
from pr_triage.supervisor.state import (
    Blocked,
    Move,
    RunState,
    RunStatus,
    StepRecord,
    apply_resolved_fields,
)
from pr_triage.supervisor.store import RunStore
from test_workflow import make_pr

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
T1 = "02-escaloes-e-cotacoes#2.1"
T33 = "03-fornecedores-e-categorias#3.3"


def fresh(**overrides) -> RunState:
    data = dict(run_id="r1", requester_email="ana.silva@exemplo.pt", messages=["pedido"],
                created_at=NOW, updated_at=NOW)
    data.update(overrides)
    return RunState(**data)


def triaged(outcome=TriageOutcome.READY, value=800.0, **request_overrides):
    request = make_pr(estimated_value_eur=value, **request_overrides)
    band = determine_band(value) if value is not None else None
    report = TriageReport(
        outcome=outcome, request=request, band=band,
        escalation=EscalationReason.OVER_THRESHOLD if band == Band.HUMAN_ACTION else None,
    )
    return request, report


def ready(value=800.0, verdict=PolicyVerdict.PERMITS, clauses=(T1,), questions=(), **overrides):
    request, report = triaged(value=value)
    investigation = InvestigationReport(
        policy_verdict=verdict, cited_clauses=list(clauses),
        questions_for_user=list(questions), summary="s",
    )
    data = dict(request=request, triage=report, investigation=investigation,
                investigated_request=request, pr_id=1, registered_request=request,
                investigated_pr_id=1)
    data.update(overrides)
    return fresh(**data)


def human(action=Action.APPROVE_PR, approver="maria.santos@exemplo.pt", expires=None, request_id=1):
    return HumanApproval(
        request_id=request_id, approver=approver, decision=action, approved_value_eur=14000.0,
        granted_at=NOW - timedelta(hours=1), expires_at=expires or NOW + timedelta(hours=23),
    )


def head(action=Action.APPROVE_PR, **kw):
    return human(action, approver="joao.ferreira@exemplo.pt", **kw).model_copy(
        update={"role": "department_head", "approved_value_eur": 5000.0}
    )


def purchasing(**kw):
    return human(approver="pedro.alves@exemplo.pt", **kw).model_copy(update={"role": "purchasing"})


def moves(state, approval=None, validation=None):
    return allowed_moves(state, approval, NOW, validation)


def test_a_new_run_starts_with_triage():
    assert moves(fresh()) == [Move.TRIAGE]


def test_suspicious_or_invalid_input_is_refused():
    for outcome in (TriageOutcome.SUSPICIOUS, TriageOutcome.INVALID):
        request, report = triaged(outcome)
        assert moves(fresh(request=request, triage=report)) == [Move.REFUSE]


def test_missing_fields_the_erp_can_supply_allow_an_investigation_or_a_question():
    request, report = triaged(TriageOutcome.NEEDS_INFO, cost_center=None)
    report = report.model_copy(update={"missing": ["cost_center"], "follow_up": "Qual?"})
    state = fresh(request=request, triage=report)
    assert moves(state) == [Move.INVESTIGATE, Move.ASK_USER]


def test_missing_fields_only_the_user_knows_go_to_the_user():
    request, report = triaged(TriageOutcome.NEEDS_INFO, item=None)
    report = report.model_copy(update={"missing": ["item"], "follow_up": "Qual?"})
    assert moves(fresh(request=request, triage=report)) == [Move.ASK_USER]


def test_after_an_unsuccessful_investigation_the_user_is_asked():
    request, report = triaged(TriageOutcome.NEEDS_INFO, cost_center=None)
    report = report.model_copy(update={"missing": ["cost_center"], "follow_up": "Qual?"})
    investigation = InvestigationReport(policy_verdict=PolicyVerdict.UNCLEAR, summary="s")
    state = fresh(request=request, triage=report, investigation=investigation)
    assert moves(state) == [Move.ASK_USER]


def test_a_completed_request_makes_the_triage_stale_and_is_triaged_again():
    request, report = triaged(TriageOutcome.NEEDS_INFO, cost_center=None)
    report = report.model_copy(update={"missing": ["cost_center"]})
    resolved = InvestigationReport(
        policy_verdict=PolicyVerdict.PERMITS, cited_clauses=[T1], summary="s",
        resolved_fields=[ResolvedField(field="cost_center", value="CC-TI-100",
                                       source_tool="get_person", evidence="e")],
    )
    completed = apply_resolved_fields(request, resolved)
    assert completed.cost_center == "CC-TI-100"
    state = fresh(request=completed, triage=report, investigation=resolved,
                  investigated_request=completed)
    assert moves(state) == [Move.TRIAGE]


def test_a_ready_triage_is_followed_by_registration_and_then_an_investigation():
    request, report = triaged()
    assert moves(fresh(request=request, triage=report)) == [Move.REGISTER]
    registered = fresh(request=request, triage=report, pr_id=1, registered_request=request)
    assert moves(registered) == [Move.INVESTIGATE]


def test_an_investigation_made_before_registration_is_repeated_with_the_request_number():
    state = ready(800.0, investigated_pr_id=None)
    assert moves(state) == [Move.INVESTIGATE]


def test_a_request_that_changed_after_registration_updates_the_open_requisition():
    changed = ready(800.0, registered_request=make_pr(estimated_value_eur=900.0))
    assert moves(changed) == [Move.UPDATE_PR]


def test_a_request_that_changed_after_a_decision_or_a_dossier_is_escalated():
    old = make_pr(estimated_value_eur=900.0)
    assert moves(ready(800.0, registered_request=old, decision="approved")) == [Move.ESCALATE]
    assert moves(ready(800.0, registered_request=old, dossier_sent=True)) == [Move.ESCALATE]
    assert moves(ready(800.0, registered_request=old, referred=True)) == [Move.ESCALATE]


def test_missing_quotes_pause_the_run_before_any_decision():
    short = ready(3000.0)
    short.investigation.quotes_required, short.investigation.quotes_found = 2, 1
    assert moves(short) == [Move.ASK_USER]
    big = ready(14000.0)
    big.investigation.quotes_required, big.investigation.quotes_found = 3, 0
    assert moves(big) == [Move.ASK_USER]


def test_enough_quotes_let_the_run_proceed():
    enough = ready(3000.0)
    enough.investigation.quotes_required, enough.investigation.quotes_found = 2, 2
    assert moves(enough) == [Move.SEND_DOSSIER]


def test_missing_policy_evidence_in_the_triage_goes_on_to_the_investigator():
    request, report = triaged()
    for reason in (EscalationReason.NO_POLICY_EVIDENCE, EscalationReason.RETRIEVAL_ERROR):
        weak = report.model_copy(update={"escalation": reason})
        assert moves(fresh(request=request, triage=weak)) == [Move.REGISTER]
        registered = fresh(request=request, triage=weak, pr_id=1, registered_request=request)
        assert moves(registered) == [Move.INVESTIGATE]


def test_below_the_first_threshold_the_agent_approves():
    assert moves(ready(500.0)) == [Move.APPROVE]


def test_below_the_first_threshold_a_blocking_verdict_cannot_be_rejected_by_the_agent():
    assert moves(ready(500.0, verdict=PolicyVerdict.BLOCKS)) == [Move.ESCALATE]


def test_middle_band_follows_the_verdict_and_needs_a_clause():
    assert moves(ready(3000.0, pr_id=1)) == [Move.SEND_DOSSIER]
    assert moves(ready(3000.0, verdict=PolicyVerdict.BLOCKS, clauses=(T33,), pr_id=1)) == [Move.REJECT]
    assert moves(ready(3000.0, clauses=())) == [Move.ESCALATE]


def test_middle_band_waits_for_the_department_head_and_then_executes_their_decision():
    sent = ready(3000.0, pr_id=1, dossier_sent=True)
    assert moves(sent) == []
    assert pending_roles(sent, None, None) == ["department_head"]
    assert pause_reason(sent, None) == RunStatus.WAITING_HUMAN
    assert moves(sent, head()) == [Move.APPROVE]
    assert moves(sent, head(Action.REJECT_PR)) == [Move.REJECT]
    assert moves(sent, human()) == [Move.ESCALATE]
    assert pending_roles(sent, head(), None) == []


def test_an_unclear_verdict_asks_the_user_when_there_is_a_question():
    unclear = ready(3000.0, verdict=PolicyVerdict.UNCLEAR, questions=("Qual o fornecedor?",))
    assert moves(unclear) == [Move.ASK_USER]
    assert moves(ready(3000.0, verdict=PolicyVerdict.UNCLEAR)) == [Move.ESCALATE]


def test_above_the_limit_the_dossier_goes_out_and_the_run_waits_for_a_human():
    assert moves(ready(14000.0)) == [Move.SEND_DOSSIER]
    waiting = ready(14000.0, dossier_sent=True)
    assert moves(waiting) == []
    assert pause_reason(waiting, None) == RunStatus.WAITING_HUMAN


def test_a_valid_human_approval_unlocks_the_decision():
    state = ready(14000.0, pr_id=1, dossier_sent=True)
    assert moves(state, human(Action.APPROVE_PR), purchasing()) == [Move.APPROVE]
    assert moves(state, human(Action.REJECT_PR)) == [Move.REJECT]
    assert pause_reason(state, human(), purchasing()) is None
    assert pause_reason(state, human(Action.REJECT_PR)) is None


def test_an_approval_without_the_purchasing_validation_keeps_waiting():
    state = ready(14000.0, pr_id=1, dossier_sent=True)
    assert moves(state, human(Action.APPROVE_PR)) == []
    assert pause_reason(state, human(Action.APPROVE_PR)) == RunStatus.WAITING_HUMAN
    assert pending_roles(state, human(Action.APPROVE_PR), None) == ["purchasing"]
    assert pending_roles(state, None, None) == ["director", "purchasing"]
    assert pending_roles(state, None, purchasing()) == ["director"]


def test_an_expired_or_self_approval_is_escalated():
    state = ready(14000.0, pr_id=1, dossier_sent=True)
    assert moves(state, human(expires=NOW - timedelta(minutes=1)), purchasing()) == [Move.ESCALATE]
    assert moves(state, human(approver="agent"), purchasing()) == [Move.ESCALATE]


def test_a_value_above_the_approved_one_is_escalated():
    state = ready(20000.0, pr_id=1, dossier_sent=True)
    assert moves(state, human(), purchasing()) == [Move.ESCALATE]


def test_the_decision_must_be_verified_before_the_email():
    state = ready(800.0, pr_id=1, decision="approved")
    assert moves(state) == [Move.VERIFY]
    verified = state.model_copy(update={"decision_verified": True})
    assert moves(verified) == [Move.SEND_DECISION_EMAIL]
    done = verified.model_copy(update={"decision_email_sent": True})
    assert moves(done) == []


def test_a_blocked_stage_may_be_retried_a_limited_number_of_times():
    request, report = triaged()
    blocked = Blocked(stage=Move.INVESTIGATE, problems=["p"], retryable=True)
    state = fresh(request=request, triage=report, blocked=blocked)
    assert moves(state) == [Move.INVESTIGATE, Move.ESCALATE]
    tired = state.model_copy(update={"attempts": {"investigate": MAX_ATTEMPTS}})
    assert moves(tired) == [Move.ESCALATE]
    hard = state.model_copy(update={"blocked": blocked.model_copy(update={"retryable": False})})
    assert moves(hard) == [Move.ESCALATE]


def test_too_many_steps_force_an_escalation():
    steps = [StepRecord(n=i, move=Move.TRIAGE, forced=True, reason="", outcome="", at=NOW)
             for i in range(MAX_STEPS)]
    assert moves(fresh(steps=steps)) == [Move.ESCALATE]


def test_a_finished_run_allows_nothing():
    assert moves(fresh(status=RunStatus.DONE)) == []
    assert moves(fresh(status=RunStatus.NEEDS_MANUAL)) == []


def test_the_run_store_round_trips_the_full_state(tmp_path):
    store = RunStore(tmp_path / "state" / "runs.db")
    state = ready(14000.0, pr_id=1, dossier_sent=True, status=RunStatus.WAITING_HUMAN)
    store.save(state)
    assert store.load("r1") == state
    assert store.load("nope") is None
    assert store.list_by_status(RunStatus.WAITING_HUMAN) == ["r1"]
    store.save(state.model_copy(update={"status": RunStatus.DONE}))
    assert store.list_by_status(RunStatus.WAITING_HUMAN) == []
    assert store.load("r1").status == RunStatus.DONE
