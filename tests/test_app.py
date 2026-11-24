import sys
import types
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pr_triage.memory import MemoryStore
from pr_triage.supervisor.orchestrator import Orchestrator
from test_orchestrator import NOW, Setup

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
APP = str(APP_DIR / "streamlit_app.py")


class FakeRuntime:
    def __init__(self, setup, tmp_path):
        self.setup = setup
        self.erp = setup.erp
        self.store = setup.store
        self.memory = MemoryStore(tmp_path / "memory.db", lambda: NOW)

    def orchestrator(self, observer=None, on_step=None):
        s = self.setup
        orchestrator = Orchestrator(s.erp, s.orchestrator.policy, s.store, s.orchestrator.specialists)
        orchestrator.observer = observer
        orchestrator.on_step = on_step
        return orchestrator


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    setup = Setup(tmp_path)
    runtime = FakeRuntime(setup, tmp_path)
    module = types.ModuleType("agentic_runtime")
    module.get_runtime = lambda: runtime
    monkeypatch.setitem(sys.modules, "agentic_runtime", module)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    return at, runtime


def submit(at, text):
    at.chat_input[0].set_value(text).run()


def page(at):
    return " ".join(m.value for m in at.markdown)


def test_the_empty_page_invites_a_request(app):
    at, _ = app
    assert not at.exception
    assert "Escreve um pedido" in page(at)


def test_a_small_request_runs_to_completion_and_shows_the_effects(app):
    at, runtime = app
    submit(at, "monitores")
    assert not at.exception
    assert "Concluído" in page(at) and "aprovado" in page(at)
    assert runtime.erp.get_pr(1).state.value == "approved"
    assert any("Email para ana.silva@exemplo.pt" in e.label for e in at.expander)


def test_a_large_request_needs_the_direction_and_then_purchasing(app):
    at, runtime = app
    submit(at, "grande")
    assert "À espera de aprovação humana" in page(at)
    at.radio(key="role").set_value("Aprovador").run()
    next(b for b in at.button if b.label == "Aprovar").click().run()
    assert not at.exception
    assert runtime.erp.get_pr(1).state.value == "open"
    at.radio(key="approver").set_value("Compras · Pedro Alves").run()
    next(b for b in at.button if b.label == "Validar").click().run()
    assert not at.exception
    assert runtime.erp.get_pr(1).state.value == "approved"
    assert "Concluído" in page(at)


def test_an_urgent_request_waits_for_the_department_head_justification(app):
    at, runtime = app
    submit(at, "urgente")
    assert "justificar a urgência" in page(at)
    runtime.erp.add_quote("q1", "ana", 1, "S02", 1900.0)
    at.radio(key="role").set_value("Aprovador").run()
    at.radio(key="approver").set_value("Chefia · João Ferreira").run()
    at.text_area[0].set_value("Prazo legal na sexta-feira.").run()
    next(b for b in at.button if b.label == "Justificar urgência").click().run()
    assert not at.exception
    assert runtime.erp.get_pr(1).state.value == "open"
    next(b for b in at.button if b.label == "Aprovar").click().run()
    assert not at.exception
    assert runtime.erp.get_pr(1).state.value == "approved"


def test_a_request_without_budget_is_referred_to_management_control(app):
    at, runtime = app
    submit(at, "sem orcamento")
    assert not at.exception
    assert "controlo de gestão" in page(at)
    assert runtime.erp.get_pr(1).state.value == "open"


def test_recent_runs_can_be_reopened(app):
    at, _ = app
    submit(at, "monitores")
    at.session_state["run_id"] = None
    at.run()
    next(b for b in at.button if b.label.endswith("· concluído")).click().run()
    assert "Concluído" in page(at)


def test_agent_cards_show_who_did_what():
    from agentic_views import agent_cards
    html = agent_cards(None)
    assert html.count('class="agent idle"') == 5
