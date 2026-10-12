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

    This says the offset EXISTS for these products, not that it still needs
    applying - a backend may already have applied it. `dn_offset` decides that
    from the pixels.
    """
    found = [int(m.group(1)) for n in scene_names for m in [_BASELINE_RE.search(n)] if m]
    if not found:
        return op.boa_add_offset, None
    baseline = min(found)       # the oldest product in the mosaic decides
    return (op.boa_add_offset if baseline >= BASELINE_WITH_OFFSET else 0.0), baseline


def dn_offset(values: np.ndarray, valid: np.ndarray, op: OpticalParams,
              scene_names: list[str] | None) -> tuple[float, int | None, str, dict]:
    """Does this product still NEED the baseline offset, or has it been applied?

    The processing baseline in the product name says whether BOA_ADD_OFFSET
    EXISTS for these products. It cannot say whether the backend has already
    applied it, and that question is not cosmetic: the offset is a tenth of the
    whole reflectance range, MNDWI and NDVI are normalised ratios, and applying
    it twice corrupts both outright while leaving values that still look like
    reflectance. Measured on 2026-10-11, the Copernicus Data Space openEO
    backend returns L2A ALREADY offset-corrected, so the question has a
    different answer for a product read through openEO than for the same
    product downloaded from the archive.

    So it is decided from the pixels, by the one thing that separates the two
    hypotheses physically: REFLECTANCE IS NOT NEGATIVE. Atmospheric correction
    over-corrects a small share of dark pixels below zero - a few percent at
    worst - whereas subtracting an offset that is not there pushes the whole
    scene down by 0.1 and takes the dark half of the green and red bands with
    it. The two cases are two orders of magnitude apart (measured: 0.0-2.0% of
    valid pixels against 9.1-62.8% over the 20 dev-set files), so:

        apply the specification offset UNLESS doing so would make more than
        `op.max_negative_fraction` of valid pixels negative.

    The test is symmetric, which is what makes it safe to leave in the path: a
    genuine archive product, offset not yet applied, has a small true negative
    fraction and keeps its offset; if this backend ever stops pre-applying the
    offset, the same line of code starts applying it again, with no edit here.

    Returns (offset, processing_baseline, method, evidence). Every number the
    decision rested on is in `evidence`, so a reader of run_report.json can
    check the conclusion instead of trusting it.
    """
    spec_offset, baseline = baseline_offset(scene_names or [], op)
    v = values[valid]
    with np.errstate(invalid="ignore"):
        as_is = float(np.mean(v / op.reflectance_scale < 0.0))
        shifted = (as_is if spec_offset == 0.0
                   else float(np.mean((v + spec_offset) / op.reflectance_scale < 0.0)))
    evidence = {
        "specification_offset": spec_offset,
        "negative_fraction_with_offset": round(shifted, 4),
        "negative_fraction_without_offset": round(as_is, 4),
        "max_negative_fraction": op.max_negative_fraction,
        "raw_min": round(float(np.min(v)), 1),
        "raw_median": round(float(np.median(v)), 1),
    }
    if spec_offset == 0.0:
        # This baseline never carried an offset; there is nothing to decide.
        evidence["decided_by"] = "processing_baseline"
        return 0.0, baseline, "dn_baseline", evidence
    if shifted > op.max_negative_fraction:
        evidence["decided_by"] = "negative_fraction"
        return 0.0, baseline, "dn_offset_already_applied", evidence
    evidence["decided_by"] = "negative_fraction"
    return spec_offset, baseline, "dn_baseline", evidence


def to_reflectance(values: np.ndarray, op: OpticalParams, *,
                   file_scales: tuple | None = None, file_offsets: tuple | None = None,
                   scene_names: list[str] | None = None) -> tuple[np.ndarray, dict]:
    """Whatever the backend gave us -> reflectance, with the conversion recorded.

    Three cases, tried in order, because no single rule is safe for every
    backend version:

      file_metadata       the GeoTIFF itself declares scale and offset. Believe it.
      dn_baseline         values are too large to be reflectance, so they are
                          digital numbers: scale them, and apply the baseline
                          offset if `dn_offset` finds it has not been applied
                          already (`dn_offset_already_applied` if it has).
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
        decision = None
    elif median_before > op.assume_dn_above:
        offset, baseline, method, decision = dn_offset(values, valid, op, scene_names)
        scale = op.reflectance_scale
        out = (values + offset) / scale
    else:
        scale, offset, baseline = 1.0, 0.0, None
        out = values.astype(np.float64, copy=True)
        method = "already_reflectance"
        decision = None

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
        # of an offset applied twice - which is what `offset_decision` below
        # now tests for and rules out, per file, before this point.
        "negative_fraction": round(negative, 4),
        "implausible_fraction": round(implausible, 4),
    }
    if decision is not None:
        info["offset_decision"] = decision
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


# OpticalParams fields consumed BEFORE build_evidence, i.e. baked into a read
# pair: they choose the scenes and convert the digital numbers. A caller that
# varies one of these must read the pair again, so eval/tune.py asserts its
# optical grid touches none of them - the same contract as
# run.PREPROCESS_FIELDS for a prepared radar scene.
READ_FIELDS = (
    "enabled", "search_days_before", "max_post_latency_days", "post_same_day_ok",
    "min_aoi_coverage", "max_scene_cloud_cover", "reflectance_scale", "boa_add_offset",
    "assume_dn_above", "plausible_reflectance_range", "max_implausible_fraction",
    "max_negative_fraction",
)


@dataclass
class ReadPair:
    """Two cached Sentinel-2 dates, read and converted to reflectance.

    Separated from the evidence so that a parameter search can try many index
    thresholds against ONE read of the files: reading and converting four
    GeoTIFFs costs seconds, computing two normalised indices costs
    milliseconds. The fields baked in here are `READ_FIELDS`.
    """

    pair: S2Pair
    pre_refl: np.ndarray
    pre_scl: np.ndarray
    post_refl: np.ndarray
    post_scl: np.ndarray
    conversion: dict
    read_params: dict          # the READ_FIELDS values already applied


def read_pair(cache: Path, grid: ingest.Grid, pair: S2Pair, op: OpticalParams) -> ReadPair:
    """Read and convert both dates of a chosen pair from the cache. No network."""
    pre_refl, pre_scl, pre_info = read_date(
        s2_cache_paths(cache, pair.pre.day, grid.res), grid, op, pair.pre.names)
    post_refl, post_scl, post_info = read_date(
        s2_cache_paths(cache, pair.post.day, grid.res), grid, op, pair.post.names)
    return ReadPair(pair=pair, pre_refl=pre_refl, pre_scl=pre_scl, post_refl=post_refl,
                    post_scl=post_scl,
                    conversion={"pre": pre_info, "post": post_info},
                    read_params={f: getattr(op, f) for f in READ_FIELDS})


def evidence_from_pair(read: ReadPair, op: OpticalParams) -> OpticalEvidence:
    """An already-read pair -> evidence, for one set of index thresholds."""
    applied = {f: getattr(op, f) for f in READ_FIELDS}
    if applied != read.read_params:
        raise OpticalError(
            f"these OpticalParams are baked into the read pair and cannot be changed "
            f"without reading it again: pair has {read.read_params}, asked for {applied}")
    pair = read.pair
    evidence = build_evidence(read.pre_refl, read.pre_scl, read.post_refl, read.post_scl,
                             op, pre_day=pair.pre.day, post_day=pair.post.day)
    evidence.stats["reflectance_conversion"] = dict(read.conversion)
    evidence.stats["scenes"] = {"pre": list(pair.pre.names), "post": list(pair.post.names)}
    evidence.stats["catalogue_cloud_cover_percent"] = {
        "pre": round(pair.pre.cloud_cover, 1), "post": round(pair.post.cloud_cover, 1)}
    evidence.stats["post_latency_days"] = pair.post_latency_days
    evidence.stats["notes"] = list(pair.notes)
    return evidence


def evidence_from_cache(cache: Path, grid: ingest.Grid, pair: S2Pair,
                        op: OpticalParams) -> OpticalEvidence:
    """Build the evidence for a chosen pair from already-downloaded files."""
    return evidence_from_pair(read_pair(cache, grid, pair, op), op)


def day_label(day: S2Day) -> str:
    return f"{day.day} ({day.cloud_cover:.0f}% cloud, {day.coverage:.0%} coverage)"
