"""Sentinel-2 optical evidence for flood water and for mud and debris deposits.

WHY: Track B requires Sentinel-2 to be used when the sky is clear, and the
measured failure of the radar-only detector is one of LOCALISATION in narrow
valleys (docs/tuning_log.md, 2026-10-11) - predicted area is close to correct
but, in the steepest gorges, in the wrong place. Optical evidence is an
independent geometric opinion rather than another radar retune, and fresh mud
and boulder deposits are far more visible in the short-wave infrared than at
C-band.

WHAT IT IS NOT: a cloud-free assumption. Nothing here guesses through cloud.
Scene choice happens on catalogue metadata, so a clouded area is never
downloaded; the scene classification layer then removes cloud, cloud shadow,
terrain shadow, cirrus and snow pixel by pixel; and what is left is reported as
a coverage fraction so a reader always knows how much of the area optical could
actually see. Where it cannot see, it contributes nothing and the map is the
radar map.

TWO SIGNATURES, each a CHANGE between two clear dates, never a single-date
classification:

  water    MNDWI = (B03 - B11) / (B03 + B11), Xu (2006). The pixel must BOTH
           read as water after the event AND have risen since before it, so
           permanent water, wet rock and dark shadow - high or ambiguous on
           both dates - cannot qualify.
  deposit  NDVI = (B08 - B04) / (B08 + B04) must DROP, and B11 reflectance must
           RISE: vegetation or soil buried under sediment loses near-infrared
           vigour and gains short-wave infrared brightness.

Everything in this module is deterministic. The index computation touches no
files and no network, so it is fully testable on synthetic arrays.

WHAT THIS MODULE DOES NOT DECIDE: where a detection is allowed to be. The
masks produced here are CANDIDATES. They are handed to `detect.detect_flood`,
which applies exactly the same height-above-drainage, slope, minimum-mapping-
unit and drainage-connection tests to them as it does to the radar channel.
There is deliberately no second definition of "valley floor" here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import Resampling

from strandline import ingest
from strandline.catalog import S2Day, S2Pair
from strandline.config import (
    FUSION_OFF,
    S2_REFLECTANCE_BANDS,
    S2_SCL_BAND,
    OpticalParams,
)

S2_COLLECTION = "SENTINEL2_L2A"

# Band order inside the cached reflectance GeoTIFF, i.e. S2_REFLECTANCE_BANDS.
BAND_INDEX = {name: i for i, name in enumerate(S2_REFLECTANCE_BANDS)}

# Processing baseline from which L2A carries BOA_ADD_OFFSET = -1000
# (ESA baseline 04.00, products from 2022-01-25). Below it the offset is zero.
BASELINE_WITH_OFFSET = 400
_BASELINE_RE = re.compile(r"_N(\d{4})_")


class OpticalError(RuntimeError):
    """Optical evidence could not be produced, with a message saying why.

    Always a REFUSAL, never a fallback: the caller drops to radar-only and
    records the reason. A wrong index is worse than no index.
    """


# --- download ----------------------------------------------------------------
#
# Two files per date, because the scene classification layer is a CLASS band:
# resampling it bilinearly would invent classes that do not exist (the average
# of "cloud high probability" 9 and "vegetation" 4 is "water" 6.5). Reflectance
# is resampled bilinearly, SCL nearest-neighbour, in separate cubes.


def s2_refl_cache_name(day: date, res: float) -> str:
    bands = "".join(b.lower() for b in S2_REFLECTANCE_BANDS)
    return f"s2_refl_{bands}_{day}_{int(res)}m.tif"


def s2_scl_cache_name(day: date, res: float) -> str:
    return f"s2_scl_{day}_{int(res)}m.tif"


def s2_cache_paths(cache: Path, day: date, res: float) -> dict[str, Path]:
    return {"refl": cache / s2_refl_cache_name(day, res),
            "scl": cache / s2_scl_cache_name(day, res)}


def download_s2_date(conn, day: date, grid: ingest.Grid, paths: dict[str, Path],
                     op: OpticalParams) -> dict[str, Path]:
    """One UTC date -> a reflectance GeoTIFF and an SCL GeoTIFF on `grid`.

    The temporal extent is the whole day, so openEO mosaics every tile that
    covers the area. Overlapping tiles are reduced with `mean` for reflectance
    and with `max` for SCL: the highest SCL code wins, which errs toward
    masking, because every code above the valid range is a reason to distrust
    the pixel.
    """
    out = dict(paths)
    if out["refl"].exists() and out["scl"].exists():
        return out
    out["refl"].parent.mkdir(parents=True, exist_ok=True)
    window = [day.isoformat(), (day + timedelta(days=1)).isoformat()]

    if not out["refl"].exists():
        cube = conn.load_collection(S2_COLLECTION, spatial_extent=grid.openeo_extent(),
                                    temporal_extent=window,
                                    bands=list(S2_REFLECTANCE_BANDS))
        cube = cube.resample_spatial(resolution=grid.res, projection=grid.epsg,
                                     method="bilinear")
        cube = cube.reduce_dimension(dimension="t", reducer="mean")
        ingest.download_cube(cube, out["refl"])

    if not out["scl"].exists():
        cube = conn.load_collection(S2_COLLECTION, spatial_extent=grid.openeo_extent(),
                                    temporal_extent=window, bands=[S2_SCL_BAND])
        cube = cube.resample_spatial(resolution=grid.res, projection=grid.epsg,
                                     method="near")
        cube = cube.reduce_dimension(dimension="t", reducer="max")
        ingest.download_cube(cube, out["scl"])
    return out


# --- digital numbers to reflectance ------------------------------------------


def baseline_offset(scene_names: list[str], op: OpticalParams) -> tuple[float, int | None]:
    """BOA_ADD_OFFSET for these products, from the processing baseline in the name.

    L2A product names carry the baseline as `_Nxxxx_`. From baseline 04.00
    (products after 2022-01-25) reflectance is stored with an additive offset
    of -1000, and before it with none. This matters out of all proportion to
    its size: MNDWI and NDVI are normalised ratios, so a multiplicative scale
    cancels but an OFFSET does not, and getting it wrong corrupts every index.

    Returns (offset, baseline). The baseline is None when no name declares one,
    in which case the configured fallback is used and recorded as such.
    """
    found = [int(m.group(1)) for n in scene_names for m in [_BASELINE_RE.search(n)] if m]
    if not found:
        return op.boa_add_offset, None
    baseline = min(found)       # the oldest product in the mosaic decides
    return (op.boa_add_offset if baseline >= BASELINE_WITH_OFFSET else 0.0), baseline


def to_reflectance(values: np.ndarray, op: OpticalParams, *,
                   file_scales: tuple | None = None, file_offsets: tuple | None = None,
                   scene_names: list[str] | None = None) -> tuple[np.ndarray, dict]:
    """Whatever the backend gave us -> reflectance, with the conversion recorded.

    Three cases, tried in order, because no single rule is safe for every
    backend version:

      file_metadata       the GeoTIFF itself declares scale and offset. Believe it.
      dn_baseline         values are too large to be reflectance, so they are
                          digital numbers: apply the baseline offset and scale.
      already_reflectance values are already small. Leave them alone.

    Then refuse outright if the result is not plausible reflectance: a wrong
    scale or offset must stop the optical stage, not quietly poison an index.
    """
    valid = np.isfinite(values)
    if not valid.any():
        raise OpticalError("Sentinel-2 reflectance has no valid pixels on this grid.")
    median_before = float(np.median(values[valid]))

    declared = (file_scales is not None and file_offsets is not None
                and (any(s not in (0.0, 1.0) for s in file_scales)
                     or any(o != 0.0 for o in file_offsets)))
    if declared:
        scale = float(file_scales[0])
        offset = float(file_offsets[0])
        out = values * scale + offset
        method, baseline = "file_metadata", None
    elif median_before > op.assume_dn_above:
        offset, baseline = baseline_offset(scene_names or [], op)
        scale = op.reflectance_scale
        out = (values + offset) / scale
        method = "dn_baseline"
    else:
        scale, offset, baseline = 1.0, 0.0, None
        out = values.astype(np.float64, copy=True)
        method = "already_reflectance"

    lo, hi = op.plausible_reflectance_range
    with np.errstate(invalid="ignore"):
        implausible = float(np.mean((out[valid] < lo) | (out[valid] > hi)))
        negative = float(np.mean(out[valid] < 0.0))
    info = {
        "method": method,
        "scale": scale,
        "offset": offset,
        "processing_baseline": baseline,
        "median_before": round(median_before, 4),
        "median_after": round(float(np.median(out[valid])), 4),
        # Recorded so the scale/offset convention can be CHECKED against a real
        # scene rather than assumed. A large negative fraction is the signature
        # of an offset applied twice.
        "negative_fraction": round(negative, 4),
        "implausible_fraction": round(implausible, 4),
    }
    if implausible > op.max_implausible_fraction:
        raise OpticalError(
            f"Sentinel-2 reflectance is not plausible after conversion: "
            f"{implausible:.1%} of valid pixels fall outside [{lo}, {hi}] "
            f"(method {method}, scale {scale}, offset {offset}, median "
            f"{info['median_before']} -> {info['median_after']}). Refusing to compute "
            f"optical indices from it. Check the scale and offset convention of this "
            f"openEO backend; the run continues with radar only.")
    return out, info


# --- masking and indices -----------------------------------------------------


def clear_mask(scl: np.ndarray, op: OpticalParams) -> np.ndarray:
    """Pixels whose scene class is usable. Everything else is not seen at all."""
    valid = np.isfinite(scl)
    codes = np.where(valid, np.rint(scl), -1).astype(np.int16)
    return valid & np.isin(codes, np.asarray(op.scl_valid_classes, dtype=np.int16))


def normalised_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(a - b) / (a + b), NaN where the denominator vanishes or input is absent."""
    out = np.full(np.broadcast(a, b).shape, np.nan, dtype=np.float64)
    denom = a + b
    ok = np.isfinite(a) & np.isfinite(b) & np.isfinite(denom) & (np.abs(denom) > 1e-9)
    out[ok] = (a - b)[ok] / denom[ok]
    return out


def mndwi(refl: np.ndarray) -> np.ndarray:
    """Modified Normalised Difference Water Index, Xu (2006): green vs SWIR1."""
    return normalised_difference(refl[BAND_INDEX["B03"]], refl[BAND_INDEX["B11"]])


def ndvi(refl: np.ndarray) -> np.ndarray:
    return normalised_difference(refl[BAND_INDEX["B08"]], refl[BAND_INDEX["B04"]])


# --- the evidence ------------------------------------------------------------


@dataclass
class OpticalEvidence:
    """Candidate optical detections, plus everything needed to report them.

    `water` and `debris` are CANDIDATES. detect.detect_flood applies the same
    terrain, minimum-mapping-unit and drainage tests to them as to the radar
    channel, so nothing here can place a detection where radar could not.
    """

    water: np.ndarray            # bool
    debris: np.ndarray           # bool
    clear: np.ndarray            # bool, both dates usable
    stats: dict
    # What this evidence is allowed to do to the class map, carried with the
    # evidence itself (from OpticalParams.fusion) so that `detect` needs no
    # optical field of its own and a radar-only configuration keeps the exact
    # params_hash it had before Sentinel-2 existed here. See config.params_dict.
    fusion: str = FUSION_OFF
    pre_day: date | None = None
    post_day: date | None = None

    @property
    def clear_fraction(self) -> float:
        return float(np.mean(self.clear))


def build_evidence(pre_refl: np.ndarray, pre_scl: np.ndarray,
                   post_refl: np.ndarray, post_scl: np.ndarray,
                   op: OpticalParams, pre_day: date | None = None,
                   post_day: date | None = None) -> OpticalEvidence:
    """Two clear-masked dates of reflectance -> water and deposit candidates.

    Inputs are REFLECTANCE (already converted), shaped (band, H, W) in
    S2_REFLECTANCE_BANDS order, and SCL shaped (H, W). Pure numpy.
    """
    clear_pre = clear_mask(pre_scl, op)
    clear_post = clear_mask(post_scl, op)

    mndwi_pre, mndwi_post = mndwi(pre_refl), mndwi(post_refl)
    ndvi_pre, ndvi_post = ndvi(pre_refl), ndvi(post_refl)
    swir_pre = pre_refl[BAND_INDEX["B11"]]
    swir_post = post_refl[BAND_INDEX["B11"]]

    finite = (np.isfinite(mndwi_pre) & np.isfinite(mndwi_post) & np.isfinite(ndvi_pre)
              & np.isfinite(ndvi_post) & np.isfinite(swir_pre) & np.isfinite(swir_post))
    clear = clear_pre & clear_post & finite

    with np.errstate(invalid="ignore"):
        # Water: reads as water now AND has become more water-like than before.
        water = (clear & (mndwi_post >= op.mndwi_water)
                 & ((mndwi_post - mndwi_pre) >= op.mndwi_rise_min))
        # Deposits: vegetation or soil lost, SWIR brightness gained.
        debris = (clear & ((ndvi_pre - ndvi_post) >= op.ndvi_drop_min)
                  & ((swir_post - swir_pre) >= op.swir_rise_min) & ~water)

    stats = {
        "pre_day": None if pre_day is None else str(pre_day),
        "post_day": None if post_day is None else str(post_day),
        "clear_fraction_pre": round(float(np.mean(clear_pre)), 4),
        "clear_fraction_post": round(float(np.mean(clear_post)), 4),
        "clear_fraction_pair": round(float(np.mean(clear)), 4),
        "fusion": op.fusion,
        "aoi_pixels": int(clear.size),
        "clear_pixels": int(clear.sum()),
        "optical_water_candidate_pixels": int(water.sum()),
        "optical_debris_candidate_pixels": int(debris.sum()),
        "parameters": {
            "mndwi_water": op.mndwi_water, "mndwi_rise_min": op.mndwi_rise_min,
            "ndvi_drop_min": op.ndvi_drop_min, "swir_rise_min": op.swir_rise_min,
            "scl_valid_classes": list(op.scl_valid_classes),
        },
    }
    if stats["clear_fraction_pair"] < op.min_clear_fraction:
        raise OpticalError(
            f"only {stats['clear_fraction_pair']:.1%} of the area is clear on BOTH "
            f"Sentinel-2 dates ({stats['clear_fraction_pre']:.1%} pre, "
            f"{stats['clear_fraction_post']:.1%} post), below the "
            f"{op.min_clear_fraction:.0%} minimum. Refusing to use optical evidence "
            f"from so little of the area; the run continues with radar only.")
    return OpticalEvidence(water=water, debris=debris, clear=clear, stats=stats,
                           fusion=op.fusion, pre_day=pre_day, post_day=post_day)


# --- reading a cached date ---------------------------------------------------


def read_date(paths: dict[str, Path], grid: ingest.Grid, op: OpticalParams,
              scene_names: list[str] | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """One cached date -> (reflectance (band, H, W), SCL (H, W), conversion info)."""
    for key in ("refl", "scl"):
        if not paths[key].exists():
            raise OpticalError(f"Sentinel-2 file missing from the cache: {paths[key]}")
    with rasterio.open(paths["refl"]) as src:
        file_scales, file_offsets = src.scales, src.offsets
        if src.count != len(S2_REFLECTANCE_BANDS):
            raise OpticalError(
                f"{paths['refl'].name} has {src.count} band(s); expected "
                f"{len(S2_REFLECTANCE_BANDS)} ({', '.join(S2_REFLECTANCE_BANDS)}).")
    raw = ingest.read_on_grid(paths["refl"], grid)
    refl, info = to_reflectance(raw, op, file_scales=file_scales, file_offsets=file_offsets,
                                scene_names=scene_names)
    # Nearest neighbour: SCL is a class band and must not be interpolated.
    scl = ingest.read_on_grid(paths["scl"], grid, resampling=Resampling.nearest)[0]
    return refl, scl, info


def evidence_from_cache(cache: Path, grid: ingest.Grid, pair: S2Pair,
                        op: OpticalParams) -> OpticalEvidence:
    """Build the evidence for a chosen pair from already-downloaded files."""
    pre_refl, pre_scl, pre_info = read_date(
        s2_cache_paths(cache, pair.pre.day, grid.res), grid, op, pair.pre.names)
    post_refl, post_scl, post_info = read_date(
        s2_cache_paths(cache, pair.post.day, grid.res), grid, op, pair.post.names)
    evidence = build_evidence(pre_refl, pre_scl, post_refl, post_scl, op,
                              pre_day=pair.pre.day, post_day=pair.post.day)
    evidence.stats["reflectance_conversion"] = {"pre": pre_info, "post": post_info}
    evidence.stats["scenes"] = {"pre": list(pair.pre.names), "post": list(pair.post.names)}
    evidence.stats["catalogue_cloud_cover_percent"] = {
        "pre": round(pair.pre.cloud_cover, 1), "post": round(pair.post.cloud_cover, 1)}
    evidence.stats["post_latency_days"] = pair.post_latency_days
    evidence.stats["notes"] = list(pair.notes)
    return evidence


def day_label(day: S2Day) -> str:
    return f"{day.day} ({day.cloud_cover:.0f}% cloud, {day.coverage:.0%} coverage)"
