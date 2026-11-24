import argparse
import os
from datetime import timedelta

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from openai import OpenAI

from pr_triage.authz import Action, HumanApproval
from pr_triage.erp import ErpStore, create_demo_erp
from pr_triage.supervisor.live import live_orchestrator
from pr_triage.supervisor.state import RunState
from pr_triage.supervisor.store import RunStore
from pr_triage.tools.policy import PolicyIndex

ERP_PATH = "state/erp.db"
RUNS_PATH = "state/runs.db"
APPROVER = "maria.santos@exemplo.pt"


def show(state: RunState, erp: ErpStore) -> None:
    print(f"\nrun {state.run_id}: {state.status.value}")
    if state.outcome_message:
        print(f"  {state.outcome_message}")
    for step in state.steps:
        mode = "forçado" if step.forced else "supervisor"
        print(f"  {step.n:>2}. {step.move.value:<20} {mode:<10} {step.outcome}")
        if not step.forced:
            print(f"      motivo: {step.reason}")
    if state.pr_id:
        pr = erp.get_pr(state.pr_id)
        print(f"  ERP: pedido {pr.pr_id} {pr.state.value}, {pr.value_eur:g} €, {pr.cost_center}")
        for mail in erp.get_outbox(pr.pr_id):
            print(f"  email [{mail.kind}] para {mail.to_addr}: {mail.subject}\n    {mail.body}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    run = sub.add_parser("run")
    run.add_argument("text")
    run.add_argument("--as", dest="email", default="ana.silva@exemplo.pt")
    run.add_argument("--id")
    reply = sub.add_parser("reply")
    reply.add_argument("run_id")
    reply.add_argument("text")
    approve = sub.add_parser("approve")
    approve.add_argument("run_id")
    approve.add_argument("--reject", action="store_true")
    quote = sub.add_parser("quote")
    quote.add_argument("run_id")
    quote.add_argument("supplier_id")
    quote.add_argument("amount", type=float)
    for name in ("resume", "show"):
        sub.add_parser(name).add_argument("run_id")
    args = parser.parse_args()

    if args.command == "init":
        create_demo_erp(ERP_PATH)
        print("ERP de demonstração criado em", ERP_PATH)
        return

    erp = ErpStore(ERP_PATH)
    store = RunStore(RUNS_PATH)
    orchestrator = live_orchestrator(erp, PolicyIndex(OpenAI()), store, OpenAI())

    if args.command == "run":
        state = orchestrator.start(args.text, args.email, args.id)
    elif args.command == "reply":
        state = orchestrator.reply(args.run_id, args.text)
    elif args.command == "resume":
        state = orchestrator.resume(args.run_id)
    elif args.command == "quote":
        state = store.load(args.run_id)
        erp.add_quote(f"quote:{args.run_id}:{args.supplier_id}:{args.amount}", "requester",
                      state.pr_id, args.supplier_id, args.amount)
        print(f"Cotação de {args.supplier_id} ({args.amount:g} €) registada no pedido {state.pr_id}. Corre `resume`.")
        return
    elif args.command == "approve":
        state = store.load(args.run_id)
        now = erp.clock()
        erp.record_human_approval(
            f"human:{args.run_id}",
            HumanApproval(
                request_id=state.pr_id,
                approver=APPROVER,
                decision=Action.REJECT_PR if args.reject else Action.APPROVE_PR,
                approved_value_eur=state.request.estimated_value_eur,
                granted_at=now,
                expires_at=now + timedelta(hours=24),
            ),
        )
        print("Decisão humana registada no ERP. Corre `resume` para continuar.")
        return
    else:
        state = store.load(args.run_id)
    show(state, erp)


if __name__ == "__main__":
    main()
