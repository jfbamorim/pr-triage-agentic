import pytest

from pr_triage.erp import Faults
from pr_triage.erp.models import PrState
from pr_triage.supervisor.orchestrator import Orchestrator, Specialists
from pr_triage.supervisor.state import Move, RunStatus
from pr_triage.supervisor.store import RunStore
from test_orchestrator import ANA, NOW, Setup, approval, validation
from test_tools import FakePolicy
from datetime import timedelta


class Crash(Exception):
    pass


class CrashingStore(RunStore):
    def __init__(self, path, crash_after_step):
        super().__init__(path)
        self.crash_after_step = crash_after_step
        self.armed = True

    def save(self, state):
        if self.armed and len(state.steps) == self.crash_after_step and state.updated_at is not None:
            self.armed = False
            raise Crash()
        super().save(state)


def crashed_setup(tmp_path, crash_after_step, text="monitores"):
    s = Setup(tmp_path)
    s.store = CrashingStore(tmp_path / "state" / "runs.db", crash_after_step)
    s.orchestrator = Orchestrator(
        s.erp, FakePolicy(), s.store, Specialists(s.triage, s.investigator, s.writer, s.decider)
    )
    with pytest.raises(Crash):
        s.start(text)
    s.store.armed = False
    return s


def fresh_orchestrator(s):
    return Orchestrator(
        s.erp, FakePolicy(), RunStore(s.store.path), Specialists(s.triage, s.investigator, s.writer, s.decider)
    )


def test_a_crash_after_registering_recovers_the_request_without_registering_again(tmp_path):
    s = crashed_setup(tmp_path, crash_after_step=2)
    assert s.store.load("run-1").pr_id is None and s.erp.get_pr(1) is not None
    state = fresh_orchestrator(s).resume("run-1")
    assert state.status == RunStatus.DONE and state.pr_id == 1
    assert [a.action for a in s.erp.get_actions()].count("register_pr") == 1
    assert Move.RECONCILE in [step.move for step in state.steps]
    assert Move.REGISTER not in [step.move for step in state.steps][2:]


def test_a_crash_after_approving_recovers_the_decision_and_verifies_it(tmp_path):
    s = crashed_setup(tmp_path, crash_after_step=4)
    assert s.store.load("run-1").decision is None and s.erp.get_pr(1).state == PrState.APPROVED
    state = fresh_orchestrator(s).resume("run-1")
    assert state.status == RunStatus.DONE and state.decision == "approved" and state.decision_verified
    assert len([a for a in s.erp.get_actions(1) if a.action == "approve_pr"]) == 1
    assert s.erp.get_budget("CC-TI-100").committed_eur == 38600.0
    assert len(s.erp.get_outbox(1)) == 1


def test_a_crash_after_the_decision_email_does_not_send_it_twice(tmp_path):
    s = crashed_setup(tmp_path, crash_after_step=6)
    assert not s.store.load("run-1").decision_email_sent and len(s.erp.get_outbox(1)) == 1
    state = fresh_orchestrator(s).resume("run-1")
    assert state.status == RunStatus.DONE and state.decision_email_sent
    assert len(s.erp.get_outbox(1)) == 1


def test_a_crash_after_the_dossier_does_not_send_it_twice(tmp_path):
    s = crashed_setup(tmp_path, crash_after_step=4, text="grande")
    assert not s.store.load("run-1").dossier_sent and len(s.erp.get_outbox(1)) == 1
    state = fresh_orchestrator(s).resume("run-1")
    assert state.status == RunStatus.WAITING_HUMAN and state.dossier_sent
    assert len(s.erp.get_outbox(1)) == 1


def test_a_resume_with_nothing_to_reconcile_adds_no_step(tmp_path):
    s = Setup(tmp_path)
    paused = s.start("grande")
    state = fresh_orchestrator(s).resume("run-1")
    assert [x.move for x in state.steps] == [x.move for x in paused.steps]


def test_a_state_that_disagrees_with_the_erp_is_escalated(tmp_path):
    s = Setup(tmp_path)
    s.start("grande")
    state = s.store.load("run-1")
    state.decision = "approved"
    s.store.save(state)
    result = fresh_orchestrator(s).resume("run-1")
    assert result.status == RunStatus.NEEDS_MANUAL and "disagree" in result.outcome_message
    assert s.erp.get_pr(1).state == PrState.OPEN


def test_a_request_decided_outside_the_run_is_escalated(tmp_path):
    s = Setup(tmp_path)
    s.start("grande")
    s.erp.approve_pr("other:approve", "someone", 1, [], "manual")
    result = fresh_orchestrator(s).resume("run-1")
    assert result.status == RunStatus.NEEDS_MANUAL and "not by this run" in result.outcome_message


def test_an_unavailable_erp_leaves_the_run_untouched(tmp_path):
    s = Setup(tmp_path)
    s.start("grande")
    s.erp.faults = Faults(unavailable={"find_action"})
    result = fresh_orchestrator(s).resume("run-1")
    assert result.status == RunStatus.WAITING_HUMAN and "unavailable" in result.outcome_message
    s.erp.faults = Faults()
    assert fresh_orchestrator(s).resume("run-1").status == RunStatus.WAITING_HUMAN


def test_a_human_approval_that_expired_during_the_interruption_is_not_used(tmp_path):
    s = Setup(tmp_path)
    s.start("grande")
    s.erp.record_human_approval("h1", approval())
    s.erp.record_human_approval("h2", validation())
    s.erp.clock = lambda: NOW + timedelta(hours=30)
    result = fresh_orchestrator(s).resume("run-1")
    assert result.status == RunStatus.NEEDS_MANUAL
    assert s.erp.get_pr(1).state == PrState.OPEN
