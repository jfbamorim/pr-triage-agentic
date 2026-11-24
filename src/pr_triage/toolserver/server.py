from typing import Any

from fastmcp.server import FastMCP
from fastmcp.tools import Tool as McpTool
from fastmcp.tools.base import ToolResult as McpResult

from pr_triage.erp import ErpStore
from pr_triage.tools import TOOLS, PolicyPort, ToolContext, run_tool
from pr_triage.tools.registry import Tool

RUN_ID = {"type": "string", "description": "Identifier of the run; part of the idempotency key."}
READ_RUN_ID = "mcp-read"


def _schema(tool: Tool) -> dict[str, Any]:
    schema = tool.input_model.model_json_schema()
    if tool.writes:
        schema = {
            **schema,
            "properties": {"run_id": RUN_ID, **schema.get("properties", {})},
            "required": ["run_id", *schema.get("required", [])],
        }
    return schema


class PurchasingTool(McpTool):
    erp: Any = None
    policy: Any = None
    writes: bool = False

    async def run(self, arguments: dict[str, Any]) -> McpResult:
        arguments = dict(arguments)
        run_id = arguments.pop("run_id", None) if self.writes else READ_RUN_ID
        if self.writes and not run_id:
            return _result({"ok": False, "error": {"code": "invalid_arguments", "message": "run_id is required."}})
        ctx = ToolContext(erp=self.erp, policy=self.policy, run_id=run_id)
        return _result(run_tool(ctx, self.name, arguments).model_dump(mode="json"))


def _result(payload: dict) -> McpResult:
    return McpResult(structured_content=payload)


def build_server(erp: ErpStore, policy: PolicyPort) -> FastMCP:
    server = FastMCP("purchasing-tools")
    for tool in TOOLS:
        server.add_tool(
            PurchasingTool(
                name=tool.name,
                description=tool.description,
                parameters=_schema(tool),
                erp=erp,
                policy=policy,
                writes=tool.writes,
            )
        )
    return server
