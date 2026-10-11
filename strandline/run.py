"""End-to-end flood mapping for ANY area and date.

    python -m strandline.run --bbox W S E N --event-date YYYY-MM-DD --out DIR [--dry-run]

--dry-run only runs the pre-flight check (catalogue search + track choice),
no login needed. Without it, the first run opens a browser login for the
Copernicus Data Space (free account); the token is then remembered.

The run is split into stages so that an evaluation harness can reuse one
expensive stage many times:

    preflight  -> which track, which dates          (catalogue only, no login)
    fetch      -> Sentinel-1 + DEM on one grid      (openEO, cached)
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
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from strandline import catalog, detect, ingest, outputs, terrain
from strandline.config import BACKSCATTER_COEFFICIENTS, DetectParams, RunParams, params_dict

# DetectParams fields consumed before `classify`, i.e. baked into a prepared
# scene. A caller that varies one of these must prepare the scene again;
# eval/tune.py asserts its grid touches none of them.
PREPROCESS_FIELDS = ("lee_window", "enl")


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

    @property
    def n_pixels(self) -> int:
        return self.grid.width * self.grid.height


def preflight(bbox, event_day: date, rp: RunParams, log=print) -> catalog.TrackChoice | None:
    """Catalogue search and track choice. No login, no download."""
    start = event_day - timedelta(days=rp.search_days_before)
    end = event_day + timedelta(days=rp.max_post_latency_days + 1)
    log(f"[1/6] Searching Sentinel-1 GRD scenes {start} .. {end} ...")
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
                      coefficient: str) -> dict:
    """Where every file of this scene is cached. Pure path arithmetic.

    The fetch script and the scoreboard both ask this function, so a file
    downloaded once is found again rather than downloaded twice.
    """
    tag = f"t{choice.track[1]}_{choice.track[2]}"
    s1 = {acq.day: cache / ingest.s1_cache_name(tag, acq.day, res, coefficient)
          for acq in list(choice.pre) + [choice.post]}
    return {"s1": s1, "dem": cache / f"dem_{int(res)}m.tif",
            "terrain": cache / f"terrain_{int(res)}m.npz"}


def fetch(choice: catalog.TrackChoice, grid: ingest.Grid, cache: Path, rp: RunParams,
          conn=None, log=print) -> dict:
    """Download (or reuse from cache) every raster this scene needs."""
    paths = scene_cache_paths(choice, cache, grid.res, rp.backscatter_coefficient)
    missing = [p for p in list(paths["s1"].values()) + [paths["dem"]] if not p.exists()]
    if not missing:
        log(f"[3/6] All {len(paths['s1']) + 1} rasters already cached")
        return paths
    conn = conn if conn is not None else ingest.connect()
    for acq in list(choice.pre) + [choice.post]:
        path = paths["s1"][acq.day]
        log(f"      S1 {acq.day} -> {path.name}")
        ingest.download_s1(conn, acq, grid, path, rp.backscatter_coefficient, rp.use_sar_mask)
    ingest.download_dem(conn, grid, paths["dem"])
    return paths


def prepare(bbox, event_day: date, cache: Path, rp: RunParams, dp: DetectParams,
            conn=None, choice: catalog.TrackChoice | None = None,
            log=print) -> PreparedScene:
    """preflight + fetch + speckle filter + dB + terrain. Raises PipelineError."""
    if choice is None:
        choice = preflight(bbox, event_day, rp, log)
    if choice is None:
        raise PipelineError(
            "No usable same-track pair: refusing to map rather than guessing. "
            "Try a later --max-post-latency-days or a smaller area.")

    grid = ingest.make_grid(bbox, rp.resolution_m)
    log(f"[2/6] Grid EPSG:{grid.epsg}, {grid.width} x {grid.height} px at {grid.res} m")
    log("[3/6] Downloading via openEO (cached) ...")
    paths = fetch(choice, grid, cache, rp, conn=conn, log=log)

    log("[4/6] Preprocessing (Lee filter, dB, alignment) ...")

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

    log("[5/6] Terrain: slope, drainage, HAND ...")
    terr = terrain.load_or_compute_terrain(dem, grid.transform, grid.crs,
                                           rp.stream_threshold_km2, paths["terrain"], log=log)
    return PreparedScene(
        grid=grid, choice=choice,
        pre_vv_db=np.stack([p[0] for p in pre]), pre_vh_db=np.stack([p[1] for p in pre]),
        post_vv_db=post_vv, post_vh_db=post_vh, terrain=terr, run_params=rp,
        preprocess_params={f: getattr(dp, f) for f in PREPROCESS_FIELDS},
        post_mask_counts=mask_counts,
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
    )


def build_report(scene: PreparedScene, result: detect.DetectionResult, bbox, event_day: date,
                 rp: RunParams, dp: DetectParams, runtime_s: float, n_poly: int | None) -> dict:
    choice, grid, terr = scene.choice, scene.grid, scene.terrain
    params = params_dict(dp, rp)
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
        "parameters_hash": params_hash(dp, rp),
        "areas_ha": outputs.class_areas_ha(result.classes, grid.res),
        "detection_stats": result.stats,
        "terrain": {"stream_pixels": int(terr.streams.sum()),
                    "hand_fallback_fraction": terr.hand_fallback_fraction},
        "runtime_s": round(runtime_s, 1),
    }
    if n_poly is not None:
        report["n_polygons"] = n_poly
    return report


def params_hash(dp: DetectParams, rp: RunParams) -> str:
    return hashlib.sha256(
        json.dumps(params_dict(dp, rp), sort_keys=True).encode()).hexdigest()[:12]


def write(out: Path, scene: PreparedScene, result: detect.DetectionResult, bbox,
          event_day: date, rp: RunParams, dp: DetectParams, runtime_s: float,
          log=print) -> dict:
    grid, choice = scene.grid, scene.choice
    outputs.write_geotiff(out / "flood_classes.tif", result.classes, grid, nodata=0)
    outputs.write_geotiff(out / "zscore_combined.tif", result.z_combined.astype(np.float32), grid)
    outputs.write_geotiff(out / "hand_m.tif", scene.terrain.hand_m.astype(np.float32), grid)
    n_poly = outputs.write_polygons(out / "flood_polygons.geojson", result.classes, grid)
    outputs.write_quicklook(out / "quicklook.png", result,
                            f"Event {event_day} | track {choice.track[1]} {choice.track[2]} | "
                            f"post {choice.post.day}")
    report = build_report(scene, result, bbox, event_day, rp, dp, runtime_s, n_poly)
    outputs.write_report(out / "run_report.json", report)
    return report


def run(bbox, event_day: date, out: Path, rp: RunParams, dp: DetectParams, dry_run: bool) -> int:
    t_start = time.time()
    out.mkdir(parents=True, exist_ok=True)

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
        scene = prepare(bbox, event_day, out / "cache", rp, dp, choice=choice)
        print("[6/6] Detecting flood and debris ...")
        result = classify(scene, dp)
    except PipelineError as err:
        raise SystemExit(str(err)) from err

    report = write(out, scene, result, bbox, event_day, rp, dp, time.time() - t_start)
    print(json.dumps(report["areas_ha"], indent=2))
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
    args = ap.parse_args(argv)

    rp = replace(RunParams(), resolution_m=args.res, n_pre=args.n_pre,
                 max_post_latency_days=args.max_post_latency_days,
                 backscatter_coefficient=args.backscatter_coefficient)
    return run(tuple(args.bbox), args.event_date, args.out, rp, DetectParams(), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
