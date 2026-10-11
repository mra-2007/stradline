"""The whole chain on SYNTHETIC events: tuner -> flood.csv -> check_gates.

These numbers are generated, not measured. The test proves the plumbing - that
a tuning run produces rows the gate checker can read, that the gate moves with
them, and that a bad event blocks the gate instead of being averaged away
(Rule 3.8: a synthetic result is never a real-world accuracy claim).
"""

import csv
import json

import pytest
import yaml
from dataclasses import replace
from pathlib import Path

from eval import check_gates, scoreboard as sb, tune
from eval.run_scoreboard import EventScore
from strandline import run as pipeline
from strandline.config import RunParams

ROOT = Path(__file__).resolve().parents[1]
GATES_FILE = ROOT / "eval" / "gates.yaml"
GATES = yaml.safe_load(GATES_FILE.read_text(encoding="utf-8"))

N_PRE = 3
# One F1 per synthetic event, fixed here so the expected gate verdict is known
# by construction. Four floods and three controls meet both min_rows.
FLOOD_F1 = {"syn_alpha": 0.96, "syn_beta": 0.94, "syn_gamma": 0.92, "syn_delta": 0.95}
CONTROL_FFR = {"syn_alpha_control": 0.001, "syn_beta_control": 0.004,
               "syn_gamma_control": 0.0}


def _events(dependent: set[str] = frozenset()) -> dict[str, dict]:
    out = {}
    for name in FLOOD_F1:
        out[name] = {"name": name, "kind": "flood", "event_date": "2024-07-10",
                     "bbox": [85.3, 27.9, 85.4, 28.0],
                     "independent_reference": name not in dependent,
                     "reference": "synthetic.shp",
                     "reference_classes": {"water": ["__all__"]}}
    for name in CONTROL_FFR:
        out[name] = {"name": name, "kind": "control", "event_date": "2024-06-10",
                     "bbox": [85.3, 27.9, 85.4, 28.0], "independent_reference": None}
    return out


class _FakeScene:
    def __init__(self, coefficient):
        self.run_params = replace(RunParams(), n_pre=N_PRE,
                                  backscatter_coefficient=coefficient)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Run the real tuner CLI over synthetic events with the rasters stubbed.

    Only the two expensive stages are replaced: preparing a scene and
    classifying it. Grid search, leave-one-event-out selection, the control
    constraint, row writing and the gate check are the real code.
    """
    events = _events()
    monkeypatch.setattr(sb, "load_events", lambda path=None: events)
    monkeypatch.setattr(tune, "select_events",
                        lambda plan="gates", only=None, evs=None: list(events.values()))

    def fake_prepare(event, rp, dp, *a, **k):
        return _FakeScene(rp.backscatter_coefficient)

    def fake_score(event, scene, dp, tolerance_px=1, **k):
        """A deterministic stand-in: the best grid point is the declared one.

        F1 peaks at z_seed 3.0 with gamma0-terrain, and sigma0-ellipsoid floods
        one control, so the expected choice is known without any real data.
        """
        name = str(event["name"])
        rp = scene.run_params
        phash = pipeline.params_hash(dp, rp)
        kind = sb.row_kind(event)
        penalty = abs(dp.z_seed - 3.0) * 0.10
        bonus = 0.02 if rp.backscatter_coefficient == "gamma0-terrain" else 0.0
        if kind == sb.KIND_CONTROL:
            ffr = CONTROL_FFR[name] + (0.05 if rp.backscatter_coefficient
                                       == "sigma0-ellipsoid" else 0.0)
            return EventScore(event=name, kind=kind, params_hash=phash,
                              metrics={"false_flood_fraction": round(ffr, 6)},
                              provenance={"domain_pixels": 1_000_000,
                                          "domain_fraction": 1.0})
        f1 = round(min(FLOOD_F1[name] + bonus - penalty, 0.999), 4)
        return EventScore(event=name, kind=kind, params_hash=phash,
                          metrics={"precision": f1 - 0.02, "recall": f1 - 0.01, "f1": f1 - 0.015,
                                   "iou": f1 - 0.05, "precision_tolerant": f1 + 0.005,
                                   "recall_tolerant": f1 - 0.005, "f1_tolerant": f1},
                          provenance={"domain_pixels": 1_000_000, "domain_fraction": 1.0,
                                      "reference": {"reference_ha": 120.0}})

    monkeypatch.setattr(tune, "prepare_event", fake_prepare)
    monkeypatch.setattr(tune, "score_with_scene", fake_score)

    results = tmp_path / "eval" / "results"
    results.mkdir(parents=True)
    chosen = tmp_path / "chosen_params.json"
    code = tune.main(["--runs-root", str(tmp_path / "runs"),
                      "--results-dir", str(results),
                      "--chosen-out", str(chosen), "--n-pre", str(N_PRE)])
    return {"code": code, "results": results, "chosen": chosen, "root": tmp_path,
            "events": events}


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --- the run itself -----------------------------------------------------------

def test_the_tuner_completes_and_writes_rows(harness):
    assert harness["code"] == 0
    rows = _rows(harness["results"] / "flood.csv")
    assert len(rows) == len(FLOOD_F1) + len(CONTROL_FFR)
    assert {r["event"] for r in rows} == set(FLOOD_F1) | set(CONTROL_FFR)


def test_flood_rows_are_leave_one_event_out_and_controls_are_not(harness):
    rows = _rows(harness["results"] / "flood.csv")
    assert all(r["split"] == "loeo" for r in rows if r["kind"] == "flood")
    assert all(r["split"] == "full" for r in rows if r["kind"] == "control")


def test_every_row_carries_its_provenance(harness):
    for row in _rows(harness["results"] / "flood.csv"):
        assert row["run_id"] and row["timestamp"] and row["git_commit"]
        assert row["params_hash"], "a row without the parameters that produced it"


def test_one_run_id_covers_the_whole_run(harness):
    """The gate checker takes the latest run_id, so a run must not be split."""
    rows = _rows(harness["results"] / "flood.csv")
    assert len({r["run_id"] for r in rows}) == 1


def test_strict_and_tolerant_are_both_written(harness):
    for row in _rows(harness["results"] / "flood.csv"):
        if row["kind"] != "control":
            assert row["f1"] and row["f1_tolerant"] and row["iou"]
            assert float(row["f1"]) != float(row["f1_tolerant"])


# --- the chosen parameters ----------------------------------------------------

def test_chosen_params_records_the_method_and_the_diff(harness):
    saved = json.loads(harness["chosen"].read_text(encoding="utf-8"))
    assert saved["method"]["objective"].startswith("maximise mean f1_tolerant")
    assert "0.01" in saved["method"]["constraint"]
    assert saved["method"]["split"] == "leave-one-event-out over the flood events in the pool"
    assert saved["chosen_parameters"]["run"]["backscatter_coefficient"] == "gamma0-terrain"
    assert saved["chosen_parameters"]["detect"]["z_seed"] == 3.0
    assert {d["key"] for d in saved["config_diff"]} >= {"run.backscatter_coefficient"}
    assert any("in-sample" in c for c in saved["caveats"])
    assert any("n_pre" in c for c in saved["caveats"])


def test_every_event_has_its_own_recorded_selection(harness):
    saved = json.loads(harness["chosen"].read_text(encoding="utf-8"))
    assert set(saved["loeo"]) == set(FLOOD_F1)
    for held, sel in saved["loeo"].items():
        assert held not in sel["train_events"], f"{held} trained on itself"
        assert len(sel["candidates"]) == 18
        assert sel["chosen"]["worst_control_false_flood_fraction"] <= 0.01


def test_the_control_constraint_rejected_the_other_coefficient(harness):
    """sigma0 floods a control in this stand-in, so it must be rejected."""
    saved = json.loads(harness["chosen"].read_text(encoding="utf-8"))
    rejected = [c for c in saved["final"]["candidates"] if c["rejected"]]
    assert rejected, "no candidate was rejected at all"
    assert all("sigma0" in str(c["point"]["run.backscatter_coefficient"])
               for c in rejected if "false-flood" in str(c["rejected"]))


# --- the gates actually move with the rows ------------------------------------

def _check(root: Path, module: str) -> dict[str, str]:
    report = check_gates.check(GATES, root, module)
    return {f"{r['agg']}({r['metric']})": r["status"] for r in report}


def test_the_flood_gate_reads_the_rows_and_passes(harness):
    status = _check(harness["root"], "flood_mapping")
    assert status["mean(f1_tolerant)"] == check_gates.PASS, status
    assert status["mean(precision_tolerant)"] == check_gates.PASS
    assert status["min(f1_tolerant)"] == check_gates.PASS
    assert check_gates.module_status(
        check_gates.check(GATES, harness["root"], "no_flood_controls")
    )["no_flood_controls"] == check_gates.PASS


def test_ungated_modules_stay_no_data(harness):
    """Nothing this run wrote may make the damage or copilot gates look done."""
    statuses = check_gates.module_status(check_gates.check(GATES, harness["root"]))
    for module in ("damage_overlay", "cutoff_analysis", "copilot", "live_drills",
                   "reproducibility"):
        assert statuses[module] == check_gates.NO_DATA, module


def test_no_held_out_leak_in_the_written_rows(harness):
    assert check_gates.find_leaks(GATES, harness["root"]) == []


# --- a bad event must block the gate, not be averaged away -------------------

def test_a_failed_event_blocks_the_flood_gate(monkeypatch, tmp_path):
    """One event that cannot run => no flood rows => NO DATA, never a mean
    over the events that happened to work (Rule 3.2)."""
    events = _events()
    monkeypatch.setattr(sb, "load_events", lambda path=None: events)
    monkeypatch.setattr(tune, "select_events",
                        lambda plan="gates", only=None, evs=None: list(events.values()))

    def fake_prepare(event, rp, dp, *a, **k):
        if event["name"] == "syn_gamma":
            raise pipeline.PipelineError("no cached raster for syn_gamma")
        return _FakeScene(rp.backscatter_coefficient)

    monkeypatch.setattr(tune, "prepare_event", fake_prepare)
    monkeypatch.setattr(tune, "score_with_scene",
                        lambda event, scene, dp, tolerance_px=1, **k: EventScore(
                            event=str(event["name"]), kind=sb.row_kind(event),
                            params_hash=pipeline.params_hash(dp, scene.run_params),
                            metrics=({"false_flood_fraction": 0.0}
                                     if sb.row_kind(event) == sb.KIND_CONTROL
                                     else {"f1_tolerant": 0.99, "precision_tolerant": 0.99,
                                           "recall_tolerant": 0.99, "f1": 0.98,
                                           "precision": 0.98, "recall": 0.98, "iou": 0.97}),
                            provenance={"domain_pixels": 1, "domain_fraction": 1.0}))

    results = tmp_path / "eval" / "results"
    results.mkdir(parents=True)
    code = tune.main(["--runs-root", str(tmp_path / "runs"), "--results-dir", str(results),
                      "--chosen-out", str(tmp_path / "chosen.json"), "--n-pre", str(N_PRE)])
    assert code == 1, "the tuner must report failure"
    rows = _rows(results / "flood.csv") if (results / "flood.csv").exists() else []
    assert not [r for r in rows if r["kind"] == "flood"], "a partial flood set was written"
    status = check_gates.module_status(check_gates.check(GATES, tmp_path, "flood_mapping"))
    assert status["flood_mapping"] == check_gates.NO_DATA


def test_a_low_scoring_event_fails_the_gate_rather_than_being_dropped(monkeypatch, tmp_path):
    events = _events()
    monkeypatch.setattr(sb, "load_events", lambda path=None: events)
    monkeypatch.setattr(tune, "select_events",
                        lambda plan="gates", only=None, evs=None: list(events.values()))
    monkeypatch.setattr(tune, "prepare_event",
                        lambda event, rp, dp, *a, **k: _FakeScene(rp.backscatter_coefficient))

    def fake_score(event, scene, dp, tolerance_px=1, **k):
        name = str(event["name"])
        kind = sb.row_kind(event)
        f1 = 0.40 if name == "syn_gamma" else 0.97     # one genuinely bad event
        return EventScore(event=name, kind=kind,
                          params_hash=pipeline.params_hash(dp, scene.run_params),
                          metrics=({"false_flood_fraction": 0.0} if kind == sb.KIND_CONTROL
                                   else {"f1_tolerant": f1, "precision_tolerant": f1,
                                         "recall_tolerant": f1, "f1": f1, "precision": f1,
                                         "recall": f1, "iou": f1}),
                          provenance={"domain_pixels": 1, "domain_fraction": 1.0})

    monkeypatch.setattr(tune, "score_with_scene", fake_score)
    results = tmp_path / "eval" / "results"
    results.mkdir(parents=True)
    tune.main(["--runs-root", str(tmp_path / "runs"), "--results-dir", str(results),
               "--chosen-out", str(tmp_path / "chosen.json"), "--n-pre", str(N_PRE)])

    rows = [r for r in _rows(results / "flood.csv") if r["kind"] == "flood"]
    assert len(rows) == len(FLOOD_F1), "the bad event was dropped from the CSV"
    assert any(float(r["f1_tolerant"]) == 0.40 for r in rows)
    report = check_gates.check(GATES, tmp_path, "flood_mapping")
    worst = [r for r in report if r["metric"] == "f1_tolerant" and r["agg"] == "min"][0]
    assert worst["status"] == check_gates.FAIL, "a 0.40 event did not fail the worst-case floor"
