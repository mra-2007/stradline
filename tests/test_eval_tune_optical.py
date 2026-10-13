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
    CONTROL_ANALYSED_AREA_CEILING,
    EXTENT_TUNE_GRID,
    FUSION_OFF,
    FUSION_UNION,
    FUSION_WATER_ONLY,
    OPTICAL_ADOPTION,
    OPTICAL_TUNE_GRIDS,
    DetectParams,
    ExtentParams,
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


def entry(event, kind, phash, *, f1_tol=None, ffr=None, clear=None,
          ffr_analysed=None) -> EventScore:
    """SYNTHETIC. A control carries both false-flood figures (see test_eval_tune)."""
    metrics = {}
    if f1_tol is not None:
        metrics["f1_tolerant"] = f1_tol
        metrics["f1"] = round(f1_tol - 0.05, 4)
    provenance = {} if clear is None else {"optical": {"status": "used",
                                                       "clear_fraction_pair": clear}}
    if ffr is not None:
        metrics["false_flood_fraction"] = ffr
        provenance["false_flood_fraction_analysed_area"] = (
            ffr if ffr_analysed is None else ffr_analysed)
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
    dp, rp, op, ep = tune.materialise_full(SAR, N_PRE)
    assert op is None and ep is None
    assert tune.full_point_hash(SAR, N_PRE) == tune.point_hash(SAR, N_PRE)


def test_fusion_off_is_a_different_configuration_from_radar_only():
    """It consulted Sentinel-2 and then changed no pixel. Both facts are recorded."""
    radar = tune.full_point_hash(SAR, N_PRE)
    off = tune.full_point_hash(tune.combine(SAR, OFF), N_PRE)
    assert off != radar
    _, _, op, _ = tune.materialise_full(tune.combine(SAR, OFF), N_PRE)
    assert op.enabled is True and op.fusion == FUSION_OFF


def test_every_second_pass_point_gets_its_own_hash():
    """Scores are cached by hash, so two points must never collide.

    14 since 2026-10-12: 7 fusion points (off 1, union 4, water_only 2) crossed
    with the two extent values.
    """
    points = [tune.combine(SAR, p) for p in tune.optical_grid_points()]
    hashes = [tune.full_point_hash(p, N_PRE) for p in points]
    assert len(set(hashes)) == len(hashes) == 14


def test_the_optical_point_reaches_the_materialised_params():
    dp, rp, op, _ = tune.materialise_full(tune.combine(SAR, union(0.2, 0.20)), N_PRE)
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
    # Two distinct SAR points x 14 second-pass points; the final one duplicates a's.
    assert len(needed) == 28
    assert {tune.full_point_hash(p, N_PRE) for p in needed} == {
        tune.full_point_hash(tune.combine(s, o), N_PRE)
        for s in (SAR, SAR_ALT) for o in tune.optical_grid_points()}


def test_identical_sar_points_across_folds_are_scored_once():
    """Most folds choose the same SAR point; the matrix must not pay for that."""
    needed = tune.optical_points_needed({"a": SAR, "b": SAR, "c": SAR},
                                        tune.optical_grid_points())
    assert len(needed) == 14


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
        # One per extent value for `off`, four thresholds x two for `union`.
        assert len(off[held].candidates) == 2
        assert len(on[held].candidates) == 8


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


# --- the SECOND control ceiling (declared 2026-10-12, before scoring) --------

def test_both_control_ceilings_must_be_satisfied():
    """A control inside the gated ceiling but over the analysed-area one is rejected.

    This is the case the class-count audit found in real data: Bhutan's control
    reads 0.0022 over the whole scoring domain and 4.37% over the ground the
    detector could classify, because 95% of that AOI is terrain-excluded and
    pads the gated denominator.
    """
    spec = {}
    for event in ("a", "b"):
        spec[event] = (sb.KIND_FLOOD, {
            key(SAR, OFF): {"f1_tol": 0.50},
            key(SAR, union()): {"f1_tol": 0.99},          # much better on floods
        })
    spec["ctrl"] = (sb.KIND_CONTROL, {
        key(SAR, OFF): {"ffr": 0.0002, "ffr_analysed": 0.0005},
        # Gated 0.0022 PASSES the 1% ceiling; analysed 0.0437 does not.
        key(SAR, union()): {"ffr": 0.0022, "ffr_analysed": 0.0437},
    })
    m = matrix_of(spec)
    sel = tune.select(m, ["a", "b"], ["ctrl"],
                      [tune.combine(SAR, OFF), tune.combine(SAR, union())],
                      CEILING, N_PRE, tune.full_point_hash)
    assert sel.ok
    assert sel.chosen.point["optical.fusion"] == FUSION_OFF, (
        "the tuner bought F1 with a control that floods 4.4% of classifiable ground")
    rejected = [c for c in sel.candidates if c.rejected]
    assert any("over analysed ground" in (c.rejected or "") for c in rejected)


def test_the_analysed_ceiling_is_the_declared_one():
    assert CONTROL_ANALYSED_AREA_CEILING == 0.01
    # Declared in config, not duplicated from the human-owned gates file - the
    # whole-domain ceiling is READ from there (test_eval_tune.py asserts that).
    assert "false_flood_fraction_analysed_area" not in (
        (ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8"))


def test_the_chosen_candidate_records_both_worst_controls():
    spec = {"a": (sb.KIND_FLOOD, {key(SAR, OFF): {"f1_tol": 0.50}}),
            "c1": (sb.KIND_CONTROL, {key(SAR, OFF): {"ffr": 0.001, "ffr_analysed": 0.004}}),
            "c2": (sb.KIND_CONTROL, {key(SAR, OFF): {"ffr": 0.003, "ffr_analysed": 0.002}})}
    sel = tune.select(matrix_of(spec), ["a"], ["c1", "c2"], [tune.combine(SAR, OFF)],
                      CEILING, N_PRE, tune.full_point_hash)
    assert sel.ok
    # The two worst controls need not be the same event, and both are reported.
    assert sel.chosen.worst_control_event == "c2"
    assert sel.chosen.worst_control_analysed_event == "c1"
    assert sel.chosen.worst_control_analysed == 0.004
    report = sel.report()["chosen"]
    assert report["worst_control_false_flood_fraction_analysed_area"] == 0.004


def test_a_control_score_predating_the_constraint_cannot_satisfy_it():
    """Rule 6: a number that was never measured is not evidence that it passed."""
    phash = tune.full_point_hash(tune.combine(SAR, OFF), N_PRE)
    stale = EventScore(event="ctrl", kind=sb.KIND_CONTROL, params_hash=phash,
                       metrics={"false_flood_fraction": 0.0},  # no provenance field
                       provenance={})
    m = {("a", phash): entry("a", sb.KIND_FLOOD, phash, f1_tol=0.9),
         ("ctrl", phash): stale}
    sel = tune.select(m, ["a"], ["ctrl"], [tune.combine(SAR, OFF)], CEILING, N_PRE,
                      tune.full_point_hash)
    assert not sel.ok
    assert any("cannot demonstrate it" in (c.rejected or "") for c in sel.candidates)
    assert not tune.reusable(stale), "a stale control score must be recomputed"


def test_a_stale_flood_score_is_still_reusable():
    """The analysed-area field is a control diagnostic; floods never carried it."""
    phash = tune.full_point_hash(tune.combine(SAR, OFF), N_PRE)
    flood = entry("a", sb.KIND_FLOOD, phash, f1_tol=0.9)
    assert flood.provenance.get("false_flood_fraction_analysed_area") is None
    assert tune.reusable(flood)


# --- the extent axis ----------------------------------------------------------

def test_the_extent_flag_reaches_the_materialised_params_and_the_hash():
    point = tune.combine(SAR, {"optical.fusion": FUSION_OFF,
                               "extent.debris_in_flood_extent": False})
    _, _, _, ep = tune.materialise_full(point, N_PRE)
    assert ep is not None and ep.debris_in_flood_extent is False
    assert ep.flood_extent_classes() == (2, 3), "debris must be out of the extent"


def test_the_baseline_extent_value_does_not_move_a_published_hash():
    """Baseline in, nothing added to params_dict: every published hash survives.

    The baseline is debris-IN, which every hash published before the option
    existed was measured at - NOT the shipped default, which moved to debris-OUT
    on 2026-10-12.
    """
    from strandline.config import EXTENT_HASH_BASELINE

    baseline = EXTENT_HASH_BASELINE.debris_in_flood_extent
    with_baseline = tune.combine(SAR, {"optical.fusion": FUSION_OFF,
                                       "extent.debris_in_flood_extent": baseline})
    without_key = tune.combine(SAR, {"optical.fusion": FUSION_OFF})
    assert tune.full_point_hash(with_baseline, N_PRE) == tune.full_point_hash(
        without_key, N_PRE)


def test_turning_debris_out_of_the_extent_is_its_own_configuration():
    a = tune.combine(SAR, {"optical.fusion": FUSION_OFF,
                           "extent.debris_in_flood_extent": True})
    b = tune.combine(SAR, {"optical.fusion": FUSION_OFF,
                           "extent.debris_in_flood_extent": False})
    assert tune.full_point_hash(a, N_PRE) != tune.full_point_hash(b, N_PRE)
    assert tune.extent_of(b) is False


# --- the second ceiling has no escape hatch ----------------------------------

def test_the_analysed_ceiling_cannot_be_switched_off_from_a_call_site():
    """It is a mandatory constraint, not an option with a default.

    An earlier draft accepted `analysed_ceiling=None` "for tests", which is an
    unenforced convention and the kind of hatch that becomes production
    behaviour. A test that wants the old behaviour must RAISE the ceiling
    explicitly, which is visible in the test.
    """
    import inspect

    sig = inspect.signature(tune.select)
    param = sig.parameters["analysed_ceiling"]
    assert param.default == CONTROL_ANALYSED_AREA_CEILING
    assert param.annotation == "float", "an optional type invites None again"

    phash = tune.full_point_hash(tune.combine(SAR, OFF), N_PRE)
    m = {("a", phash): entry("a", sb.KIND_FLOOD, phash, f1_tol=0.9),
         ("ctrl", phash): entry("ctrl", sb.KIND_CONTROL, phash, ffr=0.001,
                                ffr_analysed=0.05)}
    points = [tune.combine(SAR, OFF)]
    # Default: rejected on the analysed-area ceiling.
    assert not tune.select(m, ["a"], ["ctrl"], points, CEILING, N_PRE,
                           tune.full_point_hash).ok
    # Explicitly raised: accepted, and the raising is impossible to overlook.
    assert tune.select(m, ["a"], ["ctrl"], points, CEILING, N_PRE,
                       tune.full_point_hash, analysed_ceiling=1.0).ok


def test_one_reuse_rule_is_shared_by_every_cache_path():
    """A stale control score must be refused on the scoreboard path too."""
    from eval import run_scoreboard as rs

    assert tune.reusable is rs.reusable, (
        "two definitions of 'is this cached score usable' will drift apart")


# --- the frozen SAR fallback --------------------------------------------------

def test_the_frozen_sar_points_carry_only_the_sar_half():
    """The second pass searches the optical and extent halves; it must not
    inherit them from a previous choice."""
    frozen = tune.frozen_sar_points()
    if not frozen:
        pytest.skip("no recorded chosen_params.json in this tree")
    for fold, point in frozen.items():
        assert point, fold
        assert all(k.startswith(("detect.", "run.")) for k in point), (fold, point)


# --- the Pareto-dominance rule (declared 2026-10-12, AFTER the refusal) ------
#
# SYNTHETIC. The rule has no free parameters, so what these tests pin is that
# it cannot be made to trade one quantity for another.

INC = {"optical.fusion": FUSION_UNION, "optical.mndwi_water": 0.0,
       "optical.ndvi_drop_min": 0.2, "extent.debris_in_flood_extent": True}
CAND = {"optical.fusion": FUSION_UNION, "optical.mndwi_water": 0.0,
        "optical.ndvi_drop_min": 0.1, "extent.debris_in_flood_extent": False}
SARS = {"a": SAR, "b": SAR, "c": SAR, "": SAR}


def _dom_matrix(inc_f1, cand_f1, inc_ctrl, cand_ctrl) -> tune.Matrix:
    """Floods a,b,c and one control, at the incumbent and one candidate.

    `*_ctrl` are (gated, analysed) pairs. Every flood event gets the same F1 so
    each fold's training mean equals it, which keeps the arithmetic obvious.
    """
    spec = {}
    for event in ("a", "b", "c"):
        spec[event] = (sb.KIND_FLOOD, {key(SAR, INC): {"f1_tol": inc_f1},
                                       key(SAR, CAND): {"f1_tol": cand_f1}})
    spec["ctrl"] = (sb.KIND_CONTROL, {
        key(SAR, INC): {"ffr": inc_ctrl[0], "ffr_analysed": inc_ctrl[1]},
        key(SAR, CAND): {"ffr": cand_ctrl[0], "ffr_analysed": cand_ctrl[1]}})
    return matrix_of(spec)


def test_the_incumbent_is_read_from_the_shipped_defaults_and_is_a_grid_point():
    """So the comparison is against what the project actually ships."""
    inc = tune.incumbent_point()
    assert inc["optical.fusion"] == OpticalParams().fusion
    assert inc["optical.ndvi_drop_min"] == OpticalParams().ndvi_drop_min
    assert inc["extent.debris_in_flood_extent"] == ExtentParams().debris_in_flood_extent
    assert any(p == inc for p in tune.optical_grid_points()), (
        "the incumbent must be a point of the declared grid, or it cannot be compared")


def test_a_candidate_better_everywhere_dominates():
    m = _dom_matrix(0.50, 0.55, (0.0022, 0.0437), (0.0001, 0.0028))
    out = tune.select_by_dominance(m, [INC, CAND], ["a", "b", "c"], ["ctrl"], SARS,
                                   N_PRE, incumbent=INC)
    assert out["chosen"] == CAND
    assert out["comparisons"][tune.point_label(CAND)]["dominates"] is True


def test_a_candidate_that_gains_f1_but_worsens_any_control_is_rejected():
    """The trade the rule exists to forbid - and the mean would have hidden it."""
    m = _dom_matrix(0.50, 0.90, (0.0022, 0.0437), (0.0022, 0.0440))
    out = tune.select_by_dominance(m, [INC, CAND], ["a", "b", "c"], ["ctrl"], SARS,
                                   N_PRE, incumbent=INC)
    assert out["chosen"] is None
    cmp = out["comparisons"][tune.point_label(CAND)]
    assert cmp["dominates"] is False
    assert any("analysed false flood" in w for w in cmp["worse_on"])


def test_a_candidate_that_lowers_false_alarms_but_loses_f1_is_rejected():
    """Symmetrically forbidden: no quantity may be traded for another."""
    m = _dom_matrix(0.50, 0.49, (0.0022, 0.0437), (0.0000, 0.0000))
    out = tune.select_by_dominance(m, [INC, CAND], ["a", "b", "c"], ["ctrl"], SARS,
                                   N_PRE, incumbent=INC)
    assert out["chosen"] is None
    assert any("mean train F1" in w
               for w in out["comparisons"][tune.point_label(CAND)]["worse_on"])


def test_an_exact_tie_everywhere_is_not_strictly_better_and_is_not_adopted():
    """"At least as good" is not enough; the rule needs one strict improvement."""
    m = _dom_matrix(0.50, 0.50, (0.0022, 0.0437), (0.0022, 0.0437))
    out = tune.select_by_dominance(m, [INC, CAND], ["a", "b", "c"], ["ctrl"], SARS,
                                   N_PRE, incumbent=INC)
    assert out["chosen"] is None
    cmp = out["comparisons"][tune.point_label(CAND)]
    assert cmp["worse_on"] == [] and cmp["strictly_better_count"] == 0


def test_the_f1_comparison_uses_training_events_not_the_held_out_one():
    """The correction /audit forced: never select on held-out scores.

    Event `c` is the only one where the candidate is worse. In fold `c` - where
    `c` is HELD OUT - the training mean over a and b favours the candidate, so a
    held-out comparison would see nothing wrong; the training comparison in
    folds `a` and `b`, which DO train on `c`, is what rejects it.
    """
    spec = {}
    for event, (inc_f1, cand_f1) in {"a": (0.50, 0.60), "b": (0.50, 0.60),
                                     "c": (0.50, 0.10)}.items():
        spec[event] = (sb.KIND_FLOOD, {key(SAR, INC): {"f1_tol": inc_f1},
                                       key(SAR, CAND): {"f1_tol": cand_f1}})
    spec["ctrl"] = (sb.KIND_CONTROL, {key(SAR, INC): {"ffr": 0.001, "ffr_analysed": 0.004},
                                      key(SAR, CAND): {"ffr": 0.001, "ffr_analysed": 0.004}})
    m = matrix_of(spec)
    out = tune.select_by_dominance(m, [INC, CAND], ["a", "b", "c"], ["ctrl"], SARS,
                                   N_PRE, incumbent=INC)
    assert out["chosen"] is None, "a fold that TRAINS on the bad event must reject it"
    worse = out["comparisons"][tune.point_label(CAND)]["worse_on"]
    assert any(w.startswith("a:") for w in worse) and any(w.startswith("b:") for w in worse)
    assert not any(w.startswith("c:") for w in worse), (
        "fold c trains on a and b only, where the candidate is better")


def test_a_missing_score_makes_a_candidate_incomparable_not_dominant():
    m = _dom_matrix(0.50, 0.55, (0.0022, 0.0437), (0.0001, 0.0028))
    del m[("b", tune.full_point_hash(tune.combine(SAR, CAND), N_PRE))]
    out = tune.select_by_dominance(m, [INC, CAND], ["a", "b", "c"], ["ctrl"], SARS,
                                   N_PRE, incumbent=INC)
    assert out["chosen"] is None
    assert "incomparable" in out["comparisons"][tune.point_label(CAND)]


def test_ties_are_broken_by_fewer_false_alarms():
    """Two dominating candidates; the one with the lower analysed-area rate wins."""
    other = {"optical.fusion": FUSION_WATER_ONLY, "optical.mndwi_water": 0.0,
             "extent.debris_in_flood_extent": False}
    spec = {}
    for event in ("a", "b", "c"):
        spec[event] = (sb.KIND_FLOOD, {key(SAR, INC): {"f1_tol": 0.50},
                                       key(SAR, CAND): {"f1_tol": 0.55},
                                       key(SAR, other): {"f1_tol": 0.55}})
    spec["ctrl"] = (sb.KIND_CONTROL, {
        key(SAR, INC): {"ffr": 0.0022, "ffr_analysed": 0.0437},
        key(SAR, CAND): {"ffr": 0.0010, "ffr_analysed": 0.0100},
        key(SAR, other): {"ffr": 0.0005, "ffr_analysed": 0.0050}})
    out = tune.select_by_dominance(matrix_of(spec), [INC, CAND, other],
                                   ["a", "b", "c"], ["ctrl"], SARS, N_PRE, incumbent=INC)
    assert out["chosen"] == other, "the tie-break must prefer fewer false alarms"
    assert len(out["dominating"]) == 2


def test_the_rule_reports_that_it_makes_nothing_feasible():
    """It must never be mistaken for passing the stricter criterion."""
    m = _dom_matrix(0.50, 0.55, (0.0022, 0.0437), (0.0001, 0.0028))
    out = tune.select_by_dominance(m, [INC, CAND], ["a", "b", "c"], ["ctrl"], SARS,
                                   N_PRE, incumbent=INC)
    assert "remains FAILED" in out["does_not_make_anything_feasible"]
    assert "1.64%" in out["does_not_make_anything_feasible"]


def test_no_production_path_lifts_the_control_ceiling():
    """The hatch is for tests. An adoption path must never use it.

    An earlier version of the dominance branch rebuilt a Selection per fold with
    `select(..., analysed_ceiling=1.0)` purely to carry the chosen point, and so
    used in production the escape hatch `select`'s own docstring reserves for
    tests. /audit caught it. The rows are now read straight from the matrix,
    because by that point the rule has already chosen and there is nothing left
    to select.
    """
    import ast

    # Parsed, not grepped: the first version of this test used a regex over the
    # source with comments stripped, and matched the sentence in `select`'s own
    # DOCSTRING that explains why the hatch exists. A docstring is not a call.
    tree = ast.parse((ROOT / "eval" / "tune.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "analysed_ceiling":
                continue
            value = kw.value
            if isinstance(value, ast.Constant) and value.value in (1.0, 1, None):
                offenders.append(getattr(node, "lineno", "?"))
    assert not offenders, (
        f"eval/tune.py lifts or disables the analysed-area control ceiling at "
        f"line(s) {offenders}; that hatch is for tests only")


def test_the_extent_default_is_reported_by_extent_of():
    """`extent_of` answers 'what does this point say', defaulting to the SHIPPED
    value - which is debris-OUT since 2026-10-12."""
    from strandline.config import ExtentParams

    assert tune.extent_of({}) is ExtentParams().debris_in_flood_extent
    assert tune.extent_of({"extent.debris_in_flood_extent": True}) is True
    assert tune.extent_of({"extent.debris_in_flood_extent": False}) is False


# --- the adoption record must not be erased by a run that adopts nothing ----

def test_a_run_that_adopts_nothing_may_not_overwrite_an_adoption_record(tmp_path):
    """The rows in flood.csv are keyed to that record.

    This happened: re-running the pass on a clean tree, to refresh a provenance
    hash, replaced the adoption record with a run that correctly adopted nothing
    - because by then the shipped configuration WAS the adopted one and so could
    not be dominated. The published rows were left describing nothing.
    """
    import json

    path = tmp_path / "chosen_params.json"
    adopted = {"run_id": "A", "optical_pass": {"dominance": {"chosen": {"x": 1}}}}
    path.write_text(json.dumps(adopted), encoding="utf-8")

    nothing = {"run_id": "B", "chosen_parameters": None,
               "optical_pass": {"dominance": {"chosen": None},
                                "adoption": {"keep_fusion": False}}}
    refusal = tune.overwrite_refusal(path, nothing)
    assert refusal and "adopted nothing" in refusal
    assert "A" in refusal, "the refusal must name the record it is protecting"

    # A run that DID adopt may overwrite it, and so may any run if the existing
    # record adopted nothing itself.
    assert tune.overwrite_refusal(path, adopted) is None
    path.write_text(json.dumps(nothing), encoding="utf-8")
    assert tune.overwrite_refusal(path, nothing) is None
    assert tune.overwrite_refusal(tmp_path / "absent.json", nothing) is None


def test_adopted_something_recognises_every_declared_route():
    assert tune.adopted_something({"chosen_parameters": {"params_hash": "x"}})
    assert tune.adopted_something(
        {"optical_pass": {"dominance": {"chosen": {"optical.fusion": "union"}}}})
    assert tune.adopted_something({"optical_pass": {"adoption": {"keep_fusion": True}}})
    assert not tune.adopted_something(
        {"chosen_parameters": None,
         "optical_pass": {"dominance": {"chosen": None},
                          "adoption": {"keep_fusion": False}}})
    assert not tune.adopted_something({})
