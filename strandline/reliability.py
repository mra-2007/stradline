"""Where the flood map should be read with less confidence.

ONE layer so far: narrow valley floors. A 20 m Sentinel-1 resolution cell in a
gorge whose floor is only a few pixels wide straddles water, bank and wall, so
whatever class that pixel gets is a statement about which scatterer dominated a
mixed cell, not about the ground. The dev set measured exactly this: predicted
flood-affected AREA is close to the reference on every event, but on the two
steepest, narrowest events it is in the wrong PLACE (docs/tuning_log.md,
2026-10-11). This layer marks that ground as such instead of leaving a reader
to assume every pixel is equally trustworthy.

It is computed from the Copernicus DEM ALONE - no backscatter, no reference
map - and it CHANGES NO DETECTION. `narrow_valley_mask` never sees the class
map; `flood_affected_share` only reads it to report a fraction.

Width definition: the local thickness of the valley-floor mask, by
morphological opening with a disc of radius `min_valley_width_px / 2`. A floor
pixel is "resolved" when some disc of that radius fits inside the floor and
covers it, and "narrow" otherwise - i.e. the floor around it is thinner than
`min_valley_width_px` pixels. The opening is done on the Euclidean distance
transform so the radius need not be a whole number of pixels.
"""

from dataclasses import asdict, dataclass

import numpy as np
from scipy import ndimage

from strandline.config import (
    FLOOD_AFFECTED_CLASSES,
    RELIABILITY_NARROW_VALLEY,
    RELIABILITY_OUTSIDE_VALLEY,
    RELIABILITY_RESOLVED,
    DetectParams,
    ReliabilityParams,
)


@dataclass
class Reliability:
    """The layer, plus the counts that go into run_report.json."""

    codes: np.ndarray            # uint8, RELIABILITY_* codes
    narrow: np.ndarray           # bool, codes == RELIABILITY_NARROW_VALLEY
    valley_floor: np.ndarray     # bool, the zone the width was measured in
    stats: dict


def valley_floor_mask(hand_m: np.ndarray, slope_deg: np.ndarray,
                      dp: DetectParams) -> np.ndarray:
    """The valley floor, by the SAME HAND and slope limits the detector uses.

    Deliberately not a second definition: a confidence statement about the
    valley floor has to be about the ground the detector was willing to call
    water, or it qualifies the wrong pixels.
    """
    with np.errstate(invalid="ignore"):
        return (np.isfinite(hand_m) & np.isfinite(slope_deg)
                & (hand_m <= dp.hand_max_water_m)
                & (slope_deg <= dp.slope_max_water_deg))


def narrow_valley_mask(valley_floor: np.ndarray, min_width_px: float) -> np.ndarray:
    """Floor pixels whose local floor thickness is below `min_width_px`.

    Morphological opening by a disc of radius r = min_width_px / 2, done with
    two Euclidean distance transforms so r can be fractional:

      1. `inside` = distance from each floor pixel to the nearest non-floor
         pixel. A disc of radius r centred there fits inside the floor exactly
         when inside >= r.
      2. dilating those centres by r is the same as keeping pixels whose
         distance to the nearest centre is <= r.

    What the opening removes is what the disc could not reach: the narrow parts.
    """
    if min_width_px <= 0:
        raise ValueError(f"min_width_px must be positive, got {min_width_px!r}")
    if not valley_floor.any():
        return np.zeros(valley_floor.shape, dtype=bool)

    radius = min_width_px / 2.0
    inside = ndimage.distance_transform_edt(valley_floor)
    centres = inside >= radius
    if not centres.any():
        return valley_floor.copy()          # nowhere is the floor wide enough
    opened = ndimage.distance_transform_edt(~centres) <= radius
    return valley_floor & ~opened


def flood_affected_share(classes: np.ndarray, narrow: np.ndarray) -> dict:
    """What share of the flood-affected area falls inside the narrow zone.

    Read-only on `classes`. Flood-affected is the primary scored quantity,
    FLOOD_AFFECTED_CLASSES (flood high + medium + debris), so this fraction
    qualifies the same number the gates and the sitrep report.
    """
    affected = np.isin(classes, list(FLOOD_AFFECTED_CLASSES))
    total = int(affected.sum())
    inside = int((affected & narrow).sum())
    return {
        "flood_affected_pixels": total,
        "flood_affected_pixels_in_narrow_valley": inside,
        # None, not 0.0: with nothing detected there is no share to report, and
        # a 0.0 would read as "none of it is uncertain".
        "flood_affected_fraction_in_narrow_valley": (
            None if total == 0 else round(inside / total, 4)),
    }


def compute_reliability(hand_m: np.ndarray, slope_deg: np.ndarray, classes: np.ndarray,
                        dp: DetectParams, rel: ReliabilityParams,
                        pixel_size_m: float) -> Reliability:
    """The whole layer for one scene. Deterministic, no I/O."""
    floor = valley_floor_mask(hand_m, slope_deg, dp)
    narrow = narrow_valley_mask(floor, rel.min_valley_width_px)

    codes = np.full(floor.shape, RELIABILITY_OUTSIDE_VALLEY, dtype=np.uint8)
    codes[floor] = RELIABILITY_RESOLVED
    codes[narrow] = RELIABILITY_NARROW_VALLEY

    px_ha = pixel_size_m * pixel_size_m / 10_000.0
    floor_px = int(floor.sum())
    narrow_px = int(narrow.sum())
    stats = {
        "parameters": asdict(rel),
        "min_valley_width_m": round(rel.min_valley_width_px * pixel_size_m, 2),
        "valley_floor_pixels": floor_px,
        "valley_floor_ha": round(floor_px * px_ha, 2),
        "narrow_valley_pixels": narrow_px,
        "narrow_valley_ha": round(narrow_px * px_ha, 2),
        "narrow_valley_fraction_of_valley_floor": (
            None if floor_px == 0 else round(narrow_px / floor_px, 4)),
        **flood_affected_share(classes, narrow),
    }
    stats["flood_affected_ha_in_narrow_valley"] = round(
        stats["flood_affected_pixels_in_narrow_valley"] * px_ha, 2)
    return Reliability(codes=codes, narrow=narrow, valley_floor=floor, stats=stats)
