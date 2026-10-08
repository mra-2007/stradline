"""End-to-end flood mapping for ANY area and date.

    python -m strandline.run --bbox W S E N --event-date YYYY-MM-DD --out DIR [--dry-run]

--dry-run only runs the pre-flight check (catalogue search + track choice),
no login needed. Without it, the first run opens a browser login for the
Copernicus Data Space (free account); the token is then remembered.
"""

import argparse
import hashlib
import json
import sys
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from strandline import catalog, detect, ingest, outputs, terrain
from strandline.config import DetectParams, RunParams, params_dict


def _preflight(bbox, event_day: date, rp: RunParams):
    start = event_day - timedelta(days=rp.search_days_before)
    end = event_day + timedelta(days=rp.max_post_latency_days + 1)
    print(f"[1/6] Searching Sentinel-1 GRD scenes {start} .. {end} ...")
    try:
        scenes = catalog.search_s1_grd(bbox, start, end)
    except Exception as err:  # network / service problems: say so clearly
        raise SystemExit(f"Catalogue search failed: {err}\n"
                         "Check the internet connection and https://dataspace.copernicus.eu status.")
    acqs = catalog.group_acquisitions(scenes, catalog.aoi_polygon(bbox))
    print(f"      {len(scenes)} scenes -> {len(acqs)} acquisitions on "
          f"{len({a.track for a in acqs})} tracks")
    choice, log = catalog.choose_track(acqs, event_day, rp)
    for line in log:
        print("      " + line)
    return choice


def run(bbox, event_day: date, out: Path, rp: RunParams, dp: DetectParams, dry_run: bool) -> int:
    t_start = time.time()
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "cache"

    choice = _preflight(bbox, event_day, rp)
    if choice is None:
        print("NO USABLE SAME-TRACK PAIR. Refusing to map rather than guessing. "
              "Try a later --max-post-latency-days or a smaller area.")
        return 2
    print(f"      chosen: track {choice.track[1]} {choice.track[2]}, post {choice.post.day} "
          f"(+{choice.latency_days} d), pre {[str(a.day) for a in choice.pre]}")
    for note in choice.notes:
        print("      NOTE: " + note)
    if dry_run:
        return 0

    grid = ingest.make_grid(bbox, rp.resolution_m)
    print(f"[2/6] Grid EPSG:{grid.epsg}, {grid.width} x {grid.height} px at {grid.res} m")

    print("[3/6] Downloading via openEO (cached) ...")
    conn = ingest.connect()
    tag = f"t{choice.track[1]}_{choice.track[2]}"
    files = {}
    for acq in choice.pre + [choice.post]:
        path = cache / f"s1_{tag}_{acq.day}_{int(grid.res)}m.tif"
        print(f"      S1 {acq.day} -> {path.name}")
        files[acq.day] = ingest.download_s1(conn, acq, grid, path, rp.backscatter_coefficient,
                                            rp.use_sar_mask)
    dem_path = ingest.download_dem(conn, grid, cache / f"dem_{int(grid.res)}m.tif")

    print("[4/6] Preprocessing (Lee filter, dB, alignment) ...")

    def load(day):
        arr = ingest.read_on_grid(files[day], grid)
        if ingest.looks_like_db(arr[0]):
            raise RuntimeError(f"{files[day].name} looks like dB already; expected linear")
        vv, vh = detect.preprocess(arr[0], dp), detect.preprocess(arr[1], dp)
        mask = arr[2] if arr.shape[0] > 2 else None
        return vv, vh, mask

    pre = [load(a.day) for a in choice.pre]
    post_vv, post_vh, post_mask = load(choice.post.day)
    if post_mask is not None:
        vals, counts = np.unique(post_mask[np.isfinite(post_mask)], return_counts=True)
        print(f"      SAR mask band values (check encoding!): {dict(zip(vals.tolist(), counts.tolist()))}")
    dem = ingest.read_on_grid(dem_path, grid)[0].astype(np.float64)

    print("[5/6] Terrain: slope, drainage, HAND ...")
    terr = terrain.compute_terrain(dem, grid.transform, grid.crs, rp.stream_threshold_km2)

    print("[6/6] Detecting flood and debris ...")
    result = detect.detect_flood(
        np.stack([p[0] for p in pre]), np.stack([p[1] for p in pre]), post_vv, post_vh,
        terr.hand_m, terr.slope_deg, terr.streams, dp,
        sar_invalid=None,  # enable once the mask encoding is verified
    )

    outputs.write_geotiff(out / "flood_classes.tif", result.classes, grid, nodata=0)
    outputs.write_geotiff(out / "zscore_combined.tif", result.z_combined.astype(np.float32), grid)
    outputs.write_geotiff(out / "hand_m.tif", terr.hand_m.astype(np.float32), grid)
    n_poly = outputs.write_polygons(out / "flood_polygons.geojson", result.classes, grid)
    outputs.write_quicklook(out / "quicklook.png", result,
                            f"Event {event_day} | track {choice.track[1]} {choice.track[2]} | "
                            f"post {choice.post.day}")

    report = {
        "event_date": str(event_day),
        "bbox_wgs84": list(bbox),
        "grid": {"epsg": grid.epsg, "resolution_m": grid.res, "width": grid.width,
                 "height": grid.height},
        "track": {"relative_orbit": choice.track[1], "direction": choice.track[2],
                  "post": {"day": str(choice.post.day), "scenes": choice.post.names},
                  "pre": [{"day": str(a.day), "scenes": a.names} for a in choice.pre],
                  "post_latency_days": choice.latency_days, "notes": choice.notes},
        "parameters": params_dict(dp, rp),
        "parameters_hash": hashlib.sha256(
            json.dumps(params_dict(dp, rp), sort_keys=True).encode()).hexdigest()[:12],
        "areas_ha": outputs.class_areas_ha(result.classes, grid.res),
        "detection_stats": result.stats,
        "terrain": {"stream_pixels": int(terr.streams.sum()),
                    "hand_fallback_fraction": terr.hand_fallback_fraction},
        "n_polygons": n_poly,
        "runtime_s": round(time.time() - t_start, 1),
    }
    outputs.write_report(out / "run_report.json", report)
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
    args = ap.parse_args(argv)

    rp = replace(RunParams(), resolution_m=args.res, n_pre=args.n_pre,
                 max_post_latency_days=args.max_post_latency_days)
    return run(tuple(args.bbox), args.event_date, args.out, rp, DetectParams(), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
