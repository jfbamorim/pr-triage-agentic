from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from pr_triage.tools import read, write
from pr_triage.tools.context import ToolContext
from pr_triage.tools.envelope import ToolResult, failure


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, BaseModel], ToolResult]
    writes: bool = False


TOOLS = [
    Tool("get_person", "Look up a person by email: name, role and default cost center.", read.GetPersonIn, read.get_person),
    Tool("get_cost_center", "Look up a cost center: owner and whether it is active.", read.GetCostCenterIn, read.get_cost_center),
    Tool("get_budget", "Budget of a cost center for the current year, with the available amount.", read.GetBudgetIn, read.get_budget),
    Tool("get_supplier", "Look up a supplier and whether it is approved.", read.GetSupplierIn, read.get_supplier),
    Tool("find_framework_contracts", "Framework contracts for a category, with validity and remaining value.", read.FindContractsIn, read.find_framework_contracts),
    Tool("search_catalog", "Search catalog items by text and/or category.", read.SearchCatalogIn, read.search_catalog),
    Tool("get_pr", "Read a purchase request and its current state.", read.PrIdIn, read.get_pr),
    Tool("get_quotes", "Quotes registered for a purchase request.", read.PrIdIn, read.get_quotes),
    Tool("get_approval_status", "Whether a human approval is on record for a purchase request.", read.PrIdIn, read.get_approval_status),
    Tool("search_policy", "Search the purchasing policy clauses.", read.SearchPolicyIn, read.search_policy),
    Tool("register_pr", "Register a validated purchase request in the ERP.", write.RegisterPrIn, write.register_pr, writes=True),
    Tool("approve_pr", "Approve a purchase request. Refused by code unless the authorization rules allow it.", write.DecisionIn, write.approve_pr, writes=True),
    Tool("reject_pr", "Reject a purchase request. Refused by code unless the authorization rules allow it.", write.DecisionIn, write.reject_pr, writes=True),
    Tool("send_email", "Send a decision email to the requester or a dossier to an approver.", write.SendEmailIn, write.send_email, writes=True),
]

BY_NAME = {tool.name: tool for tool in TOOLS}


def call_tool(ctx: ToolContext, name: str, arguments: dict) -> ToolResult:
    if ctx.port is not None:
        return ctx.port.call(ctx, name, arguments)
    return run_tool(ctx, name, arguments)


def run_tool(ctx: ToolContext, name: str, arguments: dict) -> ToolResult:
    tool = BY_NAME.get(name)
    if tool is None:
        return failure("unknown_tool", f"There is no tool named {name}.")
    try:
        args = tool.input_model.model_validate(arguments)
    except ValidationError as error:
        return failure("invalid_arguments", str(error))
    return tool.handler(ctx, args)
