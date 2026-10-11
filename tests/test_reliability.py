"""The narrow-valley reliability layer.

What it must do: mark valley-floor ground the sensor cannot resolve, from the
DEM alone, and report what share of the flood-affected area sits inside it.

What it must NOT do: change a single detected pixel.
"""

import json

import numpy as np
import pytest

from strandline import reliability
from strandline.config import (
    CLASS_DEBRIS,
    CLASS_FLOOD_HIGH,
    CLASS_FLOOD_MEDIUM,
    CLASS_NO_CHANGE,
    NARROW_VALLEY_CAVEAT,
    RELIABILITY_NARROW_VALLEY,
    RELIABILITY_OUTSIDE_VALLEY,
    RELIABILITY_RESOLVED,
    DetectParams,
    ReliabilityParams,
)

DP = DetectParams()
REL = ReliabilityParams()


def valley_strip(width_px: int, shape=(40, 40)):
    """A straight valley floor `width_px` pixels wide; walls either side."""
    hand = np.full(shape, 500.0)       # wall: far above any drainage
    slope = np.full(shape, 45.0)
    mid = shape[1] // 2
    lo = mid - width_px // 2
    hand[:, lo:lo + width_px] = 1.0    # floor: low and flat
    slope[:, lo:lo + width_px] = 2.0
    return hand, slope


# --- the width test itself ----------------------------------------------------

@pytest.mark.parametrize("width_px", [1, 2])
def test_a_floor_narrower_than_the_threshold_is_all_low_confidence(width_px):
    hand, slope = valley_strip(width_px)
    floor = reliability.valley_floor_mask(hand, slope, DP)
    narrow = reliability.narrow_valley_mask(floor, min_width_px=3.0)
    assert narrow[floor].all(), f"a {width_px} px floor was called resolved"
    assert not narrow[~floor].any(), "a non-floor pixel was marked"


@pytest.mark.parametrize("width_px", [6, 12])
def test_a_floor_well_wider_than_the_threshold_is_resolved(width_px):
    hand, slope = valley_strip(width_px)
    floor = reliability.valley_floor_mask(hand, slope, DP)
    narrow = reliability.narrow_valley_mask(floor, min_width_px=3.0)
    # The outermost pixel of a floor is within half a disc of the wall, so the
    # interior is what must come out resolved, not literally every pixel.
    interior = floor & (np.cumsum(floor, axis=1) > 1) & (np.cumsum(floor[:, ::-1],
                                                                   axis=1)[:, ::-1] > 1)
    assert not narrow[interior].any(), f"a {width_px} px floor interior was called narrow"


def test_the_threshold_is_configurable_and_monotone():
    """Raising the width threshold can only ever mark more ground, never less."""
    hand, slope = valley_strip(5)
    floor = reliability.valley_floor_mask(hand, slope, DP)
    masks = [reliability.narrow_valley_mask(floor, w) for w in (1.0, 3.0, 7.0, 15.0)]
    counts = [int(m.sum()) for m in masks]
    assert counts == sorted(counts), counts
    assert counts[0] < counts[-1], "the threshold made no difference at all"
    for tighter, wider in zip(masks, masks[1:]):
        assert not (tighter & ~wider).any(), "a pixel un-marked by a wider threshold"
    assert masks[-1][floor].all(), "a 15 px threshold must condemn a 5 px floor entirely"


def test_a_wide_valley_that_pinches_is_marked_only_at_the_pinch():
    hand = np.full((40, 40), 500.0)
    slope = np.full((40, 40), 45.0)
    hand[:, 14:26] = 1.0            # 12 px wide floor
    slope[:, 14:26] = 2.0
    hand[18:22, 14:24] = 500.0      # pinch it to 2 px over four rows
    slope[18:22, 14:24] = 45.0

    floor = reliability.valley_floor_mask(hand, slope, DP)
    narrow = reliability.narrow_valley_mask(floor, min_width_px=3.0)

    assert narrow[19, 25], "the 2 px pinch was not marked"
    assert not narrow[5, 20], "the wide part was marked because of a pinch elsewhere"


def test_an_empty_or_fully_narrow_floor_does_not_crash():
    shape = (10, 10)
    empty = np.zeros(shape, dtype=bool)
    assert not reliability.narrow_valley_mask(empty, 3.0).any()
    hand, slope = valley_strip(1, shape)
    floor = reliability.valley_floor_mask(hand, slope, DP)
    assert reliability.narrow_valley_mask(floor, 3.0)[floor].all()


def test_a_non_positive_width_is_refused():
    floor = np.ones((5, 5), dtype=bool)
    with pytest.raises(ValueError, match="must be positive"):
        reliability.narrow_valley_mask(floor, 0.0)
    with pytest.raises(ValueError, match="must be positive"):
        ReliabilityParams(min_valley_width_px=-1.0)


# --- it is DEM-only and changes nothing ---------------------------------------

def test_the_layer_uses_the_detectors_own_valley_floor_definition():
    """A confidence statement must qualify the ground the detector analysed."""
    hand = np.array([[1.0, 1.0], [1.0, 100.0]])
    slope = np.array([[2.0, 40.0], [2.0, 2.0]])
    floor = reliability.valley_floor_mask(hand, slope, DP)
    assert floor[0, 0] and floor[1, 0]
    assert not floor[0, 1], "a steep pixel is not valley floor"
    assert not floor[1, 1], "a high-HAND pixel is not valley floor"


def test_nan_terrain_is_never_valley_floor():
    hand = np.array([[np.nan, 1.0]])
    slope = np.array([[2.0, np.nan]])
    assert not reliability.valley_floor_mask(hand, slope, DP).any()


def test_computing_the_layer_does_not_touch_the_class_map():
    hand, slope = valley_strip(2)
    classes = np.full(hand.shape, CLASS_NO_CHANGE, dtype=np.uint8)
    classes[:, 19:21] = CLASS_FLOOD_MEDIUM
    before = classes.copy()
    reliability.compute_reliability(hand, slope, classes, DP, REL, pixel_size_m=20.0)
    assert np.array_equal(classes, before), "the reliability layer mutated the classes"


def test_the_layer_is_deterministic():
    hand, slope = valley_strip(3)
    classes = np.full(hand.shape, CLASS_NO_CHANGE, dtype=np.uint8)
    a = reliability.compute_reliability(hand, slope, classes, DP, REL, 20.0)
    b = reliability.compute_reliability(hand, slope, classes, DP, REL, 20.0)
    assert np.array_equal(a.codes, b.codes)
    assert a.stats == b.stats


# --- the reported share -------------------------------------------------------

def test_the_share_of_flood_affected_area_inside_the_zone_is_reported():
    hand, slope = valley_strip(2)                 # the whole floor is narrow
    classes = np.full(hand.shape, CLASS_NO_CHANGE, dtype=np.uint8)
    floor = reliability.valley_floor_mask(hand, slope, DP)
    cols = np.flatnonzero(floor.any(axis=0))
    classes[0:10, cols[0]] = CLASS_FLOOD_MEDIUM   # 10 px inside the narrow zone
    classes[0:10, 0] = CLASS_FLOOD_HIGH           # 10 px on the wall, outside it

    rel = reliability.compute_reliability(hand, slope, classes, DP, REL, 20.0)
    st = rel.stats
    assert st["flood_affected_pixels"] == 20
    assert st["flood_affected_pixels_in_narrow_valley"] == 10
    assert st["flood_affected_fraction_in_narrow_valley"] == pytest.approx(0.5)
    assert st["flood_affected_ha_in_narrow_valley"] == pytest.approx(10 * 400 / 10_000)


def test_debris_counts_as_flood_affected_in_the_share():
    """The share must qualify the primary scored quantity, flood UNION debris."""
    hand, slope = valley_strip(2)
    classes = np.full(hand.shape, CLASS_NO_CHANGE, dtype=np.uint8)
    cols = np.flatnonzero(reliability.valley_floor_mask(hand, slope, DP).any(axis=0))
    classes[0:4, cols[0]] = CLASS_DEBRIS
    st = reliability.compute_reliability(hand, slope, classes, DP, REL, 20.0).stats
    assert st["flood_affected_pixels"] == 4
    assert st["flood_affected_fraction_in_narrow_valley"] == pytest.approx(1.0)


def test_no_detection_reports_no_share_rather_than_zero():
    """A 0.0 would read as 'none of it is uncertain'. There is nothing to qualify."""
    hand, slope = valley_strip(2)
    classes = np.full(hand.shape, CLASS_NO_CHANGE, dtype=np.uint8)
    st = reliability.compute_reliability(hand, slope, classes, DP, REL, 20.0).stats
    assert st["flood_affected_pixels"] == 0
    assert st["flood_affected_fraction_in_narrow_valley"] is None


def test_the_stats_record_the_parameters_and_the_width_in_metres():
    hand, slope = valley_strip(2)
    classes = np.zeros(hand.shape, dtype=np.uint8)
    st = reliability.compute_reliability(hand, slope, classes, DP,
                                         ReliabilityParams(min_valley_width_px=3.0), 20.0).stats
    assert st["parameters"] == {"min_valley_width_px": 3.0}
    assert st["min_valley_width_m"] == pytest.approx(60.0)
    assert st["narrow_valley_ha"] > 0
    assert 0.0 < st["narrow_valley_fraction_of_valley_floor"] <= 1.0


def test_the_codes_separate_outside_resolved_and_narrow():
    hand = np.full((40, 40), 500.0)
    slope = np.full((40, 40), 45.0)
    hand[:, 10:22] = 1.0            # wide floor
    slope[:, 10:22] = 2.0
    hand[:, 30:31] = 1.0            # a separate 1 px floor
    slope[:, 30:31] = 2.0
    classes = np.zeros(hand.shape, dtype=np.uint8)

    rel = reliability.compute_reliability(hand, slope, classes, DP, REL, 20.0)
    assert rel.codes[0, 0] == RELIABILITY_OUTSIDE_VALLEY
    assert rel.codes[0, 16] == RELIABILITY_RESOLVED
    assert rel.codes[0, 30] == RELIABILITY_NARROW_VALLEY
    assert set(np.unique(rel.codes)) == {RELIABILITY_OUTSIDE_VALLEY, RELIABILITY_RESOLVED,
                                         RELIABILITY_NARROW_VALLEY}


# --- the written layer --------------------------------------------------------

def test_the_geojson_layer_carries_the_caveat_sentence(tmp_path):
    from strandline import ingest, outputs

    hand, slope = valley_strip(2)
    classes = np.zeros(hand.shape, dtype=np.uint8)
    grid = ingest.make_grid((85.10, 27.85, 85.12, 27.87), 20.0)
    codes = reliability.compute_reliability(hand, slope, classes, DP, REL, 20.0).codes
    # Lay the 40x40 test terrain onto the real grid for the vectoriser.
    padded = np.zeros((grid.height, grid.width), dtype=np.uint8)
    h, w = min(grid.height, 40), min(grid.width, 40)
    padded[:h, :w] = codes[:h, :w]

    path = tmp_path / "reliability_narrow_valley.geojson"
    n = outputs.write_reliability_polygons(path, padded, grid)

    fc = json.loads(path.read_text())
    assert n == len(fc["features"]) >= 1
    for feature in fc["features"]:
        props = feature["properties"]
        assert props["caveat"] == NARROW_VALLEY_CAVEAT
        assert "narrower than radar resolution" in props["caveat"]
        assert props["reliability_code"] == RELIABILITY_NARROW_VALLEY
        assert props["area_ha"] > 0


def test_the_caveat_wording_is_single_sourced():
    """Dashboard, copilot and sitrep must all quote the same sentence."""
    from strandline.config import RELIABILITY_NAMES

    assert NARROW_VALLEY_CAVEAT == RELIABILITY_NAMES[RELIABILITY_NARROW_VALLEY]
    assert NARROW_VALLEY_CAVEAT.startswith("low confidence:")
