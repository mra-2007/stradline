"""Run the pipeline on every dev event and control, and score it.

    venv/Scripts/python.exe -m eval.run_scoreboard                      # score, print, write rows
    venv/Scripts/python.exe -m eval.run_scoreboard --no-write           # print only
    venv/Scripts/python.exe -m eval.run_scoreboard --events bosnia_konjic_2024 --write-rasters

What happens per event:

  flood events   the pipeline runs from raw Sentinel-1 + DEM, then the result
                 is compared with the event's reference map through
                 `validation.compare`, selecting reference polygons with the
                 event's own `reference_classes` mapping. Strict and
                 boundary-tolerant precision, recall, F1 and IoU are recorded.
  controls       no reference exists, so the metric is `false_flood_fraction`,
                 the share of the analysed area wrongly called flood.

Rows go to eval/results/flood.csv in the column order declared at the end of
eval/gates.yaml, with `kind` derived by `eval.scoreboard` - so a flood event
whose reference came from our own Sentinel-1 input is written as
`flood_dependent` and can never move a gate.

THREE THINGS THIS SCRIPT REFUSES TO DO

1. It downloads nothing. Rasters come from the cache that
   `eval/fetch_dev_data.py` fills; a missing file is an error naming the
   command that fetches it, not a silent skip. `--allow-download` overrides.
2. It never writes a partial set of rows for a kind. If any planned flood
   event fails, no flood row is written at all - otherwise the gate would
   quietly be computed over whichever events happened to succeed, which is
   Rule 3.2 ("excluding hard pixels, areas or events").
3. It never invents a number. An event that cannot run is reported as a
   failure with its error, and the gate stays NO DATA.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path

import yaml

from eval import scoreboard as sb
from eval.fetch_dev_data import EVAL_RUNS, cache_dir, coverage, dev_run_params, select_events
from strandline import ingest, optical, outputs, run as pipeline
from strandline.config import (
    CLASS_DEBRIS,
    DEV_N_PRE,
    FUSION_OFF,
    DetectParams,
    OpticalParams,
    RunParams,
)
from validation import compare

ROOT = Path(__file__).resolve().parents[1]
GATES = ROOT / "eval" / "gates.yaml"

# An event larger than this is refused rather than resampled or clipped: the
# prepared stack would not fit in memory, and Rule 3.2 forbids shrinking the
# scoring area to make it fit. events.yaml says the same thing about its one
# oversized event ("drop the event rather than clip it").
MAX_SCENE_PIXELS = 20_000_000

METRIC_COLUMNS = ("precision", "recall", "f1", "iou",
                  "precision_tolerant", "recall_tolerant", "f1_tolerant",
                  "false_flood_fraction")


def contract(path: Path | None = None) -> dict:
    """The scoring contract from the (human-owned) gates file.

    Read, never hardcoded: resolution and boundary tolerance are part of what
    is measured, and a copy here could drift away from the gate.
    """
    cfg = yaml.safe_load((path or GATES).read_text(encoding="utf-8"))
    return cfg["contract"]


@dataclass
class EventScore:
    event: str
    kind: str
    params_hash: str
    metrics: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def value(self, metric: str):
        v = self.metrics.get(metric)
        return None if v is None else float(v)


# --- paths --------------------------------------------------------------------

def score_path(event_name: str, params_hash: str, runs_root: Path | None = None) -> Path:
    """One (event, parameter set) score. These files are the score matrix the
    tuner reuses, so a stopped tuning run resumes instead of recomputing."""
    return (runs_root or EVAL_RUNS) / event_name / "scores" / f"{params_hash}.json"


def load_score(event_name: str, params_hash: str, runs_root: Path | None = None
               ) -> EventScore | None:
    path = score_path(event_name, params_hash, runs_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("params_hash") != params_hash:
        return None
    return EventScore(**{k: data.get(k) for k in
                         ("event", "kind", "params_hash", "metrics", "provenance", "error")})


def save_score(score: EventScore, runs_root: Path | None = None) -> Path:
    path = score_path(score.event, score.params_hash, runs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(score), indent=2, default=str), encoding="utf-8")
    return path


# --- running one event --------------------------------------------------------

def _rel(path: Path) -> str:
    """Repo-relative where possible (a test may point outside the repo)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def event_bbox(event: dict) -> tuple:
    return tuple(float(v) for v in event["bbox"])


def event_day(event: dict) -> date:
    return date.fromisoformat(str(event["event_date"]))


def missing_cache(event: dict, rp: RunParams, runs_root: Path | None = None,
                  choice=None, op: OpticalParams | None = None,
                  pair=None) -> list[Path]:
    """Which rasters this event's run needs and does not have.

    With `op`, the chosen Sentinel-2 pair counts too, so an optical scoring run
    refuses with the fetch command rather than opening an interactive login
    mid-way through a grid search. `pair=None` and `op` given means the pair is
    pre-flighted here; pass it in when the caller already has it.
    """
    bbox = event_bbox(event)
    if choice is None:
        choice = pipeline.preflight(bbox, event_day(event), rp, log=lambda *_: None)
    if choice is None:
        raise pipeline.PipelineError(
            f"{event['name']}: no usable same-track pair; the event cannot be scored.")
    cache = cache_dir(str(event["name"]), runs_root)
    grid = ingest.make_grid(bbox, rp.resolution_m)
    paths = pipeline.scene_cache_paths(choice, cache, grid.res, rp.backscatter_coefficient,
                                       rp.elevation_model)
    gaps = [p for p in list(paths["s1"].values()) + [paths["dem"]] if not p.exists()]
    if op is not None and op.enabled:
        if pair is None:
            pair = pipeline.optical_preflight(bbox, event_day(event), op,
                                              log=lambda *_: None)
        # No clear pair is not a gap: the event is scored radar-only, which is
        # the honest outcome under cloud, not a missing download.
        for day in ([pair.pre.day, pair.post.day] if pair is not None else []):
            gaps += [p for p in optical.s2_cache_paths(cache, day, grid.res).values()
                     if not p.exists()]
    return gaps


def prepare_event(event: dict, rp: RunParams, dp: DetectParams,
                  runs_root: Path | None = None, allow_download: bool = False,
                  max_pixels: int = MAX_SCENE_PIXELS, op: OpticalParams | None = None,
                  log=print) -> pipeline.PreparedScene:
    """Pre-flight, read the cache and build the prepared scene. No downloads.

    `op=None` (the default) is a radar-only preparation: the Sentinel-2 stage is
    not entered at all, which is what every already-recorded dev score was
    produced with, so their params_hash stays reproducible.
    """
    bbox = event_bbox(event)
    grid = ingest.make_grid(bbox, rp.resolution_m)
    n_px = grid.width * grid.height
    if n_px > max_pixels:
        raise pipeline.PipelineError(
            f"{event['name']}: {grid.width} x {grid.height} = {n_px / 1e6:.1f} Mpx exceeds the "
            f"{max_pixels / 1e6:.0f} Mpx budget. Refusing to run: clipping or resampling the "
            f"area to fit would change what is measured (Rule 3.2). Raise --max-pixels on a "
            f"machine with more memory, or report this event as not scored.")
    choice = pipeline.preflight(bbox, event_day(event), rp, log=log)
    if choice is None:
        raise pipeline.PipelineError(
            f"{event['name']}: no usable same-track pair with n_pre={rp.n_pre}.")
    if not allow_download:
        gaps = missing_cache(event, rp, runs_root, choice, op)
        if gaps:
            raise pipeline.PipelineError(
                f"{event['name']}: {len(gaps)} cached raster(s) missing, first "
                f"{gaps[0].name}. Fetch them first:\n"
                f"  venv/Scripts/python.exe -m eval.fetch_dev_data --events {event['name']}"
                + (" --optical" if op is not None and op.enabled else ""))
    return pipeline.prepare(bbox, event_day(event), cache_dir(str(event["name"]), runs_root),
                            rp, dp, choice=choice, op=op, log=log)


def optical_provenance(scene: pipeline.PreparedScene) -> dict:
    """What Sentinel-2 contributed to this score, whether or not it contributed.

    Always written, so no score file can be silent about optical: `status` says
    what the stage did, and for a refusal the clear fraction says how much of the
    area optical could actually see. A radar-only preparation records
    `status: disabled`, which is exactly what it was.
    """
    report = dict(scene.optical_report)
    op = scene.optical_params
    out = {
        "status": report.get("status"),
        "enabled": bool(op is not None and op.enabled),
        "fusion": (None if op is None else op.fusion),
        "fused_into_map": scene.optical_evidence is not None
                          and scene.optical_evidence.fusion != FUSION_OFF,
        "message": report.get("message"),
    }
    evidence = report.get("evidence") or {}
    for key in ("clear_fraction_pre", "clear_fraction_post", "clear_fraction_pair",
                "pre_day", "post_day", "post_latency_days",
                "optical_water_candidate_pixels", "optical_debris_candidate_pixels"):
        if key in evidence:
            out[key] = evidence[key]
    if "reflectance_conversion" in evidence:
        out["reflectance_conversion"] = evidence["reflectance_conversion"]
    if op is not None:
        out["parameters"] = asdict(op)
    return out


def score_with_scene(event: dict, scene: pipeline.PreparedScene, dp: DetectParams,
                     tolerance_px: int = 1, out_dir: Path | None = None,
                     op: OpticalParams | None = None, log=print) -> EventScore:
    """Classify an already-prepared scene and score it. The cheap stage.

    THE PRIMARY METRIC is flood-affected area: our water OR debris classes,
    against the union of the event's reference `water` and `debris` classes.
    Track B asks for flooded and debris-covered areas, and this is what the
    gate columns of flood.csv carry. Decided and recorded in
    docs/tuning_log.md on 2026-10-11, before the rough_water mode was scored.

    Water-only scores (our flood classes against the reference `water` class
    alone) are computed too and kept in the score file, so both readings of
    every event are always available.

    Controls have no reference, so they are scored on the share of the analysed
    area called flood-affected - the same union, so the 1% ceiling constrains
    the debris channel as well.
    """
    t0 = time.time()
    rp = scene.run_params
    kind = sb.row_kind(event)
    result = pipeline.classify(scene, dp)
    affected = compare.flood_affected_mask(result.classes)
    water_pred = compare.flood_mask(result.classes, include_debris=False)
    domain = compare.scoring_domain(result.classes)
    grid = scene.grid
    px_ha = grid.res * grid.res / 10_000.0

    provenance = {
        "bbox_wgs84": list(event_bbox(event)),
        "event_date": str(event_day(event)),
        "grid": {"epsg": grid.epsg, "resolution_m": grid.res,
                 "width": grid.width, "height": grid.height},
        "track": {"relative_orbit": scene.choice.track[1], "direction": scene.choice.track[2],
                  "post_day": str(scene.choice.post.day),
                  "post_latency_days": scene.choice.latency_days,
                  "pre_days": [str(a.day) for a in scene.choice.pre]},
        "n_pre": len(scene.choice.pre),
        "mode": dp.mode,
        "backscatter_coefficient": rp.backscatter_coefficient,
        "elevation_model": rp.elevation_model,
        "scored_quantity": "flood_affected = flood classes UNION debris class",
        "domain_pixels": int(domain.sum()),
        "domain_fraction": round(float(domain.mean()), 4),
        "predicted_flood_affected_ha": round(float(affected.sum()) * px_ha, 2),
        "predicted_water_only_ha": round(float(water_pred.sum()) * px_ha, 2),
        "tolerance_px": tolerance_px,
        "water_threshold": {k: result.stats[k] for k in result.stats
                            if k.startswith("water_threshold")},
        "areas_ha": outputs.class_areas_ha(result.classes, grid.res),
        "parameters": {"detect": asdict(dp), "run": asdict(rp)},
        "optical": optical_provenance(scene),
        "scored_s": None,
    }

    metrics: dict = {}
    if kind == sb.KIND_CONTROL:
        metrics["false_flood_fraction"] = compare.false_flood_fraction(affected, domain)
        provenance["false_flood_fraction_water_only"] = compare.false_flood_fraction(
            water_pred, domain)
    else:
        ref_path = ROOT / str(event["reference"])
        if not ref_path.exists():
            raise pipeline.PipelineError(
                f"{event['name']}: reference {ref_path} is missing. Fetch it with "
                f"eval/fetch_references.py; scoring without it is not possible.")
        classes_map = event.get("reference_classes") or {}
        union_values = compare.reference_class_values(classes_map, "water", "debris")
        ref, selection = compare.reference_mask(ref_path, grid, union_values)
        metrics.update(compare.flood_metrics(affected, ref, domain, tolerance_px))
        provenance["reference"] = {
            "path": _rel(ref_path),
            "scored_classes": union_values,
            "selection": asdict(selection),
            "selected_by": selection.describe(),
            "reference_ha": round(float((ref & domain).sum()) * px_ha, 2),
            "reference_fraction_of_domain": round(float((ref & domain).sum())
                                                  / max(int(domain.sum()), 1), 6),
            "credit": compare.REFERENCE_CREDIT,
            "independent_reference": event.get("independent_reference"),
            "ref_sensor_name": event.get("ref_sensor_name"),
        }

        # Water-only, reported next to the primary numbers, never gated.
        water_values = compare.reference_class_values(classes_map, "water")
        if water_values:
            w_ref, w_sel = compare.reference_mask(ref_path, grid, water_values)
            provenance["water_only"] = {
                "scored_classes": water_values,
                "selected_by": w_sel.describe(),
                "reference_ha": round(float((w_ref & domain).sum()) * px_ha, 2),
                "metrics": compare.flood_metrics(water_pred, w_ref, domain, tolerance_px),
            }

        # Debris alone, where the reference declares it. One dev event does.
        debris_values = compare.reference_class_values(classes_map, "debris")
        if debris_values:
            d_ref, d_sel = compare.reference_mask(ref_path, grid, debris_values)
            provenance["debris_only"] = {
                "scored_classes": debris_values,
                "selected_by": d_sel.describe(),
                "reference_ha": round(float((d_ref & domain).sum()) * px_ha, 2),
                "metrics": compare.flood_metrics(result.classes == CLASS_DEBRIS, d_ref,
                                                 domain, tolerance_px),
            }

    provenance["scored_s"] = round(time.time() - t0, 1)
    score = EventScore(event=str(event["name"]), kind=kind,
                       params_hash=pipeline.params_hash(dp, rp, op),
                       metrics={k: metrics[k] for k in METRIC_COLUMNS if k in metrics},
                       provenance=provenance)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        pipeline.write(out_dir, scene, result, event_bbox(event), event_day(event), rp, dp,
                       time.time() - t0, log=log)
        (out_dir / "score.json").write_text(json.dumps(asdict(score), indent=2, default=str),
                                            encoding="utf-8")
    return score


def score_event(event: dict, dp: DetectParams, rp: RunParams, runs_root: Path | None = None,
                *, tolerance_px: int = 1, allow_download: bool = False, reuse: bool = True,
                max_pixels: int = MAX_SCENE_PIXELS, out_dir: Path | None = None,
                op: OpticalParams | None = None, log=print) -> EventScore:
    """One event, one parameter set, from the cache. Errors are returned, not raised."""
    name = str(event["name"])
    phash = pipeline.params_hash(dp, rp, op)
    if reuse and out_dir is None:
        cached = load_score(name, phash, runs_root)
        if cached is not None and cached.ok:
            log(f"  {name}: score {phash} from cache")
            return cached
    try:
        scene = prepare_event(event, rp, dp, runs_root, allow_download, max_pixels, op=op,
                              log=log)
        score = score_with_scene(event, scene, dp, tolerance_px, out_dir=out_dir, op=op,
                                 log=log)
    except KeyboardInterrupt:
        raise
    # Deliberately broad: an orchestrator must survive one bad event (a missing
    # raster, a corrupt reference, a memory refusal) and report it. The error
    # text is kept, printed and written to the score file, and NOTHING is
    # scored in its place - the event counts as not scored, which blocks the
    # gate rather than quietly shrinking it (Rules 2 and 6).
    except Exception as err:
        score = EventScore(event=name, kind=sb.row_kind(event), params_hash=phash,
                           error=f"{type(err).__name__}: {err}")
    save_score(score, runs_root)
    return score


# --- writing rows -------------------------------------------------------------

def rows_for(scores: list[EventScore], events: dict[str, dict], run_id: str,
             split: str) -> list[dict]:
    return [sb.flood_row(events[s.event], s.metrics, run_id=run_id,
                         params_hash=s.params_hash, split=split)
            for s in scores if s.ok]


def completeness(scores: list[EventScore], planned: list[dict]) -> dict[str, dict]:
    """Per CSV kind: did every planned event of that kind actually score?

    A kind that is incomplete must not have rows written, or the gate would be
    an average over whichever events happened to work.
    """
    done = {s.event for s in scores if s.ok}
    out: dict[str, dict] = {}
    for kind in (sb.KIND_FLOOD, sb.KIND_FLOOD_DEPENDENT, sb.KIND_CONTROL):
        names = [str(e["name"]) for e in planned if sb.row_kind(e) == kind]
        if not names:
            continue
        out[kind] = {"planned": len(names), "scored": len([n for n in names if n in done]),
                     "missing": [n for n in names if n not in done]}
        out[kind]["complete"] = not out[kind]["missing"]
    return out


def write_rows(scores: list[EventScore], planned: list[dict], events: dict[str, dict],
               run_id: str, split: str, results_dir: Path | None = None,
               log=print) -> dict[str, int]:
    """Append rows for every COMPLETE kind. Returns rows written per kind."""
    status = completeness(scores, planned)
    written: dict[str, int] = {}
    for kind, info in status.items():
        chosen = [s for s in scores if s.ok and s.kind == kind]
        if not info["complete"]:
            log(f"  NOT WRITING {len(chosen)} {kind} row(s): {len(info['missing'])} of "
                f"{info['planned']} planned event(s) did not score "
                f"({', '.join(info['missing'])}). A gate averaged over the rest would be "
                f"measured on a set chosen by what happened to work (Rule 3.2).")
            continue
        if chosen:
            sb.append_rows("flood.csv", rows_for(chosen, events, run_id, split),
                           results_dir=results_dir)
            written[kind] = len(chosen)
    return written


# --- reporting ----------------------------------------------------------------

def print_table(scores: list[EventScore], log=print) -> None:
    head = (f"{'event':<28} {'kind':<16} {'mode':<12} {'ref ha':>8} "
            f"{'P':>6} {'R':>6} {'F1':>6} {'IoU':>6} | "
            f"{'P tol':>6} {'R tol':>6} {'F1 tol':>6} {'FFR':>7}")
    log(head)
    log("-" * len(head))
    for s in scores:
        if not s.ok:
            log(f"{s.event:<28} {s.kind:<16} {'FAILED':<12}  {s.error[:80]}")
            continue
        p = s.provenance
        mode = str(p.get("mode", "?"))
        ref_ha = p.get("reference", {}).get("reference_ha")
        f = lambda k: ("-" if s.value(k) is None else f"{s.value(k):.4f}")  # noqa: E731
        log(f"{s.event:<28} {s.kind:<16} {mode:<12} "
            f"{('-' if ref_ha is None else f'{ref_ha:>8.1f}'):>8} "
            f"{f('precision'):>6} {f('recall'):>6} {f('f1'):>6} {f('iou'):>6} | "
            f"{f('precision_tolerant'):>6} {f('recall_tolerant'):>6} "
            f"{f('f1_tolerant'):>6} {f('false_flood_fraction'):>7}")


def print_coverage(scores: list[EventScore], plan: str, log=print) -> None:
    cov = coverage([s.event for s in scores if s.ok], plan)
    log("\nCoverage (scored / planned / declared in events.yaml):")
    for kind, info in cov.items():
        if not info["declared"]:
            continue
        log(f"  {kind:<16} {info['scored']} / {info['planned']} / {info['declared']}"
            + (f"   not scored: {', '.join(info['missing'])}" if info["missing"] else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--plan", default="gates", choices=["gates", "all"])
    ap.add_argument("--events", nargs="*")
    ap.add_argument("--coefficient", help="override the backscatter coefficient")
    ap.add_argument("--n-pre", type=int, default=DEV_N_PRE)
    ap.add_argument("--split", default="full", help="the `split` column value (default: full)")
    ap.add_argument("--no-write", action="store_true", help="print only, append no rows")
    ap.add_argument("--write-rasters", action="store_true",
                    help="also write every pipeline output per event (slow, large)")
    ap.add_argument("--allow-download", action="store_true",
                    help="permit openEO downloads for anything missing from the cache")
    ap.add_argument("--no-reuse", action="store_true", help="ignore cached scores")
    ap.add_argument("--max-pixels", type=int, default=MAX_SCENE_PIXELS)
    ap.add_argument("--runs-root", type=Path, default=EVAL_RUNS)
    ap.add_argument("--results-dir", type=Path, default=None)
    args = ap.parse_args(argv)

    con = contract()
    events = sb.load_events()
    planned = select_events(args.plan, args.events, events)
    dp = DetectParams()
    rp = dev_run_params(args.coefficient or RunParams.backscatter_coefficient, args.n_pre)
    if rp.resolution_m != con["resolution_m"]:
        rp = replace(rp, resolution_m=float(con["resolution_m"]))
    run_id = sb.new_run_id()

    print(f"run_id {run_id}  params {pipeline.params_hash(dp, rp)}  "
          f"coefficient {rp.backscatter_coefficient}  n_pre {rp.n_pre}  "
          f"tolerance {con['boundary_tolerance_px']} px")
    print(f"{len(planned)} event(s) in plan '{args.plan}'\n")

    scores: list[EventScore] = []
    for ev in planned:
        name = str(ev["name"])
        print(f"[{len(scores) + 1}/{len(planned)}] {name}")
        out_dir = (args.runs_root / name / pipeline.params_hash(dp, rp)
                   if args.write_rasters else None)
        scores.append(score_event(
            ev, dp, rp, args.runs_root, tolerance_px=int(con["boundary_tolerance_px"]),
            allow_download=args.allow_download, reuse=not args.no_reuse,
            max_pixels=args.max_pixels, out_dir=out_dir,
            log=lambda *a: print("     ", *a)))
        if not scores[-1].ok:
            print(f"      FAILED: {scores[-1].error}")

    print()
    print_table(scores)
    print_coverage(scores, args.plan)

    if args.no_write:
        print("\n--no-write: no rows appended.")
    else:
        print()
        written = write_rows(scores, planned, events, run_id, args.split, args.results_dir)
        total = sum(written.values())
        print(f"  {total} row(s) appended to eval/results/flood.csv "
              + (f"({', '.join(f'{k}={v}' for k, v in written.items())})" if written else ""))
        if total:
            print("  check the gates: venv/Scripts/python.exe -m eval.check_gates")

    failed = [s for s in scores if not s.ok]
    if failed:
        print(f"\n{len(failed)} event(s) did not score:")
        for s in failed:
            print(f"  {s.event}: {s.error}")
        return 1
    return 0


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.exit(main())
