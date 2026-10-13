"""End-to-end flood mapping for ANY area and date.

    python -m strandline.run --bbox W S E N --event-date YYYY-MM-DD --out DIR [--dry-run]

--dry-run only runs the pre-flight check (catalogue search + track choice),
no login needed. Without it, the first run opens a browser login for the
Copernicus Data Space (free account); the token is then remembered.

The run is split into stages so that an evaluation harness can reuse one
expensive stage many times:

    preflight  -> which track, which dates          (catalogue only, no login)
    fetch      -> Sentinel-1 + DEM on one grid      (openEO, cached)
    optical    -> Sentinel-2 evidence, if clear     (openEO, cached, OPTIONAL
                                                     and never fatal)
    prepare    -> speckle filter, dB, terrain       (CPU, terrain cached)
    classify   -> flood and debris classes          (CPU, cheap: re-run per
                                                     parameter set)
    write      -> GeoTIFFs, polygons, run_report

`eval/tune.py` prepares a scene once per event and then calls `classify`
for every point of the parameter grid. Nothing in that path differs from
what the CLI does, so a tuned number and a live number come from the same
code.
"""

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from strandline import (catalog, damage, detect, ingest, network, optical, osm,
                        outputs, reliability, terrain)
from strandline.config import (
    BACKSCATTER_COEFFICIENTS,
    CLASS_DEBRIS,
    CLASS_NAMES,
    BRIDGE_UNVERIFIED_LABEL,
    OPTICAL_FUSION_MODES,
    DEBRIS_UNVERIFIED_LABEL,
    DamageParams,
    DetectParams,
    ExtentParams,
    MonteCarloParams,
    NetworkParams,
    OpticalParams,
    OsmParams,
    ReliabilityParams,
    RunParams,
    params_dict,
)

# What the damage stage did. One of these is ALWAYS in run_report.json under
# `damage.status`, so a report can never be silent about whether a damage
# answer exists - a count of zero and "we could not read OSM" must never look
# the same.
#
#   disabled   the overlay was not asked for (--no-damage).
#   refused    OpenStreetMap could not be read; the message says why and what to
#              try. There is NO damage answer, and `damage_available` is false.
#   failed     OSM was read but the overlay itself broke (geometry, GDAL, disk).
#              The error type and message are recorded, the flood map and the
#              report survive, and there is NO damage answer.
#   used       the overlay ran; counts, lengths and the OSM snapshot are present.
OSM_DISABLED = "disabled"
OSM_REFUSED = "refused"
OSM_FAILED = "failed"
OSM_USED = "used"

# DetectParams fields consumed before `classify`, i.e. baked into a prepared
# scene. A caller that varies one of these must prepare the scene again;
# eval/tune.py asserts its grid touches none of them.
PREPROCESS_FIELDS = ("lee_window", "enl")

# What the optical stage did. One of these is ALWAYS in run_report.json, so a
# report can never be silent about Sentinel-2, and a dashboard or a copilot
# answer can always say why optical evidence is or is not in a map.
#
#   disabled       optical was not asked for (the caller passed no OpticalParams,
#                  or --no-optical).
#   no_clear_pair  the catalogue has no clear-enough pre/post pair for this area
#                  and date. Nothing was downloaded.
#   refused        a pair was found but the evidence was rejected on inspection -
#                  too much cloud over the AOI after the scene classification
#                  layer, or reflectance that did not convert plausibly. The
#                  reason is in `message`.
#   failed         something broke (network, backend, a corrupt file). The error
#                  type and message are recorded.
#   used           optical evidence was fused into the map.
OPTICAL_DISABLED = "disabled"
OPTICAL_NO_PAIR = "no_clear_pair"
OPTICAL_REFUSED = "refused"
OPTICAL_FAILED = "failed"
OPTICAL_USED = "used"


class PipelineError(RuntimeError):
    """A run could not be completed, with a message saying what to try."""


@dataclass
class PreparedScene:
    """Everything `classify` needs, on one grid, for one acquisition stack."""

    grid: ingest.Grid
    choice: catalog.TrackChoice
    pre_vv_db: np.ndarray            # (n_pre, H, W)
    pre_vh_db: np.ndarray
    post_vv_db: np.ndarray           # (H, W)
    post_vh_db: np.ndarray
    terrain: terrain.Terrain
    run_params: RunParams
    preprocess_params: dict          # the DetectParams fields already applied
    post_mask_counts: dict | None = None
    # Named `optical_evidence`, not `optical`, so it cannot shadow the module
    # of the same name inside this class body.
    optical_evidence: optical.OpticalEvidence | None = None
    optical_params: OpticalParams | None = None
    # The Sentinel-2 pair the optical stage actually CHOSE, kept so a caller
    # that wants to rebuild the evidence (eval/tune.py, trying several index
    # thresholds against one read of the files) re-reads exactly the two dates
    # that were scored, instead of asking the catalogue again and risking a
    # different choice.
    optical_pair: catalog.S2Pair | None = None
    # What the optical stage did, whether or not it produced evidence. Always
    # present, so a report can never be silent about Sentinel-2.
    optical_report: dict = field(default_factory=lambda: {"status": OPTICAL_DISABLED})

    @property
    def n_pixels(self) -> int:
        return self.grid.width * self.grid.height


def preflight(bbox, event_day: date, rp: RunParams, log=print) -> catalog.TrackChoice | None:
    """Catalogue search and track choice. No login, no download."""
    start = event_day - timedelta(days=rp.search_days_before)
    end = event_day + timedelta(days=rp.max_post_latency_days + 1)
    log(f"[1/7] Searching Sentinel-1 GRD scenes {start} .. {end} ...")
    try:
        scenes = catalog.search_s1_grd(bbox, start, end)
    except Exception as err:  # network / service problems: say so clearly
        raise PipelineError(
            f"Catalogue search failed: {err}\n"
            "Check the internet connection and https://dataspace.copernicus.eu status.") from err
    acqs = catalog.group_acquisitions(scenes, catalog.aoi_polygon(bbox))
    log(f"      {len(scenes)} scenes -> {len(acqs)} acquisitions on "
        f"{len({a.track for a in acqs})} tracks")
    choice, lines = catalog.choose_track(acqs, event_day, rp)
    for line in lines:
        log("      " + line)
    if choice is not None:
        log(f"      chosen: track {choice.track[1]} {choice.track[2]}, post {choice.post.day} "
            f"(+{choice.latency_days} d), pre {[str(a.day) for a in choice.pre]}")
        for note in choice.notes:
            log("      NOTE: " + note)
    return choice


def scene_cache_paths(choice: catalog.TrackChoice, cache: Path, res: float,
                      coefficient: str, elevation_model: str) -> dict:
    """Where every file of this scene is cached. Pure path arithmetic.

    The fetch script and the scoreboard both ask this function, so a file
    downloaded once is found again rather than downloaded twice.
    """
    tag = f"t{choice.track[1]}_{choice.track[2]}"
    s1 = {acq.day: cache / ingest.s1_cache_name(tag, acq.day, res, coefficient, elevation_model)
          for acq in list(choice.pre) + [choice.post]}
    return {"s1": s1, "dem": cache / f"dem_{int(res)}m.tif",
            "terrain": cache / f"terrain_{int(res)}m.npz"}


def fetch(choice: catalog.TrackChoice, grid: ingest.Grid, cache: Path, rp: RunParams,
          conn=None, log=print) -> dict:
    """Download (or reuse from cache) every raster this scene needs."""
    paths = scene_cache_paths(choice, cache, grid.res, rp.backscatter_coefficient,
                              rp.elevation_model)
    missing = [p for p in list(paths["s1"].values()) + [paths["dem"]] if not p.exists()]
    if not missing:
        log(f"[3/7] All {len(paths['s1']) + 1} rasters already cached")
        return paths
    conn = conn if conn is not None else ingest.connect()
    for acq in list(choice.pre) + [choice.post]:
        path = paths["s1"][acq.day]
        log(f"      S1 {acq.day} -> {path.name}")
        ingest.download_s1(conn, acq, grid, path, rp.backscatter_coefficient,
                           rp.use_sar_mask, rp.elevation_model)
    ingest.download_dem(conn, grid, paths["dem"])
    return paths


def optical_preflight(bbox, event_day: date, op: OpticalParams, log=print
                      ) -> catalog.S2Pair | None:
    """Choose a clear Sentinel-2 pre/post pair from catalogue metadata alone.

    No login and no download: cloud cover comes from the product metadata, so
    an area that was under cloud throughout costs one catalogue query and
    nothing else.
    """
    start = event_day - timedelta(days=op.search_days_before)
    end = event_day + timedelta(days=op.max_post_latency_days + 1)
    log(f"      searching Sentinel-2 L2A scenes {start} .. {end} ...")
    scenes = catalog.search_s2_l2a(bbox, start, end)
    days = catalog.group_s2_days(scenes, catalog.aoi_polygon(bbox))
    log(f"      {len(scenes)} scene(s) on {len(days)} date(s)")
    pair, lines = catalog.choose_s2_pair(days, event_day, op)
    for line in lines:
        log("      " + line)
    return pair


def optical_stage(bbox, event_day: date, grid: ingest.Grid, cache: Path,
                  op: OpticalParams | None, conn=None, log=print
                  ) -> tuple[optical.OpticalEvidence | None, dict, catalog.S2Pair | None]:
    """Sentinel-2 evidence for this area and date. NEVER raises.

    Live robustness (project rule 5): a flood map must not be lost because the
    optical half of it could not be built. Every failure here - no clear scene,
    too much cloud, an implausible reflectance convention, a dead backend, a
    corrupt cached file - degrades the run to radar-only and is RECORDED, with
    its reason, in the returned report. That is why the `except Exception` below
    is deliberately broad: anything at all in this stage must become a reported
    status rather than a lost run. It wraps one stage and swallows nothing
    silently. The single exception is AssertionError, which is re-raised: that
    is a test's monkeypatch or a programming error, never a flood, and hiding it
    behind a status would let a broken test pass.
    """
    report: dict = {"status": OPTICAL_DISABLED, "enabled": bool(op is not None and op.enabled)}
    if op is None or not op.enabled:
        return None, report, None

    try:
        pair = optical_preflight(bbox, event_day, op, log=log)
        if pair is None:
            report["status"] = OPTICAL_NO_PAIR
            report["message"] = (
                f"no Sentinel-2 date clear enough and covering the whole area in "
                f"[-{op.search_days_before} d, +{op.max_post_latency_days} d] around "
                f"{event_day}; the map is radar-only")
            log("      " + report["message"])
            return None, report, None

        report["pair"] = {
            "pre": {"day": str(pair.pre.day), "cloud_cover_percent": round(pair.pre.cloud_cover, 1),
                    "aoi_coverage": round(pair.pre.coverage, 4), "scenes": list(pair.pre.names)},
            "post": {"day": str(pair.post.day),
                     "cloud_cover_percent": round(pair.post.cloud_cover, 1),
                     "aoi_coverage": round(pair.post.coverage, 4),
                     "scenes": list(pair.post.names)},
            "post_latency_days": pair.post_latency_days,
            "notes": list(pair.notes),
        }
        for note in pair.notes:
            log("      NOTE: " + note)

        by_day = {day: optical.s2_cache_paths(cache, day, grid.res)
                  for day in (pair.pre.day, pair.post.day)}
        missing = [day for day, paths in by_day.items()
                   if not (paths["refl"].exists() and paths["scl"].exists())]
        # Connect ONLY if something is actually missing, as `fetch` does: a run
        # off a warm cache (every dev-set score, and every rerun of a live area)
        # must not trigger an interactive login.
        connection = None
        if missing:
            connection = conn if conn is not None else ingest.connect()
        for day, paths in by_day.items():
            if day in missing:
                log(f"      S2 {day} -> {paths['refl'].name} + {paths['scl'].name}")
            optical.download_s2_date(connection, day, grid, paths, op)

        evidence = optical.evidence_from_cache(cache, grid, pair, op)
        report["status"] = OPTICAL_USED
        report["evidence"] = evidence.stats
        log(f"      optical: {evidence.stats['clear_fraction_pair']:.0%} of the area clear on "
            f"both dates, {evidence.stats['optical_water_candidate_pixels']} water and "
            f"{evidence.stats['optical_debris_candidate_pixels']} deposit candidate pixels")
        return evidence, report, pair

    except optical.OpticalError as err:
        report["status"] = OPTICAL_REFUSED
        report["message"] = str(err)
        log("      optical REFUSED: " + str(err))
        return None, report, None
    except AssertionError:
        # NOT swallowed. The broad handler below exists to protect a live run
        # from the optical stage, but an AssertionError here is never a flood;
        # it is a test's own monkeypatch firing (or a programming error), and
        # turning it into a reported status would let a test that only checks
        # the exit code pass while its assertion failed. There is no `assert`
        # in the optical path itself, so nothing in production reaches this.
        raise
    except Exception as err:  # see the docstring: never lose a run over optical
        report["status"] = OPTICAL_FAILED
        report["error_type"] = type(err).__name__
        report["message"] = str(err)
        log(f"      optical FAILED ({type(err).__name__}: {err}); continuing with radar only")
        return None, report, None


def prepare(bbox, event_day: date, cache: Path, rp: RunParams, dp: DetectParams,
            conn=None, choice: catalog.TrackChoice | None = None,
            op: OpticalParams | None = None, log=print) -> PreparedScene:
    """preflight + fetch + speckle filter + dB + terrain + optical.

    Raises PipelineError if the RADAR half cannot be built. The optical half is
    optional by construction: `op=None` means the caller did not ask for
    Sentinel-2 (which is what every cached dev-set score was produced with, so
    their params_hash stays reproducible), and an optical failure is recorded
    rather than raised. The live CLI asks for it; `eval` asks for it with
    --optical.
    """
    if choice is None:
        choice = preflight(bbox, event_day, rp, log)
    if choice is None:
        raise PipelineError(
            "No usable same-track pair: refusing to map rather than guessing. "
            "Try a later --max-post-latency-days or a smaller area.")

    grid = ingest.make_grid(bbox, rp.resolution_m)
    log(f"[2/7] Grid EPSG:{grid.epsg}, {grid.width} x {grid.height} px at {grid.res} m")
    log("[3/7] Downloading via openEO (cached) ...")
    paths = fetch(choice, grid, cache, rp, conn=conn, log=log)

    log("[4/7] Preprocessing (Lee filter, dB, alignment) ...")

    def load(day):
        arr = ingest.read_on_grid(paths["s1"][day], grid)
        if ingest.looks_like_db(arr[0]):
            raise PipelineError(f"{paths['s1'][day].name} looks like dB already; expected linear")
        vv, vh = detect.preprocess(arr[0], dp), detect.preprocess(arr[1], dp)
        mask = arr[2] if arr.shape[0] > 2 else None
        return vv, vh, mask

    pre = [load(a.day) for a in choice.pre]
    post_vv, post_vh, post_mask = load(choice.post.day)
    mask_counts = None
    if post_mask is not None:
        vals, counts = np.unique(post_mask[np.isfinite(post_mask)], return_counts=True)
        mask_counts = dict(zip(vals.tolist(), counts.tolist()))
        log(f"      SAR mask band values (check encoding!): {mask_counts}")
    dem = ingest.read_on_grid(paths["dem"], grid)[0].astype(np.float64)

    log("[5/7] Terrain: slope, drainage, HAND ...")
    terr = terrain.load_or_compute_terrain(dem, grid.transform, grid.crs,
                                           rp.stream_threshold_km2, paths["terrain"], log=log)

    log("[6/7] Sentinel-2 optical evidence (optional) ...")
    evidence, optical_report, s2_pair = optical_stage(bbox, event_day, grid, cache, op,
                                                      conn=conn, log=log)
    return PreparedScene(
        grid=grid, choice=choice,
        pre_vv_db=np.stack([p[0] for p in pre]), pre_vh_db=np.stack([p[1] for p in pre]),
        post_vv_db=post_vv, post_vh_db=post_vh, terrain=terr, run_params=rp,
        preprocess_params={f: getattr(dp, f) for f in PREPROCESS_FIELDS},
        post_mask_counts=mask_counts, optical_evidence=evidence,
        optical_params=op, optical_report=optical_report, optical_pair=s2_pair,
    )


def classify(scene: PreparedScene, dp: DetectParams) -> detect.DetectionResult:
    """The cheap stage: one parameter set over an already-prepared scene."""
    applied = {f: getattr(dp, f) for f in PREPROCESS_FIELDS}
    if applied != scene.preprocess_params:
        raise PipelineError(
            f"these DetectParams are baked into the prepared scene and cannot be changed "
            f"without preparing it again: scene has {scene.preprocess_params}, asked for "
            f"{applied}")
    return detect.detect_flood(
        scene.pre_vv_db, scene.pre_vh_db, scene.post_vv_db, scene.post_vh_db,
        scene.terrain.hand_m, scene.terrain.slope_deg, scene.terrain.streams, dp,
        sar_invalid=None,  # enable once the mask encoding is verified
        optical=scene.optical_evidence,
    )


def build_report(scene: PreparedScene, result: detect.DetectionResult, bbox, event_day: date,
                 rp: RunParams, dp: DetectParams, runtime_s: float, n_poly: int | None,
                 rel: reliability.Reliability | None = None,
                 ep: ExtentParams | None = None) -> dict:
    choice, grid, terr = scene.choice, scene.grid, scene.terrain
    op = scene.optical_params
    ep = ep if ep is not None else ExtentParams()
    params = params_dict(dp, rp, op, ep)
    report = {
        "event_date": str(event_day),
        "bbox_wgs84": list(bbox),
        "grid": {"epsg": grid.epsg, "resolution_m": grid.res, "width": grid.width,
                 "height": grid.height},
        "track": {"relative_orbit": choice.track[1], "direction": choice.track[2],
                  "post": {"day": str(choice.post.day), "scenes": choice.post.names},
                  "pre": [{"day": str(a.day), "scenes": a.names} for a in choice.pre],
                  "post_latency_days": choice.latency_days, "notes": choice.notes},
        "parameters": params,
        "parameters_hash": params_hash(dp, rp, op, ep),
        "areas_ha": outputs.class_areas_ha(result.classes, grid.res),
        "detection_stats": result.stats,
        "terrain": {"stream_pixels": int(terr.streams.sum()),
                    "hand_fallback_fraction": terr.hand_fallback_fraction},
        "runtime_s": round(runtime_s, 1),
    }
    # What this run counts as flood EXTENT. Always present, because a reader of
    # a hectare figure has to know whether debris is inside it, and the damage
    # and cut-off modules must not have to guess.
    px_ha = grid.res * grid.res / 10_000.0
    debris_ha = round(float((result.classes == CLASS_DEBRIS).sum()) * px_ha, 2)
    extent_ha = round(float(np.isin(result.classes,
                                    list(ep.flood_extent_classes())).sum()) * px_ha, 2)
    report["flood_extent"] = {
        "debris_in_flood_extent": ep.debris_in_flood_extent,
        "classes": [CLASS_NAMES[c] for c in ep.flood_extent_classes()],
        "flood_extent_ha": extent_ha,
        "debris_ha": debris_ha,
        "debris_ha_inside_extent": debris_ha if ep.debris_in_flood_extent else 0.0,
        "debris_label": (None if ep.debris_in_flood_extent else DEBRIS_UNVERIFIED_LABEL),
        "note": ("debris is part of the flood extent" if ep.debris_in_flood_extent else
                 f"debris is mapped and reported as {DEBRIS_UNVERIFIED_LABEL!r} but is NOT "
                 f"flood extent: it is excluded from flood_extent_ha and must not be used "
                 f"by the damage overlay or the cut-off analysis"),
    }
    # Always present, whatever happened, so no report is silent about Sentinel-2.
    report["optical"] = dict(scene.optical_report)
    report["optical"]["source_pixels"] = {
        outputs.source_name(code): count
        for code, count in result.stats.get("source_pixels", {}).items()}
    report["optical"]["source_ha"] = {
        outputs.source_name(code): round(count * grid.res * grid.res / 10_000.0, 2)
        for code, count in result.stats.get("source_pixels", {}).items()}
    report["optical"]["fusion"] = (op.fusion if op is not None else None)
    report["optical"]["fused_into_map"] = bool(result.stats.get("optical_used", False))
    if n_poly is not None:
        report["n_polygons"] = n_poly
    if rel is not None:
        # Outside "parameters"/"parameters_hash" by design: this layer changes
        # no output pixel of the class map, so it must not change the hash that
        # identifies a detection (see config.params_dict).
        report["reliability"] = {"narrow_valley": rel.stats}
    return report


def params_hash(dp: DetectParams, rp: RunParams, op: OpticalParams | None = None,
                ep: ExtentParams | None = None) -> str:
    """The 12-hex identity of a configuration.

    `op=None` (or optical disabled) hashes byte-identically to the way it did
    before Sentinel-2 existed here, and `ep=None` or `EXTENT_HASH_BASELINE`
    hashes byte-identically to the way it did before the extent option existed,
    so every params_hash already recorded in eval/results/ still identifies the
    same configuration. No optical or extent field lives in DetectParams or
    RunParams, which is what makes that true; it is pinned by
    tests/test_params_hash_reproducible.py. See config.params_dict.

    NOT the DEFAULT ExtentParams. The shipped default moved to debris-OUT on
    2026-10-12, so `ExtentParams()` now hashes DIFFERENTLY from `None` - which
    is the point: it scores a different quantity and must not borrow the hash of
    the rows published before the option existed. A caller that wants the
    published identity must pass `EXTENT_HASH_BASELINE`, not `ExtentParams()`.
    """
    return hashlib.sha256(
        json.dumps(params_dict(dp, rp, op, ep), sort_keys=True).encode()).hexdigest()[:12]


def damage_stage(bbox, event_day: date, scene: PreparedScene,
                 result: detect.DetectionResult, out: Path,
                 osmp: OsmParams | None, dmgp: DamageParams | None,
                 ep: ExtentParams | None = None, allow_download: bool = True,
                 snapshot=None, log=print) -> tuple[object | None, dict]:
    """OSM snapshot + damage overlay. NEVER raises.

    `snapshot` may be supplied by a caller that has already read OSM, so the
    damage and cut-off stages share one read rather than downloading twice.

    Same contract as `optical_stage`, for the same reason (Rule 5): a flood map
    must not be lost because OpenStreetMap could not be read. Every failure -
    an unreachable or blocked ohsome, an area too large for one query, a
    corrupt cache - degrades the run to flood-only and is RECORDED with its
    reason, so a reader sees a refusal rather than a damage report of zero.
    The distinction matters more here than anywhere else in the pipeline:
    "no buildings were flooded" and "we could not find out" look identical in a
    count and could not be more different to a rescuer.

    The OVERLAY is inside the same contract, not only the OSM read. `assess`
    and `write_layers` are geometry and disk work - GEOS topology, a GDAL or
    geopandas import, a CRS transform, a full disk - and an escape from either
    would propagate out of `write()` BEFORE run_report.json is written, so a
    damage crash would destroy the flood map's entire provenance. That is the
    opposite of what this stage is for, and the overlay is on by default, so it
    is the live path. The 2026-10-13 audit found exactly that gap: the
    docstring already said NEVER raises while only OsmError was caught.
    `AssertionError` is re-raised, as in `optical_stage`, so a broken
    invariant still fails loudly instead of being filed as a damage problem.
    """
    if osmp is None:
        return None, {"status": OSM_DISABLED,
                      "message": "the damage overlay was not requested (--no-damage)"}
    if snapshot is None:
        try:
            snapshot = osm.load_snapshot(bbox, event_day, out / "cache", osmp,
                                         allow_download=allow_download, log=log)
        except osm.OsmError as err:
            log(f"      OSM REFUSED: {err}")
            return None, {"status": OSM_REFUSED, "message": str(err),
                          "damage_available": False}
    try:
        assessment = damage.assess(result.classes, scene.grid, snapshot.layers,
                                   dmgp if dmgp is not None else DamageParams(), ep, log=log)
        damage.write_layers(out, assessment, log=log)
    except AssertionError:
        raise
    except Exception as err:
        log(f"      DAMAGE OVERLAY FAILED: {type(err).__name__}: {err}")
        return None, {
            "status": OSM_FAILED, "damage_available": False,
            "error_type": type(err).__name__, "message": str(err),
            "osm": snapshot.report(),
            "note": ("the OSM snapshot was read but the overlay could not be computed. "
                     "There is NO damage answer for this run; the flood map and this "
                     "report are unaffected."),
        }
    return assessment, {"status": OSM_USED, "damage_available": True,
                        "osm": snapshot.report(), **assessment.report()}


def cutoff_stage(scene: PreparedScene, result: detect.DetectionResult,
                 rel_codes, out: Path, snapshot, assessment,
                 dp: DetectParams, npar: NetworkParams | None,
                 mc: MonteCarloParams | None, dmgp: DamageParams | None,
                 ep: ExtentParams | None = None, log=print) -> tuple[object | None, dict]:
    """Who is cut off. NEVER raises, for the `damage_stage` reasons.

    Needs the OSM snapshot the damage stage already read, so it runs after it
    and is skipped when that stage produced nothing - with the reason recorded,
    never as "nobody is cut off".
    """
    if snapshot is None:
        return None, {"status": OSM_DISABLED,
                      "message": ("no OpenStreetMap snapshot for this run, so no road "
                                  "network and no cut-off answer. This is NOT a finding "
                                  "that nobody is isolated.")}
    bridges_out = None
    if assessment is not None:
        layer = assessment.layers.get("bridges_out")
        if layer is not None and len(layer) and "@osmId" in layer.columns:
            bridges_out = {str(v) for v in layer["@osmId"]}
    try:
        cutoff = network.assess(
            result.classes, result.z_combined, rel_codes, scene.grid, snapshot.layers,
            z_seed=dp.z_seed, np_=npar, mc=mc, dmgp=dmgp, extent=ep,
            bridges_out_ids=bridges_out, log=log)
        network.write_layers(out, cutoff, scene.grid, log=log)
    except network.NetworkError as err:
        log(f"      CUT-OFF REFUSED: {err}")
        return None, {"status": OSM_REFUSED, "cutoff_available": False,
                      "message": str(err)}
    except AssertionError:
        raise
    except Exception as err:
        log(f"      CUT-OFF ANALYSIS FAILED: {type(err).__name__}: {err}")
        return None, {"status": OSM_FAILED, "cutoff_available": False,
                      "error_type": type(err).__name__, "message": str(err),
                      "note": ("there is NO cut-off answer for this run; the flood map, "
                               "the damage layers and this report are unaffected.")}
    return cutoff, {"status": OSM_USED, "cutoff_available": True, **cutoff.report()}


def write(out: Path, scene: PreparedScene, result: detect.DetectionResult, bbox,
          event_day: date, rp: RunParams, dp: DetectParams, runtime_s: float,
          relp: ReliabilityParams | None = None, ep: ExtentParams | None = None,
          osmp: OsmParams | None = None, dmgp: DamageParams | None = None,
          npar: NetworkParams | None = None, mc: MonteCarloParams | None = None,
          allow_osm_download: bool = True, log=print) -> dict:
    grid, choice = scene.grid, scene.choice
    relp = relp if relp is not None else ReliabilityParams()
    rel = reliability.compute_reliability(
        scene.terrain.hand_m, scene.terrain.slope_deg, result.classes, dp, relp, grid.res)
    outputs.write_geotiff(out / "flood_classes.tif", result.classes, grid, nodata=0)
    outputs.write_geotiff(out / "zscore_combined.tif", result.z_combined.astype(np.float32), grid)
    outputs.write_geotiff(out / "hand_m.tif", scene.terrain.hand_m.astype(np.float32), grid)
    n_poly = outputs.write_polygons(out / "flood_polygons.geojson", result.classes, grid)
    outputs.write_geotiff(out / "reliability_narrow_valley.tif", rel.codes, grid)
    if result.source is not None:
        outputs.write_geotiff(out / "flood_source.tif", result.source, grid)
    outputs.write_reliability_polygons(
        out / "reliability_narrow_valley.geojson", rel.codes, grid)
    outputs.write_quicklook(out / "quicklook.png", result,
                            f"Event {event_day} | track {choice.track[1]} {choice.track[2]} | "
                            f"post {choice.post.day}")
    report = build_report(scene, result, bbox, event_day, rp, dp, runtime_s, n_poly, rel=rel,
                          ep=ep)
    # Last, and outside build_report: the overlay reads the classes that were
    # just written and may refuse, and its parameters are deliberately NOT part
    # of params_hash (config.params_dict), so it contributes a report section
    # rather than a configuration. Always present, like `optical`, so no report
    # is silent about whether a damage answer exists.
    snapshot = None
    if osmp is not None:
        try:
            snapshot = osm.load_snapshot(bbox, event_day, out / "cache", osmp,
                                         allow_download=allow_osm_download, log=log)
        except osm.OsmError as err:
            log(f"      OSM REFUSED: {err}")
            snapshot = None
            report["damage"] = {"status": OSM_REFUSED, "message": str(err),
                                "damage_available": False}
    if "damage" not in report:
        assessment, damage_report = damage_stage(
            bbox, event_day, scene, result, out, osmp, dmgp, ep,
            allow_download=allow_osm_download, snapshot=snapshot, log=log)
        report["damage"] = damage_report
    else:
        assessment = None
    _cutoff, cutoff_report = cutoff_stage(
        scene, result, rel.codes if rel is not None else None, out, snapshot,
        assessment, dp, npar, mc, dmgp, ep, log=log)
    report["cutoff"] = cutoff_report
    outputs.write_report(out / "run_report.json", report)
    return report


def run(bbox, event_day: date, out: Path, rp: RunParams, dp: DetectParams, dry_run: bool,
        op: OpticalParams | None = None, ep: ExtentParams | None = None,
        osmp: OsmParams | None = None, dmgp: DamageParams | None = None,
        npar: NetworkParams | None = None, mc: MonteCarloParams | None = None) -> int:
    t_start = time.time()
    out.mkdir(parents=True, exist_ok=True)
    op = OpticalParams() if op is None else op

    try:
        choice = preflight(bbox, event_day, rp)
    except PipelineError as err:
        raise SystemExit(str(err)) from err
    if choice is None:
        print("NO USABLE SAME-TRACK PAIR. Refusing to map rather than guessing. "
              "Try a later --max-post-latency-days or a smaller area.")
        return 2
    if dry_run:
        return 0

    try:
        scene = prepare(bbox, event_day, out / "cache", rp, dp, choice=choice, op=op)
        print("[7/7] Detecting flood and debris ...")
        result = classify(scene, dp)
    except PipelineError as err:
        raise SystemExit(str(err)) from err

    report = write(out, scene, result, bbox, event_day, rp, dp, time.time() - t_start, ep=ep,
                   osmp=osmp, dmgp=dmgp, npar=npar, mc=mc)
    print(json.dumps(report["areas_ha"], indent=2))
    optical_status = report["optical"]["status"]
    print(f"Sentinel-2 optical evidence: {optical_status}"
          + (f" ({report['optical'].get('message', '')})"
             if optical_status != OPTICAL_USED else
             f", fused: {report['optical']['fused_into_map']}"))
    dmg = report["damage"]
    if dmg["status"] == OSM_USED:
        print(f"Damage overlay (OSM snapshot {dmg['osm']['snapshot']}): "
              f"{dmg['buildings']['damaged']} of {dmg['buildings']['total']} building(s), "
              f"{dmg['roads']['segments_cut']} road segment(s) cut "
              f"({dmg['roads']['length_flooded_km']} km flooded), "
              f"{dmg['bridges']['likely_out']} bridge(s) {BRIDGE_UNVERIFIED_LABEL}")
    else:
        print(f"Damage overlay: {dmg['status'].upper()} - NO damage answer for this run. "
              f"{dmg.get('message', '')}")
    cut = report["cutoff"]
    if cut["status"] == OSM_USED:
        print(f"Cut off ({cut['definition']['gated_definition']}): "
              f"{cut['cut_off_by_the_gated_definition']} of "
              f"{cut['settlements']['total']} settlement(s); "
              f"{cut['probability']['at_or_above_0.5']} at probability >= 0.5 over "
              f"{cut['probability']['runs']} perturbed run(s). The probability is a "
              f"SENSITIVITY measure, not a calibrated chance.")
    else:
        print(f"Cut-off analysis: {cut['status'].upper()} - NO cut-off answer for this run. "
              f"{cut.get('message', '')}")
    print(f"Done in {report['runtime_s']} s -> {out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    ap.add_argument("--event-date", required=True, type=date.fromisoformat)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--res", type=float, default=RunParams.resolution_m)
    ap.add_argument("--n-pre", type=int, default=RunParams.n_pre)
    ap.add_argument("--max-post-latency-days", type=int, default=RunParams.max_post_latency_days)
    ap.add_argument("--backscatter-coefficient", choices=BACKSCATTER_COEFFICIENTS,
                    default=RunParams.backscatter_coefficient,
                    help="openEO backscatter coefficient (default: %(default)s)")
    ap.add_argument("--optical", dest="optical", action="store_true", default=True,
                    help="use Sentinel-2 evidence where the sky is clear (default)")
    ap.add_argument("--no-optical", dest="optical", action="store_false",
                    help="radar only: skip the Sentinel-2 stage entirely")
    ap.add_argument("--optical-post-days", type=int, default=OpticalParams.max_post_latency_days,
                    help="days after the event to look for a clear Sentinel-2 scene "
                         "(default: %(default)s; deposits persist, standing water may not)")
    ap.add_argument("--optical-fusion", choices=OPTICAL_FUSION_MODES,
                    default=OpticalParams.fusion,
                    help="what Sentinel-2 evidence may do to the classes. 'union' (default "
                         "since 2026-10-12, adopted by the leave-one-event-out optical pass): "
                         "optical candidates are added to the map, subject to the same terrain "
                         "and drainage tests as radar. 'off': computed, written and reported, "
                         "but changes no pixel, so the map is the radar-only map")
    ap.add_argument("--no-damage", dest="damage", action="store_false", default=True,
                    help="skip the OpenStreetMap download, the damage overlay AND the "
                         "cut-off analysis (both need the road network). The flood map is "
                         "unaffected; run_report.json records them as disabled")
    ap.add_argument("--osm-buffer-km", type=float, default=OsmParams.buffer_km,
                    help="context beyond the mapped area for OSM, in km (default: "
                         "%(default)s). A settlement can be cut off by a bridge outside the "
                         "flood bbox, so the overlay needs more ground than the map")
    ap.add_argument("--mc-runs", type=int, default=MonteCarloParams.n_runs,
                    help="perturbed runs behind each cut-off probability (default: "
                         "%(default)s). The probability is a sensitivity measure over "
                         "these runs, never a calibrated chance of isolation")
    args = ap.parse_args(argv)

    rp = replace(RunParams(), resolution_m=args.res, n_pre=args.n_pre,
                 max_post_latency_days=args.max_post_latency_days,
                 backscatter_coefficient=args.backscatter_coefficient)
    op = replace(OpticalParams(), enabled=args.optical,
                 max_post_latency_days=args.optical_post_days,
                 fusion=args.optical_fusion)
    osmp = replace(OsmParams(), buffer_km=args.osm_buffer_km) if args.damage else None
    return run(tuple(args.bbox), args.event_date, args.out, rp, DetectParams(), args.dry_run,
               op=op, osmp=osmp, dmgp=DamageParams() if args.damage else None,
               npar=NetworkParams() if args.damage else None,
               mc=replace(MonteCarloParams(), n_runs=args.mc_runs) if args.damage else None)


if __name__ == "__main__":
    sys.exit(main())
