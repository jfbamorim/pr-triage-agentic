import uuid
from collections.abc import Callable
from dataclasses import dataclass

from pr_triage.agents.communicator import (
    CommunicationBrief,
    CommunicationResult,
    build_decision_brief,
    build_dossier_brief,
    build_referral_brief,
    deliver,
)
from pr_triage.agents.investigator import InvestigationResult
from pr_triage.agents.triage import TriageOutcome, TriageReport, check_report
from pr_triage.domain.schemas import Evidence, PurchaseRequest
from pr_triage.erp import ErpStore, ErpUnavailable
from pr_triage.supervisor.agent import Choice
from pr_triage.memory import MemoryStore, MemoryView
from pr_triage.supervisor.moves import allowed_moves, pause_reason, pending_roles, quotes_short
from pr_triage.supervisor.reconcile import reconcile
from pr_triage.supervisor.state import (
    Blocked,
    Move,
    RunState,
    RunStatus,
    StepRecord,
    apply_resolved_fields,
)
from pr_triage.supervisor.store import RunStore
from pr_triage.tools import PolicyPort, ToolContext, ToolPort, ToolResult, call_tool

USER_FIXABLE = {"unknown_cost_center", "inactive_cost_center"}


@dataclass
class Specialists:
    triage: Callable[[str, PurchaseRequest | None], TriageReport]
    investigate: Callable[[PurchaseRequest, str, set[str], ToolContext, int | None], InvestigationResult]
    write: Callable[[CommunicationBrief], CommunicationResult]
    decide: Callable[[RunState, list[Move]], Choice]


class Orchestrator:
    def __init__(
        self,
        erp: ErpStore,
        policy: PolicyPort,
        store: RunStore,
        specialists: Specialists,
        port: ToolPort | None = None,
        memory: MemoryStore | None = None,
        observer: Callable[[RunState], None] | None = None,
        on_step: Callable[[RunState, Choice], None] | None = None,
    ):
        self.observer = observer
        self.on_step = on_step
        self.port = port
        self.memory = memory
        self.erp = erp
        self.policy = policy
        self.store = store
        self.specialists = specialists

    def start(self, text: str, requester_email: str, run_id: str | None = None) -> RunState:
        now = self.erp.clock()
        state = RunState(
            run_id=run_id or uuid.uuid4().hex[:8],
            requester_email=requester_email,
            messages=[text],
            created_at=now,
            updated_at=now,
        )
        return self._loop(state)

    def reply(self, run_id: str, text: str) -> RunState:
        state = self._load(run_id)
        if state.status != RunStatus.WAITING_USER:
            raise ValueError(f"Run {run_id} is not waiting for the user.")
        state.messages.append(text)
        state.question = None
        state.outcome_message = None
        state.triage = None
        state.investigation = None
        state.blocked = None
        state.status = RunStatus.RUNNING
        return self._loop(state)

    def resume(self, run_id: str) -> RunState:
        state = self._load(run_id)
        if state.status not in (RunStatus.WAITING_USER, RunStatus.WAITING_HUMAN, RunStatus.RUNNING):
            return state
        if not self._reconcile(state):
            return state
        if state.status == RunStatus.WAITING_USER:
            state.investigation = None
            state.question = None
        state.status = RunStatus.RUNNING
        return self._loop(state)

    def _reconcile(self, state: RunState) -> bool:
        try:
            found = reconcile(self.erp, state)
        except ErpUnavailable as error:
            state.outcome_message = f"The ERP is unavailable ({error}); nothing was changed. Resume again later."
            self._save(state)
            return False
        if found.changes or found.contradictions:
            outcome = "; ".join(found.changes + [f"contradiction: {c}" for c in found.contradictions])
            state.steps.append(
                StepRecord(
                    n=len(state.steps) + 1,
                    move=Move.RECONCILE,
                    forced=True,
                    reason="resume",
                    outcome=outcome,
                    at=self.erp.clock(),
                )
            )
        if found.contradictions:
            self._escalate(state, "The run state and the ERP disagree: " + "; ".join(found.contradictions))
            return False
        self._save(state)
        return True

    def _load(self, run_id: str) -> RunState:
        state = self.store.load(run_id)
        if state is None:
            raise KeyError(f"Unknown run {run_id}.")
        return state

    def _ctx(self, state: RunState) -> ToolContext:
        view = MemoryView(self.memory, state.requester_email) if self.memory is not None else None
        return ToolContext(erp=self.erp, policy=self.policy, run_id=state.run_id, port=self.port, memory=view)

    def _save(self, state: RunState) -> None:
        state.updated_at = self.erp.clock()
        self.store.save(state)
        if self.observer is not None:
            self.observer(state)

    def _loop(self, state: RunState) -> RunState:
        self._save(state)
        while state.status == RunStatus.RUNNING:
            try:
                approval = self.erp.get_human_approval(state.pr_id) if state.pr_id else None
                validation = self.erp.get_purchasing_validation(state.pr_id) if state.pr_id else None
            except ErpUnavailable as error:
                self._escalate(state, f"The ERP is unavailable: {error}")
                break
            moves = allowed_moves(state, approval, self.erp.clock(), validation)
            if not moves:
                self._pause_or_finish(state, approval, validation)
                self._save(state)
                break
            forced = len(moves) == 1
            choice = Choice(move=moves[0], reason="forced") if forced else self.specialists.decide(state, moves)
            if choice.move not in moves and choice.move != Move.ESCALATE:
                choice = Choice(move=Move.ESCALATE, reason=f"the supervisor chose {choice.move.value}, which is not allowed")
            if self.on_step is not None:
                self.on_step(state, choice)
            outcome = self._execute(state, choice, approval)
            state.steps.append(
                StepRecord(
                    n=len(state.steps) + 1,
                    move=choice.move,
                    forced=forced,
                    reason=choice.reason,
                    outcome=outcome,
                    at=self.erp.clock(),
                )
            )
            self._save(state)
        return state

    def _pause_or_finish(self, state: RunState, approval, validation) -> None:
        if state.referred:
            state.status = RunStatus.REFERRED
            state.outcome_message = f"Request {state.pr_id} was referred to management control: the budget is insufficient."
        elif state.decision_email_sent:
            state.status = RunStatus.DONE
            state.outcome_message = f"Request {state.pr_id} {state.decision}; the requester was notified."
            self._remember(state)
        elif pause_reason(state, approval, validation) == RunStatus.WAITING_HUMAN:
            state.status = RunStatus.WAITING_HUMAN
            state.outcome_message = (
                f"Request {state.pr_id} is waiting for a human approval ({', '.join(pending_roles(state, approval, validation))})."
            )
        else:
            self._escalate(state, "No allowed move and nothing to wait for.")

    def _remember(self, state: RunState) -> None:
        if self.memory is None or state.decision != "approved" or not state.decision_verified:
            return
        cost_center = state.request.cost_center if state.request else None
        person = self.erp.get_person(state.requester_email)
        if cost_center is None or person is None or cost_center == person.default_cost_center:
            return
        self.memory.remember(
            state.requester_email, "cost_center", cost_center, "approved_request", state.run_id, state.pr_id
        )

    def _escalate(self, state: RunState, reason: str) -> None:
        state.status = RunStatus.NEEDS_MANUAL
        state.outcome_message = reason
        self._save(state)

    def _execute(self, state: RunState, choice: Choice, approval) -> str:
        move = choice.move
        state.attempts[move.value] = state.attempts.get(move.value, 0) + 1
        try:
            handler = getattr(self, f"_do_{move.value}")
            outcome = handler(state, choice)
            if state.blocked is None:
                state.attempts[move.value] = 0
            return outcome
        except ErpUnavailable as error:
            state.blocked = Blocked(stage=move, problems=[f"erp_unavailable: {error}"], retryable=True)
            return "erp_unavailable"

    def _block(self, state: RunState, move: Move, problems: list[str], retryable: bool) -> str:
        state.blocked = Blocked(stage=move, problems=problems, retryable=retryable)
        return "blocked: " + "; ".join(problems)

    def _tool_failed(self, state: RunState, move: Move, result: ToolResult) -> str:
        error = result.error
        if error.code in USER_FIXABLE:
            state.status = RunStatus.WAITING_USER
            state.question = f"{error.message} Que centro de custo devo usar?"
            state.outcome_message = state.question
            return f"asked_user: {error.code}"
        detail = f"{error.code}: {error.message}" + (f" ({error.detail})" if error.detail else "")
        return self._block(state, move, [detail], error.retryable)

    def _do_triage(self, state: RunState, choice: Choice) -> str:
        report = self.specialists.triage(state.messages[-1], state.request)
        if report.failure is not None:
            return self._block(state, Move.TRIAGE, [report.failure.message], report.failure.retryable)
        problems = check_report(report)
        if problems:
            return self._block(state, Move.TRIAGE, problems, True)
        state.triage = report
        state.blocked = None
        if report.request is not None:
            state.request = report.request
        return report.outcome.value

    def _do_investigate(self, state: RunState, choice: Choice) -> str:
        ids = {item.chunk_id for item in state.triage.evidence}
        result = self.specialists.investigate(
            state.request, state.requester_email, ids, self._ctx(state), state.pr_id
        )
        if result.failure is not None:
            return self._block(state, Move.INVESTIGATE, [result.failure.message], result.failure.retryable)
        report = result.report
        state.blocked = None
        state.investigation = report
        state.request = apply_resolved_fields(state.request, report)
        state.investigated_request = state.request
        state.investigated_pr_id = state.pr_id
        return f"verdict={report.policy_verdict.value}"

    def _do_ask_user(self, state: RunState, choice: Choice) -> str:
        triage, inv = state.triage, state.investigation
        question = None
        if inv is not None and quotes_short(state):
            question = (
                f"Faltam cotações para o pedido {state.pr_id}: a política exige {inv.quotes_required} "
                f"de fornecedores distintos e há {inv.quotes_found or 0} registadas. "
                "Regista-as no ERP e retoma o pedido."
            )
            if inv.urgency_unjustified:
                question += (
                    " O pedido é urgente: para bastar uma cotação, o responsável do departamento "
                    "tem de justificar a urgência por escrito (cláusula 2.4)."
                )
        if question is None and inv is not None and inv.questions_for_user:
            question = inv.questions_for_user[0]
        if question is None and triage.outcome == TriageOutcome.NEEDS_INFO:
            question = triage.follow_up
        state.question = question or "Podes dar mais informação sobre o pedido?"
        state.status = RunStatus.WAITING_USER
        state.outcome_message = state.question
        return "waiting_user"

    def _do_refuse(self, state: RunState, choice: Choice) -> str:
        triage = state.triage
        state.status = RunStatus.REFUSED
        if triage.outcome == TriageOutcome.SUSPICIOUS:
            state.outcome_message = "The text was classified as an attempt to control the system."
        else:
            state.outcome_message = "Invalid data: " + ", ".join(triage.invalid)
        return "refused"

    def _do_escalate(self, state: RunState, choice: Choice) -> str:
        reason = choice.reason
        if state.blocked is not None:
            reason += " | " + "; ".join(state.blocked.problems)
        state.status = RunStatus.NEEDS_MANUAL
        state.outcome_message = reason
        return "needs_manual"

    def _do_register(self, state: RunState, choice: Choice) -> str:
        request = state.request
        result = call_tool(
            self._ctx(state),
            "register_pr",
            dict(
                requester_email=state.requester_email,
                item=request.item,
                quantity=request.quantity,
                value_eur=request.estimated_value_eur,
                cost_center=request.cost_center,
                category=request.category.value,
                urgent=request.is_urgent,
            ),
        )
        if not result.ok:
            return self._tool_failed(state, Move.REGISTER, result)
        state.pr_id = result.data["result"]["pr_id"]
        state.registered_request = request
        state.blocked = None
        return f"pr_id={state.pr_id}"

    def _do_update_pr(self, state: RunState, choice: Choice) -> str:
        request = state.request
        result = call_tool(
            self._ctx(state),
            "update_pr",
            dict(
                pr_id=state.pr_id,
                requester_email=state.requester_email,
                item=request.item,
                quantity=request.quantity,
                value_eur=request.estimated_value_eur,
                cost_center=request.cost_center,
                category=request.category.value,
                urgent=request.is_urgent,
            ),
        )
        if not result.ok:
            return self._tool_failed(state, Move.UPDATE_PR, result)
        state.registered_request = request
        state.blocked = None
        return f"pr_id={state.pr_id} updated"

    def _decide(self, state: RunState, move: Move, tool: str, decision: str) -> str:
        inv = state.investigation
        result = call_tool(
            self._ctx(state),
            tool,
            dict(
                pr_id=state.pr_id,
                policy_verdict=inv.policy_verdict.value,
                cited_clauses=inv.cited_clauses,
                justification=inv.summary,
            ),
        )
        if not result.ok:
            return self._tool_failed(state, move, result)
        state.decision = decision
        state.decision_verified = False
        state.blocked = None
        return decision

    def _do_approve(self, state: RunState, choice: Choice) -> str:
        return self._decide(state, Move.APPROVE, "approve_pr", "approved")

    def _do_reject(self, state: RunState, choice: Choice) -> str:
        return self._decide(state, Move.REJECT, "reject_pr", "rejected")

    def _evidence(self, state: RunState) -> list[Evidence]:
        items = list(state.triage.evidence)
        known = {item.chunk_id for item in items}
        for chunk_id in state.investigation.cited_clauses:
            if chunk_id not in known:
                try:
                    items.append(self.policy.get(chunk_id))
                except Exception:
                    continue
        return items

    def _budget_available(self, state: RunState) -> float | None:
        budget = self.erp.get_budget(state.request.cost_center)
        return budget.available_eur if budget else None

    def _send(self, state: RunState, move: Move, brief: CommunicationBrief) -> ToolResult | str:
        written = self.specialists.write(brief)
        if written.failure is not None:
            return self._block(state, move, [written.failure.message], written.failure.retryable)
        result = deliver(self._ctx(state), brief, written.draft)
        if not result.ok:
            return self._tool_failed(state, move, result)
        return result

    def _do_send_dossier(self, state: RunState, choice: Choice) -> str:
        approvers = self.erp.find_people_by_role("director")
        if not approvers:
            return self._block(state, Move.SEND_DOSSIER, ["no approver is registered"], False)
        pr = self.erp.get_pr(state.pr_id)
        inv = state.investigation
        reasons = [inv.summary] + [f"{c.name.value}: {c.result.value} - {c.evidence}" for c in inv.checks]
        brief = build_dossier_brief(
            pr,
            self.erp.get_person(state.requester_email),
            approvers[0],
            self._evidence(state),
            inv.cited_clauses,
            reasons,
            self._budget_available(state),
        )
        outcome = self._send(state, Move.SEND_DOSSIER, brief)
        if isinstance(outcome, str):
            return outcome
        state.dossier_sent = True
        state.blocked = None
        return f"dossier sent to {approvers[0].email}"

    def _do_refer_controlling(self, state: RunState, choice: Choice) -> str:
        controllers = self.erp.find_people_by_role("controlling")
        if not controllers:
            return self._block(state, Move.REFER_CONTROLLING, ["no management control contact is registered"], False)
        pr = self.erp.get_pr(state.pr_id)
        inv = state.investigation
        reasons = [inv.summary] + [f"{c.name.value}: {c.result.value} - {c.evidence}" for c in inv.checks]
        brief = build_referral_brief(
            pr,
            self.erp.get_person(state.requester_email),
            controllers[0],
            self._evidence(state),
            inv.cited_clauses,
            reasons,
            self._budget_available(state),
        )
        outcome = self._send(state, Move.REFER_CONTROLLING, brief)
        if isinstance(outcome, str):
            return outcome
        state.referred = True
        state.blocked = None
        return f"referred to {controllers[0].email}"

    def _do_verify(self, state: RunState, choice: Choice) -> str:
        problems = verify_decision(self.erp, state)
        if problems:
            return self._block(state, Move.VERIFY, problems, False)
        state.decision_verified = True
        state.blocked = None
        return "verified"

    def _do_send_decision_email(self, state: RunState, choice: Choice) -> str:
        pr = self.erp.get_pr(state.pr_id)
        approval = self.erp.get_human_approval(state.pr_id)
        approver = self.erp.get_person(approval.approver) if approval else None
        brief = build_decision_brief(
            pr,
            self.erp.get_person(state.requester_email),
            self._evidence(state),
            None,
            human_approver=approver,
        )
        outcome = self._send(state, Move.SEND_DECISION_EMAIL, brief)
        if isinstance(outcome, str):
            return outcome
        state.decision_email_sent = True
        state.blocked = None
        return "decision email sent"


def verify_decision(erp: ErpStore, state: RunState) -> list[str]:
    problems = []
    pr = erp.get_pr(state.pr_id)
    if pr is None:
        return ["the purchase request does not exist in the ERP"]
    if pr.state.value != state.decision:
        problems.append(f"the ERP says {pr.state.value}, expected {state.decision}")
    if set(pr.cited_clauses) != set(state.investigation.cited_clauses):
        problems.append("the clauses recorded in the ERP differ from the decision")
    tool = "approve_pr" if state.decision == "approved" else "reject_pr"
    applied = [a for a in erp.get_actions(state.pr_id) if a.action == tool]
    if len(applied) != 1:
        problems.append(f"expected exactly one {tool} action in the ERP log, found {len(applied)}")
    return problems
