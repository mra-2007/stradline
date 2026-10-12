"""The optical leave-one-event-out pass, and the rule that decides adoption.

Tested on a fabricated score matrix where the right answer is known by
construction (SYNTHETIC - no number here is a measurement). The properties that
matter are the anti-gaming ones, the same ones the radar pass is held to:

  - a fold's optical point is chosen WITHOUT the held-out event, and crossed
    with the SAR point that was itself chosen without that event (Rule 3.4);
  - `off` and `union` are each given their own best shot, on the same training
    events and the same SAR point, so the comparison is a comparison;
  - the control ceiling is a hard constraint on the optical point too: fusion
    only ever ADDS pixels, so it can only raise the false-flood rate;
  - the adoption rule is applied as declared in config.OPTICAL_ADOPTION, not
    re-argued: winning the mean is not enough on its own;
  - a radar-only configuration still hashes exactly as it always has.
"""

import pytest
import yaml
from dataclasses import replace
from pathlib import Path

from eval import scoreboard as sb
from eval import tune
from eval.run_scoreboard import EventScore
from strandline import optical, run as pipeline
from strandline.config import (
    FUSION_OFF,
    FUSION_UNION,
    OPTICAL_ADOPTION,
    OPTICAL_TUNE_GRIDS,
    DetectParams,
    OpticalParams,
    RunParams,
)

ROOT = Path(__file__).resolve().parents[1]
GATES = yaml.safe_load((ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8"))
N_PRE = 3
CEILING = 0.01

SAR = {"detect.mode": "rough_water", "detect.z_seed": 3.0}
SAR_ALT = {"detect.mode": "rough_water", "detect.z_seed": 2.5}
OFF = {"optical.fusion": FUSION_OFF}


def union(mndwi=0.0, ndvi=0.10) -> dict:
    return {"optical.fusion": FUSION_UNION, "optical.mndwi_water": mndwi,
            "optical.ndvi_drop_min": ndvi}


def entry(event, kind, phash, *, f1_tol=None, ffr=None, clear=None) -> EventScore:
    metrics = {}
    if f1_tol is not None:
        metrics["f1_tolerant"] = f1_tol
        metrics["f1"] = round(f1_tol - 0.05, 4)
    if ffr is not None:
        metrics["false_flood_fraction"] = ffr
    provenance = {} if clear is None else {"optical": {"status": "used",
                                                       "clear_fraction_pair": clear}}
    return EventScore(event=event, kind=kind, params_hash=phash, metrics=metrics,
                      provenance=provenance)


def matrix_of(spec: dict) -> tune.Matrix:
    """spec: {event: (kind, {point_as_dict: {metric: value}})} -> a Matrix."""
    out: tune.Matrix = {}
    for event, (kind, per_point) in spec.items():
        for point, metrics in per_point.items():
            phash = tune.full_point_hash(dict(point), N_PRE)
            out[(event, phash)] = entry(event, kind, phash, **metrics)
    return out


def key(*dicts) -> tuple:
    """A hashable spelling of a combined point, for the spec dicts above."""
    merged = {}
    for d in dicts:
        merged.update(d)
    return tuple(sorted(merged.items()))


# --- hashing and materialisation ----------------------------------------------

def test_a_point_with_no_optical_key_is_still_the_radar_only_configuration():
    """The property every already-published params_hash depends on."""
    dp, rp, op = tune.materialise_full(SAR, N_PRE)
    assert op is None
    assert tune.full_point_hash(SAR, N_PRE) == tune.point_hash(SAR, N_PRE)


def test_fusion_off_is_a_different_configuration_from_radar_only():
    """It consulted Sentinel-2 and then changed no pixel. Both facts are recorded."""
    radar = tune.full_point_hash(SAR, N_PRE)
    off = tune.full_point_hash(tune.combine(SAR, OFF), N_PRE)
    assert off != radar
    _, _, op = tune.materialise_full(tune.combine(SAR, OFF), N_PRE)
    assert op.enabled is True and op.fusion == FUSION_OFF


def test_every_optical_point_gets_its_own_hash():
    """Scores are cached by hash, so two points must never collide."""
    points = [tune.combine(SAR, p) for p in tune.optical_grid_points()]
    hashes = [tune.full_point_hash(p, N_PRE) for p in points]
    assert len(set(hashes)) == len(hashes) == 5


def test_the_optical_point_reaches_the_materialised_params():
    dp, rp, op = tune.materialise_full(tune.combine(SAR, union(0.2, 0.20)), N_PRE)
    assert (op.fusion, op.mndwi_water, op.ndvi_drop_min) == (FUSION_UNION, 0.2, 0.20)
    assert dp.z_seed == 3.0 and rp.n_pre == N_PRE
    assert op.mndwi_rise_min == OpticalParams().mndwi_rise_min, "an untouched field changed"


# --- the grid cannot acquire a read-time axis ---------------------------------

@pytest.mark.parametrize("field", ["max_negative_fraction", "boa_add_offset",
                                   "reflectance_scale", "search_days_before", "enabled"])
def test_a_read_time_optical_axis_is_rejected(field):
    """Those fields choose the scenes or convert the DNs, before the indices exist.

    The search reads each pair once and tries every index threshold against it,
    so an axis that changed the read would silently be ignored.
    """
    with pytest.raises(ValueError, match="before the pair is read"):
        tune.validate_optical_grids({FUSION_UNION: {f"optical.{field}": (1.0,)}})


def test_every_read_field_is_a_real_field_of_opticalparams():
    known = {f for f in OpticalParams().__dataclass_fields__}
    assert set(optical.READ_FIELDS) <= known
    # The fields the grid DOES search must not be read-time, or the pass is wrong.
    searched = {k.split(".", 1)[1] for axes in OPTICAL_TUNE_GRIDS.values() for k in axes}
    assert searched and not (searched & set(optical.READ_FIELDS))


def test_rebuilding_evidence_with_a_read_time_change_is_refused():
    """The contract that makes one read per event safe."""
    import numpy as np
    from shapely.geometry import box
    from strandline.catalog import S2Day, S2Pair
    import datetime as dt

    day = S2Day(day=dt.date(2024, 7, 1),
                start=dt.datetime(2024, 7, 1, 10, tzinfo=dt.timezone.utc),
                names=["x"], footprint=box(0, 0, 1, 1), coverage=1.0, cloud_cover=1.0)
    pair = S2Pair(pre=day, post=replace(day, day=dt.date(2024, 7, 11)), post_latency_days=1)
    read = optical.ReadPair(
        pair=pair, pre_refl=np.zeros((5, 4, 4)), pre_scl=np.full((4, 4), 4.0),
        post_refl=np.zeros((5, 4, 4)), post_scl=np.full((4, 4), 4.0), conversion={},
        read_params={f: getattr(OpticalParams(), f) for f in optical.READ_FIELDS})
    with pytest.raises(optical.OpticalError, match="baked into the read pair"):
        optical.evidence_from_pair(read, replace(OpticalParams(), max_negative_fraction=0.4))


# --- which configurations the pass scores -------------------------------------

def test_each_fold_crosses_the_optical_grid_with_its_own_sar_point():
    sar_points = {"a": SAR, "b": SAR_ALT, "": SAR}
    needed = tune.optical_points_needed(sar_points, tune.optical_grid_points())
    # Two distinct SAR points x 5 optical points; the final one duplicates a's.
    assert len(needed) == 10
    assert {tune.full_point_hash(p, N_PRE) for p in needed} == {
        tune.full_point_hash(tune.combine(s, o), N_PRE)
        for s in (SAR, SAR_ALT) for o in tune.optical_grid_points()}


def test_identical_sar_points_across_folds_are_scored_once():
    """Most folds choose the same SAR point; the matrix must not pay for that."""
    needed = tune.optical_points_needed({"a": SAR, "b": SAR, "c": SAR},
                                        tune.optical_grid_points())
    assert len(needed) == 5


# --- leave-one-event-out over the optical grid --------------------------------

def _three_event_matrix(on_values: list, off_value: float = 0.50,
                        ffr_off: float = 0.001, ffr_on: float = 0.002) -> tune.Matrix:
    """Floods a, b, c plus one control, scored at `off` and at every union point.

    `on_values` is a list of (union point, {event: f1_tolerant}) pairs - a list
    rather than a dict because a grid point is itself a dict.
    """
    spec = {}
    for event in ("a", "b", "c"):
        per_point = {key(SAR, OFF): {"f1_tol": off_value, "clear": 0.8}}
        for point, value in on_values:
            per_point[key(SAR, point)] = {"f1_tol": value.get(event, 0.5), "clear": 0.8}
        spec[event] = (sb.KIND_FLOOD, per_point)
    ctrl = {key(SAR, OFF): {"ffr": ffr_off}}
    for point, _ in on_values:
        ctrl[key(SAR, point)] = {"ffr": ffr_on}
    spec["ctrl"] = (sb.KIND_CONTROL, ctrl)
    return matrix_of(spec)


def test_an_event_never_trains_on_itself_in_the_optical_pass():
    """The one property the whole split exists for, now for the optical half."""
    m = _three_event_matrix([(union(), {"a": 0.9, "b": 0.9, "c": 0.9})])
    sels = tune.optical_fold_selections(m, {"a": SAR, "b": SAR, "c": SAR},
                                        tune.optical_grid_points(),
                                        ["a", "b", "c"], ["ctrl"], CEILING, N_PRE,
                                        FUSION_UNION)
    assert set(sels) == {"a", "b", "c"}
    for held, sel in sels.items():
        assert held not in sel.train_events
        assert set(sel.train_events) == {"a", "b", "c"} - {held}


def test_the_left_out_event_can_change_the_optical_choice():
    """Event c prefers the stricter MNDWI; with c held out, the looser one wins."""
    m = _three_event_matrix([
        (union(0.0, 0.10), {"a": 0.90, "b": 0.90, "c": 0.10}),
        (union(0.2, 0.10), {"a": 0.50, "b": 0.50, "c": 0.99}),
    ])
    sels = tune.optical_fold_selections(m, {"a": SAR, "b": SAR, "c": SAR},
                                        tune.optical_grid_points(),
                                        ["a", "b", "c"], ["ctrl"], CEILING, N_PRE,
                                        FUSION_UNION)
    assert sels["c"].chosen.point["optical.mndwi_water"] == 0.0
    assert sels["a"].chosen.point["optical.mndwi_water"] == 0.2


def test_each_fusion_mode_is_selected_within_its_own_candidates():
    """`off` must never be chosen as the best `union` point, or vice versa."""
    m = _three_event_matrix([(union(), {"a": 0.9, "b": 0.9, "c": 0.9})])
    args = ({"a": SAR, "b": SAR, "c": SAR}, tune.optical_grid_points(),
            ["a", "b", "c"], ["ctrl"], CEILING, N_PRE)
    off = tune.optical_fold_selections(m, *args, FUSION_OFF)
    on = tune.optical_fold_selections(m, *args, FUSION_UNION)
    for held in ("a", "b", "c"):
        assert off[held].chosen.point["optical.fusion"] == FUSION_OFF
        assert on[held].chosen.point["optical.fusion"] == FUSION_UNION
        assert len(off[held].candidates) == 1
        assert len(on[held].candidates) == 4


def test_a_union_point_that_floods_a_control_is_rejected_however_good_its_f1():
    """Fusion only ADDS pixels, so the ceiling is the constraint that matters."""
    m = _three_event_matrix([(union(), {"a": 0.99, "b": 0.99, "c": 0.99})],
                            ffr_on=0.05)
    sels = tune.optical_fold_selections(m, {"a": SAR, "b": SAR, "c": SAR},
                                        tune.optical_grid_points(),
                                        ["a", "b", "c"], ["ctrl"], CEILING, N_PRE,
                                        FUSION_UNION)
    assert not any(sel.ok for sel in sels.values())
    assert all(any("false-flood" in (c.rejected or "") for c in sel.candidates)
               for sel in sels.values())


# --- the adoption rule --------------------------------------------------------

def test_the_declared_rule_is_what_is_applied():
    assert set(OPTICAL_ADOPTION) == {"min_mean_gain", "max_event_regression"}
    v = tune.adoption_verdict({"a": 0.5}, {"a": 0.6}, {"c": 0.001}, {"c": 0.002}, CEILING)
    assert v["rule"] == OPTICAL_ADOPTION


def test_fusion_is_kept_when_it_improves_every_event_and_costs_no_control():
    v = tune.adoption_verdict(off={"a": 0.50, "b": 0.60}, on={"a": 0.62, "b": 0.71},
                              controls_off={"c": 0.001}, controls_on={"c": 0.002},
                              ceiling=CEILING)
    assert v["keep_fusion"] is True
    assert v["criteria"]["mean_tolerant_f1_improves"]["gain"] == pytest.approx(0.115)


def test_a_mean_gain_does_not_buy_a_single_event_regression():
    """The criterion the mean would hide: +0.30 on one event, -0.02 on another."""
    v = tune.adoption_verdict(off={"a": 0.50, "b": 0.60}, on={"a": 0.80, "b": 0.58},
                              controls_off={"c": 0.001}, controls_on={"c": 0.001},
                              ceiling=CEILING)
    assert v["criteria"]["mean_tolerant_f1_improves"]["pass"] is True
    assert v["criteria"]["no_event_regresses_more_than_allowed"]["pass"] is False
    assert v["criteria"]["no_event_regresses_more_than_allowed"]["events_worse"] == {
        "b": pytest.approx(-0.02)}
    assert v["keep_fusion"] is False


def test_a_regression_inside_the_allowance_is_tolerated():
    v = tune.adoption_verdict(off={"a": 0.50, "b": 0.60}, on={"a": 0.80, "b": 0.595},
                              controls_off={"c": 0.001}, controls_on={"c": 0.001},
                              ceiling=CEILING)
    assert v["criteria"]["no_event_regresses_more_than_allowed"]["pass"] is True
    assert v["keep_fusion"] is True


def test_a_control_that_gets_worse_by_more_than_the_allowance_blocks_adoption():
    """Even well under the ceiling: a control that degrades that much is a warning.

    The ceiling here is deliberately loosened to 0.05 so the two control
    criteria are tested apart - this one fails on the DELTA (0.001 -> 0.015,
    i.e. +0.014 against an allowance of 0.01) while the absolute ceiling is
    still satisfied.
    """
    v = tune.adoption_verdict(off={"a": 0.50}, on={"a": 0.90},
                              controls_off={"c": 0.001}, controls_on={"c": 0.015},
                              ceiling=0.05)
    assert v["criteria"]["every_control_under_the_gate_ceiling"]["pass"] is True
    assert v["criteria"]["no_control_regresses_more_than_allowed"]["pass"] is False
    assert v["criteria"]["no_control_regresses_more_than_allowed"]["controls_worse"] == {
        "c": pytest.approx(0.014)}
    assert v["keep_fusion"] is False


def test_a_control_change_inside_the_allowance_is_tolerated():
    v = tune.adoption_verdict(off={"a": 0.50}, on={"a": 0.90},
                              controls_off={"c": 0.001}, controls_on={"c": 0.009},
                              ceiling=CEILING)
    assert v["criteria"]["no_control_regresses_more_than_allowed"]["pass"] is True
    assert v["keep_fusion"] is True


def test_a_control_over_the_gate_ceiling_blocks_adoption():
    v = tune.adoption_verdict(off={"a": 0.50}, on={"a": 0.99},
                              controls_off={"c": 0.0}, controls_on={"c": 0.02},
                              ceiling=CEILING)
    assert v["criteria"]["every_control_under_the_gate_ceiling"]["pass"] is False
    assert v["criteria"]["every_control_under_the_gate_ceiling"]["controls_over"] == {"c": 0.02}
    assert v["keep_fusion"] is False


def test_a_gain_carried_by_one_event_is_flagged_even_when_the_rule_passes():
    """The mean can hide where the improvement came from; the tool must say so.

    This is not a veto - the declared rule decides adoption - but a reader who
    sees only the mean would conclude the change helps broadly when it may help
    in exactly one place.
    """
    v = tune.adoption_verdict(off={"a": 0.50, "b": 0.60, "c": 0.70},
                              on={"a": 0.505, "b": 0.60, "c": 0.90},
                              controls_off={"x": 0.0}, controls_on={"x": 0.0},
                              ceiling=CEILING)
    assert v["keep_fusion"] is True
    conc = v["concentration"]
    assert conc["largest_single_gain_event"] == "c"
    assert conc["share_of_summed_gain"] == pytest.approx(0.2 / 0.205, abs=1e-3)
    assert "warning" in conc
    assert conc["events_improved"] == ["a", "c"]
    assert conc["events_unchanged"] == ["b"]
    assert conc["events_worse"] == []


def test_an_evenly_spread_gain_is_not_flagged():
    v = tune.adoption_verdict(off={"a": 0.50, "b": 0.50, "c": 0.50},
                              on={"a": 0.55, "b": 0.55, "c": 0.55},
                              controls_off={"x": 0.0}, controls_on={"x": 0.0},
                              ceiling=CEILING)
    assert v["keep_fusion"] is True
    assert "warning" not in v["concentration"]
    assert v["concentration"]["share_of_summed_gain"] == pytest.approx(1 / 3, abs=1e-3)


def test_a_tie_is_not_an_improvement():
    """The simpler configuration wins a tie: it is the one the gate numbers measured."""
    v = tune.adoption_verdict(off={"a": 0.50, "b": 0.60}, on={"a": 0.50, "b": 0.60},
                              controls_off={"c": 0.001}, controls_on={"c": 0.001},
                              ceiling=CEILING)
    assert v["criteria"]["mean_tolerant_f1_improves"]["gain"] == 0.0
    assert v["keep_fusion"] is False


def test_the_ceiling_comes_from_the_gates_file_not_from_the_rule():
    """So the two can never drift apart (the rule says so in config.py)."""
    declared = [g["value"] for g in GATES["modules"]["no_flood_controls"]["gates"]
                if g["metric"] == "false_flood_fraction"]
    assert tune.control_ceiling() == float(declared[0])
    assert "control_ceiling" not in OPTICAL_ADOPTION


def test_an_empty_comparison_cannot_adopt_anything():
    """No measured event means no evidence, which is not the same as no regression."""
    v = tune.adoption_verdict({}, {}, {}, {}, CEILING)
    assert v["keep_fusion"] is False
    assert v["criteria"]["mean_tolerant_f1_improves"]["gain"] is None


# --- reading values back out of the matrix ------------------------------------

def test_held_out_values_use_each_folds_own_chosen_point():
    m = _three_event_matrix([
        (union(0.0, 0.10), {"a": 0.90, "b": 0.90, "c": 0.10}),
        (union(0.2, 0.10), {"a": 0.50, "b": 0.50, "c": 0.99}),
    ])
    sels = tune.optical_fold_selections(m, {"a": SAR, "b": SAR, "c": SAR},
                                        tune.optical_grid_points(),
                                        ["a", "b", "c"], ["ctrl"], CEILING, N_PRE,
                                        FUSION_UNION)
    values = tune.held_out_values(m, sels)
    # a and b were chosen on data preferring the strict point, and scored at it.
    assert values["a"] == 0.50 and values["b"] == 0.50
    assert values["c"] == 0.10


def test_a_failed_held_out_score_is_absent_not_zero():
    m = _three_event_matrix([(union(), {"a": 0.9, "b": 0.9, "c": 0.9})])
    phash = tune.full_point_hash(tune.combine(SAR, union()), N_PRE)
    m[("a", phash)] = EventScore(event="a", kind=sb.KIND_FLOOD, params_hash=phash,
                                 error="boom")
    sels = tune.optical_fold_selections(m, {"a": SAR, "b": SAR, "c": SAR},
                                        tune.optical_grid_points(),
                                        ["a", "b", "c"], ["ctrl"], CEILING, N_PRE,
                                        FUSION_UNION)
    values = tune.held_out_values(m, sels)
    assert "a" not in values, "a failed score must not be read as a number"


def test_control_values_are_read_at_one_named_point():
    m = _three_event_matrix([(union(), {"a": 0.9, "b": 0.9, "c": 0.9})])
    off = tune.control_values(m, tune.combine(SAR, OFF), ["ctrl"], N_PRE)
    on = tune.control_values(m, tune.combine(SAR, union()), ["ctrl"], N_PRE)
    assert off == {"ctrl": 0.001} and on == {"ctrl": 0.002}


# --- fusion `off` must BE the radar-only configuration ------------------------

def test_fusion_off_is_checked_against_the_radar_only_numbers():
    """`off` adds no pixel, so every metric must match. Checked, not assumed."""
    off_hash = tune.full_point_hash(tune.combine(SAR, OFF), N_PRE)
    radar_hash = tune.point_hash(SAR, N_PRE)
    optical_matrix = {("a", off_hash): entry("a", sb.KIND_FLOOD, off_hash, f1_tol=0.70)}
    radar_matrix = {("a", radar_hash): entry("a", sb.KIND_FLOOD, radar_hash, f1_tol=0.70)}
    out = tune.fusion_off_matches_radar_only(optical_matrix, radar_matrix, {"a": SAR},
                                             ["a"], N_PRE)
    assert out == {"compared": 1, "identical": 1, "mismatched": {}, "pass": True}


def test_a_fusion_off_mismatch_is_reported_not_smoothed_over():
    """It would mean the eval plumbing changed a pixel it had no business changing."""
    off_hash = tune.full_point_hash(tune.combine(SAR, OFF), N_PRE)
    radar_hash = tune.point_hash(SAR, N_PRE)
    optical_matrix = {("a", off_hash): entry("a", sb.KIND_FLOOD, off_hash, f1_tol=0.71)}
    radar_matrix = {("a", radar_hash): entry("a", sb.KIND_FLOOD, radar_hash, f1_tol=0.70)}
    out = tune.fusion_off_matches_radar_only(optical_matrix, radar_matrix, {"a": SAR},
                                             ["a"], N_PRE)
    assert out["pass"] is False
    assert list(out["mismatched"]) == [f"a@{radar_hash}"]


def test_nothing_compared_is_not_a_pass():
    """An empty radar matrix must not read as agreement."""
    out = tune.fusion_off_matches_radar_only({}, {}, {"a": SAR}, ["a"], N_PRE)
    assert out["compared"] == 0 and out["pass"] is False


# --- the scene handed to each point -------------------------------------------

class _FakeScene:
    """Enough of a PreparedScene for scene_with_optical, which only replaces."""

    def __init__(self):
        self.optical_evidence = "original"
        self.optical_params = None

    def __eq__(self, other):
        return self is other


def test_a_refused_optical_stage_still_scores_the_point_radar_only():
    """The configuration asked for Sentinel-2 and got none. That is the score."""
    from dataclasses import dataclass

    @dataclass
    class Scene:
        optical_evidence: object = "original"
        optical_params: object = None

    op = OpticalParams()
    out = tune.scene_with_optical(Scene(), None, op)
    assert out.optical_evidence is None
    assert out.optical_params is op


def test_no_optical_params_leaves_the_scene_untouched():
    from dataclasses import dataclass

    @dataclass
    class Scene:
        optical_evidence: object = "original"
        optical_params: object = None

    scene = Scene()
    assert tune.scene_with_optical(scene, None, None) is scene
