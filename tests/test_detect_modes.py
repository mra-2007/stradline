"""The two detection modes, on a flood that darkens and a flood that brightens.

SYNTHETIC: the valley, both floods and the bait are generated, so these numbers
test the logic and are never real-world accuracy (Rule 3.8). The real-world
question - which mode maps actual floods better - is answered only by
eval/tune.py over the dev set.

What is established here:
  - `darkening` (the default, unchanged) finds a darkening flood and MISSES a
    brightening one. That miss is the measured failure mode this work exists
    for, so it is pinned down as a test rather than left as an anecdote.
  - `rough_water` finds BOTH, because it tests the magnitude of a coherent
    change low on the valley floor rather than its sign.
  - `rough_water` does not buy that by calling everything flood: the dark bait
    patch high on the valley wall, the isolated speck and a dry scene all stay
    clean, and its debris class is far tighter than the old one.
  - `min_abs_change_db` really gates: a sub-threshold change is not detected.
"""

import numpy as np
import pytest
from dataclasses import replace

from strandline import detect
from strandline.config import (
    CLASS_DEBRIS,
    CLASS_FLOOD_HIGH,
    CLASS_FLOOD_MEDIUM,
    CLASS_NODATA,
    DETECT_MODES,
    FLOOD_AFFECTED_CLASSES,
    MODE_DARKENING,
    MODE_ROUGH_WATER,
    DetectParams,
)

RES = 20.0
DARKENS = -10.0      # open water, specular: -9 dB -> -19 dB
BRIGHTENS = 6.0      # turbulent sediment-laden gorge flood: -9 dB -> -3 dB


def run(scene_data, params: DetectParams):
    g = scene_data["geometry"]
    pre_vv = np.stack([p[0] for p in scene_data["pre"]])
    pre_vh = np.stack([p[1] for p in scene_data["pre"]])
    return detect.detect_flood(pre_vv, pre_vh, scene_data["post"][0], scene_data["post"][1],
                               g["hand"].astype(float), scene_data["slope"], g["streams"],
                               params)


def scores(result, truth):
    """Recall and precision of flood-affected area against a truth mask."""
    pred = np.isin(result.classes, list(FLOOD_AFFECTED_CLASSES))
    tp = int((pred & truth).sum())
    return {
        "recall": tp / max(int(truth.sum()), 1),
        "precision": tp / max(int(pred.sum()), 1),
        "pred_fraction": float(pred.mean()),
    }


def water_only(result):
    return np.isin(result.classes, [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM])


# --- the modes exist and the default is unchanged ----------------------------

def test_both_modes_are_available_and_the_default_is_the_chosen_one():
    """The default is the mode eval/tune.py chose by leave-one-event-out.

    It was MODE_DARKENING while the two modes were being compared. The
    comparison finished on 2026-10-11 (eval/chosen_params.json, run_id
    20261011T110254Z): rough_water wins on 5 of 6 held-out events and
    darkening is infeasible - all 18 of its grid points breach the 1% control
    ceiling. Both modes stay selectable, so the comparison is still runnable.
    """
    assert DETECT_MODES == (MODE_DARKENING, MODE_ROUGH_WATER)
    assert DetectParams().mode == MODE_ROUGH_WATER, \
        "the default must be the mode the tuner chose, not a hand-picked one"


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="not supported"):
        DetectParams(mode="magic")


# --- a flood that darkens: both modes must find it ---------------------------

@pytest.mark.parametrize("mode", DETECT_MODES)
def test_a_darkening_flood_is_found_by_both_modes(scene, mode):
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=DARKENS)
    result = run(data, DetectParams(mode=mode))
    s = scores(result, data["geometry"]["flood"])
    assert s["recall"] > 0.90, f"{mode}: recall {s['recall']:.3f} on a darkening flood"
    assert s["precision"] > 0.80, f"{mode}: precision {s['precision']:.3f}"


def test_the_darkening_mode_still_calls_dark_water_high_confidence(scene):
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=DARKENS)
    result = run(data, DetectParams(mode=MODE_DARKENING))
    assert (result.classes == CLASS_FLOOD_HIGH).sum() > 0


def test_rough_water_keeps_darkness_as_the_high_confidence_test(scene):
    """Dark water is still the strongest evidence, so it must still grade high."""
    dark = run(scene(event=True, n_pre=5, seed=0, flood_delta_db=DARKENS),
               DetectParams(mode=MODE_ROUGH_WATER))
    bright = run(scene(event=True, n_pre=5, seed=0, flood_delta_db=BRIGHTENS),
                 DetectParams(mode=MODE_ROUGH_WATER))
    assert (dark.classes == CLASS_FLOOD_HIGH).sum() > 0, "dark water did not grade high"
    assert (bright.classes == CLASS_FLOOD_HIGH).sum() == 0, \
        "water that brightened must not be called high confidence: it is not specular"
    assert (bright.classes == CLASS_FLOOD_MEDIUM).sum() > 0


# --- a flood that brightens: the whole point --------------------------------

def test_the_darkening_mode_misses_a_brightening_flood(scene):
    """The measured failure mode (EMSR770), pinned as a test.

    Not a bug in `darkening` - it is what "open water is dark" means. It is the
    reason `rough_water` exists.
    """
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=BRIGHTENS)
    result = run(data, DetectParams(mode=MODE_DARKENING))
    truth = data["geometry"]["flood"]
    assert water_only(result)[truth].mean() < 0.05, \
        "darkening mode unexpectedly found a brightening flood as water"


def test_rough_water_finds_a_brightening_flood(scene):
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=BRIGHTENS)
    result = run(data, DetectParams(mode=MODE_ROUGH_WATER))
    s = scores(result, data["geometry"]["flood"])
    assert s["recall"] > 0.90, f"recall {s['recall']:.3f} on a brightening flood"
    assert s["precision"] > 0.80, f"precision {s['precision']:.3f}"


def test_rough_water_calls_a_brightening_flood_water_not_debris(scene):
    """Terrain position decides the class: this change is on the valley floor."""
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=BRIGHTENS)
    result = run(data, DetectParams(mode=MODE_ROUGH_WATER))
    truth = data["geometry"]["flood"]
    assert water_only(result)[truth].mean() > 0.90
    assert (result.classes[truth] == CLASS_DEBRIS).mean() < 0.05


@pytest.mark.parametrize("delta", [DARKENS, BRIGHTENS])
def test_rough_water_is_symmetric_in_the_sign_of_the_change(scene, delta):
    """Equal and opposite changes of the same size must behave the same."""
    data = scene(event=True, n_pre=5, seed=1, flood_delta_db=delta)
    result = run(data, DetectParams(mode=MODE_ROUGH_WATER))
    assert scores(result, data["geometry"]["flood"])["recall"] > 0.90


# --- it must not buy recall with false alarms --------------------------------

@pytest.mark.parametrize("mode", DETECT_MODES)
def test_the_mountain_bait_is_never_called_flood(scene, mode):
    """A dark patch high on a valley wall is radar shadow or wet snow."""
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=BRIGHTENS)
    result = run(data, DetectParams(mode=mode))
    bait = data["geometry"]["bait"]
    affected = np.isin(result.classes, list(FLOOD_AFFECTED_CLASSES))
    assert affected[bait].sum() == 0, f"{mode}: called the mountain bait flood-affected"


@pytest.mark.parametrize("mode", DETECT_MODES)
def test_an_isolated_speck_is_removed(scene, mode):
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=BRIGHTENS)
    result = run(data, DetectParams(mode=mode))
    speck = data["geometry"]["speck"]
    assert not np.isin(result.classes[speck], list(FLOOD_AFFECTED_CLASSES)).any()


@pytest.mark.parametrize("mode", DETECT_MODES)
def test_a_dry_scene_produces_almost_no_flood_affected_area(scene, mode):
    """The control gate in miniature: no event means almost nothing called flood."""
    data = scene(event=False, n_pre=5, seed=2)
    result = run(data, DetectParams(mode=mode))
    affected = np.isin(result.classes, list(FLOOD_AFFECTED_CLASSES))
    domain = result.classes != CLASS_NODATA
    fraction = affected.sum() / max(int(domain.sum()), 1)
    assert fraction <= 0.01, f"{mode}: false flood-affected fraction {fraction:.4f} > 1%"


@pytest.mark.parametrize("mode", DETECT_MODES)
def test_a_dry_scene_with_speckle_stays_clean(scene, mode):
    data = scene(event=False, speckle=True, n_pre=5, seed=3)
    pre_vv = np.stack([detect.preprocess(p[0], DetectParams()) for p in data["pre"]])
    pre_vh = np.stack([detect.preprocess(p[1], DetectParams()) for p in data["pre"]])
    g = data["geometry"]
    result = detect.detect_flood(
        pre_vv, pre_vh, detect.preprocess(data["post"][0], DetectParams()),
        detect.preprocess(data["post"][1], DetectParams()),
        g["hand"].astype(float), data["slope"], g["streams"], DetectParams(mode=mode))
    affected = np.isin(result.classes, list(FLOOD_AFFECTED_CLASSES))
    domain = result.classes != CLASS_NODATA
    assert affected.sum() / max(int(domain.sum()), 1) <= 0.01


# --- the debris channel is tighter -------------------------------------------

def test_rough_water_debris_is_far_tighter_than_the_old_channel(scene):
    """The measured complaint about the old channel: 3.8% of an AOI at
    precision 0.0062. Here the same scene must yield a smaller debris class."""
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=DARKENS)
    old = run(data, DetectParams(mode=MODE_DARKENING))
    new = run(data, DetectParams(mode=MODE_ROUGH_WATER))
    old_px = int((old.classes == CLASS_DEBRIS).sum())
    new_px = int((new.classes == CLASS_DEBRIS).sum())
    assert new_px <= old_px, f"debris grew from {old_px} to {new_px} px"


def test_the_debris_fan_is_still_found_somewhere_in_the_union(scene):
    """Tightening must not throw the real debris fan away."""
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=DARKENS)
    result = run(data, DetectParams(mode=MODE_ROUGH_WATER))
    fan = data["geometry"]["debris"]
    affected = np.isin(result.classes, list(FLOOD_AFFECTED_CLASSES))
    assert affected[fan].mean() > 0.50, \
        f"only {affected[fan].mean():.2f} of the debris fan survived the tightening"


# --- the new parameters actually do something --------------------------------

def test_min_abs_change_db_gates_a_small_change(scene):
    """A 1 dB change must not be detected when 3 dB is required.

    This is the guard against the old failure: a z-score alone would call this
    a 2-sigma change, because std_floor_db is 0.5 dB.
    """
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=1.0)
    strict = run(data, DetectParams(mode=MODE_ROUGH_WATER, min_abs_change_db=3.0))
    loose = run(data, DetectParams(mode=MODE_ROUGH_WATER, min_abs_change_db=0.2))
    truth = data["geometry"]["flood"]
    assert scores(strict, truth)["recall"] < 0.10, "the 3 dB gate let a 1 dB change through"
    assert scores(loose, truth)["recall"] > scores(strict, truth)["recall"]


def test_pol_agreement_rejects_a_change_in_one_polarisation_only(scene):
    """A real surface change moves VV and VH together."""
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=BRIGHTENS)
    g = data["geometry"]
    pre_vv = np.stack([p[0] for p in data["pre"]])
    pre_vh = np.stack([p[1] for p in data["pre"]])
    post_vv, post_vh = data["post"]
    # Undo the VH part of the change: VV brightens, VH does not.
    post_vh = post_vh.copy()
    post_vh[g["flood"]] -= BRIGHTENS

    result = detect.detect_flood(pre_vv, pre_vh, post_vv, post_vh,
                                 g["hand"].astype(float), data["slope"], g["streams"],
                                 DetectParams(mode=MODE_ROUGH_WATER))
    assert scores(result, g["flood"])["recall"] < 0.20, \
        "a one-polarisation change was accepted as flood"


def test_hand_widening_enlarges_the_analysed_zone_not_the_false_alarms(scene):
    """The terrain axis in the grid: a wider zone must not flood a dry scene."""
    dry = scene(event=False, n_pre=5, seed=4)
    wide = run(dry, DetectParams(mode=MODE_ROUGH_WATER, hand_max_water_m=35.0))
    narrow = run(dry, DetectParams(mode=MODE_ROUGH_WATER, hand_max_water_m=20.0))
    domain = wide.classes != CLASS_NODATA
    for result in (wide, narrow):
        affected = np.isin(result.classes, list(FLOOD_AFFECTED_CLASSES))
        assert affected.sum() / max(int(domain.sum()), 1) <= 0.01


def test_the_mode_is_recorded_in_the_detection_stats(scene):
    """Every run must say which mode produced it, for run_report.json."""
    data = scene(event=True, n_pre=5, seed=0)
    for mode in DETECT_MODES:
        assert run(data, DetectParams(mode=mode)).stats["mode"] == mode


def test_both_modes_are_deterministic(scene):
    data = scene(event=True, n_pre=5, seed=0, flood_delta_db=BRIGHTENS)
    for mode in DETECT_MODES:
        a = run(data, DetectParams(mode=mode))
        b = run(data, DetectParams(mode=mode))
        assert np.array_equal(a.classes, b.classes)
