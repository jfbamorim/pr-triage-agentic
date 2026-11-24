import os
from dataclasses import dataclass
from pathlib import Path

import streamlit as st
from openai import OpenAI

from pr_triage.erp import ErpStore, create_demo_erp
from pr_triage.memory import MemoryStore
from pr_triage.supervisor.live import live_orchestrator
from pr_triage.supervisor.orchestrator import Orchestrator
from pr_triage.supervisor.store import RunStore
from pr_triage.toolserver.client import McpToolPort
from pr_triage.tools.policy import PolicyIndex

ERP_PATH = "state/erp.db"
RUNS_PATH = "state/runs.db"
MEMORY_PATH = "state/memory.db"


@dataclass
class Runtime:
    erp: ErpStore
    store: RunStore
    memory: MemoryStore
    policy: PolicyIndex
    port: McpToolPort
    client: OpenAI

    def orchestrator(self, observer=None, on_step=None) -> Orchestrator:
        orchestrator = live_orchestrator(
            self.erp, self.policy, self.store, self.client, port=self.port, memory=self.memory
        )
        orchestrator.observer = observer
        orchestrator.on_step = on_step
        return orchestrator


@st.cache_resource
def get_runtime() -> Runtime:
    if not Path(ERP_PATH).exists():
        create_demo_erp(ERP_PATH)
    erp = ErpStore(ERP_PATH)
    client = OpenAI()
    return Runtime(
        erp=erp,
        store=RunStore(RUNS_PATH),
        memory=MemoryStore(MEMORY_PATH, erp.clock),
        policy=PolicyIndex(client),
        port=McpToolPort.stdio(ERP_PATH).start(),
        client=client,
    )
