from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from pydantic import BaseModel

from pr_triage.domain.schemas import PurchaseRequest
from pr_triage.tools import ToolContext, ToolError, ToolResult, call_tool

CATALOG_TOLERANCE = 0.2
FIRST_THRESHOLD = 1000.0
SECOND_THRESHOLD = 5000.0


class CheckName(str, Enum):
    COST_CENTER = "cost_center"
    BUDGET = "budget"
    CATALOG = "catalog"
    FRAMEWORK_CONTRACT = "framework_contract"
    SUPPLIER = "supplier"
    QUOTES = "quotes"


class CheckResult(str, Enum):
    OK = "ok"
    WARNING = "warning"
    PROBLEM = "problem"
    NOT_CHECKED = "not_checked"


class ResolvedField(BaseModel):
    field: Literal["cost_center", "estimated_value_eur"]
    value: str | float
    source_tool: str
    evidence: str


class Check(BaseModel):
    name: CheckName
    result: CheckResult
    evidence: str


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict
    result: ToolResult


class FactsUnavailable(Exception):
    def __init__(self, error: ToolError, tool: str):
        super().__init__(f"{tool}: {error.code}: {error.message}")
        self.error = error
        self.tool = tool


@dataclass
class Facts:
    cost_center: str | None = None
    value_eur: float | None = None
    resolved_fields: list[ResolvedField] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    quotes_required: int | None = None
    quotes_found: int | None = None
    budget_short: bool = False
    urgency_unjustified: bool = False
    log: list[ToolCallRecord] = field(default_factory=list)

    def check(self, name: CheckName) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    @property
    def problems(self) -> list[CheckName]:
        return [c.name for c in self.checks if c.result == CheckResult.PROBLEM]

    @property
    def complete(self) -> bool:
        return self.cost_center is not None and self.value_eur is not None

    @property
    def quotes_short(self) -> bool:
        return self.quotes_required is not None and (self.quotes_found or 0) < self.quotes_required


def quotes_required_for(
    value: float, urgent: bool, contract_in_force: bool, urgency_justified: bool = False
) -> int:
    if value < FIRST_THRESHOLD or contract_in_force:
        return 0
    if urgent and urgency_justified and value <= SECOND_THRESHOLD:
        return 1
    return 2 if value <= SECOND_THRESHOLD else 3


def _read(ctx: ToolContext, facts: Facts, name: str, arguments: dict) -> ToolResult:
    result = call_tool(ctx, name, arguments)
    facts.log.append(ToolCallRecord(name=name, arguments=arguments, result=result))
    if not result.ok and result.error.retryable:
        raise FactsUnavailable(result.error, name)
    return result


def _money(value: float) -> str:
    return f"{value:g} €"


def _cost_center_question(ctx: ToolContext, facts: Facts) -> str:
    plain = "Qual o centro de custo a usar neste pedido?"
    entry = ctx.memory.recall("cost_center") if ctx.memory is not None else None
    if entry is None:
        return plain
    found = call_tool(ctx, "get_cost_center", {"code": entry.value})
    facts.log.append(ToolCallRecord(name="get_cost_center", arguments={"code": entry.value}, result=found))
    if not found.ok and found.error.retryable:
        raise FactsUnavailable(found.error, "get_cost_center")
    if not found.ok or not found.data["active"]:
        ctx.memory.invalidate(entry, f"cost center {entry.value} is no longer valid in the ERP")
        return plain
    return (
        f"{plain} Da última vez usaste {entry.value} (pedido nº {entry.origin_pr_id}, "
        f"{entry.created_at:%d/%m/%Y}). Para confirmares, responde: Centro de custo: {entry.value}"
    )


def gather_facts(
    ctx: ToolContext, request: PurchaseRequest, requester_email: str, pr_id: int | None
) -> Facts:
    facts = Facts(cost_center=request.cost_center, value_eur=request.estimated_value_eur)

    if facts.cost_center is None:
        person = _read(ctx, facts, "get_person", {"email": requester_email})
        default = person.data.get("default_cost_center") if person.ok else None
        if default:
            facts.cost_center = default
            facts.resolved_fields.append(
                ResolvedField(
                    field="cost_center",
                    value=default,
                    source_tool="get_person",
                    evidence=f"Centro de custo por omissão de {person.data['name']}.",
                )
            )
        else:
            facts.questions.append(_cost_center_question(ctx, facts))

    catalog = _read(
        ctx, facts, "search_catalog", {"query": request.item, "category": request.category.value}
    )
    items = [i for i in (catalog.data if catalog.ok else []) if i["available"]]

    if facts.value_eur is None:
        if request.quantity and len(items) == 1:
            facts.value_eur = round(request.quantity * items[0]["unit_price_eur"], 2)
            facts.resolved_fields.append(
                ResolvedField(
                    field="estimated_value_eur",
                    value=facts.value_eur,
                    source_tool="search_catalog",
                    evidence=f"{request.quantity} x {items[0]['name']} a {_money(items[0]['unit_price_eur'])}.",
                )
            )
        else:
            facts.questions.append("Qual o valor estimado do pedido?")

    cost_center, value = facts.cost_center, facts.value_eur

    if cost_center is None:
        facts.checks.append(Check(name=CheckName.COST_CENTER, result=CheckResult.NOT_CHECKED, evidence="Sem centro de custo."))
    else:
        found = _read(ctx, facts, "get_cost_center", {"code": cost_center})
        if not found.ok:
            facts.checks.append(Check(name=CheckName.COST_CENTER, result=CheckResult.PROBLEM, evidence=f"O centro de custo {cost_center} não existe."))
        elif found.data["active"]:
            facts.checks.append(Check(name=CheckName.COST_CENTER, result=CheckResult.OK, evidence=f"O centro de custo {cost_center} está ativo ({found.data['name']})."))
        else:
            facts.checks.append(Check(name=CheckName.COST_CENTER, result=CheckResult.PROBLEM, evidence=f"O centro de custo {cost_center} está inativo."))
            facts.questions.append(f"O centro de custo {cost_center} está inativo. Que centro de custo devo usar?")

    if cost_center is None or value is None or facts.check(CheckName.COST_CENTER).result == CheckResult.PROBLEM:
        facts.checks.append(Check(name=CheckName.BUDGET, result=CheckResult.NOT_CHECKED, evidence="Faltam dados para verificar o orçamento."))
    else:
        budget = _read(ctx, facts, "get_budget", {"cost_center": cost_center})
        if not budget.ok:
            facts.checks.append(Check(name=CheckName.BUDGET, result=CheckResult.PROBLEM, evidence=f"Não há orçamento registado para {cost_center}."))
        else:
            available = budget.data["available_eur"]
            if available >= value:
                facts.checks.append(Check(name=CheckName.BUDGET, result=CheckResult.OK, evidence=f"Orçamento disponível de {_money(available)}, suficiente para {_money(value)}."))
            else:
                facts.checks.append(Check(name=CheckName.BUDGET, result=CheckResult.PROBLEM, evidence=f"Orçamento disponível de {_money(available)}, insuficiente para {_money(value)}."))
                facts.budget_short = True

    if not items:
        facts.checks.append(Check(name=CheckName.CATALOG, result=CheckResult.WARNING, evidence="O item não foi encontrado no catálogo."))
    elif value is None or not request.quantity:
        names = ", ".join(i["name"] for i in items)
        facts.checks.append(Check(name=CheckName.CATALOG, result=CheckResult.OK, evidence=f"Encontrado no catálogo: {names}."))
    else:
        best = min(items, key=lambda i: abs(i["unit_price_eur"] * request.quantity - value))
        expected = round(best["unit_price_eur"] * request.quantity, 2)
        close = abs(value - expected) <= CATALOG_TOLERANCE * expected
        facts.checks.append(
            Check(
                name=CheckName.CATALOG,
                result=CheckResult.OK if close else CheckResult.WARNING,
                evidence=f"{best['name']}: {request.quantity} x {_money(best['unit_price_eur'])} = {_money(expected)}; pedido de {_money(value)}.",
            )
        )

    contracts = _read(ctx, facts, "find_framework_contracts", {"category": request.category.value})
    in_force = [c for c in (contracts.data if contracts.ok else []) if c["in_force"]]
    usable = [c for c in in_force if value is None or c["remaining_eur"] >= value]
    if usable:
        c = usable[0]
        facts.checks.append(Check(name=CheckName.FRAMEWORK_CONTRACT, result=CheckResult.OK, evidence=f"Contrato-quadro {c['id']} ({c['supplier_name']}) em vigor, com {_money(c['remaining_eur'])} disponíveis."))
    elif in_force:
        facts.checks.append(Check(name=CheckName.FRAMEWORK_CONTRACT, result=CheckResult.WARNING, evidence="Há contrato-quadro em vigor mas sem valor disponível suficiente."))
    else:
        facts.checks.append(Check(name=CheckName.FRAMEWORK_CONTRACT, result=CheckResult.WARNING, evidence="Não há contrato-quadro em vigor para esta categoria."))

    supplier_ids = {c["supplier_id"] for c in usable} or {i["supplier_id"] for i in items}
    if len(supplier_ids) == 1:
        supplier_id = next(iter(supplier_ids))
        supplier = _read(ctx, facts, "get_supplier", {"supplier_id": supplier_id})
        if supplier.ok and supplier.data["approved"]:
            facts.checks.append(Check(name=CheckName.SUPPLIER, result=CheckResult.OK, evidence=f"Fornecedor {supplier.data['name']} ({supplier_id}) aprovado."))
        elif supplier.ok:
            facts.checks.append(Check(name=CheckName.SUPPLIER, result=CheckResult.PROBLEM, evidence=f"Fornecedor {supplier.data['name']} ({supplier_id}) não está aprovado."))
        else:
            facts.checks.append(Check(name=CheckName.SUPPLIER, result=CheckResult.PROBLEM, evidence=f"Fornecedor {supplier_id} não existe."))
    else:
        facts.checks.append(Check(name=CheckName.SUPPLIER, result=CheckResult.NOT_CHECKED, evidence="Nenhum fornecedor único identificado pelo catálogo ou pelo contrato."))

    if pr_id is None or value is None:
        facts.checks.append(Check(name=CheckName.QUOTES, result=CheckResult.NOT_CHECKED, evidence="Pedido ainda não registado ou sem valor."))
    else:
        urgency_applies = request.is_urgent and FIRST_THRESHOLD <= value <= SECOND_THRESHOLD and not in_force
        justified = False
        justification = None
        if urgency_applies:
            found = _read(ctx, facts, "get_urgency_justification", {"pr_id": pr_id})
            justified = found.ok and found.data["justified"]
            justification = found.data["justification"] if justified else None
            facts.urgency_unjustified = not justified
        required = quotes_required_for(value, request.is_urgent, bool(in_force), justified)
        facts.quotes_required = required
        if required == 0:
            facts.checks.append(Check(name=CheckName.QUOTES, result=CheckResult.OK, evidence="A política não exige cotações para este pedido."))
        else:
            quotes = _read(ctx, facts, "get_quotes", {"pr_id": pr_id})
            facts.quotes_found = len({q["supplier_id"] for q in (quotes.data if quotes.ok else [])})
            result = CheckResult.OK if facts.quotes_found >= required else CheckResult.PROBLEM
            evidence = f"{facts.quotes_found} cotações de fornecedores distintos, a política exige {required}."
            if justification:
                evidence += f" Urgência justificada por escrito por {justification['by']} (cláusula 2.4)."
            elif facts.urgency_unjustified:
                evidence += " O pedido é urgente, mas falta a justificação escrita do responsável do departamento (cláusula 2.4)."
            facts.checks.append(Check(name=CheckName.QUOTES, result=result, evidence=evidence))
    return facts
