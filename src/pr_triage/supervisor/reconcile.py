from dataclasses import dataclass, field

from pr_triage.erp import ErpStore
from pr_triage.erp.models import PrState
from pr_triage.supervisor.state import RunState

EMAIL_FLAGS = {"dossier": "dossier_sent", "decision": "decision_email_sent"}
DECISION_ACTIONS = {"approve_pr": "approved", "reject_pr": "rejected"}


@dataclass
class Reconciliation:
    changes: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)


def reconcile(erp: ErpStore, state: RunState) -> Reconciliation:
    result = Reconciliation()
    run = state.run_id

    if state.pr_id is None:
        registered = erp.find_action(f"{run}:register_pr")
        if registered is None:
            return result
        pr_id = registered.result["pr_id"]
        pr = erp.get_pr(pr_id)
        request = state.request
        if pr is None or request is None or (pr.item, pr.quantity, pr.value_eur, pr.cost_center) != (
            request.item, request.quantity, request.estimated_value_eur, request.cost_center
        ):
            result.contradictions.append(f"the request {pr_id} in the ERP does not match this run's request")
            return result
        state.pr_id = pr_id
        state.registered_request = request
        result.changes.append(f"request {pr_id} was already registered in the ERP")

    pr = erp.get_pr(state.pr_id)
    if pr is None:
        result.contradictions.append(f"request {state.pr_id} does not exist in the ERP")
        return result

    applied = {
        decision: erp.find_action(f"{run}:{action}:{state.pr_id}")
        for action, decision in DECISION_ACTIONS.items()
    }
    done = [decision for decision, action in applied.items() if action is not None]
    if len(done) > 1:
        result.contradictions.append("the ERP has both an approval and a rejection from this run")
        return result
    if done:
        decision = done[0]
        if state.decision is None:
            state.decision = decision
            state.decision_verified = False
            result.changes.append(f"the decision {decision} was already applied in the ERP")
        elif state.decision != decision:
            result.contradictions.append(f"the run says {state.decision} but the ERP applied {decision}")
    elif state.decision is not None:
        result.contradictions.append(f"the run says {state.decision} but the ERP has no such action from this run")
    elif pr.state != PrState.OPEN:
        result.contradictions.append(f"the request is {pr.state.value} in the ERP but not by this run")

    for kind, flag in EMAIL_FLAGS.items():
        sent = erp.find_action(f"{run}:send_email:{state.pr_id}:{kind}") is not None
        if sent and not getattr(state, flag):
            setattr(state, flag, True)
            result.changes.append(f"the {kind} email was already sent")
        elif getattr(state, flag) and not sent:
            result.contradictions.append(f"the run says the {kind} email was sent but the ERP has no record")
    return result
