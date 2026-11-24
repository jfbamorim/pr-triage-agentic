from datetime import datetime, timedelta, timezone

from pr_triage.authz import (
    Action,
    AuthContext,
    HumanApproval,
    PolicyVerdict,
    authorize,
)
from pr_triage.domain.band import Band, determine_band
from pr_triage.domain.status import EscalationReason, Status

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


def ctx(value: float, **overrides) -> AuthContext:
    data = dict(
        request_id=1,
        status=Status.VALID,
        value_eur=value,
        band=determine_band(value),
        policy_verdict=PolicyVerdict.PERMITS,
        cited_clauses=["02-escaloes#2.1"],
        now=NOW,
    )
    data.update(overrides)
    return AuthContext(**data)


def approval(action: Action = Action.APPROVE_PR, **overrides) -> HumanApproval:
    data = dict(
        request_id=1,
        approver="maria",
        decision=action,
        approved_value_eur=14000,
        granted_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=23),
    )
    data.update(overrides)
    return HumanApproval(**data)


def validation(**overrides) -> HumanApproval:
    data = dict(approver="pedro", role="purchasing")
    data.update(overrides)
    return approval(**data)


def test_below_first_threshold_agent_approves():
    verdict = authorize(Action.APPROVE_PR, ctx(500, cited_clauses=[]))
    assert verdict.allowed and verdict.code == "no_blockers"


def test_below_first_threshold_agent_does_not_reject():
    assert authorize(Action.REJECT_PR, ctx(500)).code == "nothing_to_reject"


def head(action: Action = Action.APPROVE_PR, **overrides) -> HumanApproval:
    data = dict(approver="joao", role="department_head", approved_value_eur=5000)
    data.update(overrides)
    return approval(action, **data)


def test_middle_band_approval_waits_for_the_department_head():
    assert authorize(Action.APPROVE_PR, ctx(3000)).code == "human_approval_required"


def test_middle_band_approves_with_the_department_head_and_no_purchasing_validation():
    verdict = authorize(Action.APPROVE_PR, ctx(3000, approval=head()))
    assert verdict.allowed and verdict.code == "human_approved"


def test_middle_band_refuses_the_wrong_person_or_a_stale_approval():
    assert authorize(Action.APPROVE_PR, ctx(3000, approval=approval(approved_value_eur=5000))).code == "wrong_role"
    assert authorize(Action.APPROVE_PR, ctx(3000, approval=head(), proposed_by="joao")).code == "same_person"
    expired = head(expires_at=NOW - timedelta(minutes=1))
    assert authorize(Action.APPROVE_PR, ctx(3000, approval=expired)).code == "approval_expired"
    assert authorize(Action.APPROVE_PR, ctx(4000, approval=head(approved_value_eur=3000))).code == "value_changed"


def test_middle_band_approval_still_needs_a_permitting_verdict():
    verdict = authorize(Action.APPROVE_PR, ctx(3000, policy_verdict=PolicyVerdict.BLOCKS, approval=head()))
    assert verdict.code == "policy_unclear"


def test_middle_band_department_head_can_reject_even_when_the_policy_permits():
    assert authorize(Action.REJECT_PR, ctx(3000, approval=head(Action.REJECT_PR))).allowed
    assert authorize(Action.REJECT_PR, ctx(3000)).code == "policy_unclear"


def test_middle_band_rejects_when_policy_blocks():
    verdict = authorize(Action.REJECT_PR, ctx(3000, policy_verdict=PolicyVerdict.BLOCKS))
    assert verdict.allowed and verdict.code == "policy_blocks"


def test_middle_band_refuses_approval_when_policy_blocks():
    verdict = authorize(Action.APPROVE_PR, ctx(3000, policy_verdict=PolicyVerdict.BLOCKS))
    assert not verdict.allowed and verdict.code == "policy_unclear"


def test_middle_band_refuses_unclear_policy():
    verdict = authorize(Action.APPROVE_PR, ctx(3000, policy_verdict=PolicyVerdict.UNCLEAR))
    assert not verdict.allowed


def test_middle_band_requires_a_cited_clause():
    verdict = authorize(Action.APPROVE_PR, ctx(3000, cited_clauses=[]))
    assert verdict.code == "no_clause"


def test_agent_cannot_approve_above_limit_without_human():
    verdict = authorize(Action.APPROVE_PR, ctx(14000))
    assert not verdict.allowed and verdict.code == "human_approval_required"


def test_above_limit_with_valid_human_approval():
    verdict = authorize(Action.APPROVE_PR, ctx(14000, approval=approval(), validation=validation()))
    assert verdict.allowed and verdict.code == "human_approved"


def test_the_direction_alone_does_not_approve_above_the_limit():
    verdict = authorize(Action.APPROVE_PR, ctx(14000, approval=approval()))
    assert not verdict.allowed and verdict.code == "validation_required"


def test_purchasing_alone_does_not_approve_above_the_limit():
    verdict = authorize(Action.APPROVE_PR, ctx(14000, validation=validation()))
    assert verdict.code == "human_approval_required"


def test_the_validation_must_come_from_purchasing_and_from_someone_else():
    wrong_role = validation(role="director")
    assert authorize(Action.APPROVE_PR, ctx(14000, approval=approval(), validation=wrong_role)).code == "wrong_role"
    same = validation(approver="maria")
    assert authorize(Action.APPROVE_PR, ctx(14000, approval=approval(), validation=same)).code == "same_person"


def test_an_expired_or_smaller_validation_is_refused():
    expired = validation(expires_at=NOW - timedelta(minutes=1))
    assert authorize(Action.APPROVE_PR, ctx(14000, approval=approval(), validation=expired)).code == "approval_expired"
    smaller = validation(approved_value_eur=9000)
    assert authorize(Action.APPROVE_PR, ctx(14000, approval=approval(), validation=smaller)).code == "value_changed"


def test_the_approval_must_come_from_the_direction():
    other = approval(role="purchasing")
    assert authorize(Action.APPROVE_PR, ctx(14000, approval=other, validation=validation())).code == "wrong_role"


def test_above_limit_rejects_only_with_human_decision():
    verdict = authorize(Action.REJECT_PR, ctx(14000, approval=approval(Action.REJECT_PR)))
    assert verdict.allowed


def test_approval_for_another_decision_is_refused():
    verdict = authorize(Action.APPROVE_PR, ctx(14000, approval=approval(Action.REJECT_PR)))
    assert verdict.code == "approval_other_decision"


def test_expired_approval_is_refused():
    expired = approval(expires_at=NOW - timedelta(minutes=1))
    assert authorize(Action.APPROVE_PR, ctx(14000, approval=expired)).code == "approval_expired"


def test_proposer_cannot_approve_own_proposal():
    verdict = authorize(Action.APPROVE_PR, ctx(14000, approval=approval(approver="agent")))
    assert verdict.code == "same_person"


def test_approval_for_another_request_is_refused():
    verdict = authorize(Action.APPROVE_PR, ctx(14000, approval=approval(request_id=2)))
    assert verdict.code == "approval_other_request"


def test_value_higher_than_approved_is_refused():
    verdict = authorize(Action.APPROVE_PR, ctx(16000, approval=approval()))
    assert verdict.code == "value_changed"


def test_band_that_does_not_match_value_is_refused():
    verdict = authorize(Action.APPROVE_PR, ctx(14000, band=Band.NO_BLOCKERS))
    assert verdict.code == "band_mismatch"


def test_invalid_request_is_not_decided():
    verdict = authorize(Action.APPROVE_PR, ctx(500, status=Status.NEEDS_INFO))
    assert verdict.code == "request_not_valid"


def test_missing_policy_evidence_goes_to_manual_action():
    verdict = authorize(
        Action.APPROVE_PR, ctx(3000, escalation=EscalationReason.NO_POLICY_EVIDENCE)
    )
    assert verdict.code == "manual_action"


def test_dossier_only_above_limit():
    assert authorize(Action.SEND_DOSSIER_EMAIL, ctx(14000)).allowed
    assert authorize(Action.SEND_DOSSIER_EMAIL, ctx(3000)).allowed
    assert authorize(Action.SEND_DOSSIER_EMAIL, ctx(3000, cited_clauses=[])).code == "no_clause"
    assert authorize(Action.SEND_DOSSIER_EMAIL, ctx(500)).code == "no_dossier"


def test_decision_email_requires_an_executed_decision():
    assert authorize(Action.SEND_DECISION_EMAIL, ctx(3000)).code == "nothing_executed"


def test_decision_email_after_execution_must_cite_clauses():
    done = ctx(3000, executed_decision=Action.APPROVE_PR)
    assert authorize(Action.SEND_DECISION_EMAIL, done).allowed
    no_clauses = ctx(3000, executed_decision=Action.APPROVE_PR, cited_clauses=[])
    assert authorize(Action.SEND_DECISION_EMAIL, no_clauses).code == "no_clause"


def test_decision_email_below_first_threshold_needs_no_clause():
    done = ctx(500, executed_decision=Action.APPROVE_PR, cited_clauses=[])
    assert authorize(Action.SEND_DECISION_EMAIL, done).allowed
