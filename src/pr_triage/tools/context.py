from dataclasses import dataclass
from typing import Protocol

from pr_triage.domain.schemas import Evidence
from pr_triage.erp import ErpStore


class PolicyPort(Protocol):
    def search(self, query: str, top_k: int) -> list[Evidence]: ...

    def clause_ids(self) -> set[str]: ...


@dataclass
class ToolContext:
    erp: ErpStore
    policy: PolicyPort
    run_id: str
    actor: str = "agent"
