import asyncio

import pytest
from fastmcp.client import Client

from pr_triage.erp import create_demo_erp
from pr_triage.erp.models import PrState
from pr_triage.supervisor.orchestrator import Orchestrator, Specialists
from pr_triage.supervisor.state import RunStatus
from pr_triage.supervisor.store import RunStore
from pr_triage.toolserver.client import McpToolPort
from pr_triage.toolserver.server import build_server
from pr_triage.tools import TOOLS, ToolContext, call_tool, run_tool
from test_orchestrator import ANA, NOW, Decider, FakeInvestigator, FakeTriage, FakeWriter
from test_tools import FakePolicy, decide, make, register


@pytest.fixture
def served(tmp_path):
    local = make(tmp_path)
    port = McpToolPort(build_server(local.erp, local.policy)).start()
    yield local, ToolContext(erp=local.erp, policy=local.policy, run_id=local.run_id, port=port), port
    port.close()


def test_the_server_exposes_every_tool_with_its_contract(served):
    _, _, port = served
    assert sorted(port.list_tools()) == sorted(tool.name for tool in TOOLS)


def test_a_read_through_mcp_equals_the_direct_call(served):
    local, remote, _ = served
    args = {"email": "ana.silva@exemplo.pt"}
    assert call_tool(remote, "get_person", args) == run_tool(local, "get_person", args)
    assert call_tool(remote, "get_person", args).data["default_cost_center"] == "CC-TI-100"


def test_invalid_arguments_and_unknown_tools_come_back_as_envelopes(served):
    _, remote, _ = served
    bad = call_tool(remote, "get_person", {})
    assert not bad.ok and bad.error.code == "invalid_arguments"
    assert call_tool(remote, "nope", {}).error.code == "unknown_tool"


def test_writes_through_mcp_are_idempotent_per_run(served):
    local, remote, _ = served
    first = register(remote)
    again = register(remote)
    assert first == again == 1
    assert [a.action for a in local.erp.get_actions()] == ["register_pr"]


def test_authorization_is_enforced_by_the_server_whoever_the_client_is(served):
    local, remote, _ = served
    pr_id = register(remote, value=14000.0)
    refused = decide(remote, "approve_pr", pr_id)
    assert not refused.ok and refused.error.code == "not_authorized"
    assert local.erp.get_pr(pr_id).state == PrState.OPEN


def test_a_write_without_run_id_is_refused_by_the_server(served):
    local, _, _ = served

    async def raw():
        async with Client(build_server(local.erp, local.policy)) as client:
            return await client.call_tool("register_pr", {"requester_email": "x"}, raise_on_error=False)

    response = asyncio.run(raw())
    assert response.structured_content["ok"] is False


def test_a_closed_server_is_reported_as_unavailable_and_retryable(tmp_path):
    local = make(tmp_path)
    port = McpToolPort(build_server(local.erp, local.policy)).start()
    ctx = ToolContext(erp=local.erp, policy=local.policy, run_id="run-1", port=port)
    port.close()
    result = call_tool(ctx, "get_person", {"email": "ana.silva@exemplo.pt"})
    assert not result.ok and result.error.code == "mcp_unavailable" and result.error.retryable


def test_a_full_run_works_with_every_tool_call_going_through_mcp(tmp_path):
    erp = create_demo_erp(tmp_path / "erp.db", clock=lambda: NOW)
    with McpToolPort(build_server(erp, FakePolicy())) as port:
        orchestrator = Orchestrator(
            erp,
            FakePolicy(),
            RunStore(tmp_path / "runs.db"),
            Specialists(FakeTriage(), FakeInvestigator(erp), FakeWriter(), Decider()),
            port,
        )
        state = orchestrator.start("monitores", ANA, run_id="run-1")
    assert state.status == RunStatus.DONE
    assert erp.get_pr(1).state == PrState.APPROVED
    assert [a.action for a in erp.get_actions(1)] == ["approve_pr", "send_email"]
    assert len(erp.get_outbox(1)) == 1


def test_the_real_server_over_stdio(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-used")
    erp = create_demo_erp(tmp_path / "erp.db", clock=lambda: NOW)
    with McpToolPort.stdio(str(tmp_path / "erp.db"), timeout=60) as port:
        ctx = ToolContext(erp=erp, policy=FakePolicy(), run_id="run-1", port=port)
        assert len(port.list_tools()) == len(TOOLS)
        person = call_tool(ctx, "get_person", {"email": "ana.silva@exemplo.pt"})
        assert person.ok and person.data["default_cost_center"] == "CC-TI-100"
        pr_id = register(ctx, value=14000.0)
        refused = decide(ctx, "approve_pr", pr_id)
        assert refused.error.code == "not_authorized"
    assert erp.get_pr(pr_id).state == PrState.OPEN
