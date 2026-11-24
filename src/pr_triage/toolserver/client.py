import asyncio
import os
import sys
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from fastmcp.client import Client
from fastmcp.client.transports import StdioTransport

from pr_triage.tools import ToolContext, ToolResult
from pr_triage.tools.envelope import failure
from pr_triage.tools.registry import BY_NAME

TIMEOUT_SECONDS = 60


class McpToolPort:
    def __init__(self, target: Any, timeout: float = TIMEOUT_SECONDS):
        self._target = target
        self._timeout = timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._client: Client | None = None

    @classmethod
    def stdio(cls, erp_path: str, timeout: float = TIMEOUT_SECONDS, profile: str = "agent") -> "McpToolPort":
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "pr_triage.toolserver", "--erp", erp_path, "--profile", profile],
            env=dict(os.environ),
        )
        return cls(transport, timeout)

    def _run(self, coroutine, timeout: float):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout)

    def start(self) -> "McpToolPort":
        self._thread.start()
        self._client = Client(self._target)
        self._run(self._client.__aenter__(), self._timeout)
        return self

    def close(self) -> None:
        try:
            if self._client is not None:
                self._run(self._client.__aexit__(None, None, None), 10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def __enter__(self) -> "McpToolPort":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    def list_tools(self) -> list[str]:
        return [tool.name for tool in self._run(self._client.list_tools(), self._timeout)]

    def call(self, ctx: ToolContext, name: str, arguments: dict) -> ToolResult:
        tool = BY_NAME.get(name)
        if tool is None:
            return failure("unknown_tool", f"There is no tool named {name}.")
        payload = {**arguments, "run_id": ctx.run_id} if tool.writes else dict(arguments)
        if not self._loop.is_running():
            return failure("mcp_unavailable", "The tool server connection is closed.", retryable=True)
        try:
            response = self._run(
                self._client.call_tool(name, payload, raise_on_error=False), self._timeout
            )
            return ToolResult.model_validate(response.structured_content)
        except (FutureTimeout, TimeoutError):
            return failure("mcp_unavailable", f"The tool server did not answer {name} in time.", retryable=True)
        except Exception as error:
            return failure("mcp_unavailable", f"The tool server failed on {name}: {error}", retryable=True)
