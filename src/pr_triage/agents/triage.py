from enum import Enum

import openai
from openai import OpenAI
from pydantic import BaseModel

from pr_triage.agents.common import AgentFailure as TriageFailure
from pr_triage.domain.band import Band, determine_band
from pr_triage.domain.citations import validate_citations
from pr_triage.domain.schemas import Evidence, LLMCall, PurchaseRequest, ResultSummary
from pr_triage.domain.status import EscalationReason, Reason, Status
from pr_triage.domain.validation import validate_request
from pr_triage.workflow import ScreeningUnavailable, StepEvent, WorkflowResult, iter_workflow

MAX_INPUT_CHARS = 4000

RETRYABLE_API_ERRORS = (
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)


class TriageOutcome(str, Enum):
    READY = "ready"
    NEEDS_INFO = "needs_info"
    INVALID = "invalid"
    SUSPICIOUS = "suspicious"
    TECHNICAL_FAILURE = "technical_failure"


class TriageReport(BaseModel):
    outcome: TriageOutcome
    request: PurchaseRequest | None = None
    missing: list[str] = []
    invalid: list[str] = []
    band: Band | None = None
    escalation: EscalationReason | None = None
    evidence: list[Evidence] = []
    summary: ResultSummary | None = None
    follow_up: str | None = None
    failure: TriageFailure | None = None
    steps: list[StepEvent] = []
    calls: list[LLMCall] = []


def _failed(
    code: str, stage: str, message: str, retryable: bool, steps=None, calls=None
) -> TriageReport:
    return TriageReport(
        outcome=TriageOutcome.TECHNICAL_FAILURE,
        failure=TriageFailure(code=code, stage=stage, message=message, retryable=retryable),
        steps=steps or [],
        calls=calls or [],
    )


def _from_result(result: WorkflowResult, steps: list[StepEvent]) -> TriageReport:
    if result.status == Status.VALID:
        outcome = TriageOutcome.READY
    elif result.status == Status.NEEDS_INFO:
        outcome = TriageOutcome.NEEDS_INFO
    elif result.reason == Reason.SUSPICIOUS_ACTIVITY:
        outcome = TriageOutcome.SUSPICIOUS
    else:
        outcome = TriageOutcome.INVALID
    return TriageReport(
        outcome=outcome,
        request=result.request,
        missing=result.missing,
        invalid=result.invalid,
        band=result.band,
        escalation=result.escalation,
        evidence=result.evidence,
        summary=result.summary,
        follow_up=result.follow_up,
        steps=steps,
        calls=result.calls,
    )


def run_triage(
    text: str, client: OpenAI, previous: PurchaseRequest | None = None
) -> TriageReport:
    if len(text) > MAX_INPUT_CHARS:
        return _failed(
            "input_too_long", "input", f"The text has more than {MAX_INPUT_CHARS} characters.", False
        )
    steps: list[StepEvent] = []
    result: WorkflowResult | None = None
    try:
        for event in iter_workflow(text, client, previous, strict_screening=True):
            if isinstance(event, WorkflowResult):
                result = event
            else:
                steps.append(event)
    except ScreeningUnavailable as error:
        return _failed("screening_unavailable", "screening", str(error), True, steps)
    except RETRYABLE_API_ERRORS as error:
        return _failed("llm_unavailable", _stage(steps), str(error), True, steps)
    except Exception as error:
        return _failed("unexpected_error", _stage(steps), f"{type(error).__name__}: {error}", False, steps)
    if result is None:
        return _failed("no_result", _stage(steps), "The workflow ended without a result.", False, steps)
    return _from_result(result, steps)


def _stage(steps: list[StepEvent]) -> str:
    return steps[-1].step.value if steps else "start"


def check_report(report: TriageReport) -> list[str]:
    problems: list[str] = []
    outcome = report.outcome
    if outcome == TriageOutcome.TECHNICAL_FAILURE:
        if report.failure is None:
            problems.append("a technical failure must describe the failure")
        return problems
    if report.failure is not None:
        problems.append("only a technical failure may carry a failure")
    if outcome == TriageOutcome.SUSPICIOUS:
        if report.request is not None:
            problems.append("a suspicious text must not produce a request")
        return problems
    if report.request is None:
        problems.append("the report has no request")
        return problems
    missing, invalid = validate_request(report.request)
    if outcome == TriageOutcome.INVALID:
        if not report.invalid or sorted(report.invalid) != sorted(invalid):
            problems.append("the invalid fields do not match the request")
    elif outcome == TriageOutcome.NEEDS_INFO:
        if not report.missing or sorted(report.missing) != sorted(missing) or invalid:
            problems.append("the missing fields do not match the request")
        if not report.follow_up:
            problems.append("a request that needs information must carry the question")
    else:
        problems.extend(_check_ready(report, missing, invalid))
    return problems


def _check_ready(report: TriageReport, missing: list[str], invalid: list[str]) -> list[str]:
    problems: list[str] = []
    if missing or invalid:
        problems.append("a ready request must be complete and valid")
        return problems
    value = report.request.estimated_value_eur
    if report.band != determine_band(value):
        problems.append("the band does not match the value")
    if (report.escalation == EscalationReason.OVER_THRESHOLD) != (report.band == Band.HUMAN_ACTION):
        problems.append("the over-threshold escalation does not match the band")
    if report.escalation != EscalationReason.RETRIEVAL_ERROR and not report.evidence:
        problems.append("a ready request needs policy evidence")
    if report.summary is not None and not validate_citations(
        report.summary.cited_chunk_ids, report.evidence
    ):
        problems.append("the summary cites clauses that are not in the evidence")
    return problems
