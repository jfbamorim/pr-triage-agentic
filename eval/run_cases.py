import argparse
import json
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

CASES_PATH = Path("data/eval/e2e_cases.jsonl")
RESULTS_DIR = Path("eval/results")
CLOCK = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
RUN_ID = "case"


def git_state() -> str:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "desconhecido"
    return commit + (" (alterações por commitar)" if dirty else "")


def load_cases(only: list[str] | None) -> list[dict]:
    cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [c for c in cases if not only or c["id"] in only]


def version_label() -> str:
    try:
        return subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def observe(erp, state, before: float | None, cost_center: str | None) -> dict:
    actions: dict[str, int] = {}
    for action in erp.get_actions():
        actions[action.action] = actions.get(action.action, 0) + 1
    pr = erp.get_pr(state.pr_id) if state.pr_id else None
    outbox = erp.get_outbox()
    committed = None
    if cost_center and before is not None:
        committed = erp.get_budget(cost_center).committed_eur - before
    return {
        "status": state.status.value,
        "pr_state": pr.state.value if pr else None,
        "pr_value": pr.value_eur if pr else None,
        "actions": actions,
        "emails": [[m.kind, m.to_addr] for m in outbox],
        "committed_delta": committed,
        "steps": [f"{s.move.value}{'' if s.forced else '*'}" for s in state.steps],
        "message": state.outcome_message,
    }


def check(expect: dict, seen: dict, requester: str) -> list[str]:
    failures = []
    if seen["status"] != expect["status"]:
        failures.append(f"estado: esperado {expect['status']}, obtido {seen['status']}")
    if seen["pr_state"] != expect.get("pr_state"):
        failures.append(f"requisição: esperado {expect.get('pr_state')}, obtido {seen['pr_state']}")
    if "pr_value" in expect and seen["pr_value"] != expect["pr_value"]:
        failures.append(f"valor no ERP: esperado {expect['pr_value']}, obtido {seen['pr_value']}")
    for name, count in expect.get("actions", {}).items():
        if seen["actions"].get(name, 0) != count:
            failures.append(f"ações {name}: esperado {count}, obtido {seen['actions'].get(name, 0)}")
    for name in expect.get("never", []):
        if seen["actions"].get(name, 0):
            failures.append(f"ação proibida {name} executada {seen['actions'][name]} vez(es)")
    if "emails" in expect:
        wanted = [[kind, requester if to == "requester" else to] for kind, to in expect["emails"]]
        if seen["emails"] != wanted:
            failures.append(f"emails: esperado {wanted}, obtido {seen['emails']}")
    if "committed" in expect and seen["committed_delta"] != expect["committed"]["delta"]:
        failures.append(f"orçamento comprometido: esperado {expect['committed']['delta']}, obtido {seen['committed_delta']}")
    return failures


def apply_step(step: dict, orchestrator, erp, store, actions, state):
    kind = step["do"]
    if kind == "resume":
        return orchestrator.resume(RUN_ID)
    if kind == "reply":
        return orchestrator.reply(RUN_ID, step["text"])
    current = store.load(RUN_ID)
    if kind == "quote":
        actions.record_quote(erp, current, step["supplier"], step["amount"])
    elif kind == "justify":
        actions.record_urgency_justification(erp, current, "joao.ferreira@exemplo.pt", step["text"])
    elif kind == "decide":
        actions.record_human_decision(erp, current, step["as"], step["approve"])
    elif kind == "tamper":
        with closing(erp.connect()) as conn, conn:
            conn.execute(f"UPDATE purchase_requests SET {step['column']} = ? WHERE pr_id = ?", (step["value"], current.pr_id))
    return state


def run_simulated(case: dict, workdir: Path) -> dict:
    from e2e_fakes import Decider, FakeInvestigator, FakePolicy, FakeTriage, FakeWriter
    from pr_triage.erp import Faults, create_demo_erp
    from pr_triage.supervisor import actions
    from pr_triage.supervisor.orchestrator import Orchestrator, Specialists
    from pr_triage.supervisor.store import RunStore

    erp = create_demo_erp(workdir / "erp.db", clock=lambda: CLOCK, faults=Faults(**case["faults"]) if case["faults"] else None)
    store = RunStore(workdir / "runs.db")
    fake = case["fake"]
    orchestrator = Orchestrator(
        erp, FakePolicy(), store,
        Specialists(FakeTriage(fake["requests"], fake["suspicious"]), FakeInvestigator(erp, fake["verdict"], fake["clauses"]), FakeWriter(), Decider()),
    )
    return drive(case, orchestrator, erp, store, actions)


def run_real(case: dict, workdir: Path, shared: dict) -> dict:
    from pr_triage.erp import create_demo_erp
    from pr_triage.supervisor import actions
    from pr_triage.supervisor.live import live_orchestrator
    from pr_triage.supervisor.store import RunStore
    from pr_triage.toolserver.client import McpToolPort

    erp = create_demo_erp(workdir / "erp.db")
    store = RunStore(workdir / "runs.db")
    port = McpToolPort.stdio(str(workdir / "erp.db")).start()
    try:
        orchestrator = live_orchestrator(erp, shared["policy"], store, shared["client"], port=port)
        return drive(case, orchestrator, erp, store, actions)
    finally:
        port.close()


def drive(case: dict, orchestrator, erp, store, actions) -> dict:
    cost_center = case["expect"].get("committed", {}).get("cc")
    before = erp.get_budget(cost_center).committed_eur if cost_center else None
    state = orchestrator.start(case["text"], case["requester"], run_id=RUN_ID)
    for step in case["script"]:
        state = apply_step(step, orchestrator, erp, store, actions, state)
    return observe(erp, state, before, cost_center)


def run_baseline(case: dict, shared: dict) -> dict:
    from pr_triage.workflow import run_workflow

    result = run_workflow(case["text"], shared["client"])
    return {"status": result.status.value, "band": result.band.value if result.band else None,
            "reason": result.reason.value if result.reason else None}


def check_baseline(case: dict, seen: dict) -> list[str]:
    expected = case["triage"]
    if not expected:
        return []
    failures = []
    for field in ("status", "band"):
        if field in expected and seen[field] != expected[field]:
            failures.append(f"{field}: esperado {expected[field]}, obtido {seen[field]}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["simulated", "real", "baseline"], default="simulated")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--label")
    args = parser.parse_args()

    shared = {}
    if args.mode in ("real", "baseline"):
        from dotenv import load_dotenv
        from openai import OpenAI
        load_dotenv()
        shared["client"] = OpenAI()
        if args.mode == "real":
            from pr_triage.tools.policy import PolicyIndex
            shared["policy"] = PolicyIndex(shared["client"])

    label = args.label or version_label()
    results = []
    for case in load_cases(args.only):
        for attempt in range(1, args.repeat + 1):
            record = {"id": case["id"], "group": case["group"], "attempt": attempt, "note": case["note"]}
            try:
                if args.mode == "baseline":
                    seen = run_baseline(case, shared)
                    record.update(seen=seen, failures=check_baseline(case, seen), effects="n/a (sem ERP nem efeitos)")
                else:
                    with tempfile.TemporaryDirectory() as tmp:
                        seen = run_simulated(case, Path(tmp)) if args.mode == "simulated" else run_real(case, Path(tmp), shared)
                    record.update(seen=seen, failures=check(case["expect"], seen, case["requester"]))
            except Exception as error:
                record.update(seen=None, failures=[f"erro: {type(error).__name__}: {error}"])
            if args.mode == "baseline" and not case["triage"]:
                record["passed"] = None
            else:
                record["passed"] = not record["failures"]
            results.append(record)
            status = "N/A " if record["passed"] is None else "OK  " if record["passed"] else "FALHA"
            print(f"{status} {case['id']}" + (f" #{attempt}" if args.repeat > 1 else ""))
            for failure in record["failures"]:
                print("      ", failure)

    applicable = [r for r in results if r["passed"] is not None]
    passed = sum(r["passed"] for r in applicable)
    skipped = len(results) - len(applicable)
    suffix = f"; {skipped} não aplicáveis" if skipped else ""
    print(f"\n{passed}/{len(applicable)}{suffix} ({args.mode}, versão {label})")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"e2e-{args.mode}-{label.replace('/', '_')}.json"
    meta = {"version": label, "mode": args.mode, "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": None if args.mode == "simulated" else "gpt-4.1-mini (OPENAI_MODEL / OPENAI_AGENT_MODEL por omissão)",
            "commit": git_state()}
    out.write_text(json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Resultados em", out)


if __name__ == "__main__":
    main()
