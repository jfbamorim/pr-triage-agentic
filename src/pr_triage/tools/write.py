from typing import Literal

from pydantic import BaseModel, Field

from pr_triage.authz import Action, AuthContext, PolicyVerdict, authorize
from pr_triage.domain.band import determine_band
from pr_triage.domain.category import Category
from pr_triage.domain.status import Status
from pr_triage.erp.models import PrState
from pr_triage.tools.context import ToolContext
from pr_triage.tools.envelope import ToolResult, failure, guarded

DOSSIER_RECIPIENT_ROLES = ("director", "department_head")


class RegisterPrIn(BaseModel):
    requester_email: str
    item: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    value_eur: float = Field(gt=0)
    cost_center: str
    category: Category
    urgent: bool = False


class DecisionIn(BaseModel):
    pr_id: int
    policy_verdict: PolicyVerdict
    cited_clauses: list[str] = []
    justification: str = Field(min_length=1)


class SendEmailIn(BaseModel):
    pr_id: int
    kind: Literal["decision", "dossier"]
    to: str
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    cited_clauses: list[str] = []


def _unknown_clauses(ctx: ToolContext, clauses: list[str]) -> list[str]:
    known = ctx.policy.clause_ids()
    return [clause for clause in clauses if clause not in known]


def _auth_context(ctx: ToolContext, pr, **overrides) -> AuthContext:
    data = dict(
        request_id=pr.pr_id,
        status=Status.VALID,
        value_eur=pr.value_eur,
        band=determine_band(pr.value_eur),
        proposed_by=ctx.actor,
        approval=ctx.erp.get_human_approval(pr.pr_id),
        now=ctx.erp.clock(),
    )
    data.update(overrides)
    return AuthContext(**data)


def _denied(verdict) -> ToolResult:
    return failure("not_authorized", verdict.reason, detail=verdict.code)


def register_pr(ctx: ToolContext, args: RegisterPrIn) -> ToolResult:
    return guarded(
        lambda: ctx.erp.register_pr(
            f"{ctx.run_id}:register_pr",
            ctx.actor,
            args.requester_email,
            args.item,
            args.quantity,
            args.value_eur,
            args.cost_center,
            args.category,
            args.urgent,
        )
    )


def _decide(ctx: ToolContext, action: Action, args: DecisionIn) -> ToolResult:
    def run():
        pr = ctx.erp.get_pr(args.pr_id)
        if pr is None:
            return failure("not_found", f"Purchase request {args.pr_id} does not exist.")
        unknown = _unknown_clauses(ctx, args.cited_clauses)
        if unknown:
            return failure("unknown_clause", f"These clauses do not exist: {unknown}.")
        auth = _auth_context(
            ctx, pr, policy_verdict=args.policy_verdict, cited_clauses=args.cited_clauses
        )
        verdict = authorize(action, auth)
        if not verdict.allowed:
            return _denied(verdict)
        key = f"{ctx.run_id}:{action.value}:{pr.pr_id}"
        operation = ctx.erp.approve_pr if action == Action.APPROVE_PR else ctx.erp.reject_pr
        return operation(key, ctx.actor, pr.pr_id, args.cited_clauses, args.justification)

    return guarded(run)


def approve_pr(ctx: ToolContext, args: DecisionIn) -> ToolResult:
    return _decide(ctx, Action.APPROVE_PR, args)


def reject_pr(ctx: ToolContext, args: DecisionIn) -> ToolResult:
    return _decide(ctx, Action.REJECT_PR, args)


def send_email(ctx: ToolContext, args: SendEmailIn) -> ToolResult:
    def run():
        pr = ctx.erp.get_pr(args.pr_id)
        if pr is None:
            return failure("not_found", f"Purchase request {args.pr_id} does not exist.")
        if args.kind == "decision":
            if args.to != pr.requester_email:
                return failure("wrong_recipient", "A decision email goes to the requester.")
            executed = {PrState.APPROVED: Action.APPROVE_PR, PrState.REJECTED: Action.REJECT_PR}.get(
                pr.state
            )
            clauses = pr.cited_clauses
            action = Action.SEND_DECISION_EMAIL
            auth = _auth_context(ctx, pr, cited_clauses=clauses, executed_decision=executed)
        else:
            person = ctx.erp.get_person(args.to)
            if person is None or person.role.value not in DOSSIER_RECIPIENT_ROLES:
                return failure("wrong_recipient", "A dossier goes to an approver.")
            unknown = _unknown_clauses(ctx, args.cited_clauses)
            if unknown:
                return failure("unknown_clause", f"These clauses do not exist: {unknown}.")
            clauses = args.cited_clauses
            action = Action.SEND_DOSSIER_EMAIL
            auth = _auth_context(ctx, pr, cited_clauses=clauses)
        verdict = authorize(action, auth)
        if not verdict.allowed:
            return _denied(verdict)
        key = f"{ctx.run_id}:send_email:{pr.pr_id}:{args.kind}"
        return ctx.erp.send_email(
            key, ctx.actor, pr.pr_id, args.to, args.subject, args.body, args.kind, clauses
        )

    return guarded(run)
