import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pr_triage.agents.investigator import READ_TOOLS, check_investigation, run_investigation
from pr_triage.erp import Faults
from test_tools import FakePolicy, make
from pr_triage.domain.category import Category
from test_workflow import make_pr

T1 = "02-escaloes-e-cotacoes#2.1"
T33 = "03-fornecedores-e-categorias#3.3"
TRIAGE = {T1, T33}


def script(plan):
    def respond(messages, info: AgentInfo):
        turn = sum(isinstance(m, ModelResponse) for m in messages)
        step = plan[min(turn, len(plan) - 1)]
        if step[0] == "final":
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, step[1])])
        return ModelResponse(
            parts=[ToolCallPart(name, args) for name, args in step[1]]
        )

    return FunctionModel(respond)


def tool_step(*calls):
    return ("tools", list(calls))


def final(**report):
    report.setdefault("summary", "resumo")
    return ("final", report)


def investigate(tmp_path, plan, request=None, email="ana.silva@exemplo.pt", faults=None,
                pr_id=None, setup=None):
    ctx = make(tmp_path, faults=faults)
    if setup:
        setup(ctx)
    request = request or make_pr(
        item="monitores", quantity=2, estimated_value_eur=600.0,
        cost_center="CC-TI-100", category="it_equipment",
    )
    return run_investigation(request, email, TRIAGE, ctx, pr_id=pr_id, model=script(plan))


READ_ALL = tool_step(
    ("get_cost_center", {"code": "CC-TI-100"}),
    ("get_budget", {"cost_center": "CC-TI-100"}),
    ("search_catalog", {"query": "monitor"}),
    ("find_framework_contracts", {"category": "it_equipment"}),
)

GOOD_CHECKS = [
    {"name": "cost_center", "result": "ok", "evidence": "CC-TI-100 ativo"},
    {"name": "budget", "result": "ok", "evidence": "112000 disponíveis"},
    {"name": "catalog", "result": "warning", "evidence": "MON-27 289 x 2 = 578, pedido 600"},
    {"name": "framework_contract", "result": "ok", "evidence": "FC-001 em vigor"},
]


def test_the_investigator_has_no_tools_bound_to_a_registered_request():
    names = {t.name for t in READ_TOOLS}
    assert names.isdisjoint({"get_pr", "get_approval_status"})
    assert "get_quotes" in names


def test_a_supplier_check_needs_a_supplier_from_the_data(tmp_path):
    check = {"name": "supplier", "result": "ok", "evidence": "x"}
    bad = investigate(
        tmp_path,
        [tool_step(("get_supplier", {"supplier_id": "default"})),
         final(checks=[check], policy_verdict="unclear")],
    )
    assert bad.failure.code == "invalid_output"
    assert "supplier must be one that appeared" in bad.failure.message
    good = investigate(
        tmp_path,
        [tool_step(("search_catalog", {"query": "monitor"})),
         tool_step(("get_supplier", {"supplier_id": "S01"})),
         final(checks=[check], policy_verdict="unclear")],
    )
    assert good.failure is None


def test_rejections_are_recorded_for_diagnosis(tmp_path):
    result = investigate(
        tmp_path,
        [READ_ALL,
         final(checks=GOOD_CHECKS, policy_verdict="permits", cited_clauses=["99-inventada#9.9"]),
         final(checks=GOOD_CHECKS, policy_verdict="permits", cited_clauses=[T1])],
    )
    assert result.failure is None and len(result.rejections) == 1
    assert "never retrieved" in result.rejections[0][0]


def test_the_investigator_has_no_write_tools():
    assert {t.name for t in READ_TOOLS}.isdisjoint(
        {"register_pr", "approve_pr", "reject_pr", "send_email"}
    )


def test_happy_path_returns_a_validated_report(tmp_path):
    result = investigate(
        tmp_path,
        [READ_ALL, final(checks=GOOD_CHECKS, policy_verdict="permits", cited_clauses=[T1, T33])],
    )
    assert result.failure is None
    assert result.report.policy_verdict.value == "permits"
    assert sorted(c.name for c in result.tool_calls) == [
        "find_framework_contracts", "get_budget", "get_cost_center", "search_catalog"
    ]
    assert all(c.result.ok for c in result.tool_calls)


def test_missing_cost_center_is_completed_from_the_requester(tmp_path):
    request = make_pr(item="monitores", quantity=2, estimated_value_eur=600.0,
                      cost_center=None, category="it_equipment")
    resolved = {"field": "cost_center", "value": "CC-TI-100", "source_tool": "get_person",
                "evidence": "default cost center of ana.silva"}
    result = investigate(
        tmp_path,
        [tool_step(("get_person", {"email": "ana.silva@exemplo.pt"})),
         final(resolved_fields=[resolved], policy_verdict="permits", cited_clauses=[T1])],
        request=request,
    )
    assert result.failure is None
    assert result.report.resolved_fields[0].value == "CC-TI-100"


def test_requester_without_default_must_be_asked(tmp_path):
    request = make_pr(cost_center=None)
    result = investigate(
        tmp_path,
        [tool_step(("get_person", {"email": "tiago.lopes@exemplo.pt"})),
         final(policy_verdict="unclear",
               questions_for_user=["Qual é o centro de custo?"])],
        request=request, email="tiago.lopes@exemplo.pt",
    )
    assert result.failure is None and result.report.questions_for_user


def test_a_field_with_no_source_call_is_sent_back_and_corrected(tmp_path):
    request = make_pr(cost_center=None)
    invented = {"field": "cost_center", "value": "CC-TI-100", "source_tool": "get_person",
                "evidence": "I assumed it"}
    result = investigate(
        tmp_path,
        [final(resolved_fields=[invented], policy_verdict="permits", cited_clauses=[T1]),
         final(policy_verdict="unclear", questions_for_user=["Qual é o centro de custo?"])],
        request=request,
    )
    assert result.failure is None
    assert result.report.resolved_fields == []
    assert result.report.policy_verdict.value == "unclear"


def test_a_cost_center_that_is_not_the_requesters_is_refused(tmp_path):
    request = make_pr(cost_center=None)
    wrong = {"field": "cost_center", "value": "CC-FAC-200", "source_tool": "get_person",
             "evidence": "x"}
    result = investigate(
        tmp_path,
        [tool_step(("get_person", {"email": "ana.silva@exemplo.pt"})),
         final(resolved_fields=[wrong], policy_verdict="unclear", questions_for_user=["?"])],
        request=request,
    )
    assert result.failure.code == "invalid_output"


def test_missing_value_is_computed_from_the_catalog(tmp_path):
    request = make_pr(item="monitores", quantity=2, estimated_value_eur=None,
                      cost_center="CC-TI-100", category="it_equipment")
    value = {"field": "estimated_value_eur", "value": 578.0, "source_tool": "search_catalog",
             "evidence": "2 x MON-27 289"}
    result = investigate(
        tmp_path,
        [tool_step(("search_catalog", {"query": "monitor"})),
         final(resolved_fields=[value], policy_verdict="unclear", questions_for_user=["Confirmas 578 €?"])],
        request=request,
    )
    assert result.failure is None and result.report.resolved_fields[0].value == 578.0


def test_a_user_value_cannot_be_overwritten(tmp_path):
    value = {"field": "estimated_value_eur", "value": 578.0, "source_tool": "search_catalog",
             "evidence": "x"}
    result = investigate(
        tmp_path,
        [tool_step(("search_catalog", {"query": "monitor"})),
         final(resolved_fields=[value], policy_verdict="unclear")],
    )
    assert result.failure.code == "invalid_output"


def test_a_clause_that_was_never_retrieved_is_refused(tmp_path):
    result = investigate(
        tmp_path,
        [READ_ALL, final(checks=GOOD_CHECKS, policy_verdict="permits", cited_clauses=["99-inventada#9.9"]),
         final(checks=GOOD_CHECKS, policy_verdict="permits", cited_clauses=[T1])],
    )
    assert result.failure is None and result.report.cited_clauses == [T1]


def test_blocks_without_a_problem_check_is_refused(tmp_path):
    result = investigate(
        tmp_path,
        [tool_step(("search_policy", {"query": "equipamento informático"})),
         final(policy_verdict="blocks", cited_clauses=[T33],
               checks=[]),
         ],
    )
    assert result.failure.code == "invalid_output"


def test_blocks_with_a_problem_check_and_a_retrieved_clause_is_valid(tmp_path):
    result = investigate(
        tmp_path,
        [tool_step(("search_policy", {"query": "equipamento informático"}),
                   ("search_catalog", {"query": "teclado"})),
         final(policy_verdict="blocks", cited_clauses=[T33],
               checks=[{"name": "catalog", "result": "problem", "evidence": "not bought through the catalog"}]),
         ],
    )
    assert result.failure is None and result.report.policy_verdict.value == "blocks"


def test_permits_with_a_problem_check_is_sent_back(tmp_path):
    bad = [{**c, "result": "problem"} if c["name"] == "budget" else c for c in GOOD_CHECKS]
    result = investigate(
        tmp_path,
        [READ_ALL,
         final(checks=bad, policy_verdict="permits", cited_clauses=[T1]),
         final(checks=bad, policy_verdict="blocks", cited_clauses=[T1])],
    )
    assert result.failure is None and result.report.policy_verdict.value == "blocks"


def test_ok_cannot_be_claimed_after_the_tool_failed(tmp_path):
    result = investigate(
        tmp_path,
        [READ_ALL,
         final(checks=GOOD_CHECKS, policy_verdict="permits", cited_clauses=[T1]),
         final(checks=[c if c["name"] != "budget" else {**c, "result": "not_checked", "evidence": "ERP failed"}
                       for c in GOOD_CHECKS],
               policy_verdict="unclear", questions_for_user=["Repetir o orçamento mais tarde"])],
        faults=Faults(unavailable={"get_budget"}),
    )
    assert result.failure is None
    budget = next(c for c in result.report.checks if c.name.value == "budget")
    assert budget.result.value == "not_checked"
    assert any(not c.result.ok and c.result.error.code == "erp_unavailable" for c in result.tool_calls)


def test_endless_tool_calls_stop_at_the_step_limit(tmp_path):
    loop = tool_step(("get_budget", {"cost_center": "CC-TI-100"}))
    result = investigate(tmp_path, [loop])
    assert result.failure.code == "step_limit" and not result.failure.retryable


def test_model_outage_is_a_retryable_failure(tmp_path):
    def down(messages, info):
        raise ModelHTTPError(503, "gpt")

    ctx = make(tmp_path)
    result = run_investigation(make_pr(), "ana.silva@exemplo.pt", TRIAGE, ctx, model=FunctionModel(down))
    assert result.failure.code == "llm_unavailable" and result.failure.retryable


def test_invalid_arguments_from_the_model_come_back_as_data(tmp_path):
    result = investigate(
        tmp_path,
        [tool_step(("get_budget", {"wrong": 1})),
         final(policy_verdict="unclear", questions_for_user=["?"])],
    )
    assert result.failure is None
    assert result.tool_calls[0].result.error.code == "invalid_arguments"


SERVICE = dict(item="consultoria", quantity=1, estimated_value_eur=3000.0,
               cost_center="CC-TI-100", category=Category.SERVICES)


def with_quotes(*suppliers, value=3000.0, category=Category.SERVICES):
    def setup(ctx):
        ctx.erp.register_pr("r", "agent", "ana.silva@exemplo.pt", "consultoria", 1, value,
                            "CC-TI-100", category)
        for i, supplier in enumerate(suppliers):
            ctx.erp.add_quote(f"q{i}", "ana", 1, supplier, 2900.0 + i)

    return setup


READ_QUOTES = tool_step(
    ("get_quotes", {"pr_id": 1}),
    ("find_framework_contracts", {"category": "services"}),
    ("search_policy", {"query": "cotações"}),
)
QUOTES_PROBLEM = [{"name": "quotes", "result": "problem", "evidence": "0 de 2"}]
QUOTES_OK = [{"name": "quotes", "result": "ok", "evidence": "2 de 2"}]


def test_missing_quotes_are_reported_and_the_verdict_cannot_permit(tmp_path):
    request = make_pr(**SERVICE)
    result = investigate(
        tmp_path,
        [READ_QUOTES,
         final(checks=QUOTES_OK, quotes_required=2, quotes_found=0, policy_verdict="permits", cited_clauses=[T1]),
         final(checks=QUOTES_PROBLEM, quotes_required=2, quotes_found=0, policy_verdict="unclear",
               questions_for_user=["Faltam 2 cotações."])],
        request=request, pr_id=1, setup=with_quotes(),
    )
    assert result.failure is None and result.report.quotes_found == 0
    assert result.report.policy_verdict.value == "unclear"
    assert "missing quotes must be reported as a problem" in "; ".join(result.rejections[0])


def test_the_quotes_count_must_match_the_erp(tmp_path):
    request = make_pr(**SERVICE)
    lying = final(checks=QUOTES_OK, quotes_required=2, quotes_found=2, policy_verdict="permits", cited_clauses=[T1])
    result = investigate(tmp_path, [READ_QUOTES, lying], request=request, pr_id=1, setup=with_quotes("S02"))
    assert result.failure.code == "invalid_output"
    assert "distinct suppliers in the ERP (1)" in result.failure.message


def test_two_quotes_from_the_same_supplier_count_once(tmp_path):
    request = make_pr(**SERVICE)
    result = investigate(
        tmp_path,
        [READ_QUOTES,
         final(checks=QUOTES_PROBLEM, quotes_required=2, quotes_found=1, policy_verdict="unclear",
               questions_for_user=["Falta uma cotação de outro fornecedor."])],
        request=request, pr_id=1, setup=with_quotes("S02", "S02"),
    )
    assert result.failure is None and result.report.quotes_found == 1


def test_enough_quotes_from_different_suppliers_allow_a_permit(tmp_path):
    request = make_pr(**SERVICE)
    result = investigate(
        tmp_path,
        [READ_QUOTES,
         final(checks=QUOTES_OK, quotes_required=2, quotes_found=2, policy_verdict="permits", cited_clauses=[T1])],
        request=request, pr_id=1, setup=with_quotes("S02", "S03"),
    )
    assert result.failure is None and result.report.policy_verdict.value == "permits"


def test_the_requirement_cannot_be_lowered_below_the_policy(tmp_path):
    request = make_pr(**SERVICE)
    result = investigate(
        tmp_path,
        [READ_QUOTES,
         final(checks=QUOTES_OK, quotes_required=1, quotes_found=0, policy_verdict="unclear")],
        request=request, pr_id=1, setup=with_quotes(),
    )
    assert "requires at least 2 quotes" in result.failure.message


def test_the_quotes_must_actually_be_read(tmp_path):
    request = make_pr(**SERVICE)
    result = investigate(
        tmp_path,
        [tool_step(("find_framework_contracts", {"category": "services"})),
         final(policy_verdict="unclear", questions_for_user=["?"])],
        request=request, pr_id=1, setup=with_quotes("S02", "S03"),
    )
    assert "must be read with get_quotes" in result.failure.message


def test_a_framework_contract_in_force_dispenses_the_quotes(tmp_path):
    request = make_pr(item="portáteis", quantity=4, estimated_value_eur=14000.0,
                      cost_center="CC-TI-100", category=Category.IT_EQUIPMENT)
    setup = with_quotes(value=14000.0, category=Category.IT_EQUIPMENT)
    result = investigate(
        tmp_path,
        [tool_step(("find_framework_contracts", {"category": "it_equipment"})),
         final(policy_verdict="permits", cited_clauses=[T33])],
        request=request, pr_id=1, setup=setup,
    )
    assert result.failure is None


def test_before_registration_no_quotes_are_required(tmp_path):
    request = make_pr(**SERVICE)
    result = investigate(tmp_path, [final(policy_verdict="unclear", questions_for_user=["?"])], request=request)
    assert result.failure is None
