"""Flood and debris detection from a same-orbit Sentinel-1 stack.

Pure numpy/scipy: no network, no files. Every step is deterministic.

Method (per pixel):
  1. z-score of the post-event image against the pre-event stack
     (multi-temporal, as in DeVries et al., 2020), per polarisation,
     combined as zc = (z_VV + z_VH) / sqrt(2).
  2. Open water: strong DECREASE plus dark post-event VV (threshold found
     by Otsu on the valley floor, clamped to a plausible range).
     Debris / deposits: strong INCREASE (rough boulder fields brighten).
  3. Hysteresis region growing: strong-change seeds start a region; weaker
     change joins it only when connected. Rejects isolated noise without
     cutting off real flood edges.
  4. Terrain masks: height above nearest drainage (HAND) and slope.
     Permanent water (dark before AND after) is never counted as flood.
  5. Cleanup: minimum mapping unit, and every flood/debris region must
     connect to the drainage network (real floods do).
"""

import warnings
from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np
from scipy import ndimage
from skimage.filters import threshold_otsu

from strandline.config import (
    FUSION_UNION,
    MODE_DARKENING,
    MODE_ROUGH_WATER,
    SOURCE_BOTH,
    SOURCE_NONE,
    SOURCE_OPTICAL,
    SOURCE_SAR,
    CLASS_DEBRIS,
    CLASS_FLOOD_HIGH,
    CLASS_FLOOD_MEDIUM,
    CLASS_NO_CHANGE,
    CLASS_NODATA,
    CLASS_PERMANENT_WATER,
    CLASS_TERRAIN_EXCLUDED,
    DetectParams,
)

EIGHT_CONNECTED = np.ones((3, 3), dtype=bool)


# --- Preprocessing -----------------------------------------------------------

def lee_filter(linear: np.ndarray, window: int, enl: float) -> np.ndarray:
    """Standard Lee speckle filter on LINEAR backscatter, NaN-aware."""
    valid = np.isfinite(linear) & (linear > 0)
    x = np.where(valid, linear, 0.0)
    weight = ndimage.uniform_filter(valid.astype(np.float64), window)
    safe_w = np.maximum(weight, 1e-9)
    mean = ndimage.uniform_filter(x, window) / safe_w
    mean_sq = ndimage.uniform_filter(x * x, window) / safe_w
    var = np.maximum(mean_sq - mean * mean, 0.0)

    cu2 = 1.0 / enl                                   # speckle noise CV^2
    ci2 = var / np.maximum(mean * mean, 1e-12)        # local CV^2
    w = np.clip(1.0 - cu2 / np.maximum(ci2, 1e-12), 0.0, 1.0)
    out = mean + w * (x - mean)
    out[~valid] = np.nan
    return out


def to_db(linear: np.ndarray) -> np.ndarray:
    out = np.full(linear.shape, np.nan, dtype=np.float64)
    ok = np.isfinite(linear) & (linear > 0)
    out[ok] = 10.0 * np.log10(linear[ok])
    return out


def preprocess(linear: np.ndarray, params: DetectParams) -> np.ndarray:
    """Linear backscatter -> speckle-filtered dB."""
    return to_db(lee_filter(linear.astype(np.float64), params.lee_window, params.enl))


# --- Statistics --------------------------------------------------------------

def temporal_stats(stack_db: np.ndarray, std_floor: float) -> tuple[np.ndarray, np.ndarray]:
    """Per-pixel mean and (floored) sample std over axis 0 of a dB stack."""
    count = np.sum(np.isfinite(stack_db), axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels
        mean = np.nanmean(stack_db, axis=0)
        std = np.nanstd(stack_db, axis=0, ddof=1)
    mean = np.where(count >= 2, mean, np.nan)  # need at least 2 valid dates
    std = np.where(np.isfinite(std), std, 0.0)
    return mean, np.maximum(std, std_floor)


def zscore(post_db: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return (post_db - mean) / std


def combine_z(z_vv: np.ndarray, z_vh: np.ndarray) -> np.ndarray:
    both = np.isfinite(z_vv) & np.isfinite(z_vh)
    return np.where(both, (z_vv + z_vh) / np.sqrt(2.0), z_vv)


class WaterThreshold(NamedTuple):
    """The darkness threshold actually used, plus how it was arrived at.

    `value` is what the classifier uses; `otsu_raw` is what Otsu returned
    before clamping, and `clamped` says which bound bit (or None). In steep
    valleys the valley-floor histogram is often unimodal land, so Otsu
    returns a land-like value and the clamp becomes the operative threshold.
    Reporting only `value` would present a parameter ceiling as a
    measurement, so all three go into run_report.json.
    """

    value: float
    otsu_raw: float | None      # None when there were too few pixels to run Otsu
    clamped: str | None         # "low" | "high" | None
    n_pixels: int


def otsu_water_threshold(post_vv_db: np.ndarray, region: np.ndarray, lo: float, hi: float,
                         min_pixels: int = 500) -> WaterThreshold:
    values = post_vv_db[region & np.isfinite(post_vv_db)]
    if values.size < min_pixels:
        return WaterThreshold(hi, None, "high", int(values.size))
    raw = float(threshold_otsu(values))
    value = float(np.clip(raw, lo, hi))
    clamped = "low" if raw < lo else "high" if raw > hi else None
    return WaterThreshold(value, raw, clamped, int(values.size))


# --- Morphology --------------------------------------------------------------

def grow_from_seeds(seed: np.ndarray, grow: np.ndarray) -> np.ndarray:
    """Hysteresis: keep connected regions of (grow | seed) that contain a seed."""
    candidate = grow | seed
    labels, n = ndimage.label(candidate, structure=EIGHT_CONNECTED)
    if n == 0:
        return np.zeros_like(seed, dtype=bool)
    has_seed = np.zeros(n + 1, dtype=bool)
    has_seed[np.unique(labels[seed & candidate])] = True
    has_seed[0] = False
    return has_seed[labels]


def remove_small(mask: np.ndarray, min_pixels: int) -> np.ndarray:
    if min_pixels <= 1:
        return mask
    labels, n = ndimage.label(mask, structure=EIGHT_CONNECTED)
    if n == 0:
        return mask
    sizes = np.bincount(labels.ravel())
    keep = sizes >= min_pixels
    keep[0] = False
    return keep[labels]


def keep_connected_to(mask: np.ndarray, target: np.ndarray, buffer_px: int) -> np.ndarray:
    """Keep only regions of `mask` that touch `target` within buffer_px."""
    labels, n = ndimage.label(mask, structure=EIGHT_CONNECTED)
    if n == 0:
        return mask
    reach = ndimage.binary_dilation(target, structure=EIGHT_CONNECTED, iterations=max(buffer_px, 1))
    touching = np.zeros(n + 1, dtype=bool)
    touching[np.unique(labels[reach & mask])] = True
    touching[0] = False
    return touching[labels]


# --- Water finders (one per detection mode) ----------------------------------
#
# Each takes the statistics and terrain zones and returns
# (water, debris, high_confidence) boolean masks. Everything after them -
# minimum mapping unit, drainage connection, class codes - is shared, so the
# modes can only differ in the change test itself.


def _find_darkening(p, zc, z_vv, z_vh, post_vv_db, post_vh_db, mean_vv, mean_vh,
                    zone_water, zone_debris, permanent, t_water):
    """The original method: water is dark AND darkens; debris brightens."""
    w_seed = zone_water & ~permanent & (zc <= -p.z_seed) & (post_vv_db <= t_water)
    w_grow = (zone_water & ~permanent & (zc <= -p.z_grow)
              & (post_vv_db <= t_water + p.grow_darkness_margin_db))
    water = grow_from_seeds(w_seed, w_grow)

    rough_after = post_vv_db >= p.debris_min_post_vv_db
    d_seed = (zone_debris & rough_after & (zc >= p.z_seed)
              & (z_vv >= p.z_pol_agree) & (z_vh >= p.z_pol_agree))
    d_grow = zone_debris & rough_after & (zc >= p.z_grow)
    debris = grow_from_seeds(d_seed, d_grow) & ~water

    high = (water & (z_vv <= -p.z_pol_agree) & (z_vh <= -p.z_pol_agree)
            & (post_vv_db <= t_water))
    return water, debris, high


def _find_rough_water(p, zc, z_vv, z_vh, post_vv_db, post_vh_db, mean_vv, mean_vh,
                      zone_water, zone_debris, permanent, t_water):
    """A significant change of EITHER sign on the valley floor is water.

    Three tests must pass together for a seed, and the first two for growth:

      magnitude  both polarisations must move by at least `min_abs_change_db`.
                 A z-score alone cannot do this: with std_floor_db 0.5 a z of
                 1.5 is a 0.75 dB wobble, at the sensor's noise level.
      agreement  both polarisations must move in the SAME direction. A real
                 surface change moves VV and VH together; speckle does not.
      strength   |z| must clear z_seed in the combined score, and both
                 polarisations must clear z_pol_agree, for a seed.

    Water and debris are then separated by TERRAIN POSITION rather than by
    radiometry: inside the valley-floor zone a strong change is water; above it,
    within the debris zone, strong brightening is debris. Water lies low and
    near the channel, debris fans sit higher and steeper.
    """
    d_vv = post_vv_db - mean_vv            # signed change, dB
    d_vh = post_vh_db - mean_vh
    big = (np.abs(d_vv) >= p.min_abs_change_db) & (np.abs(d_vh) >= p.min_abs_change_db)
    if p.require_pol_agreement:
        big &= np.sign(d_vv) == np.sign(d_vh)
    strong_pol = np.minimum(np.abs(z_vv), np.abs(z_vh)) >= p.z_pol_agree

    changed_seed = big & (np.abs(zc) >= p.z_seed) & strong_pol
    changed_grow = big & (np.abs(zc) >= p.z_grow)

    water = grow_from_seeds(zone_water & ~permanent & changed_seed,
                            zone_water & ~permanent & changed_grow)

    # Debris: brightening only, and only above the valley-floor zone.
    brighten = d_vv > 0
    rough_after = post_vv_db >= p.debris_min_post_vv_db
    outside = zone_debris & ~zone_water
    debris = grow_from_seeds(outside & changed_seed & brighten & rough_after,
                             outside & changed_grow & brighten & rough_after) & ~water

    # Darkness is no longer required for water, so it becomes what separates
    # high confidence from medium: specular dark water, measurably darker.
    high = water & (d_vv <= -p.min_abs_change_db) & (post_vv_db <= t_water) & strong_pol
    return water, debris, high


_WATER_FINDERS = {MODE_DARKENING: _find_darkening, MODE_ROUGH_WATER: _find_rough_water}


# --- Per-pixel provenance ----------------------------------------------------

def source_map(water: np.ndarray, debris: np.ndarray, sar_seen: np.ndarray,
               optical_seen: np.ndarray) -> np.ndarray:
    """Which sensor(s) saw each FINAL flood-affected pixel.

    `water` and `debris` are the classes after the minimum mapping unit and the
    drainage-connection test, both of which only ever REMOVE pixels, so every
    flood-affected pixel was seen by at least one sensor. `sar_seen` and
    `optical_seen` are each sensor's pre-cleanup candidates - either channel of
    that sensor, because the question the layer answers is "did this sensor see
    a change here", not "did it give the pixel this exact class".
    """
    affected = water | debris
    sar_hit = affected & sar_seen
    opt_hit = affected & optical_seen
    source = np.full(affected.shape, SOURCE_NONE, dtype=np.uint8)
    source[sar_hit & ~opt_hit] = SOURCE_SAR
    source[opt_hit & ~sar_hit] = SOURCE_OPTICAL
    source[sar_hit & opt_hit] = SOURCE_BOTH
    return source


# --- Main --------------------------------------------------------------------

@dataclass
class DetectionResult:
    classes: np.ndarray
    z_combined: np.ndarray
    pre_mean_vv_db: np.ndarray
    post_vv_db: np.ndarray
    water_threshold_db: float
    stats: dict = field(default_factory=dict)
    # Per-pixel provenance of every flood-affected pixel (SOURCE_* codes). Its
    # own layer, never folded into `classes`: WHAT a pixel is and WHICH SENSOR
    # said so are two different statements and a reader needs both.
    source: np.ndarray | None = None


def detect_flood(
    pre_vv_db: np.ndarray,
    pre_vh_db: np.ndarray,
    post_vv_db: np.ndarray,
    post_vh_db: np.ndarray,
    hand_m: np.ndarray,
    slope_deg: np.ndarray,
    streams: np.ndarray,
    params: DetectParams,
    sar_invalid: np.ndarray | None = None,
    optical=None,
) -> DetectionResult:
    """pre_*: (n_dates, H, W) dB stacks; post_*: (H, W) dB; all on one grid.

    `params.mode` selects the channel that finds water:
      darkening    water is dark and darkens (specular). The original method.
      rough_water  a significant change of EITHER sign low on the valley floor
                   is water, because a turbulent gorge flood is a rough
                   scatterer. Darkness becomes the high-confidence test only.
    Both run the same statistics, terrain zones, minimum mapping unit and
    drainage-connection test; only the change test differs.

    `optical` is optional Sentinel-2 evidence (strandline.optical.OpticalEvidence,
    duck-typed here so this module keeps no optical import and stays pure
    numpy). It is fused when the evidence's own `fusion` mode is `union`
    (OpticalParams.fusion - deliberately NOT a DetectParams field, so that a
    radar-only configuration keeps the params_hash it has always had):

      - an optical water or deposit candidate is added to the corresponding
        radar mask BEFORE the shared terrain, minimum-mapping-unit and
        drainage-connection tests, so optical evidence is held to exactly the
        same standard of place as radar evidence and cannot put water somewhere
        radar would not have been allowed to;
      - where both sensors say the same pixel is flood water, it becomes HIGH
        confidence;
      - `result.source` records per pixel which sensor(s) saw it.

    With `optical=None`, or fusion `off`, every array returned is
    exactly what the radar-only detector produced before Sentinel-2 existed in
    this pipeline. That is asserted by tests/test_detect_optical_fusion.py.
    """
    p = params
    mean_vv, std_vv = temporal_stats(pre_vv_db, p.std_floor_db)
    mean_vh, std_vh = temporal_stats(pre_vh_db, p.std_floor_db)
    z_vv = zscore(post_vv_db, mean_vv, std_vv)
    z_vh = zscore(post_vh_db, mean_vh, std_vh)
    zc = combine_z(z_vv, z_vh)

    valid = (np.isfinite(zc) & np.isfinite(post_vv_db) & np.isfinite(mean_vv)
             & np.isfinite(hand_m) & np.isfinite(slope_deg))
    if sar_invalid is not None:
        valid &= ~sar_invalid

    zone_water = valid & (hand_m <= p.hand_max_water_m) & (slope_deg <= p.slope_max_water_deg)
    zone_debris = valid & (hand_m <= p.hand_max_debris_m) & (slope_deg <= p.slope_max_debris_deg)

    thr = otsu_water_threshold(post_vv_db, zone_water, p.water_db_min, p.water_db_max)
    t_water = thr.value

    with np.errstate(invalid="ignore"):
        permanent = zone_water & (mean_vv <= t_water) & (post_vv_db <= t_water)
        finder = _WATER_FINDERS[p.mode]
        sar_water, sar_debris, high = finder(
            p, zc, z_vv, z_vh, post_vv_db, post_vh_db, mean_vv, mean_vh,
            zone_water, zone_debris, permanent, t_water)

    # Optical evidence enters HERE, before every shared test below, so it is
    # subject to the same terrain zones, minimum mapping unit and drainage
    # connection as the radar channel. There is no separate optical pathway.
    fuse = optical is not None and optical.fusion == FUSION_UNION
    if fuse:
        opt_clear = valid & optical.clear
        opt_water = zone_water & ~permanent & opt_clear & optical.water
        opt_debris = (zone_debris & ~zone_water) & opt_clear & optical.debris
        water = sar_water | opt_water
        debris = (sar_debris | opt_debris) & ~water
    else:
        opt_water = opt_debris = np.zeros(zc.shape, dtype=bool)
        water, debris = sar_water, sar_debris

    water = remove_small(water, p.mmu_pixels)
    debris = remove_small(debris, p.mmu_pixels_debris)

    if p.require_drainage_connection:
        river = streams.astype(bool) | permanent
        water = keep_connected_to(water, river, p.drainage_buffer_px)
        debris = keep_connected_to(debris, river | water, p.drainage_buffer_px)

    high = high & water
    if fuse:
        # Agreement between two independent sensors is the strongest evidence
        # this pipeline can produce, so it joins the radar darkness test as a
        # second route to high confidence.
        high = high | (water & sar_water & opt_water)

    classes = np.full(zc.shape, CLASS_NODATA, dtype=np.uint8)
    classes[valid] = CLASS_NO_CHANGE
    classes[valid & ~(zone_water | zone_debris)] = CLASS_TERRAIN_EXCLUDED
    classes[permanent] = CLASS_PERMANENT_WATER
    classes[debris] = CLASS_DEBRIS
    classes[water] = CLASS_FLOOD_MEDIUM
    classes[high] = CLASS_FLOOD_HIGH

    source = source_map(water, debris, sar_water | sar_debris, opt_water | opt_debris)

    stats = {
        "mode": p.mode,
        "optical_fusion": (None if optical is None else optical.fusion),
        "optical_used": bool(fuse),
        "source_pixels": {int(c): int(np.sum(source == c)) for c in np.unique(source)},
        "water_threshold_db": round(t_water, 3),
        "water_threshold_otsu_raw_db": (None if thr.otsu_raw is None else round(thr.otsu_raw, 3)),
        "water_threshold_clamped": thr.clamped,
        "water_threshold_zone_pixels": thr.n_pixels,
        "pixels": {int(c): int(np.sum(classes == c)) for c in np.unique(classes)},
        "valid_fraction": round(float(np.mean(valid)), 4),
    }
    return DetectionResult(classes=classes, z_combined=zc, pre_mean_vv_db=mean_vv,
                           post_vv_db=post_vv_db, water_threshold_db=t_water, stats=stats,
                           source=source)
