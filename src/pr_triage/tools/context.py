from dataclasses import dataclass
from typing import Protocol

from pr_triage.domain.schemas import Evidence
from pr_triage.erp import ErpStore
from pr_triage.memory import MemoryView
from pr_triage.tools.envelope import ToolResult


class PolicyPort(Protocol):
    def search(self, query: str, top_k: int) -> list[Evidence]: ...

    def clause_ids(self) -> set[str]: ...

    def get(self, chunk_id: str) -> Evidence: ...


class ToolPort(Protocol):
    def call(self, ctx: "ToolContext", name: str, arguments: dict) -> "ToolResult": ...


@dataclass
class ToolContext:
    erp: ErpStore
    policy: PolicyPort
    run_id: str
    actor: str = "agent"
    port: ToolPort | None = None
    memory: MemoryView | None = None
