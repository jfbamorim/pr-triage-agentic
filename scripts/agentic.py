import argparse
import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from openai import OpenAI

from pr_triage.erp import ErpStore, create_demo_erp
from pr_triage.supervisor.live import live_orchestrator
from pr_triage.memory import InvalidMemory, MemoryStore
from pr_triage.supervisor.actions import record_human_decision, record_quote
from pr_triage.supervisor.state import RunState
from pr_triage.toolserver.client import McpToolPort
from pr_triage.supervisor.store import RunStore
from pr_triage.tools.policy import PolicyIndex

ERP_PATH = "state/erp.db"
MEMORY_PATH = "state/memory.db"
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
    inv = state.investigation
    if inv is not None:
        print(f"  investigação: {inv.policy_verdict.value} | cláusulas {', '.join(inv.cited_clauses) or '-'}")
        for check in inv.checks:
            print(f"    {check.name.value:<18} {check.result.value:<11} {check.evidence}")
        print(f"    {inv.summary}")
    if state.pr_id:
        pr = erp.get_pr(state.pr_id)
        print(f"  ERP: pedido {pr.pr_id} {pr.state.value}, {pr.value_eur:g} €, {pr.cost_center}")
        for mail in erp.get_outbox(pr.pr_id):
            print(f"  email [{mail.kind}] para {mail.to_addr}: {mail.subject}\n    {mail.body}")


def manage_memory(args, erp: ErpStore, memory: MemoryStore) -> None:
    if args.action == "list":
        for e in memory.history(args.email):
            origin = f"corrida {e.origin_run_id}, pedido {e.origin_pr_id}" if e.origin_run_id else e.source
            reason = f" ({e.status_reason})" if e.status_reason else ""
            print(f"#{e.id} {e.key}={e.value} [{e.status}{reason}] origem: {origin}; criada {e.created_at:%d/%m/%Y}; válida até {e.valid_until:%d/%m/%Y}")
        return
    if args.action == "forget":
        print(f"{memory.forget(args.email, 'cost_center')} entrada(s) esquecida(s).")
        return
    center = erp.get_cost_center(args.value or "")
    if center is None or not center.active:
        print(f"O centro de custo {args.value} não existe ou está inativo; nada foi alterado.")
        return
    try:
        entry = memory.remember(args.email, "cost_center", args.value, "user_correction")
    except InvalidMemory as error:
        print(error)
        return
    print(f"Memória corrigida: #{entry.id} cost_center={entry.value}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-mcp", action="store_true", help="chama as tools diretamente, sem servidor MCP")
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
    memory_cmd = sub.add_parser("memory", help="ver, corrigir ou esquecer a memória de um requerente")
    memory_cmd.add_argument("action", choices=["list", "correct", "forget"])
    memory_cmd.add_argument("email")
    memory_cmd.add_argument("value", nargs="?")
    args = parser.parse_args()

    if args.command == "init":
        create_demo_erp(ERP_PATH)
        print("ERP de demonstração criado em", ERP_PATH)
        return

    erp = ErpStore(ERP_PATH)
    memory = MemoryStore(MEMORY_PATH, erp.clock)
    if args.command == "memory":
        manage_memory(args, erp, memory)
        return
    store = RunStore(RUNS_PATH)
    port = None if args.no_mcp or args.command in ("quote", "approve", "show") else McpToolPort.stdio(ERP_PATH).start()
    orchestrator = live_orchestrator(erp, PolicyIndex(OpenAI()), store, OpenAI(), port=port, memory=memory)

    if args.command == "run":
        state = orchestrator.start(args.text, args.email, args.id)
    elif args.command == "reply":
        state = orchestrator.reply(args.run_id, args.text)
    elif args.command == "resume":
        state = orchestrator.resume(args.run_id)
    elif args.command == "quote":
        state = store.load(args.run_id)
        record_quote(erp, state, args.supplier_id, args.amount)
        print(f"Cotação de {args.supplier_id} ({args.amount:g} €) registada no pedido {state.pr_id}. Corre `resume`.")
        return
    elif args.command == "approve":
        state = store.load(args.run_id)
        record_human_decision(erp, state, APPROVER, not args.reject)
        print("Decisão humana registada no ERP. Corre `resume` para continuar.")
        return
    else:
        state = store.load(args.run_id)
    if port is not None:
        port.close()
    show(state, erp)


if __name__ == "__main__":
    main()
