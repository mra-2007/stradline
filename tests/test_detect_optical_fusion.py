"""How Sentinel-2 evidence is allowed to act on the class map.

SYNTHETIC: the valley, the radar flood and the optical masks are all generated
(tests/conftest.py), so nothing here is a real-world accuracy figure (Rule 3.8).
What is established:

  - with no optical evidence, or with fusion off, the output is EXACTLY the
    radar-only output. Adding Sentinel-2 to this pipeline cannot have changed a
    single radar-only pixel.
  - an optical detection is held to the same standard of PLACE as a radar one:
    the height-above-drainage limit, the slope limit, the minimum mapping unit
    and the drainage-connection test all apply to it unchanged.
  - a pixel both sensors call water becomes high confidence.
  - the source layer says, per pixel, which sensor saw it.
"""

from dataclasses import replace
from pathlib import Path

import numpy as np

from strandline import detect
from strandline.config import (
    CLASS_DEBRIS,
    CLASS_FLOOD_HIGH,
    CLASS_FLOOD_MEDIUM,
    FUSION_OFF,
    FUSION_UNION,
    SOURCE_BOTH,
    SOURCE_NONE,
    SOURCE_OPTICAL,
    SOURCE_SAR,
    DetectParams,
    OpticalParams,
)
from strandline.optical import OpticalEvidence

ROOT = Path(__file__).resolve().parents[1]

BRIGHTENS = 6.0      # a gorge flood that radar sees only as medium confidence


def _run(scene_data, params, optical=None):
    g = scene_data["geometry"]
    pre_vv = np.stack([p[0] for p in scene_data["pre"]])
    pre_vh = np.stack([p[1] for p in scene_data["pre"]])
    return detect.detect_flood(pre_vv, pre_vh, scene_data["post"][0], scene_data["post"][1],
                               g["hand"].astype(float), scene_data["slope"], g["streams"],
                               params, optical=optical)


def _evidence(shape, water=None, debris=None, clear=None, fusion=FUSION_UNION):
    """Evidence carrying its own fusion mode, as OpticalParams.fusion sets it.

    The tests pass a fusion mode explicitly rather than reading the shipped
    default, so that what is tested here is the fusion MACHINERY and the tests
    do not all change meaning when the default does. The shipped default is
    pinned separately, in test_the_shipped_default_is_the_one_that_was_measured.
    """
    zeros = np.zeros(shape, dtype=bool)
    return OpticalEvidence(
        water=zeros.copy() if water is None else water,
        debris=zeros.copy() if debris is None else debris,
        clear=np.ones(shape, dtype=bool) if clear is None else clear,
        stats={}, fusion=fusion,
    )


def _patch(shape, g, rows, d_lo, d_hi):
    mask = np.zeros(shape, dtype=bool)
    mask[rows] = True
    return mask & (g["d"] >= d_lo) & (g["d"] <= d_hi)


# --- the invariant: radar-only output is untouched ----------------------------

def test_no_optical_evidence_leaves_the_radar_map_exactly_as_it_was(scene):
    data = scene()
    dp = DetectParams()
    bare = _run(data, dp)
    off = _run(data, dp, optical=_evidence(bare.classes.shape,
                                           water=np.ones(bare.classes.shape, dtype=bool),
                                           fusion=FUSION_OFF))
    assert np.array_equal(bare.classes, off.classes)
    assert bare.stats["pixels"] == off.stats["pixels"]


def test_the_shipped_default_is_the_one_that_was_measured():
    """The shipped fusion mode must be the one the optical pass actually adopted.

    This replaced `test_the_shipped_default_changes_no_pixel` on 2026-10-12,
    when the leave-one-event-out optical pass adopted `union` under the rule
    declared in config.OPTICAL_ADOPTION (held-out mean tolerant F1 0.5134 ->
    0.5232, no event worse by more than 0.0033, worst control 0.0022 against a
    1% ceiling). The old test pinned `off` with the reason "until the optical
    leave-one-event-out pass has run" - that condition is now satisfied, so the
    pin moves rather than disappears.

    It moves to something STRONGER: the default is no longer merely asserted
    here, it is checked against the recorded verdict in
    eval/chosen_params.json. A default that no measurement endorses now fails,
    and so does a measurement nobody shipped.
    """
    import json

    chosen = json.loads((ROOT / "eval" / "chosen_params.json").read_text(encoding="utf-8"))
    optical_pass = chosen.get("optical_pass") or {}
    if optical_pass.get("status") != "scored":
        assert OpticalParams().fusion == FUSION_OFF, (
            "no optical pass has been scored, so fusion must be off: shipping a fusion "
            "mode no measurement endorses is exactly what OPTICAL_ADOPTION forbids")
        return
    adopted = optical_pass["adoption"]["keep_fusion"]
    assert OpticalParams().fusion == (FUSION_UNION if adopted else FUSION_OFF), (
        f"eval/chosen_params.json records keep_fusion={adopted}, but the shipped "
        f"OpticalParams.fusion is {OpticalParams().fusion!r}")
    if adopted:
        point = optical_pass["chosen_configuration"]
        for key, value in point.items():
            if key.startswith("optical."):
                field = key.split(".", 1)[1]
                assert getattr(OpticalParams(), field) == value, (
                    f"the optical pass adopted {field}={value} but config.py ships "
                    f"{getattr(OpticalParams(), field)!r}")


def test_fusion_off_still_changes_no_pixel(scene):
    """`off` remains available and remains exactly off, whatever the default is.

    It is one of the two values OPTICAL_TUNE_GRIDS searches and the comparison
    the adoption rule rests on, so it has to keep meaning what it says.
    """
    data = scene()
    shape = data["geometry"]["dem"].shape
    off = _evidence(shape, water=np.ones(shape, dtype=bool),
                    debris=np.ones(shape, dtype=bool), fusion=FUSION_OFF)
    assert np.array_equal(_run(data, DetectParams()).classes,
                          _run(data, DetectParams(), optical=off).classes)


def test_fusion_off_ignores_even_a_wall_to_wall_optical_detection(scene):
    """`off` must really be off: it is one of the two values the grid searches."""
    data = scene()
    shape = data["geometry"]["dem"].shape
    everywhere = np.ones(shape, dtype=bool)
    dp = DetectParams()
    result = _run(data, dp, optical=_evidence(shape, water=everywhere, debris=everywhere,
                                              fusion=FUSION_OFF))
    bare = _run(data, dp)
    assert np.array_equal(result.classes, bare.classes)
    assert result.stats["optical_used"] is False


def test_without_optical_every_detected_pixel_is_attributed_to_radar(scene):
    data = scene()
    result = _run(data, DetectParams())
    affected = np.isin(result.classes, [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM, CLASS_DEBRIS])
    assert affected.any()
    assert np.all(result.source[affected] == SOURCE_SAR)
    assert np.all(result.source[~affected] == SOURCE_NONE)


# --- optical evidence is held to the same standard of place -------------------

def test_optical_water_above_the_hand_limit_is_rejected(scene):
    """A patch high on the valley wall: radar could not be flood there, nor can optical."""
    data = scene()
    g = data["geometry"]
    shape = g["dem"].shape
    wall = _patch(shape, g, slice(60, 180), 25, 35)       # HAND 35-105 m
    assert (g["hand"][wall] > DetectParams().hand_max_water_m).all()
    result = _run(data, DetectParams(), optical=_evidence(shape, water=wall))
    assert not np.isin(result.classes[wall], [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM]).any()


def test_optical_water_disconnected_from_drainage_is_rejected(scene):
    """On the valley floor, but nowhere near a channel. Real floods connect."""
    data = scene()
    g = data["geometry"]
    shape = g["dem"].shape
    island = _patch(shape, g, slice(10, 30), 10, 17)      # floor, but 10+ px from the river
    result = _run(data, DetectParams(), optical=_evidence(shape, water=island))
    assert not np.isin(result.classes[island], [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM]).any()


def test_optical_water_below_the_minimum_mapping_unit_is_removed(scene):
    data = scene()
    g = data["geometry"]
    shape = g["dem"].shape
    speck = np.zeros(shape, dtype=bool)
    speck[30, 4] = True                                   # beside the river, 1 px
    result = _run(data, DetectParams(), optical=_evidence(shape, water=speck))
    assert not np.isin(result.classes[speck], [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM]).any()


def test_optical_water_is_ignored_where_the_sky_was_not_clear(scene):
    data = scene()
    g = data["geometry"]
    shape = g["dem"].shape
    strip = _patch(shape, g, slice(10, 40), 4, 17)
    clouded = np.ones(shape, dtype=bool)
    clouded[slice(10, 40)] = False
    result = _run(data, DetectParams(), optical=_evidence(shape, water=strip, clear=clouded))
    assert not np.isin(result.classes[strip], [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM]).any()


def test_optical_water_connected_to_the_channel_is_added_and_credited(scene):
    """A reach radar missed entirely, upstream of the radar flood, beside the river."""
    data = scene(event=False)                             # radar sees no flood at all
    g = data["geometry"]
    shape = g["dem"].shape
    reach = _patch(shape, g, slice(60, 120), 1, 17)       # touches the channel
    bare = _run(data, DetectParams())
    assert not np.isin(bare.classes, [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM]).any()

    result = _run(data, DetectParams(), optical=_evidence(shape, water=reach))
    added = np.isin(result.classes, [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM]) & reach
    assert added.sum() > 100
    assert np.all(result.source[added] == SOURCE_OPTICAL)
    assert result.stats["optical_used"] is True


# --- agreement -----------------------------------------------------------------

def test_agreement_between_the_two_sensors_raises_confidence(scene):
    """Radar alone calls a brightening gorge flood MEDIUM; optical agreement makes it HIGH."""
    data = scene(flood_delta_db=BRIGHTENS)
    g = data["geometry"]
    shape = g["dem"].shape
    dp = DetectParams()

    bare = _run(data, dp)
    medium_only = g["flood"] & (bare.classes == CLASS_FLOOD_MEDIUM)
    assert medium_only.sum() > 100, "fixture must give radar-only medium confidence here"
    assert not (g["flood"] & (bare.classes == CLASS_FLOOD_HIGH)).any()

    fused = _run(data, dp, optical=_evidence(shape, water=g["flood"].copy()))
    promoted = medium_only & (fused.classes == CLASS_FLOOD_HIGH)
    assert promoted.sum() > 100
    assert np.all(fused.source[promoted] == SOURCE_BOTH)


def test_fusion_never_shrinks_the_flood_affected_area(scene):
    """Union means union: optical can add evidence, never remove a radar detection."""
    data = scene()
    g = data["geometry"]
    shape = g["dem"].shape
    classes = [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM, CLASS_DEBRIS]
    bare = _run(data, DetectParams())
    fused = _run(data, DetectParams(),
                 optical=_evidence(shape, water=_patch(shape, g, slice(60, 120), 1, 17)))
    assert np.isin(fused.classes, classes).sum() >= np.isin(bare.classes, classes).sum()


# --- deposits ------------------------------------------------------------------

def test_optical_deposits_are_mapped_in_the_debris_zone_only(scene):
    """Deposits may sit above the water zone, but not above the debris zone."""
    data = scene(event=False)
    g = data["geometry"]
    shape = g["dem"].shape
    dp = replace(DetectParams(), require_drainage_connection=False)

    fan = _patch(shape, g, slice(100, 160), 21, 25)       # HAND 7-35 m: debris zone
    too_high = _patch(shape, g, slice(100, 160), 30, 40)  # HAND 70-140 m: nowhere
    result = _run(data, dp, optical=_evidence(shape, debris=fan | too_high))
    assert (result.classes[fan] == CLASS_DEBRIS).sum() > 100
    assert not (result.classes[too_high] == CLASS_DEBRIS).any()


def test_optical_deposits_must_still_connect_to_water_or_drainage(scene):
    data = scene(event=False)
    g = data["geometry"]
    shape = g["dem"].shape
    fan = _patch(shape, g, slice(100, 160), 21, 25)
    result = _run(data, DetectParams(), optical=_evidence(shape, debris=fan))
    assert not (result.classes[fan] == CLASS_DEBRIS).any()


def test_a_pixel_is_never_both_water_and_debris(scene):
    data = scene()
    g = data["geometry"]
    shape = g["dem"].shape
    both = _patch(shape, g, slice(60, 120), 1, 17)
    result = _run(data, DetectParams(), optical=_evidence(shape, water=both, debris=both))
    assert not ((result.classes == CLASS_DEBRIS)
                & np.isin(result.classes, [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM])).any()


# --- determinism and reporting -------------------------------------------------

def test_fusion_is_deterministic(scene):
    data = scene()
    g = data["geometry"]
    shape = g["dem"].shape
    ev = _evidence(shape, water=_patch(shape, g, slice(60, 120), 1, 17))
    a = _run(data, DetectParams(), optical=ev)
    b = _run(data, DetectParams(), optical=ev)
    assert np.array_equal(a.classes, b.classes) and np.array_equal(a.source, b.source)


def test_source_pixel_counts_are_reported(scene):
    data = scene()
    g = data["geometry"]
    shape = g["dem"].shape
    result = _run(data, DetectParams(),
                  optical=_evidence(shape, water=_patch(shape, g, slice(60, 120), 1, 17)))
    counts = result.stats["source_pixels"]
    assert counts[SOURCE_NONE] > 0
    assert sum(counts.values()) == result.classes.size
    assert result.stats["optical_fusion"] == FUSION_UNION
