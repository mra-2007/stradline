"""The leave-one-event-out tuner.

The properties that matter here are anti-gaming properties, so they are tested
on a fabricated score matrix where the right answer is known by construction
(SYNTHETIC - no number here is a measurement):

  - an event's parameters are never chosen using that event (Rule 3.4);
  - the control false-flood ceiling is a hard constraint, never traded off
    against flood F1, and it is read from the gates file rather than copied;
  - the objective is the boundary-tolerant F1 the gate names, not the strict one;
  - ties resolve by grid order, so the same inputs choose the same parameters;
  - when no parameter set is feasible the tuner refuses instead of choosing;
  - a prepared scene is reused across the grid points that share its
    backscatter coefficient, so tuning costs one download per coefficient.
"""

import json

import pytest
import yaml
from dataclasses import replace
from pathlib import Path

from eval import scoreboard as sb
from eval import tune
from eval.run_scoreboard import EventScore
from strandline import run as pipeline
from strandline.config import (
    BACKSCATTER_COEFFICIENTS,
    DETECT_MODES,
    MODE_DARKENING,
    MODE_ROUGH_WATER,
    DetectParams,
    RunParams,
    TUNE_GRIDS,
)

ROOT = Path(__file__).resolve().parents[1]
GATES = yaml.safe_load((ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8"))

N_PRE = 3


def entry(event, kind, phash, *, f1_tol=None, f1=None, ffr=None, ffr_analysed=None):
    """One fabricated score. SYNTHETIC - no number here is a measurement.

    A control carries BOTH false-flood figures, because since 2026-10-12 a
    candidate is feasible only if both are under their ceilings. `ffr_analysed`
    defaults to `ffr`, which is the value that leaves every test below testing
    exactly what it was written to test; the real relationship is
    analysed >= gated, and the tests that care about the difference set it.
    """
    metrics = {}
    if f1_tol is not None:
        metrics.update({"f1_tolerant": f1_tol, "precision_tolerant": f1_tol,
                        "recall_tolerant": f1_tol})
    if f1 is not None:
        metrics.update({"f1": f1, "precision": f1, "recall": f1, "iou": f1})
    provenance = {}
    if ffr is not None:
        metrics["false_flood_fraction"] = ffr
        provenance["false_flood_fraction_analysed_area"] = (
            ffr if ffr_analysed is None else ffr_analysed)
    return EventScore(event=event, kind=kind, params_hash=phash, metrics=metrics,
                      provenance=provenance)


def matrix_from(spec: dict, points: list[dict]) -> tune.Matrix:
    """spec: {event: {"kind":..., point_index: {metric: value}}} -> a Matrix."""
    out: tune.Matrix = {}
    for event, per_point in spec.items():
        kind = per_point["kind"]
        for idx, metrics in per_point.items():
            if idx == "kind":
                continue
            phash = tune.point_hash(points[idx], N_PRE)
            out[(event, phash)] = entry(event, kind, phash, **metrics)
    return out


# --- the grid -----------------------------------------------------------------

def test_the_real_grid_is_valid_and_small():
    points = tune.grid_points()
    assert len(points) == 30, "the declared grid changed size"
    assert len(points) == len(set(tune.point_hash(p) for p in points)), "two points collide"


def test_the_grid_covers_both_modes_with_their_own_axes():
    per_mode = tune.points_by_mode(tune.grid_points())
    assert set(per_mode) == set(DETECT_MODES)
    assert len(per_mode[MODE_DARKENING]) == 18
    assert len(per_mode[MODE_ROUGH_WATER]) == 12
    # Each mode only varies parameters it actually uses.
    assert all("detect.water_db_max" in p for p in per_mode[MODE_DARKENING])
    assert all("detect.min_abs_change_db" in p for p in per_mode[MODE_ROUGH_WATER])
    assert not any("detect.min_abs_change_db" in p for p in per_mode[MODE_DARKENING])
    assert not any("detect.water_db_max" in p for p in per_mode[MODE_ROUGH_WATER])


def test_the_terrain_axis_is_searched_in_both_modes():
    """So the scoreboard can say whether a gain came from the rough-water
    channel or merely from widening the terrain zone."""
    per_mode = tune.points_by_mode(tune.grid_points())
    for mode, pts in per_mode.items():
        assert {p["detect.hand_max_water_m"] for p in pts} == {20.0, 35.0}, mode


def test_the_mode_reaches_the_materialised_params():
    for mode in DETECT_MODES:
        pts = [p for p in tune.grid_points() if tune.mode_of(p) == mode]
        dp, _ = tune.materialise(pts[0], N_PRE)
        assert dp.mode == mode


def test_grid_points_are_deterministic():
    assert tune.grid_points() == tune.grid_points()
    first = tune.point_label(tune.grid_points()[0])
    assert first.startswith(f"mode={MODE_DARKENING}"),         f"grid order is not declaration order: {first}"


def test_the_grid_searches_exactly_the_coefficients_the_backend_offers():
    """gamma0-terrain was dropped on 2026-10-11: the Copernicus Data Space
    backend declares coefficient as enum ["sigma0-ellipsoid"] and refuses it
    before a job is created. The axis stays declared, so putting the name back
    in BACKSCATTER_COEFFICIENTS re-enables the search with no other change."""
    coeffs = {tune.coefficient_of(p) for p in tune.grid_points()}
    assert coeffs == set(BACKSCATTER_COEFFICIENTS) == {"sigma0-ellipsoid"}
    assert all("run.backscatter_coefficient" in axes for axes in TUNE_GRIDS.values())


@pytest.mark.parametrize("bad", [
    {"nope.z_seed": (1.0,)},                 # unknown section
    {"detect.not_a_field": (1.0,)},          # unknown field
    {"run.not_a_field": (1.0,)},
    {"detect.lee_window": (5, 7)},           # applied before a scene is prepared
    {"detect.enl": (4.4,)},
    {"detect.z_seed": ()},                   # no values
    {"detect.mode": (MODE_DARKENING,)},      # the mode is the grid's key, not an axis
])
def test_an_untunable_grid_is_rejected(bad):
    with pytest.raises(ValueError):
        tune.validate_grid({MODE_DARKENING: bad})


def test_a_grid_for_an_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown detection mode"):
        tune.validate_grid({"magic": {"detect.z_seed": (3.0,)}})


def test_materialise_puts_each_value_on_the_right_dataclass():
    dp, rp = tune.materialise({"detect.z_seed": 2.5,
                               "run.max_post_latency_days": 9}, N_PRE)
    assert dp.z_seed == 2.5 and rp.max_post_latency_days == 9
    assert dp.z_grow == DetectParams().z_grow, "an untouched field changed"
    assert rp.n_pre == N_PRE


def test_params_hash_separates_grid_points():
    """Scores are cached by params_hash, so two points must never share one."""
    a, _ = tune.materialise({"detect.z_seed": 2.5}, N_PRE)
    b, _ = tune.materialise({"detect.z_seed": 3.5}, N_PRE)
    assert pipeline.params_hash(a, RunParams()) != pipeline.params_hash(b, RunParams())


# --- the control constraint ---------------------------------------------------

def test_ceiling_is_read_from_the_gates_file_not_copied():
    declared = [g["value"] for g in GATES["modules"]["no_flood_controls"]["gates"]
                if g["metric"] == "false_flood_fraction"]
    assert tune.control_ceiling() == float(declared[0]) == 0.01


def test_a_point_that_floods_a_control_is_rejected_however_good_its_f1():
    points = tune.grid_points()
    m = matrix_from({
        "flood_a": {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.99}, 1: {"f1_tol": 0.80}},
        "flood_b": {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.99}, 1: {"f1_tol": 0.80}},
        "ctrl":    {"kind": sb.KIND_CONTROL, 0: {"ffr": 0.02}, 1: {"ffr": 0.005}},
    }, points)
    sel = tune.select(m, ["flood_a", "flood_b"], ["ctrl"], points[:2], 0.01, N_PRE)
    assert sel.ok
    assert sel.chosen.params_hash == tune.point_hash(points[1], N_PRE), \
        "the tuner bought F1 with a flooded control"
    rejected = [c for c in sel.candidates if c.rejected]
    assert any("false-flood" in c.rejected for c in rejected)


def test_every_control_must_be_under_the_ceiling_not_just_the_mean():
    points = tune.grid_points()
    m = matrix_from({
        "flood_a": {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.99}, 1: {"f1_tol": 0.70}},
        "ctrl_1":  {"kind": sb.KIND_CONTROL, 0: {"ffr": 0.0}, 1: {"ffr": 0.001}},
        "ctrl_2":  {"kind": sb.KIND_CONTROL, 0: {"ffr": 0.05}, 1: {"ffr": 0.002}},
    }, points)
    sel = tune.select(m, ["flood_a"], ["ctrl_1", "ctrl_2"], points[:2], 0.01, N_PRE)
    assert sel.chosen.params_hash == tune.point_hash(points[1], N_PRE)
    assert sel.chosen.worst_control_event == "ctrl_2"


def test_no_feasible_point_refuses_rather_than_choosing():
    points = tune.grid_points()
    m = matrix_from({
        "flood_a": {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.99}, 1: {"f1_tol": 0.98}},
        "ctrl":    {"kind": sb.KIND_CONTROL, 0: {"ffr": 0.9}, 1: {"ffr": 0.8}},
    }, points)
    sel = tune.select(m, ["flood_a"], ["ctrl"], points[:2], 0.01, N_PRE)
    assert not sel.ok and sel.chosen is None
    assert all(c.rejected for c in sel.candidates)


# --- the objective ------------------------------------------------------------

def test_the_objective_is_the_tolerant_f1_the_gate_names():
    """Strict and tolerant disagree here; the gate is on tolerant."""
    points = tune.grid_points()
    m = matrix_from({
        "flood_a": {"kind": sb.KIND_FLOOD,
                    0: {"f1_tol": 0.80, "f1": 0.99},
                    1: {"f1_tol": 0.95, "f1": 0.10}},
        "ctrl": {"kind": sb.KIND_CONTROL, 0: {"ffr": 0.0}, 1: {"ffr": 0.0}},
    }, points)
    sel = tune.select(m, ["flood_a"], ["ctrl"], points[:2], 0.01, N_PRE)
    assert sel.chosen.params_hash == tune.point_hash(points[1], N_PRE)
    assert tune.OBJECTIVE_METRIC == "f1_tolerant"


def test_the_objective_is_the_mean_over_training_events():
    points = tune.grid_points()
    m = matrix_from({
        "a":    {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.90}},
        "b":    {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.70}},
        "ctrl": {"kind": sb.KIND_CONTROL, 0: {"ffr": 0.0}},
    }, points)
    sel = tune.select(m, ["a", "b"], ["ctrl"], points[:1], 0.01, N_PRE)
    assert sel.chosen.objective == pytest.approx(0.80)
    assert sel.chosen.per_event == {"a": 0.9, "b": 0.7}


def test_a_point_missing_any_training_event_is_not_comparable():
    """Otherwise a point could win by being scored only on the easy events."""
    points = tune.grid_points()
    m = matrix_from({
        "a":    {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.99}, 1: {"f1_tol": 0.5}},
        "b":    {"kind": sb.KIND_FLOOD, 1: {"f1_tol": 0.5}},          # no point 0
        "ctrl": {"kind": sb.KIND_CONTROL, 0: {"ffr": 0.0}, 1: {"ffr": 0.0}},
    }, points)
    sel = tune.select(m, ["a", "b"], ["ctrl"], points[:2], 0.01, N_PRE)
    assert sel.chosen.params_hash == tune.point_hash(points[1], N_PRE)
    assert any("no score for" in (c.rejected or "") for c in sel.candidates)


def test_a_failed_score_does_not_count_as_a_score():
    points = tune.grid_points()
    phash = tune.point_hash(points[0], N_PRE)
    m = {("a", phash): EventScore(event="a", kind=sb.KIND_FLOOD, params_hash=phash,
                                  error="boom")}
    sel = tune.select(m, ["a"], [], points[:1], 0.01, N_PRE)
    assert not sel.ok


def test_ties_are_broken_by_grid_order():
    points = tune.grid_points()
    m = matrix_from({
        "a":    {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.9}, 1: {"f1_tol": 0.9},
                 2: {"f1_tol": 0.9}},
        "ctrl": {"kind": sb.KIND_CONTROL, 0: {"ffr": 0.0}, 1: {"ffr": 0.0},
                 2: {"ffr": 0.0}},
    }, points)
    sel = tune.select(m, ["a"], ["ctrl"], points[:3], 0.01, N_PRE)
    assert sel.ties == 3
    assert sel.chosen.params_hash == tune.point_hash(points[0], N_PRE)
    again = tune.select(m, ["a"], ["ctrl"], points[:3], 0.01, N_PRE)
    assert again.chosen.params_hash == sel.chosen.params_hash, "selection is not deterministic"


# --- leave-one-event-out ------------------------------------------------------

def test_an_event_never_trains_on_itself():
    """The one property the whole split exists for (Rule 3.4)."""
    points = tune.grid_points()
    floods = ["a", "b", "c"]
    spec = {e: {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.9}, 1: {"f1_tol": 0.8}} for e in floods}
    spec["ctrl"] = {"kind": sb.KIND_CONTROL, 0: {"ffr": 0.0}, 1: {"ffr": 0.0}}
    m = matrix_from(spec, points)
    sels = tune.leave_one_out(m, floods, ["ctrl"], points[:2], 0.01, N_PRE)
    assert set(sels) == set(floods)
    for held, sel in sels.items():
        assert held not in sel.train_events, f"{held} trained on itself"
        assert set(sel.train_events) == set(floods) - {held}


def test_the_left_out_event_can_change_the_choice():
    """Event c prefers point 1; with c held out, point 0 wins."""
    points = tune.grid_points()
    m = matrix_from({
        "a": {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.90}, 1: {"f1_tol": 0.50}},
        "b": {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.90}, 1: {"f1_tol": 0.50}},
        "c": {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.10}, 1: {"f1_tol": 0.99}},
        "ctrl": {"kind": sb.KIND_CONTROL, 0: {"ffr": 0.0}, 1: {"ffr": 0.0}},
    }, points)
    sels = tune.leave_one_out(m, ["a", "b", "c"], ["ctrl"], points[:2], 0.01, N_PRE)
    assert sels["c"].chosen.params_hash == tune.point_hash(points[0], N_PRE)
    assert sels["a"].chosen.params_hash == tune.point_hash(points[1], N_PRE), \
        "holding out a should let c's preference win"


def test_held_out_score_comes_from_the_chosen_parameters():
    points = tune.grid_points()
    m = matrix_from({
        "a": {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.90}, 1: {"f1_tol": 0.20}},
        "b": {"kind": sb.KIND_FLOOD, 0: {"f1_tol": 0.90}, 1: {"f1_tol": 0.20}},
        "ctrl": {"kind": sb.KIND_CONTROL, 0: {"ffr": 0.0}, 1: {"ffr": 0.0}},
    }, points)
    sels = tune.leave_one_out(m, ["a", "b"], ["ctrl"], points[:2], 0.01, N_PRE)
    chosen = sels["a"].chosen.params_hash
    assert m[("a", chosen)].value("f1_tolerant") == 0.90


# --- the config diff ----------------------------------------------------------

def test_config_diff_is_empty_for_the_declared_defaults():
    assert tune.config_diff(DetectParams(), RunParams()) == []


def test_config_diff_names_every_change():
    dp = replace(DetectParams(), z_seed=2.5, water_db_max=-18.0)
    rp = replace(RunParams(), n_pre=3)
    diff = {d["key"]: (d["config_py"], d["chosen"]) for d in tune.config_diff(dp, rp)}
    assert diff["detect.z_seed"] == (3.0, 2.5)
    assert diff["detect.water_db_max"] == (-15.0, -18.0)
    assert diff["run.n_pre"] == (5, 3)


def test_the_tuner_writes_no_source_file(tmp_path):
    """It emits a diff for a human to apply, and never edits config.py itself.

    Checked two ways: statically, that the only write_text in tune.py targets
    the chosen-parameters file, and behaviourally, that reading a saved choice
    back and printing its diff leaves config.py byte-identical.
    """
    import ast

    source = (ROOT / "eval" / "tune.py").read_text(encoding="utf-8")
    targets = [ast.get_source_segment(source, node.func.value)
               for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr in ("write_text", "write_bytes", "unlink", "replace")]
    assert targets, "no write found at all: has the artefact write moved?"
    assert all("chosen_out" in t for t in targets),         f"tune.py writes to something other than the chosen-parameters file: {targets}"

    config = ROOT / "strandline" / "config.py"
    before = config.read_bytes()
    saved = tmp_path / "chosen_params.json"
    saved.write_text(json.dumps({
        "run_id": "20260101T000000Z",
        "config_diff": [{"key": "detect.z_seed", "config_py": 3.0, "chosen": 2.5}],
    }), encoding="utf-8")
    assert tune.main(["--apply-check", "--chosen-out", str(saved)]) == 0
    assert config.read_bytes() == before, "config.py was modified"


# --- the matrix: one prepared scene per coefficient ---------------------------

class _FakeScene:
    def __init__(self, coefficient):
        self.run_params = replace(RunParams(), n_pre=N_PRE,
                                  backscatter_coefficient=coefficient)


def test_a_scene_is_prepared_once_per_event_and_coefficient(monkeypatch, tmp_path):
    """9 grid points over 1 coefficient must cost 1 preparation, not 9.

    The expensive stage is per (event, coefficient); the cheap stage is per
    point. Were this to regress, tuning would re-download the dev set.
    """
    prepared, scored = [], []

    def fake_prepare(event, rp, dp, *a, **k):
        prepared.append((str(event["name"]), rp.backscatter_coefficient))
        return _FakeScene(rp.backscatter_coefficient)

    def fake_score(event, scene, dp, tolerance_px=1, **k):
        scored.append((str(event["name"]), scene.run_params.backscatter_coefficient, dp.z_seed))
        return entry(str(event["name"]), sb.row_kind(event),
                     pipeline.params_hash(dp, scene.run_params), f1_tol=0.9, f1=0.8)

    monkeypatch.setattr(tune, "prepare_event", fake_prepare)
    monkeypatch.setattr(tune, "score_with_scene", fake_score)

    events = [{"name": "e1", "kind": "flood", "independent_reference": True,
               "event_date": "2024-07-10", "bbox": [85.3, 27.9, 85.4, 28.0]}]
    points = tune.grid_points()
    m = tune.build_matrix(events, points, tmp_path, n_pre=N_PRE, reuse=False,
                          log=lambda *a: None)
    coefficients = {c for _, c in prepared}
    assert len(prepared) == len(coefficients) == 1,         f"prepared {len(prepared)} scenes, expected one per coefficient"
    assert len(scored) == len(points) == 30
    assert len(m) == 30


def test_cached_scores_are_reused_and_nothing_is_prepared(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("must not prepare a scene when every score is cached")

    event = {"name": "e1", "kind": "flood", "independent_reference": True,
             "event_date": "2024-07-10", "bbox": [85.3, 27.9, 85.4, 28.0]}
    points = tune.grid_points()
    from eval import run_scoreboard as rs
    for p in points:
        dp, rp = tune.materialise(p, N_PRE)
        rs.save_score(entry("e1", sb.KIND_FLOOD, pipeline.params_hash(dp, rp), f1_tol=0.77),
                      runs_root=tmp_path)
    monkeypatch.setattr(tune, "prepare_event", boom)
    m = tune.build_matrix([event], points, tmp_path, n_pre=N_PRE, reuse=True,
                          log=lambda *a: None)
    assert len(m) == len(points) == 30
    assert all(s.value("f1_tolerant") == 0.77 for s in m.values())


def test_one_unusable_event_does_not_lose_the_matrix(monkeypatch, tmp_path):
    def fake_prepare(event, rp, dp, *a, **k):
        if event["name"] == "bad":
            raise pipeline.PipelineError("no cached raster")
        return _FakeScene(rp.backscatter_coefficient)

    def fake_score(event, scene, dp, tolerance_px=1, **k):
        return entry(str(event["name"]), sb.row_kind(event),
                     pipeline.params_hash(dp, scene.run_params), f1_tol=0.9)

    monkeypatch.setattr(tune, "prepare_event", fake_prepare)
    monkeypatch.setattr(tune, "score_with_scene", fake_score)
    events = [{"name": n, "kind": "flood", "independent_reference": True,
               "event_date": "2024-07-10", "bbox": [85.3, 27.9, 85.4, 28.0]}
              for n in ("bad", "good")]
    m = tune.build_matrix(events, tune.grid_points()[:2], tmp_path, n_pre=N_PRE,
                          reuse=False, log=lambda *a: None)
    good = [s for s in m.values() if s.event == "good"]
    assert good and all(s.ok for s in good)
    assert any(not s.ok and "no cached raster" in s.error
               for s in m.values() if s.event == "bad")


# --- the real grid against the real pool -------------------------------------

def test_the_grid_axes_exist_in_config_and_are_documented():
    source = (ROOT / "strandline" / "config.py").read_text(encoding="utf-8")
    assert "TUNE_GRIDS" in source
    for axes in TUNE_GRIDS.values():
        for key in axes:
            field = key.split(".", 1)[1]
            assert field in source, f"{field} is not declared in config.py"
    assert "Rule 3.4" in source, "the grid must say where parameter values may come from"
