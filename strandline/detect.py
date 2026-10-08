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

import numpy as np
from scipy import ndimage
from skimage.filters import threshold_otsu

from strandline.config import (
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


def otsu_water_threshold(post_vv_db: np.ndarray, region: np.ndarray, lo: float, hi: float,
                         min_pixels: int = 500) -> float:
    values = post_vv_db[region & np.isfinite(post_vv_db)]
    if values.size < min_pixels:
        return hi
    return float(np.clip(threshold_otsu(values), lo, hi))


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


# --- Main --------------------------------------------------------------------

@dataclass
class DetectionResult:
    classes: np.ndarray
    z_combined: np.ndarray
    pre_mean_vv_db: np.ndarray
    post_vv_db: np.ndarray
    water_threshold_db: float
    stats: dict = field(default_factory=dict)


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
) -> DetectionResult:
    """pre_*: (n_dates, H, W) dB stacks; post_*: (H, W) dB; all on one grid."""
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

    t_water = otsu_water_threshold(post_vv_db, zone_water, p.water_db_min, p.water_db_max)

    with np.errstate(invalid="ignore"):
        permanent = zone_water & (mean_vv <= t_water) & (post_vv_db <= t_water)

        # Open water: darkening + dark after.
        w_seed = zone_water & ~permanent & (zc <= -p.z_seed) & (post_vv_db <= t_water)
        w_grow = (zone_water & ~permanent & (zc <= -p.z_grow)
                  & (post_vv_db <= t_water + p.grow_darkness_margin_db))
        water = grow_from_seeds(w_seed, w_grow)

        # Debris / deposits: brightening.
        rough_after = post_vv_db >= p.debris_min_post_vv_db
        d_seed = (zone_debris & rough_after & (zc >= p.z_seed)
                  & (z_vv >= p.z_pol_agree) & (z_vh >= p.z_pol_agree))
        d_grow = zone_debris & rough_after & (zc >= p.z_grow)
        debris = grow_from_seeds(d_seed, d_grow) & ~water

    water = remove_small(water, p.mmu_pixels)
    debris = remove_small(debris, p.mmu_pixels_debris)

    if p.require_drainage_connection:
        river = streams.astype(bool) | permanent
        water = keep_connected_to(water, river, p.drainage_buffer_px)
        debris = keep_connected_to(debris, river | water, p.drainage_buffer_px)

    with np.errstate(invalid="ignore"):
        high = water & (z_vv <= -p.z_pol_agree) & (z_vh <= -p.z_pol_agree) & (post_vv_db <= t_water)

    classes = np.full(zc.shape, CLASS_NODATA, dtype=np.uint8)
    classes[valid] = CLASS_NO_CHANGE
    classes[valid & ~(zone_water | zone_debris)] = CLASS_TERRAIN_EXCLUDED
    classes[permanent] = CLASS_PERMANENT_WATER
    classes[debris] = CLASS_DEBRIS
    classes[water] = CLASS_FLOOD_MEDIUM
    classes[high] = CLASS_FLOOD_HIGH

    stats = {
        "water_threshold_db": round(t_water, 3),
        "pixels": {int(c): int(np.sum(classes == c)) for c in np.unique(classes)},
        "valid_fraction": round(float(np.mean(valid)), 4),
    }
    return DetectionResult(classes=classes, z_combined=zc, pre_mean_vv_db=mean_vv,
                           post_vv_db=post_vv_db, water_threshold_db=t_water, stats=stats)
