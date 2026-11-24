import json
import sys
from collections import defaultdict
from pathlib import Path

CASES_PATH = Path("data/eval/e2e_cases.jsonl")


def load(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    meta, by_case = data["meta"], defaultdict(list)
    for record in data["results"]:
        by_case[record["id"]].append(record)
    return {"meta": meta, "cases": by_case}


def cell(meta: dict, records: list[dict] | None) -> str:
    if not records:
        return "n/a"
    ok = sum(r["passed"] for r in records)
    if meta["mode"] == "baseline":
        return "triagem OK, sem efeitos" if ok == len(records) else "triagem FALHA, sem efeitos"
    return "OK" if ok == len(records) and len(records) == 1 else f"{ok}/{len(records)}" if len(records) > 1 else "FALHA"


def main(paths: list[str]) -> None:
    runs = [load(p) for p in paths]
    cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    print("# Casos e resultados\n")
    print("## Execuções comparadas\n")
    print("| Execução | Modo | Versão | Modelo | Data |\n|---|---|---|---|---|")
    for number, run in enumerate(runs, 1):
        m = run["meta"]
        print(f"| E{number} | {m['mode']} | {m['version']} | {m['model'] or 'modelos simulados (sem LLM)'} | {m['date']} |")
    heads = " | ".join(f"E{n}" for n in range(1, len(runs) + 1))
    print(f"\n## Resultado por caso\n\n| Caso | Grupo | Esperado | {heads} |\n|---|---|---|{'---|' * len(runs)}")
    for case in cases:
        expect = case["expect"]
        wanted = f"{expect['status']}" + (f" · {expect['pr_state']}" if expect.get("pr_state") else "")
        cells = " | ".join(cell(run["meta"], run["cases"].get(case["id"])) for run in runs)
        print(f"| `{case['id']}` | {case['group']} | {wanted} | {cells} |")
    print("\n## Totais\n")
    for number, run in enumerate(runs, 1):
        records = [r for rs in run["cases"].values() for r in rs]
        print(f"- E{number}: {sum(r['passed'] for r in records)}/{len(records)}")
    print("\n## O que falhou e porquê\n")
    any_failure = False
    for number, run in enumerate(runs, 1):
        for case_id, records in run["cases"].items():
            for record in records:
                if not record["passed"]:
                    any_failure = True
                    print(f"- **E{number} · `{case_id}`**: " + "; ".join(record["failures"]))
    if not any_failure:
        print("Nenhuma falha.")
    print("\n## Notas dos casos\n")
    for case in cases:
        print(f"- `{case['id']}`: {case['note']}")


if __name__ == "__main__":
    main(sys.argv[1:])
