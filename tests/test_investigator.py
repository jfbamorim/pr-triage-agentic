from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pr_triage.agents.facts import CheckName, CheckResult, gather_facts
from pr_triage.agents.investigator import POLICY_TOOLS, run_investigation
from pr_triage.domain.category import Category
from pr_triage.erp import Faults
from test_tools import make
from test_workflow import blank, make_pr

T1 = "02-escaloes-e-cotacoes#2.1"
T33 = "03-fornecedores-e-categorias#3.3"
TRIAGE = {T1, T33}
ANA = "ana.silva@exemplo.pt"
SOFIA = "sofia.matos@exemplo.pt"
TIAGO = "tiago.lopes@exemplo.pt"


def monitors(**overrides):
    values = dict(item="monitores", quantity=2, estimated_value_eur=600.0,
                  cost_center="CC-TI-100", category=Category.IT_EQUIPMENT)
    values.update(overrides)
    return make_pr(**values)


def consulting(**overrides):
    values = dict(item="consultoria", quantity=1, estimated_value_eur=3000.0,
                  cost_center="CC-TI-100", category=Category.SERVICES)
    values.update(overrides)
    return make_pr(**values)


def registered(ctx, request, *suppliers):
    result = ctx.erp.register_pr("r", "agent", ANA, request.item, request.quantity,
                                 request.estimated_value_eur, request.cost_center, request.category)
    pr_id = result.result["pr_id"]
    for i, supplier in enumerate(suppliers):
        ctx.erp.add_quote(f"q{i}", "ana", pr_id, supplier, 2900.0 + i)
    return pr_id


def facts_for(tmp_path, request, email=ANA, pr_id=None, setup=None):
    ctx = make(tmp_path)
    if setup:
        pr_id = setup(ctx)
    return gather_facts(ctx, request, email, pr_id)


def check(facts, name):
    return facts.check(name).result


def script(plan):
    def respond(messages, info: AgentInfo):
        turn = sum(isinstance(m, ModelResponse) for m in messages)
        step = plan[min(turn, len(plan) - 1)]
        if step[0] == "final":
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, step[1])])
        return ModelResponse(parts=[ToolCallPart(name, args) for name, args in step[1]])

    return FunctionModel(respond)


def search(query="cotações"):
    return ("tools", [("search_policy", {"query": query})])


def final(**opinion):
    opinion.setdefault("summary", "resumo")
    return ("final", opinion)


def investigate(tmp_path, plan, request=None, email=ANA, faults=None, setup=None, pr_id=None):
    ctx = make(tmp_path, faults=faults)
    if setup:
        pr_id = setup(ctx)
    return run_investigation(request or monitors(), email, TRIAGE, ctx, pr_id=pr_id, model=script(plan))


def test_the_investigator_only_has_the_policy_search_tool():
    assert [t.name for t in POLICY_TOOLS] == ["search_policy"]


def test_a_complete_it_request_has_every_fact_ok(tmp_path):
    facts = facts_for(tmp_path, monitors())
    assert [c.result for c in facts.checks] == [CheckResult.OK] * 5 + [CheckResult.NOT_CHECKED]
    assert facts.problems == [] and facts.questions == [] and facts.resolved_fields == []
    assert "FC-001" in facts.check(CheckName.FRAMEWORK_CONTRACT).evidence
    assert "TechNova" in facts.check(CheckName.SUPPLIER).evidence


def test_a_missing_cost_center_is_taken_from_the_requester(tmp_path):
    facts = facts_for(tmp_path, monitors(cost_center=None))
    assert facts.cost_center == "CC-TI-100"
    field = facts.resolved_fields[0]
    assert field.field == "cost_center" and field.value == "CC-TI-100" and field.source_tool == "get_person"
    assert check(facts, CheckName.COST_CENTER) == CheckResult.OK


def test_a_requester_without_a_cost_center_leads_to_a_question(tmp_path):
    facts = facts_for(tmp_path, monitors(cost_center=None), email=TIAGO)
    assert facts.cost_center is None and not facts.complete
    assert facts.questions == ["Qual o centro de custo a usar neste pedido?"]
    assert check(facts, CheckName.COST_CENTER) == CheckResult.NOT_CHECKED
    assert check(facts, CheckName.BUDGET) == CheckResult.NOT_CHECKED


def test_a_missing_value_is_resolved_from_a_single_catalog_item(tmp_path):
    facts = facts_for(tmp_path, monitors(estimated_value_eur=None))
    assert facts.value_eur == 578.0
    assert facts.resolved_fields[0].field == "estimated_value_eur"
    assert check(facts, CheckName.BUDGET) == CheckResult.OK


def test_an_ambiguous_catalog_match_does_not_invent_a_value(tmp_path):
    request = make_pr(item="caixa", quantity=2, estimated_value_eur=None,
                      cost_center="CC-LAB-300", category=Category.LAB_CONSUMABLES)
    facts = facts_for(tmp_path, request, email=SOFIA)
    assert facts.value_eur is None and not facts.complete
    assert facts.questions == ["Qual o valor estimado do pedido?"]


def test_an_inactive_cost_center_is_a_problem_with_a_question(tmp_path):
    facts = facts_for(tmp_path, monitors(cost_center="CC-OLD-900"))
    assert check(facts, CheckName.COST_CENTER) == CheckResult.PROBLEM
    assert check(facts, CheckName.BUDGET) == CheckResult.NOT_CHECKED
    assert "inativo" in facts.questions[0]


def test_an_unknown_cost_center_is_a_problem(tmp_path):
    facts = facts_for(tmp_path, monitors(cost_center="CC-XXX-999"))
    assert check(facts, CheckName.COST_CENTER) == CheckResult.PROBLEM


def test_an_insufficient_budget_is_a_problem_that_asks_nothing_of_the_user(tmp_path):
    request = monitors(cost_center="CC-LAB-300", estimated_value_eur=1445.0)
    facts = facts_for(tmp_path, request, email=SOFIA)
    assert check(facts, CheckName.BUDGET) == CheckResult.PROBLEM
    assert "800 €" in facts.check(CheckName.BUDGET).evidence
    assert facts.budget_short and facts.questions == []


def test_a_value_far_from_the_catalog_price_is_only_a_warning(tmp_path):
    facts = facts_for(tmp_path, monitors(estimated_value_eur=900.0))
    assert check(facts, CheckName.CATALOG) == CheckResult.WARNING
    assert facts.problems == []


def test_no_contract_and_no_supplier_for_services_are_not_problems(tmp_path):
    facts = facts_for(tmp_path, consulting())
    assert check(facts, CheckName.CATALOG) == CheckResult.WARNING
    assert check(facts, CheckName.FRAMEWORK_CONTRACT) == CheckResult.WARNING
    assert check(facts, CheckName.SUPPLIER) == CheckResult.NOT_CHECKED
    assert facts.problems == []


def test_an_unapproved_supplier_is_a_problem(tmp_path):
    request = make_pr(item="caixas de cartão", quantity=10, estimated_value_eur=350.0,
                      cost_center="CC-TI-100", category=Category.PACKAGING)
    facts = facts_for(tmp_path, request)
    assert check(facts, CheckName.SUPPLIER) == CheckResult.PROBLEM
    assert "não está aprovado" in facts.check(CheckName.SUPPLIER).evidence


def test_quotes_are_not_checked_before_registration(tmp_path):
    facts = facts_for(tmp_path, consulting())
    assert check(facts, CheckName.QUOTES) == CheckResult.NOT_CHECKED
    assert facts.quotes_required is None


def test_missing_quotes_are_a_problem(tmp_path):
    facts = facts_for(tmp_path, consulting(), setup=lambda ctx: registered(ctx, consulting()))
    assert check(facts, CheckName.QUOTES) == CheckResult.PROBLEM
    assert (facts.quotes_required, facts.quotes_found, facts.quotes_short) == (2, 0, True)


def test_two_quotes_from_different_suppliers_are_enough(tmp_path):
    facts = facts_for(tmp_path, consulting(), setup=lambda ctx: registered(ctx, consulting(), "S02", "S03"))
    assert check(facts, CheckName.QUOTES) == CheckResult.OK and not facts.quotes_short


def test_two_quotes_from_the_same_supplier_count_once(tmp_path):
    facts = facts_for(tmp_path, consulting(), setup=lambda ctx: registered(ctx, consulting(), "S02", "S02"))
    assert facts.quotes_found == 1 and facts.quotes_short


def test_a_framework_contract_dispenses_the_quotes(tmp_path):
    request = monitors(estimated_value_eur=3000.0)
    facts = facts_for(tmp_path, request, setup=lambda ctx: registered(ctx, request))
    assert facts.quotes_required == 0 and check(facts, CheckName.QUOTES) == CheckResult.OK


def test_an_urgent_request_without_a_written_justification_still_needs_two_quotes(tmp_path):
    urgent = consulting(is_urgent=True, estimated_value_eur=2000.0)
    facts = facts_for(tmp_path, urgent, setup=lambda ctx: registered(ctx, urgent, "S02"))
    assert facts.quotes_required == 2 and facts.urgency_unjustified and facts.quotes_short
    assert "falta a justificação" in facts.check(CheckName.QUOTES).evidence


def test_a_department_head_justification_lets_one_quote_be_enough(tmp_path):
    urgent = consulting(is_urgent=True, estimated_value_eur=2000.0)

    def setup(ctx):
        pr_id = registered(ctx, urgent, "S02")
        ctx.erp.record_urgency_justification("j1", "joao.ferreira@exemplo.pt", pr_id, "Paragem da linha de apoio.")
        return pr_id

    facts = facts_for(tmp_path, urgent, setup=setup)
    assert facts.quotes_required == 1 and not facts.urgency_unjustified and not facts.quotes_short
    assert "joao.ferreira@exemplo.pt" in facts.check(CheckName.QUOTES).evidence


def test_only_a_department_head_can_record_an_urgency_justification(tmp_path):
    import pytest
    from pr_triage.erp.faults import ErpRejected
    ctx = make(tmp_path)
    pr_id = registered(ctx, consulting(is_urgent=True))
    with pytest.raises(ErpRejected):
        ctx.erp.record_urgency_justification("j1", ANA, pr_id, "Preciso já.")
    with pytest.raises(ErpRejected):
        ctx.erp.record_urgency_justification("j2", "joao.ferreira@exemplo.pt", pr_id, "  ")
    assert ctx.erp.get_urgency_justification(pr_id) is None


def test_urgency_does_not_reduce_the_quotes_above_the_second_threshold(tmp_path):
    big = consulting(is_urgent=True, estimated_value_eur=8000.0)

    def setup(ctx):
        pr_id = registered(ctx, big)
        ctx.erp.record_urgency_justification("j1", "joao.ferreira@exemplo.pt", pr_id, "Urgente.")
        return pr_id

    assert facts_for(tmp_path, big, setup=setup).quotes_required == 3


def test_large_requests_need_three_quotes(tmp_path):
    large = consulting(estimated_value_eur=8000.0)
    assert facts_for(tmp_path, large, setup=lambda ctx: registered(ctx, large)).quotes_required == 3


def test_no_quotes_are_required_below_the_first_threshold(tmp_path):
    request = consulting(estimated_value_eur=900.0)
    facts = facts_for(tmp_path, request, setup=lambda ctx: registered(ctx, request))
    assert facts.quotes_required == 0 and check(facts, CheckName.QUOTES) == CheckResult.OK


def test_the_happy_path_returns_the_facts_and_the_policy_opinion(tmp_path):
    result = investigate(tmp_path, [search(), final(policy_verdict="permits", cited_clauses=[T1, T33])])
    assert result.failure is None
    report = result.report
    assert report.policy_verdict.value == "permits" and report.cited_clauses == [T1, T33]
    assert len(report.checks) == 6 and report.quotes_required is None
    names = [c.name for c in result.tool_calls]
    assert "get_budget" in names and names[-1] == "search_policy"
    assert all(c.result.ok for c in result.tool_calls)


def test_a_request_without_the_data_skips_the_model(tmp_path):
    def exploding(messages, info):
        raise AssertionError("the model must not be called")

    ctx = make(tmp_path)
    result = run_investigation(monitors(cost_center=None), TIAGO, TRIAGE, ctx, model=FunctionModel(exploding))
    assert result.failure is None and result.report.policy_verdict.value == "unclear"
    assert result.report.questions_for_user == ["Qual o centro de custo a usar neste pedido?"]


def test_resolved_fields_reach_the_report(tmp_path):
    result = investigate(
        tmp_path, [search(), final(policy_verdict="permits", cited_clauses=[T1])],
        request=monitors(cost_center=None, estimated_value_eur=None),
    )
    assert {f.field for f in result.report.resolved_fields} == {"cost_center", "estimated_value_eur"}


def test_clauses_never_retrieved_are_rejected_and_recorded(tmp_path):
    result = investigate(
        tmp_path,
        [search(),
         final(policy_verdict="permits", cited_clauses=["1.3"]),
         final(policy_verdict="permits", cited_clauses=[T1])],
    )
    assert result.failure is None and len(result.rejections) == 1
    assert "never retrieved" in result.rejections[0][0] and T1 in result.rejections[0][0]


def test_a_permit_or_a_block_must_cite_a_clause(tmp_path):
    result = investigate(tmp_path, [search(), final(policy_verdict="permits")])
    assert result.failure.code == "invalid_output" and "must cite at least one clause" in result.failure.message


def test_a_permit_cannot_come_with_open_questions(tmp_path):
    result = investigate(
        tmp_path, [search(), final(policy_verdict="permits", cited_clauses=[T1], questions_for_user=["?"])]
    )
    assert "cannot have open questions" in result.failure.message


def test_a_permit_is_refused_when_the_facts_show_a_problem(tmp_path):
    result = investigate(
        tmp_path,
        [search(),
         final(policy_verdict="permits", cited_clauses=[T1]),
         final(policy_verdict="blocks", cited_clauses=[T1])],
        request=make_pr(item="caixas de cartão", quantity=10, estimated_value_eur=350.0,
                        cost_center="CC-TI-100", category=Category.PACKAGING),
    )
    assert result.failure is None and result.report.policy_verdict.value == "blocks"
    assert "cannot be permits" in result.rejections[0][0]


def test_an_insufficient_budget_is_flagged_and_never_permitted(tmp_path):
    request = monitors(cost_center="CC-LAB-300", estimated_value_eur=1445.0)
    result = investigate(
        tmp_path,
        [search(),
         final(policy_verdict="permits", cited_clauses=[T1]),
         final(policy_verdict="blocks", cited_clauses=[T1])],
        request=request, email=SOFIA,
    )
    assert result.failure is None and result.report.policy_verdict.value == "blocks"
    assert "cannot be permits" in result.rejections[0][0]
    assert result.report.budget_short and result.report.questions_for_user == []


def test_missing_quotes_force_an_unclear_verdict(tmp_path):
    request = consulting()
    result = investigate(
        tmp_path,
        [search(), final(policy_verdict="permits", cited_clauses=[T1]), final(policy_verdict="unclear")],
        request=request, setup=lambda ctx: registered(ctx, request),
    )
    assert result.report.policy_verdict.value == "unclear"
    assert (result.report.quotes_required, result.report.quotes_found) == (2, 0)


def test_enough_quotes_allow_a_permit(tmp_path):
    request = consulting()
    result = investigate(
        tmp_path,
        [search(), final(policy_verdict="permits", cited_clauses=[T1])],
        request=request, setup=lambda ctx: registered(ctx, request, "S02", "S03"),
    )
    assert result.failure is None and result.report.policy_verdict.value == "permits"


def test_an_erp_failure_while_gathering_facts_is_a_retryable_failure(tmp_path):
    result = investigate(
        tmp_path, [search(), final(policy_verdict="unclear")], faults=Faults(unavailable={"get_budget"})
    )
    assert result.failure.code == "erp_unavailable" and result.failure.retryable and result.report is None


def test_endless_tool_calls_stop_at_the_step_limit(tmp_path):
    result = investigate(tmp_path, [search()])
    assert result.failure.code == "step_limit" and not result.failure.retryable


def test_model_outage_is_a_retryable_failure(tmp_path):
    def down(messages, info):
        raise ModelHTTPError(503, "gpt")

    result = run_investigation(monitors(), ANA, TRIAGE, make(tmp_path), model=FunctionModel(down))
    assert result.failure.code == "llm_unavailable" and result.failure.retryable


def test_invalid_arguments_from_the_model_come_back_as_data(tmp_path):
    result = investigate(
        tmp_path,
        [("tools", [("search_policy", {"wrong": 1})]), final(policy_verdict="unclear", questions_for_user=["?"])],
    )
    assert result.failure is None
    assert [c.result.error.code for c in result.tool_calls if c.name == "search_policy"] == ["invalid_arguments"]
