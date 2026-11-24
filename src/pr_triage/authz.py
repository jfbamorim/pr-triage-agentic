from datetime import datetime
from enum import Enum

from pydantic import BaseModel

from pr_triage.domain.band import Band, determine_band
from pr_triage.domain.status import EscalationReason, Status


class Action(str, Enum):
    APPROVE_PR = "approve_pr"
    REJECT_PR = "reject_pr"
    SEND_DECISION_EMAIL = "send_decision_email"
    SEND_DOSSIER_EMAIL = "send_dossier_email"
    SEND_REFERRAL_EMAIL = "send_referral_email"


class PolicyVerdict(str, Enum):
    PERMITS = "permits"
    BLOCKS = "blocks"
    UNCLEAR = "unclear"


class HumanApproval(BaseModel):
    request_id: int
    approver: str
    decision: Action
    approved_value_eur: float
    role: str = "director"
    granted_at: datetime
    expires_at: datetime


class AuthContext(BaseModel):
    request_id: int
    status: Status
    value_eur: float | None
    band: Band | None
    escalation: EscalationReason | None = None
    policy_verdict: PolicyVerdict = PolicyVerdict.UNCLEAR
    cited_clauses: list[str] = []
    proposed_by: str = "agent"
    approval: HumanApproval | None = None
    validation: HumanApproval | None = None
    budget_insufficient: bool = False
    executed_decision: Action | None = None
    now: datetime


class Verdict(BaseModel):
    allowed: bool
    code: str
    reason: str


def _deny(code: str, reason: str) -> Verdict:
    return Verdict(allowed=False, code=code, reason=reason)


def _allow(code: str, reason: str) -> Verdict:
    return Verdict(allowed=True, code=code, reason=reason)


ROLE_LABELS = {"director": "direction", "department_head": "department head"}


def _check_human_approval(action: Action, ctx: AuthContext, role: str) -> Verdict:
    approval = ctx.approval
    if approval is None:
        return _deny("human_approval_required", "Above the agent limit: no human approval on record.")
    if approval.request_id != ctx.request_id:
        return _deny("approval_other_request", "The approval belongs to another request.")
    if approval.decision != action:
        return _deny("approval_other_decision", "The human decided something different from this action.")
    if approval.approver == ctx.proposed_by:
        return _deny("same_person", "The proposer cannot approve their own proposal.")
    if approval.expires_at <= ctx.now:
        return _deny("approval_expired", "The human approval has expired.")
    if ctx.value_eur is None or ctx.value_eur > approval.approved_value_eur:
        return _deny("value_changed", "The value is higher than the one approved.")
    if approval.role != role:
        return _deny("wrong_role", f"The approval must come from the {ROLE_LABELS[role]}.")
    if action == Action.APPROVE_PR and role == "director":
        return _check_validation(approval, ctx)
    return _allow("human_approved", f"Decided by {approval.approver}.")


def _check_validation(approval: HumanApproval, ctx: AuthContext) -> Verdict:
    validation = ctx.validation
    if validation is None:
        return _deny("validation_required", "Above the agent limit: the Purchasing validation is missing.")
    if validation.request_id != ctx.request_id or validation.decision != Action.APPROVE_PR:
        return _deny("validation_mismatch", "The Purchasing validation does not match this approval.")
    if validation.role != "purchasing":
        return _deny("wrong_role", "The validation must come from Purchasing.")
    if validation.approver in (ctx.proposed_by, approval.approver):
        return _deny("same_person", "The validation must come from a different person.")
    if validation.expires_at <= ctx.now:
        return _deny("approval_expired", "The Purchasing validation has expired.")
    if ctx.value_eur is None or ctx.value_eur > validation.approved_value_eur:
        return _deny("value_changed", "The value is higher than the one validated.")
    return _allow("human_approved", f"Approved by {approval.approver}; validated by {validation.approver}.")


def _check_decision(action: Action, ctx: AuthContext) -> Verdict:
    if ctx.status != Status.VALID:
        return _deny("request_not_valid", "Only a valid request can be decided.")
    if ctx.value_eur is None:
        return _deny("no_value", "The request has no value.")
    if determine_band(ctx.value_eur) != ctx.band:
        return _deny("band_mismatch", "The band does not match the value.")
    if ctx.escalation in (EscalationReason.NO_POLICY_EVIDENCE, EscalationReason.RETRIEVAL_ERROR):
        return _deny("manual_action", "No usable policy evidence: a human must handle it.")
    if ctx.band == Band.HUMAN_ACTION:
        return _check_human_approval(action, ctx, "director")
    if ctx.band == Band.NO_BLOCKERS:
        if action == Action.APPROVE_PR:
            return _allow("no_blockers", "Below the first threshold with no blockers.")
        return _deny("nothing_to_reject", "A request without blockers is not rejected by the agent.")
    if not ctx.cited_clauses:
        return _deny("no_clause", "A decision in this band must cite a policy clause.")
    if action == Action.APPROVE_PR:
        if ctx.policy_verdict != PolicyVerdict.PERMITS:
            return _deny("policy_unclear", "The policy verdict does not support this action.")
        return _check_human_approval(action, ctx, "department_head")
    if ctx.policy_verdict == PolicyVerdict.BLOCKS:
        return _allow("policy_blocks", "The cited clauses do not permit it.")
    if ctx.approval is not None and ctx.approval.decision == Action.REJECT_PR:
        return _check_human_approval(action, ctx, "department_head")
    return _deny("policy_unclear", "The policy verdict does not support this action.")


def authorize(action: Action, ctx: AuthContext) -> Verdict:
    if action in (Action.APPROVE_PR, Action.REJECT_PR):
        return _check_decision(action, ctx)
    if action == Action.SEND_REFERRAL_EMAIL:
        if ctx.status != Status.VALID or not ctx.budget_insufficient:
            return _deny("no_referral", "A referral is only for a valid request whose budget is insufficient.")
        return _allow("referral", "Management control receives the request.")
    if action == Action.SEND_DOSSIER_EMAIL:
        if ctx.band == Band.NO_BLOCKERS or ctx.status != Status.VALID:
            return _deny("no_dossier", "A dossier is only for a valid request that needs a human approval.")
        if not ctx.cited_clauses and ctx.band == Band.POLICY_TO_BE_VERIFIED:
            return _deny("no_clause", "The dossier must cite the clauses behind the proposal.")
        return _allow("dossier", "The approver receives the dossier.")
    if ctx.executed_decision is None:
        return _deny("nothing_executed", "The decision email follows an executed decision.")
    if not ctx.cited_clauses and ctx.band != Band.NO_BLOCKERS:
        return _deny("no_clause", "The email must cite the clauses behind the decision.")
    return _allow("decision_email", "The decision has been executed.")
