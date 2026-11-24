from datetime import datetime, timedelta, timezone

import pytest

from pr_triage.authz import Action, HumanApproval
from pr_triage.domain.category import Category
from pr_triage.erp import (
    ErpConflict,
    ErpRejected,
    ErpResponseLost,
    ErpUnavailable,
    Faults,
    create_demo_erp,
)
from pr_triage.erp.models import PrState

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


def make(tmp_path, faults=None):
    return create_demo_erp(tmp_path / "erp.db", clock=lambda: NOW, faults=faults)


def register(erp, key="k-reg", value=800.0, cost_center="CC-TI-100", category=Category.IT_EQUIPMENT):
    result = erp.register_pr(
        key, "agent", "ana.silva@exemplo.pt", "Monitores", 2, value, cost_center, category
    )
    return result.result["pr_id"]


def test_master_data_is_seeded(tmp_path):
    erp = make(tmp_path)
    assert erp.get_person("tiago.lopes@exemplo.pt").default_cost_center is None
    assert erp.get_person("ana.silva@exemplo.pt").default_cost_center == "CC-TI-100"
    assert erp.get_cost_center("CC-OLD-900").active is False
    assert erp.get_cost_center("CC-XXX") is None
    assert [p.name for p in erp.find_people_by_role("director")] == ["Maria Santos"]


def test_budget_available_is_total_minus_committed(tmp_path):
    erp = make(tmp_path)
    assert erp.get_budget("CC-LAB-300").available_eur == 800.0
    assert erp.get_budget("CC-TI-100").available_eur == 112000.0


def test_contract_validity_depends_on_the_date(tmp_path):
    erp = make(tmp_path)
    it = erp.find_framework_contracts(Category.IT_EQUIPMENT)
    assert [c.id for c in it] == ["FC-001"] and it[0].in_force
    cleaning = erp.find_framework_contracts(Category.CLEANING)
    assert cleaning[0].in_force is False
    marketing = erp.find_framework_contracts(Category.MARKETING)[0]
    assert marketing.in_force and marketing.remaining_eur == 500.0
    assert erp.find_framework_contracts(Category.SERVICES) == []


def test_catalog_search(tmp_path):
    erp = make(tmp_path)
    laptops = erp.search_catalog("portátil")
    assert {item.sku for item in laptops} == {"LT-14", "LT-16"}
    chairs = erp.search_catalog(category=Category.FURNITURE)
    assert {item.sku: item.available for item in chairs}["CHR-02"] is False
    assert erp.get_supplier("S06").approved is False


def test_register_pr_validates_the_cost_center(tmp_path):
    erp = make(tmp_path)
    with pytest.raises(ErpRejected) as unknown:
        register(erp, "k1", cost_center="CC-XXX")
    assert unknown.value.code == "unknown_cost_center"
    with pytest.raises(ErpRejected) as inactive:
        register(erp, "k2", cost_center="CC-OLD-900")
    assert inactive.value.code == "inactive_cost_center"
    assert erp.get_actions() == []


def test_approve_commits_the_budget_and_logs_the_action(tmp_path):
    erp = make(tmp_path)
    pr_id = register(erp)
    result = erp.approve_pr("k-app", "agent", pr_id, ["02-escaloes#2.1"], "Within policy.")
    assert not result.replayed
    pr = erp.get_pr(pr_id)
    assert pr.state == PrState.APPROVED and pr.cited_clauses == ["02-escaloes#2.1"]
    assert erp.get_budget("CC-TI-100").committed_eur == 38800.0
    assert [a.action for a in erp.get_actions(pr_id)] == ["approve_pr"]


def test_same_key_replays_without_repeating_the_effect(tmp_path):
    erp = make(tmp_path)
    pr_id = register(erp)
    first = erp.approve_pr("k-app", "agent", pr_id, [], "ok")
    second = erp.approve_pr("k-app", "agent", pr_id, [], "ok")
    assert second.replayed and second.action_id == first.action_id
    assert erp.get_budget("CC-TI-100").committed_eur == 38800.0
    assert len(erp.get_actions(pr_id)) == 1


def test_second_decision_with_another_key_conflicts(tmp_path):
    erp = make(tmp_path)
    pr_id = register(erp)
    erp.approve_pr("k-1", "agent", pr_id, [], "ok")
    with pytest.raises(ErpConflict):
        erp.approve_pr("k-2", "agent", pr_id, [], "again")
    with pytest.raises(ErpConflict):
        erp.reject_pr("k-3", "agent", pr_id, [], "changed my mind")
    assert erp.get_budget("CC-TI-100").committed_eur == 38800.0


def test_key_reused_for_another_action_conflicts(tmp_path):
    erp = make(tmp_path)
    pr_id = register(erp)
    erp.approve_pr("k-1", "agent", pr_id, [], "ok")
    with pytest.raises(ErpConflict):
        erp.reject_pr("k-1", "agent", pr_id, [], "no")


def test_approval_above_the_available_budget_is_rejected(tmp_path):
    erp = make(tmp_path)
    pr_id = register(erp, value=1500.0, cost_center="CC-LAB-300", category=Category.LAB_CONSUMABLES)
    with pytest.raises(ErpRejected) as error:
        erp.approve_pr("k-1", "agent", pr_id, [], "ok")
    assert error.value.code == "insufficient_budget"
    assert erp.get_pr(pr_id).state == PrState.OPEN
    assert erp.get_actions(pr_id) == []


def test_reject_does_not_touch_the_budget(tmp_path):
    erp = make(tmp_path)
    pr_id = register(erp)
    erp.reject_pr("k-1", "agent", pr_id, ["03-fornecedores#3.3"], "Catalog only.")
    assert erp.get_pr(pr_id).state == PrState.REJECTED
    assert erp.get_budget("CC-TI-100").committed_eur == 38000.0


def test_lost_response_still_applies_and_a_retry_replays(tmp_path):
    erp = make(tmp_path, Faults(drop_response={"approve_pr": [1]}))
    pr_id = register(erp)
    with pytest.raises(ErpResponseLost):
        erp.approve_pr("k-1", "agent", pr_id, [], "ok")
    assert erp.get_pr(pr_id).state == PrState.APPROVED
    retry = erp.approve_pr("k-1", "agent", pr_id, [], "ok")
    assert retry.replayed
    assert erp.get_budget("CC-TI-100").committed_eur == 38800.0


def test_service_failures_are_injected_per_tool_and_per_call(tmp_path):
    erp = make(tmp_path, Faults(fail_calls={"get_budget": [1]}, unavailable={"get_outbox"}))
    with pytest.raises(ErpUnavailable):
        erp.get_budget("CC-TI-100")
    assert erp.get_budget("CC-TI-100").total_eur == 150000.0
    with pytest.raises(ErpUnavailable):
        erp.get_outbox()


def test_email_is_logged_and_idempotent(tmp_path):
    erp = make(tmp_path)
    pr_id = register(erp)
    args = ("k-mail", "agent", pr_id, "ana.silva@exemplo.pt", "Decisão", "Aprovada.", "decision", ["02-escaloes#2.1"])
    erp.send_email(*args)
    again = erp.send_email(*args)
    assert again.replayed
    outbox = erp.get_outbox(pr_id)
    assert len(outbox) == 1 and outbox[0].cited_clauses == ["02-escaloes#2.1"]


def test_quotes_and_human_approval_round_trip(tmp_path):
    erp = make(tmp_path)
    pr_id = register(erp, value=14000.0)
    erp.add_quote("q1", "ana.silva@exemplo.pt", pr_id, "S01", 13800.0)
    erp.add_quote("q2", "ana.silva@exemplo.pt", pr_id, "S02", 14500.0)
    assert [q.supplier_id for q in erp.get_quotes(pr_id)] == ["S01", "S02"]
    assert erp.get_human_approval(pr_id) is None
    approval = HumanApproval(
        request_id=pr_id,
        approver="maria.santos@exemplo.pt",
        decision=Action.APPROVE_PR,
        approved_value_eur=14000.0,
        granted_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    erp.record_human_approval("a1", approval)
    assert erp.get_human_approval(pr_id) == approval


def test_catalog_search_tolerates_plurals_and_accents(tmp_path):
    erp = make(tmp_path)
    assert [i.sku for i in erp.search_catalog("monitores")] == ["MON-27"]
    assert {i.sku for i in erp.search_catalog("portáteis")} == {"LT-14", "LT-16"}
    assert {i.sku for i in erp.search_catalog("PORTATIL")} == {"LT-14", "LT-16"}
    assert {i.sku for i in erp.search_catalog("cadeiras")} == {"CHR-01", "CHR-02"}
    assert [i.sku for i in erp.search_catalog("monitor", Category.IT_EQUIPMENT)] == ["MON-27"]
    assert erp.search_catalog("monitores", Category.FURNITURE) == []
    assert erp.search_catalog("submarino") == []
