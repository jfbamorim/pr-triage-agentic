from datetime import datetime

from pr_triage.agents.triage import TriageOutcome
from pr_triage.authz import Action, AuthContext, HumanApproval, PolicyVerdict, authorize
from pr_triage.domain.band import Band, approver_role, determine_band
from pr_triage.domain.status import Status
from pr_triage.supervisor.state import TERMINAL, Move, RunState, RunStatus

MAX_ATTEMPTS = 2
MAX_STEPS = 25
RESOLVABLE = {"cost_center", "estimated_value_eur"}


def _allowed(
    action: Action,
    state: RunState,
    approval: HumanApproval | None,
    now: datetime,
    validation: HumanApproval | None = None,
) -> bool:
    value = state.request.estimated_value_eur
    executed = {"approved": Action.APPROVE_PR, "rejected": Action.REJECT_PR}.get(state.decision)
    context = AuthContext(
        request_id=state.pr_id or 0,
        status=Status.VALID,
        value_eur=value,
        band=determine_band(value),
        policy_verdict=state.investigation.policy_verdict,
        cited_clauses=state.investigation.cited_clauses,
        approval=approval,
        validation=validation,
        executed_decision=executed,
        now=now,
    )
    return authorize(action, context).allowed


def quotes_short(state: RunState) -> bool:
    inv = state.investigation
    return inv.quotes_required is not None and (inv.quotes_found or 0) < inv.quotes_required


def _blocked_moves(state: RunState) -> list[Move]:
    blocked = state.blocked
    moves = []
    if blocked.retryable and state.attempts.get(blocked.stage.value, 0) < MAX_ATTEMPTS:
        moves.append(blocked.stage)
    return moves + [Move.ESCALATE]


def _needs_info_moves(state: RunState) -> list[Move]:
    triage = state.triage
    if set(triage.missing) <= RESOLVABLE and state.investigation is None:
        return [Move.INVESTIGATE, Move.ASK_USER]
    return [Move.ASK_USER]


def _decision_moves(
    state: RunState, approval: HumanApproval | None, now: datetime, validation: HumanApproval | None
) -> list[Move]:
    band = determine_band(state.request.estimated_value_eur)
    verdict = state.investigation.policy_verdict
    if state.decision is not None:
        if not state.decision_verified:
            return [Move.VERIFY]
        if state.decision_email_sent:
            return []
        if _allowed(Action.SEND_DECISION_EMAIL, state, approval, now, validation):
            return [Move.SEND_DECISION_EMAIL]
        return [Move.ESCALATE]
    if state.investigation.budget_short:
        return [] if state.referred else [Move.REFER_CONTROLLING]
    if quotes_short(state):
        return [Move.ASK_USER]
    if band == Band.HUMAN_ACTION or (band == Band.POLICY_TO_BE_VERIFIED and verdict == PolicyVerdict.PERMITS):
        if not state.dossier_sent:
            if _allowed(Action.SEND_DOSSIER_EMAIL, state, approval, now):
                return [Move.SEND_DOSSIER]
            return [Move.ESCALATE]
        needs_validation = band == Band.HUMAN_ACTION and approval is not None and approval.decision == Action.APPROVE_PR
        if approval is None or (needs_validation and validation is None):
            return []
        action = approval.decision
        move = Move.APPROVE if action == Action.APPROVE_PR else Move.REJECT
        if action in (Action.APPROVE_PR, Action.REJECT_PR) and _allowed(action, state, approval, now, validation):
            return [move]
        return [Move.ESCALATE]
    if verdict == PolicyVerdict.UNCLEAR:
        if state.investigation.questions_for_user:
            return [Move.ASK_USER]
        return [Move.ESCALATE]
    action = Action.APPROVE_PR if verdict == PolicyVerdict.PERMITS else Action.REJECT_PR
    if not _allowed(action, state, approval, now, validation):
        return [Move.ESCALATE]
    return [Move.APPROVE if action == Action.APPROVE_PR else Move.REJECT]


def allowed_moves(
    state: RunState, approval: HumanApproval | None, now: datetime, validation: HumanApproval | None = None
) -> list[Move]:
    if state.status in TERMINAL:
        return []
    if len(state.steps) >= MAX_STEPS:
        return [Move.ESCALATE]
    if state.blocked is not None:
        return _blocked_moves(state)
    triage = state.triage
    if triage is None or (state.request is not None and triage.request != state.request):
        return [Move.TRIAGE]
    if triage.outcome in (TriageOutcome.SUSPICIOUS, TriageOutcome.INVALID):
        return [Move.REFUSE]
    if triage.outcome == TriageOutcome.NEEDS_INFO:
        return _needs_info_moves(state)
    if state.pr_id is None:
        return [Move.REGISTER]
    if state.registered_request != state.request:
        if state.decision is not None or state.dossier_sent or state.referred:
            return [Move.ESCALATE]
        return [Move.UPDATE_PR]
    if (
        state.investigation is None
        or state.investigated_request != state.request
        or state.investigated_pr_id != state.pr_id
    ):
        return [Move.INVESTIGATE]
    return _decision_moves(state, approval, now, validation)


def pending_roles(state: RunState, approval: HumanApproval | None, validation: HumanApproval | None) -> list[str]:
    if approver_role(state.request.estimated_value_eur) == "department_head":
        return ["department_head"] if approval is None else []
    if approval is None:
        return ["director", "purchasing"] if validation is None else ["director"]
    if approval.decision == Action.APPROVE_PR and validation is None:
        return ["purchasing"]
    return []


def pause_reason(
    state: RunState, approval: HumanApproval | None, validation: HumanApproval | None = None
) -> RunStatus | None:
    if state.status in TERMINAL or state.decision is not None or not state.dossier_sent:
        return None
    return RunStatus.WAITING_HUMAN if pending_roles(state, approval, validation) else None
