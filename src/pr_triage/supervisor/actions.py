from datetime import timedelta

from pr_triage.authz import Action, HumanApproval
from pr_triage.erp import ErpStore
from pr_triage.supervisor.state import RunState

APPROVAL_HOURS = 24


def record_quote(erp: ErpStore, state: RunState, supplier_id: str, amount_eur: float) -> None:
    erp.add_quote(
        f"quote:{state.run_id}:{supplier_id}:{amount_eur}", "requester", state.pr_id, supplier_id, amount_eur
    )


def record_urgency_justification(erp: ErpStore, state: RunState, head: str, text: str) -> None:
    erp.record_urgency_justification(f"urgency:{state.run_id}:{head}", head, state.pr_id, text)


def record_human_decision(erp: ErpStore, state: RunState, approver: str, approve: bool) -> None:
    person = erp.get_person(approver)
    now = erp.clock()
    erp.record_human_approval(
        f"human:{state.run_id}:{approver}",
        HumanApproval(
            request_id=state.pr_id,
            approver=approver,
            decision=Action.APPROVE_PR if approve else Action.REJECT_PR,
            approved_value_eur=state.request.estimated_value_eur,
            role=person.role.value,
            granted_at=now,
            expires_at=now + timedelta(hours=APPROVAL_HOURS),
        ),
    )
