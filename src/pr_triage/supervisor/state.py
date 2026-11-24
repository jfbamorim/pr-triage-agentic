from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel

from pr_triage.agents.investigator import InvestigationReport
from pr_triage.agents.triage import TriageReport
from pr_triage.domain.schemas import PurchaseRequest


class RunStatus(str, Enum):
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_HUMAN = "waiting_human"
    DONE = "done"
    REFERRED = "referred"
    NEEDS_MANUAL = "needs_manual"
    REFUSED = "refused"
    FAILED = "failed"


TERMINAL = {RunStatus.DONE, RunStatus.REFERRED, RunStatus.NEEDS_MANUAL, RunStatus.REFUSED, RunStatus.FAILED}


class Move(str, Enum):
    TRIAGE = "triage"
    INVESTIGATE = "investigate"
    ASK_USER = "ask_user"
    REFUSE = "refuse"
    REGISTER = "register"
    UPDATE_PR = "update_pr"
    APPROVE = "approve"
    REJECT = "reject"
    SEND_DOSSIER = "send_dossier"
    REFER_CONTROLLING = "refer_controlling"
    VERIFY = "verify"
    SEND_DECISION_EMAIL = "send_decision_email"
    ESCALATE = "escalate"
    RECONCILE = "reconcile"


class StepRecord(BaseModel):
    n: int
    move: Move
    forced: bool
    reason: str
    outcome: str
    at: datetime


class Blocked(BaseModel):
    stage: Move
    problems: list[str]
    retryable: bool


class RunState(BaseModel):
    run_id: str
    requester_email: str
    messages: list[str] = []
    request: PurchaseRequest | None = None
    triage: TriageReport | None = None
    investigation: InvestigationReport | None = None
    investigated_request: PurchaseRequest | None = None
    investigated_pr_id: int | None = None
    registered_request: PurchaseRequest | None = None
    update_revision: int = 0
    update_in_flight: PurchaseRequest | None = None
    pr_id: int | None = None
    decision: Literal["approved", "rejected"] | None = None
    decision_verified: bool = False
    dossier_sent: bool = False
    decision_email_sent: bool = False
    referred: bool = False
    blocked: Blocked | None = None
    attempts: dict[str, int] = {}
    status: RunStatus = RunStatus.RUNNING
    question: str | None = None
    outcome_message: str | None = None
    steps: list[StepRecord] = []
    created_at: datetime
    updated_at: datetime


def apply_resolved_fields(request: PurchaseRequest, report: InvestigationReport) -> PurchaseRequest:
    updates: dict = {}
    for item in report.resolved_fields:
        if item.field == "cost_center" and request.cost_center is None:
            updates["cost_center"] = str(item.value)
        if item.field == "estimated_value_eur" and request.estimated_value_eur is None:
            updates["estimated_value_eur"] = float(item.value)
    return request.model_copy(update=updates)
