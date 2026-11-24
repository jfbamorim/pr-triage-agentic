from openai import OpenAI

from pr_triage.agents.communicator import write_email
from pr_triage.agents.investigator import run_investigation
from pr_triage.agents.triage import run_triage
from pr_triage.erp import ErpStore
from pr_triage.supervisor.agent import make_decider
from pr_triage.supervisor.orchestrator import Orchestrator, Specialists
from pr_triage.supervisor.store import RunStore
from pr_triage.tools import PolicyPort


def live_specialists(client: OpenAI, model=None) -> Specialists:
    return Specialists(
        triage=lambda text, previous: run_triage(text, client, previous),
        investigate=lambda request, email, ids, ctx, pr_id: run_investigation(
            request, email, ids, ctx, pr_id=pr_id, model=model
        ),
        write=lambda brief: write_email(brief, model=model),
        decide=make_decider(model),
    )


def live_orchestrator(erp: ErpStore, policy: PolicyPort, store: RunStore, client: OpenAI, model=None) -> Orchestrator:
    return Orchestrator(erp, policy, store, live_specialists(client, model))
