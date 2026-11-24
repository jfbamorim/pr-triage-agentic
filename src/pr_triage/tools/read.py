from pydantic import BaseModel, Field

from pr_triage.domain.category import Category
from pr_triage.tools.context import ToolContext
from pr_triage.tools.envelope import ToolResult, failure, guarded


class GetPersonIn(BaseModel):
    email: str


class GetCostCenterIn(BaseModel):
    code: str


class GetBudgetIn(BaseModel):
    cost_center: str


class GetSupplierIn(BaseModel):
    supplier_id: str


class FindContractsIn(BaseModel):
    category: Category


class SearchCatalogIn(BaseModel):
    query: str | None = None
    category: Category | None = None


class PrIdIn(BaseModel):
    pr_id: int


class SearchPolicyIn(BaseModel):
    query: str = Field(min_length=3)
    top_k: int = Field(default=5, ge=1, le=10)


def _found(value, what: str, key) -> ToolResult | object:
    if value is None:
        return failure("not_found", f"{what} {key} does not exist.")
    return value


def get_person(ctx: ToolContext, args: GetPersonIn) -> ToolResult:
    return guarded(lambda: _found(ctx.erp.get_person(args.email), "Person", args.email))


def get_cost_center(ctx: ToolContext, args: GetCostCenterIn) -> ToolResult:
    return guarded(lambda: _found(ctx.erp.get_cost_center(args.code), "Cost center", args.code))


def get_budget(ctx: ToolContext, args: GetBudgetIn) -> ToolResult:
    return guarded(
        lambda: _found(ctx.erp.get_budget(args.cost_center), "Budget for", args.cost_center)
    )


def get_supplier(ctx: ToolContext, args: GetSupplierIn) -> ToolResult:
    return guarded(lambda: _found(ctx.erp.get_supplier(args.supplier_id), "Supplier", args.supplier_id))


def find_framework_contracts(ctx: ToolContext, args: FindContractsIn) -> ToolResult:
    return guarded(lambda: ctx.erp.find_framework_contracts(args.category))


def search_catalog(ctx: ToolContext, args: SearchCatalogIn) -> ToolResult:
    return guarded(lambda: ctx.erp.search_catalog(args.query, args.category))


def get_pr(ctx: ToolContext, args: PrIdIn) -> ToolResult:
    return guarded(lambda: _found(ctx.erp.get_pr(args.pr_id), "Purchase request", args.pr_id))


def get_quotes(ctx: ToolContext, args: PrIdIn) -> ToolResult:
    return guarded(lambda: ctx.erp.get_quotes(args.pr_id))


def get_approval_status(ctx: ToolContext, args: PrIdIn) -> ToolResult:
    def read():
        approval = ctx.erp.get_human_approval(args.pr_id)
        return {
            "has_human_approval": approval is not None,
            "approval": approval.model_dump(mode="json") if approval else None,
        }

    return guarded(read)


def search_policy(ctx: ToolContext, args: SearchPolicyIn) -> ToolResult:
    try:
        evidence = ctx.policy.search(args.query, args.top_k)
    except Exception as error:
        return failure("policy_unavailable", f"Policy search failed: {error}", retryable=True)
    return guarded(lambda: evidence)
