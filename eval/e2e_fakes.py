from pr_triage.agents.common import AgentFailure
from pr_triage.agents.communicator import CommunicationResult, EmailDraft, check_email
from pr_triage.agents.investigator import InvestigationReport, InvestigationResult, ResolvedField
from pr_triage.agents.triage import TriageOutcome, TriageReport
from pr_triage.authz import PolicyVerdict
from pr_triage.domain.band import Band, determine_band
from pr_triage.domain.category import Category
from pr_triage.domain.merge import merge_requests
from pr_triage.domain.schemas import Evidence, PurchaseRequest
from pr_triage.domain.status import EscalationReason
from pr_triage.domain.validation import validate_request
from pr_triage.supervisor.agent import Choice
from pr_triage.supervisor.state import Move

CLAUSE_TITLES = {
    "02-escaloes-e-cotacoes#2.1": ("2.1", "Abaixo de 1 000 €"),
    "02-escaloes-e-cotacoes#2.2": ("2.2", "De 1 000 € a 5 000 €"),
    "02-escaloes-e-cotacoes#2.3": ("2.3", "Acima de 5 000 €"),
    "02-escaloes-e-cotacoes#2.4": ("2.4", "Compras urgentes"),
    "03-fornecedores-e-categorias#3.3": ("3.3", "Equipamento informático"),
    "03-fornecedores-e-categorias#3.5": ("3.5", "Serviços e consultoria"),
}


def evidence(chunk_id: str) -> Evidence:
    clause, title = CLAUSE_TITLES[chunk_id]
    return Evidence(chunk_id=chunk_id, source=chunk_id.split("#")[0] + ".md", document="d", clause=clause,
                    title=title, effective_date=None, text=title)


class FakePolicy:
    def search(self, query, top_k):
        return [evidence("03-fornecedores-e-categorias#3.3")]

    def clause_ids(self):
        return set(CLAUSE_TITLES)

    def get(self, chunk_id):
        return evidence(chunk_id)


def request_from(spec: dict) -> PurchaseRequest:
    values = dict(item=None, quantity=None, estimated_value_eur=None, cost_center=None,
                  category=Category.OTHER, is_urgent=False, mentions_framework_contract=False)
    values.update(spec)
    values["category"] = Category(values["category"])
    return PurchaseRequest(**values)


class FakeTriage:
    def __init__(self, requests: dict, suspicious: list[str]):
        self.requests = {text: request_from(spec) for text, spec in requests.items()}
        self.suspicious = set(suspicious)

    def __call__(self, text, previous):
        if text in self.suspicious:
            return TriageReport(outcome=TriageOutcome.SUSPICIOUS)
        new = self.requests[text]
        request = merge_requests(previous, new) if previous else new
        missing, _ = validate_request(request)
        if missing:
            return TriageReport(outcome=TriageOutcome.NEEDS_INFO, request=request, missing=missing,
                                follow_up="Podes indicar " + ", ".join(missing) + "?")
        band = determine_band(request.estimated_value_eur)
        return TriageReport(
            outcome=TriageOutcome.READY, request=request, band=band,
            escalation=EscalationReason.OVER_THRESHOLD if band == Band.HUMAN_ACTION else None,
            evidence=[evidence("02-escaloes-e-cotacoes#2.1"), evidence("03-fornecedores-e-categorias#3.3")],
        )


class FakeInvestigator:
    def __init__(self, erp, verdict: str, clauses: list[str]):
        self.erp, self.verdict, self.clauses = erp, PolicyVerdict(verdict), clauses

    def __call__(self, request, email, ids, ctx, pr_id=None):
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
            if request.is_urgent and value <= 5000 and self.erp.get_urgency_justification(pr_id):
                required = 1
            found = len({q.supplier_id for q in self.erp.get_quotes(pr_id)})
            if found < required:
                verdict, questions = PolicyVerdict.UNCLEAR, ["Faltam cotações."]
        budget = self.erp.get_budget(request.cost_center) if request.cost_center else None
        budget_short = budget is not None and value is not None and budget.available_eur < value
        unjustified = bool(
            pr_id and request.is_urgent and 1000 <= value <= 5000 and not contract
            and not self.erp.get_urgency_justification(pr_id)
        )
        return InvestigationResult(report=InvestigationReport(
            budget_short=budget_short, urgency_unjustified=unjustified, resolved_fields=resolved,
            policy_verdict=verdict, cited_clauses=list(self.clauses), questions_for_user=questions,
            quotes_required=required, quotes_found=found, summary="Dentro da política."))


class FakeWriter:
    def __call__(self, brief):
        f = brief.facts
        clauses = " ".join(f"Cláusula {c.clause}: {c.title}." for c in brief.clauses)
        if brief.kind.value == "decision":
            verb = "aprovado" if brief.decision == "approved" else "rejeitado"
            body = f"Olá {f.requester_name}, o pedido de {f.quantity} {f.item}, de {f.value_eur:g} €, foi {verb}. {clauses}"
            subject = "Decisão sobre o pedido de compra"
        elif brief.kind.value == "referral":
            body = (f"Olá {brief.recipient_name}, {f.requester_name} pede {f.quantity} {f.item} por "
                    f"{f.value_eur:g} € no centro de custo {f.cost_center}, que só tem "
                    f"{f.budget_available_eur:g} € disponíveis. {clauses} Peço que trate do encaminhamento.")
            subject = "Orçamento insuficiente"
        else:
            budget = f" com {f.budget_available_eur:g} € de orçamento disponível" if f.budget_available_eur else ""
            body = (f"Olá {brief.recipient_name}, {f.requester_name} pede {f.quantity} {f.item} por "
                    f"{f.value_eur:g} € no centro de custo {f.cost_center}{budget}. {clauses} Peço a sua decisão.")
            subject = "Aprovação necessária"
        draft = EmailDraft(subject=subject, body=body)
        problems = check_email(draft, brief)
        if problems:
            return CommunicationResult(failure=AgentFailure(
                code="invalid_output", stage="write_email", message="; ".join(problems), retryable=False))
        return CommunicationResult(draft=draft)


class Decider:
    def __call__(self, state, moves):
        for move in list(Move):
            if move in moves:
                return Choice(move=move, reason="simulated")
        return Choice(move=moves[0], reason="simulated")
