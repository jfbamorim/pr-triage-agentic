from datetime import datetime, timedelta, timezone

import pytest

from pr_triage.authz import Action, HumanApproval
from pr_triage.domain.schemas import Evidence
from pr_triage.erp import Faults, create_demo_erp
from pr_triage.erp.models import PrState
from pr_triage.tools import BY_NAME, TOOLS, ToolContext, call_tool

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
CLAUSES = {"02-escaloes-e-cotacoes#2.1", "03-fornecedores-e-categorias#3.3"}


class FakePolicy:
    def __init__(self, fail: bool = False):
        self.fail = fail

    def search(self, query, top_k):
        if self.fail:
            raise RuntimeError("index down")
        return [
            Evidence(
                chunk_id="03-fornecedores-e-categorias#3.3",
                source="03-fornecedores-e-categorias.md",
                document="Fornecedores",
                clause="3.3",
                title="Equipamento informático",
                effective_date=None,
                text="IT equipment is bought from the IT catalog.",
            )
        ]

    def clause_ids(self):
        return CLAUSES

    def get(self, chunk_id):
        return self.search(chunk_id, 1)[0].model_copy(update={"chunk_id": chunk_id})


def make(tmp_path, faults=None, policy=None, run_id="run-1"):
    erp = create_demo_erp(tmp_path / "erp.db", clock=lambda: NOW, faults=faults)
    return ToolContext(erp=erp, policy=policy or FakePolicy(), run_id=run_id)


def register(ctx, value=800.0, cost_center="CC-TI-100", category="it_equipment"):
    result = call_tool(
        ctx,
        "register_pr",
        dict(
            requester_email="ana.silva@exemplo.pt",
            item="Monitores",
            quantity=2,
            value_eur=value,
            cost_center=cost_center,
            category=category,
        ),
    )
    assert result.ok, result.error
    return result.data["result"]["pr_id"]


def decide(ctx, tool, pr_id, verdict="permits", clauses=None):
    return call_tool(
        ctx,
        tool,
        dict(
            pr_id=pr_id,
            policy_verdict=verdict,
            cited_clauses=clauses if clauses is not None else [],
            justification="Because.",
        ),
    )


def human(pr_id, action=Action.APPROVE_PR, value=14000.0, role="director", approver="maria.santos@exemplo.pt"):
    return HumanApproval(
        request_id=pr_id,
        role=role,
        approver=approver,
        decision=action,
        approved_value_eur=value,
        granted_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=23),
    )


def head_approves(ctx, pr_id, action=Action.APPROVE_PR, value=5000.0):
    ctx.erp.record_human_approval(
        f"head-{pr_id}-{action.value}", human(pr_id, action, value, "department_head", "joao.ferreira@exemplo.pt")
    )


def test_every_tool_has_a_unique_name_and_a_contract():
    names = [tool.name for tool in TOOLS]
    assert len(names) == len(set(names))
    assert all(tool.input_model.model_json_schema() and tool.description for tool in TOOLS)
    assert {n for n, t in BY_NAME.items() if t.writes} == {
        "register_pr", "update_pr", "approve_pr", "reject_pr", "send_email"
    }


def test_read_tool_returns_json_data(tmp_path):
    ctx = make(tmp_path)
    result = call_tool(ctx, "get_budget", {"cost_center": "CC-LAB-300"})
    assert result.ok and result.data["available_eur"] == 800.0


def test_missing_record_is_a_structured_error(tmp_path):
    result = call_tool(make(tmp_path), "get_cost_center", {"code": "CC-XXX"})
    assert not result.ok and result.error.code == "not_found" and not result.error.retryable


def test_invalid_arguments_are_rejected_before_any_call(tmp_path):
    ctx = make(tmp_path)
    bad = call_tool(ctx, "register_pr", {"item": "x", "quantity": 0, "value_eur": -1})
    assert bad.error.code == "invalid_arguments"
    assert call_tool(ctx, "nope", {}).error.code == "unknown_tool"
    assert ctx.erp.get_actions() == []


def test_erp_outage_is_retryable(tmp_path):
    ctx = make(tmp_path, Faults(fail_calls={"get_budget": [1]}))
    first = call_tool(ctx, "get_budget", {"cost_center": "CC-TI-100"})
    assert first.error.code == "erp_unavailable" and first.error.retryable
    assert call_tool(ctx, "get_budget", {"cost_center": "CC-TI-100"}).ok


def test_policy_failure_is_retryable(tmp_path):
    ctx = make(tmp_path, policy=FakePolicy(fail=True))
    result = call_tool(ctx, "search_policy", {"query": "portátil"})
    assert result.error.code == "policy_unavailable" and result.error.retryable


def test_policy_search_returns_evidence(tmp_path):
    result = call_tool(make(tmp_path), "search_policy", {"query": "portátil"})
    assert result.ok and result.data[0]["chunk_id"] == "03-fornecedores-e-categorias#3.3"


def test_register_rejects_an_inactive_cost_center(tmp_path):
    ctx = make(tmp_path)
    result = call_tool(
        ctx,
        "register_pr",
        dict(requester_email="a@b.pt", item="x", quantity=1, value_eur=10,
             cost_center="CC-OLD-900", category="other"),
    )
    assert result.error.code == "inactive_cost_center"


def test_agent_approves_below_the_first_threshold(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 800)
    result = decide(ctx, "approve_pr", pr_id)
    assert result.ok and not result.data["replayed"]
    assert ctx.erp.get_pr(pr_id).state == PrState.APPROVED


def test_agent_cannot_reject_below_the_first_threshold(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 500)
    result = decide(ctx, "reject_pr", pr_id, "blocks", ["03-fornecedores-e-categorias#3.3"])
    assert result.error.code == "not_authorized" and result.error.detail == "nothing_to_reject"
    assert ctx.erp.get_pr(pr_id).state == PrState.OPEN


def test_middle_band_needs_a_real_clause_and_a_permitting_verdict(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 3000)
    assert decide(ctx, "approve_pr", pr_id).error.detail == "no_clause"
    invented = decide(ctx, "approve_pr", pr_id, clauses=["99-inventada#9.9"])
    assert invented.error.code == "unknown_clause"
    blocked = decide(ctx, "approve_pr", pr_id, "blocks", ["02-escaloes-e-cotacoes#2.1"])
    assert blocked.error.detail == "policy_unclear"
    assert ctx.erp.get_pr(pr_id).state == PrState.OPEN
    waiting = decide(ctx, "approve_pr", pr_id, clauses=["02-escaloes-e-cotacoes#2.1"])
    assert waiting.error.detail == "human_approval_required"
    head_approves(ctx, pr_id)
    ok = decide(ctx, "approve_pr", pr_id, clauses=["02-escaloes-e-cotacoes#2.1"])
    assert ok.ok
    assert ctx.erp.get_pr(pr_id).cited_clauses == ["02-escaloes-e-cotacoes#2.1"]


def test_middle_band_rejects_when_the_clause_blocks(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 3000)
    result = decide(ctx, "reject_pr", pr_id, "blocks", ["03-fornecedores-e-categorias#3.3"])
    assert result.ok and ctx.erp.get_pr(pr_id).state == PrState.REJECTED


def test_above_the_limit_needs_a_human_approval_on_record(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 14000)
    refused = decide(ctx, "approve_pr", pr_id, clauses=["02-escaloes-e-cotacoes#2.1"])
    assert refused.error.detail == "human_approval_required"
    assert ctx.erp.get_pr(pr_id).state == PrState.OPEN
    ctx.erp.record_human_approval("a1", human(pr_id))
    assert decide(ctx, "approve_pr", pr_id).error.detail == "validation_required"
    ctx.erp.record_human_approval("a2", human(pr_id, role="purchasing", approver="pedro.alves@exemplo.pt"))
    assert decide(ctx, "approve_pr", pr_id).ok
    assert ctx.erp.get_pr(pr_id).state == PrState.APPROVED


def update_args(pr_id, **overrides):
    values = dict(pr_id=pr_id, requester_email="ana.silva@exemplo.pt", item="monitores", quantity=2,
                  value_eur=700.0, cost_center="CC-TI-100", category="it_equipment", revision=1)
    values.update(overrides)
    return values


def test_an_open_requisition_can_be_updated_but_not_by_someone_else_nor_after_a_decision(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 800)
    assert call_tool(ctx, "update_pr", update_args(pr_id)).ok
    assert ctx.erp.get_pr(pr_id).value_eur == 700.0
    other = call_tool(ctx, "update_pr", update_args(pr_id, requester_email="rui.costa@exemplo.pt", value_eur=10, revision=2))
    assert other.error.code == "not_requester" or other.error.detail == "not_requester"
    assert decide(ctx, "approve_pr", pr_id, clauses=["02-escaloes-e-cotacoes#2.1"]).ok
    late = call_tool(ctx, "update_pr", update_args(pr_id, value_eur=100.0, revision=3))
    assert not late.ok and ctx.erp.get_pr(pr_id).value_eur == 700.0


def test_an_expired_human_approval_is_refused(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 14000)
    expired = human(pr_id).model_copy(update={"expires_at": NOW - timedelta(minutes=1)})
    ctx.erp.record_human_approval("a1", expired)
    assert decide(ctx, "approve_pr", pr_id).error.detail == "approval_expired"


def test_lost_response_then_retry_applies_the_effect_once(tmp_path):
    ctx = make(tmp_path, Faults(drop_response={"approve_pr": [1]}))
    pr_id = register(ctx, 800)
    first = decide(ctx, "approve_pr", pr_id)
    assert first.error.code == "response_lost" and first.error.retryable
    assert ctx.erp.get_pr(pr_id).state == PrState.APPROVED
    again = decide(ctx, "approve_pr", pr_id)
    assert again.ok and again.data["replayed"]
    assert ctx.erp.get_budget("CC-TI-100").committed_eur == 38800.0


def test_second_decision_conflicts(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 3000)
    clauses = ["02-escaloes-e-cotacoes#2.1"]
    head_approves(ctx, pr_id)
    assert decide(ctx, "approve_pr", pr_id, clauses=clauses).ok
    result = decide(ctx, "reject_pr", pr_id, "blocks", clauses)
    assert result.error.code == "conflict"


def test_decision_email_needs_an_executed_decision(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 800)
    mail = dict(pr_id=pr_id, kind="decision", to="ana.silva@exemplo.pt", subject="Decisão", body="Aprovado.")
    assert call_tool(ctx, "send_email", mail).error.detail == "nothing_executed"
    decide(ctx, "approve_pr", pr_id)
    assert call_tool(ctx, "send_email", mail).ok
    assert call_tool(ctx, "send_email", mail).data["replayed"]
    assert len(ctx.erp.get_outbox(pr_id)) == 1


def test_decision_email_only_goes_to_the_requester(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 800)
    decide(ctx, "approve_pr", pr_id)
    result = call_tool(
        ctx, "send_email",
        dict(pr_id=pr_id, kind="decision", to="outsider@fora.pt", subject="x", body="y"),
    )
    assert result.error.code == "wrong_recipient"
    assert ctx.erp.get_outbox() == []


def test_decision_email_uses_the_clauses_recorded_in_the_erp(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 3000)
    head_approves(ctx, pr_id)
    decide(ctx, "approve_pr", pr_id, clauses=["02-escaloes-e-cotacoes#2.1"])
    call_tool(
        ctx, "send_email",
        dict(pr_id=pr_id, kind="decision", to="ana.silva@exemplo.pt", subject="x", body="y",
             cited_clauses=["03-fornecedores-e-categorias#3.3"]),
    )
    assert ctx.erp.get_outbox(pr_id)[0].cited_clauses == ["02-escaloes-e-cotacoes#2.1"]


def test_dossier_only_above_the_limit_and_only_to_an_approver(tmp_path):
    ctx = make(tmp_path)
    tiny = register(ctx, 800)
    mail = dict(pr_id=tiny, kind="dossier", to="joao.ferreira@exemplo.pt", subject="Dossier", body="...")
    assert call_tool(ctx, "send_email", mail).error.detail == "no_dossier"

    ctx3 = make(tmp_path, run_id="run-3")
    middle = register(ctx3, 3000)
    to_head = dict(pr_id=middle, kind="dossier", to="joao.ferreira@exemplo.pt", subject="Dossier", body="...",
                   cited_clauses=["02-escaloes-e-cotacoes#2.1"])
    assert call_tool(ctx3, "send_email", to_head).ok
    to_director = {**to_head, "to": "maria.santos@exemplo.pt"}
    assert call_tool(ctx3, "send_email", to_director).error.code == "wrong_recipient"

    ctx2 = make(tmp_path, run_id="run-2")
    big = register(ctx2, 14000)
    good = dict(pr_id=big, kind="dossier", to="maria.santos@exemplo.pt", subject="Dossier", body="...")
    assert call_tool(ctx2, "send_email", good).ok
    wrong = {**good, "to": "ana.silva@exemplo.pt"}
    assert call_tool(ctx2, "send_email", wrong).error.code == "wrong_recipient"
