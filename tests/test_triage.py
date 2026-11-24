import httpx
import openai
import pytest

from pr_triage.agents.triage import MAX_INPUT_CHARS, TriageOutcome, check_report, run_triage
from pr_triage.domain.band import Band
from pr_triage.domain.schemas import ResultSummary
from pr_triage.domain.status import EscalationReason
from pr_triage import workflow
from test_workflow import CLAUSE, SUSPICIOUS, install, make_pr


def triage(previous=None, text="texto do pedido"):
    return run_triage(text, None, previous)


def test_complete_request_is_ready_and_passes_the_check(monkeypatch):
    install(monkeypatch)
    report = triage()
    assert report.outcome == TriageOutcome.READY
    assert report.band == Band.NO_BLOCKERS and report.escalation is None
    assert [s.step.value for s in report.steps][0] == "screening"
    assert check_report(report) == []


def test_request_above_the_limit_is_ready_with_the_escalation(monkeypatch):
    install(monkeypatch, pr=make_pr(estimated_value_eur=14000.0))
    report = triage()
    assert report.outcome == TriageOutcome.READY
    assert report.band == Band.HUMAN_ACTION
    assert report.escalation == EscalationReason.OVER_THRESHOLD
    assert check_report(report) == []


def test_missing_cost_center_needs_information(monkeypatch):
    install(monkeypatch, pr=make_pr(cost_center=None))
    report = triage()
    assert report.outcome == TriageOutcome.NEEDS_INFO
    assert report.missing == ["cost_center"] and report.follow_up
    assert check_report(report) == []


def test_invalid_quantity_is_invalid(monkeypatch):
    install(monkeypatch, pr=make_pr(quantity=-3))
    report = triage()
    assert report.outcome == TriageOutcome.INVALID and report.invalid == ["quantity"]
    assert check_report(report) == []


def test_suspicious_text_is_reported_as_suspicious(monkeypatch):
    install(monkeypatch, screening=SUSPICIOUS)
    report = triage()
    assert report.outcome == TriageOutcome.SUSPICIOUS and report.request is None
    assert check_report(report) == []


def test_screening_without_an_answer_is_a_technical_failure(monkeypatch):
    install(monkeypatch, screening=None)
    report = triage()
    assert report.outcome == TriageOutcome.TECHNICAL_FAILURE
    assert report.failure.code == "screening_unavailable" and report.failure.retryable
    assert report.failure.stage == "screening"
    assert check_report(report) == []


def test_the_course_1_workflow_still_treats_no_answer_as_suspicious(monkeypatch):
    install(monkeypatch, screening=None)
    result = workflow.run_workflow("texto", None)
    assert result.reason.value == "suspicious_activity"


def test_api_outage_is_a_retryable_failure(monkeypatch):
    install(monkeypatch)
    error = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))

    def boom(text, client, calls=None):
        raise error

    monkeypatch.setattr(workflow, "screen_input", boom)
    report = triage()
    assert report.failure.code == "llm_unavailable" and report.failure.retryable


def test_unexpected_error_is_not_retryable(monkeypatch):
    install(monkeypatch)

    def boom(text, client, calls=None):
        raise ValueError("bad payload")

    monkeypatch.setattr(workflow, "extract_request", boom)
    report = triage()
    assert report.failure.code == "unexpected_error" and not report.failure.retryable
    assert report.failure.stage == "extraction"


def test_oversized_input_is_refused_without_calling_the_model(monkeypatch):
    called = install(monkeypatch)
    report = triage(text="x" * (MAX_INPUT_CHARS + 1))
    assert report.failure.code == "input_too_long" and not report.failure.retryable
    assert called == []


def test_failed_retrieval_still_gives_a_ready_report_for_manual_handling(monkeypatch):
    install(monkeypatch, retrieved=RuntimeError("index down"), pr=make_pr(estimated_value_eur=3000.0))
    report = triage()
    assert report.outcome == TriageOutcome.READY
    assert report.escalation == EscalationReason.RETRIEVAL_ERROR
    assert check_report(report) == []


def test_check_detects_a_band_that_does_not_match_the_value(monkeypatch):
    install(monkeypatch)
    report = triage().model_copy(update={"band": Band.HUMAN_ACTION})
    problems = check_report(report)
    assert "the band does not match the value" in problems


def test_check_detects_a_missing_follow_up(monkeypatch):
    install(monkeypatch, pr=make_pr(cost_center=None))
    report = triage().model_copy(update={"follow_up": None})
    assert check_report(report) == ["a request that needs information must carry the question"]


def test_check_detects_a_summary_that_cites_an_unknown_clause(monkeypatch):
    install(monkeypatch)
    forged = ResultSummary(text="x", cited_chunk_ids=["99-inventada#9.9"])
    report = triage().model_copy(update={"summary": forged})
    assert "the summary cites clauses that are not in the evidence" in check_report(report)


def test_check_detects_a_ready_report_with_no_evidence(monkeypatch):
    install(monkeypatch)
    report = triage().model_copy(update={"evidence": [], "summary": None})
    assert "a ready request needs policy evidence" in check_report(report)
