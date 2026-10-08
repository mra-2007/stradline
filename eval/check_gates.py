"""Check every accuracy gate in eval/gates.yaml against the latest eval results.

    python -m eval.check_gates                 # all modules, exit 0 only if ALL pass
    python -m eval.check_gates --module flood_mapping
    python -m eval.check_gates --brief         # one line per module (session start)
    python -m eval.check_gates --json

Exit codes: 0 = every selected gate passes, 1 = a gate fails or has no data,
3 = held-out leakage detected (the Trishuli case study appears in dev results).
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GATES = ROOT / "eval" / "gates.yaml"

PASS, FAIL, NO_DATA, REPORT = "PASS", "FAIL", "NO DATA", "REPORT"


def _float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def aggregate(values: list[float], how: str) -> float:
    if not values or any(math.isnan(v) for v in values):
        return math.nan
    vals = sorted(values)
    if how == "mean":
        return sum(vals) / len(vals)
    if how == "min":
        return vals[0]
    if how == "max":
        return vals[-1]
    if how in ("median", "p50"):
        how = "p50"
    if how.startswith("p"):
        q = float(how[1:]) / 100.0
        idx = q * (len(vals) - 1)
        lo, hi = math.floor(idx), math.ceil(idx)
        return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)
    raise ValueError(f"unknown aggregation {how!r}")


def compare(value: float, op: str, target: float) -> bool:
    if math.isnan(value):
        return False
    if op == ">=":
        return value >= target
    if op == "<=":
        return value <= target
    raise ValueError(f"unknown operator {op!r}")


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def select_latest(rows: list[dict], where: dict) -> tuple[str | None, list[dict]]:
    matching = [r for r in rows if all(str(r.get(k, "")) == str(v) for k, v in where.items())]
    run_ids = [r.get("run_id", "") for r in matching if r.get("run_id")]
    if not run_ids:
        return None, []
    latest = max(run_ids)
    return latest, [r for r in matching if r.get("run_id") == latest]


def find_leaks(cfg: dict, root: Path) -> list[str]:
    held = cfg.get("heldout", {})
    names = [n.lower() for n in held.get("forbidden_names", [])]
    leaks, seen = [], set()
    for mod in cfg.get("modules", {}).values():
        path = root / mod["results"]
        if path in seen:
            continue
        seen.add(path)
        for i, row in enumerate(read_rows(path), start=2):
            event = str(row.get("event", "")).lower()
            hit = [n for n in names if n in event]
            if hit:
                leaks.append(f"{mod['results']} line {i}: event {row.get('event')!r} matches held-out name {hit[0]!r}")
    return leaks


def check(cfg: dict, root: Path, only: str | None = None) -> list[dict]:
    report = []
    for name, mod in cfg["modules"].items():
        if only and name != only:
            continue
        rows = read_rows(root / mod["results"])
        run_id, latest = select_latest(rows, mod.get("where", {}))
        n, need = len(latest), int(mod.get("min_rows", 1))
        enough = n >= need
        for g in mod["gates"]:
            value = aggregate([_float(r.get(g["metric"])) for r in latest], g["agg"]) if latest else math.nan
            if g.get("report_only"):
                status = REPORT
            elif not enough:
                status = NO_DATA
            else:
                status = PASS if compare(value, g["op"], float(g["value"])) else FAIL
            report.append({
                "module": name, "metric": g["metric"], "agg": g["agg"],
                "target": None if g.get("report_only") else f"{g['op']} {g['value']}",
                "value": None if math.isnan(value) else round(value, 4),
                "status": status, "rows": n, "min_rows": need, "run_id": run_id,
            })
    return report


def module_status(report: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in report:
        if r["status"] == REPORT:
            out.setdefault(r["module"], PASS)
            continue
        current = out.get(r["module"], PASS)
        if r["status"] == FAIL or current == FAIL:
            out[r["module"]] = FAIL
        elif r["status"] == NO_DATA:
            out[r["module"]] = NO_DATA
        else:
            out[r["module"]] = current
    return out


def print_table(report: list[dict], leaks: list[str]) -> None:
    head = f"{'module':<18} {'gate':<28} {'value':>8} {'target':>9}  {'status':<8} rows"
    print(head)
    print("-" * len(head))
    for r in report:
        gate = f"{r['agg']}({r['metric']})"
        value = "-" if r["value"] is None else f"{r['value']:.4f}"
        target = r["target"] or "report"
        rows = f"{r['rows']}/{r['min_rows']}"
        print(f"{r['module']:<18} {gate:<28} {value:>8} {target:>9}  {r['status']:<8} {rows}")
    for leak in leaks:
        print(f"HELD-OUT LEAK: {leak}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--gates", type=Path, default=DEFAULT_GATES)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--module")
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(args.gates.read_text(encoding="utf-8"))
    if args.module and args.module not in cfg["modules"]:
        print(f"unknown module {args.module!r}; known: {', '.join(cfg['modules'])}")
        return 1
    report = check(cfg, args.root, args.module)
    leaks = find_leaks(cfg, args.root)
    statuses = module_status(report)

    if args.brief:
        parts = [f"{m}={s}" for m, s in statuses.items()]
        print("Strandline accuracy gates: " + ", ".join(parts))
        if leaks:
            print(f"HELD-OUT LEAK DETECTED ({len(leaks)} rows): run python -m eval.check_gates")
        return 0
    if args.json:
        print(json.dumps({"modules": statuses, "gates": report, "leaks": leaks}, indent=2))
    else:
        print_table(report, leaks)
        print()
        print("  ".join(f"{m}: {s}" for m, s in statuses.items()))
    if leaks:
        return 3
    return 0 if all(s == PASS for s in statuses.values()) else 1


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.exit(main())
