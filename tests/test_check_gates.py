import csv
from pathlib import Path

import yaml

from eval import check_gates as cg

ROOT = Path(__file__).resolve().parents[1]

GATES = {
    "contract": {"boundary_tolerance_px": 1},
    "heldout": {"forbidden_names": ["trishuli", "emsr927"]},
    "modules": {
        "flood_mapping": {
            "results": "eval/results/flood.csv",
            "where": {"kind": "flood", "split": "loeo"},
            "min_rows": 3,
            "gates": [
                {"metric": "f1_tolerant", "agg": "mean", "op": ">=", "value": 0.90},
                {"metric": "f1_tolerant", "agg": "min", "op": ">=", "value": 0.80, "floor": True},
                {"metric": "f1", "agg": "mean", "report_only": True},
            ],
        },
        "no_flood_controls": {
            "results": "eval/results/flood.csv",
            "where": {"kind": "control"},
            "min_rows": 2,
            "gates": [{"metric": "false_flood_fraction", "agg": "max", "op": "<=", "value": 0.01}],
        },
    },
}


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    gates = tmp_path / "gates.yaml"
    gates.write_text(yaml.safe_dump(GATES))
    out = tmp_path / "eval" / "results" / "flood.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for r in rows for k in r})
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return gates


def _flood(run, event, f1t, f1=0.8):
    return {"run_id": run, "event": event, "kind": "flood", "split": "loeo", "f1_tolerant": f1t, "f1": f1}


def _control(run, event, frac):
    return {"run_id": run, "event": event, "kind": "control", "false_flood_fraction": frac}


def _run(gates: Path, tmp_path: Path, *extra) -> int:
    return cg.main(["--gates", str(gates), "--root", str(tmp_path), *extra])


def test_all_pass(tmp_path):
    rows = [_flood("20261008T100000Z", e, v) for e, v in [("a", 0.93), ("b", 0.91), ("c", 0.92)]]
    rows += [_control("20261008T100000Z", e, 0.001) for e in ("x", "y")]
    assert _run(_write(tmp_path, rows), tmp_path) == 0


def test_mean_below_target_fails(tmp_path):
    rows = [_flood("20261008T100000Z", e, v) for e, v in [("a", 0.93), ("b", 0.85), ("c", 0.86)]]
    rows += [_control("20261008T100000Z", e, 0.0) for e in ("x", "y")]
    assert _run(_write(tmp_path, rows), tmp_path) == 1


def test_one_bad_event_hits_worst_case_floor(tmp_path):
    rows = [_flood("20261008T100000Z", e, v) for e, v in [("a", 0.99), ("b", 0.99), ("c", 0.75)]]
    rows += [_control("20261008T100000Z", e, 0.0) for e in ("x", "y")]
    cfg = yaml.safe_load(_write(tmp_path, rows).read_text())
    report = cg.check(cfg, tmp_path)
    floor = [r for r in report if r["agg"] == "min"][0]
    assert floor["status"] == cg.FAIL


def test_too_few_events_is_no_data(tmp_path):
    rows = [_flood("20261008T100000Z", e, 0.99) for e in ("a", "b")]
    cfg = yaml.safe_load(_write(tmp_path, rows).read_text())
    statuses = cg.module_status(cg.check(cfg, tmp_path))
    assert statuses["flood_mapping"] == cg.NO_DATA


def test_latest_run_is_used_not_the_best_one(tmp_path):
    good = [_flood("20261007T100000Z", e, 0.99) for e in ("a", "b", "c")]
    bad = [_flood("20261008T100000Z", e, 0.70) for e in ("a", "b", "c")]
    controls = [_control("20261008T100000Z", e, 0.0) for e in ("x", "y")]
    assert _run(_write(tmp_path, good + bad + controls), tmp_path) == 1


def test_control_with_false_flood_fails(tmp_path):
    rows = [_flood("20261008T100000Z", e, 0.95) for e in ("a", "b", "c")]
    rows += [_control("20261008T100000Z", "x", 0.0), _control("20261008T100000Z", "y", 0.05)]
    assert _run(_write(tmp_path, rows), tmp_path) == 1


def test_missing_metric_value_fails(tmp_path):
    rows = [_flood("20261008T100000Z", e, v) for e, v in [("a", 0.95), ("b", ""), ("c", 0.95)]]
    rows += [_control("20261008T100000Z", e, 0.0) for e in ("x", "y")]
    assert _run(_write(tmp_path, rows), tmp_path) == 1


def test_heldout_leak_detected(tmp_path):
    rows = [_flood("20261008T100000Z", e, 0.95) for e in ("a", "b", "Trishuli_2026")]
    rows += [_control("20261008T100000Z", e, 0.0) for e in ("x", "y")]
    assert _run(_write(tmp_path, rows), tmp_path) == 3


def test_no_results_yet_is_not_done(tmp_path):
    gates = tmp_path / "gates.yaml"
    gates.write_text(yaml.safe_dump(GATES))
    assert _run(gates, tmp_path) == 1
    assert _run(gates, tmp_path, "--brief") == 0  # session brief never blocks


def test_percentile_aggregate():
    assert cg.aggregate([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], "p90") == 9.1
    assert cg.aggregate([3.0, 1.0], "mean") == 2.0


def test_real_gates_file_loads_and_reports(tmp_path):
    cfg = yaml.safe_load((ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8"))
    report = cg.check(cfg, tmp_path)  # empty root: everything NO DATA, nothing crashes
    assert {r["status"] for r in report} <= {cg.NO_DATA, cg.REPORT}
