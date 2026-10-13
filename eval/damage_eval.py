"""Score the damage overlay: our flood extent vs the reference extent.

    venv/Scripts/python.exe -m eval.damage_eval
    venv/Scripts/python.exe -m eval.damage_eval --events <dev_event_name> --no-write

CHECKING ONLY. Reference maps are read here and never by `strandline/`.

WHAT IS MEASURED, STATED PLAINLY

The gates file declares this module's metrics as "our flood map vs reference
extent". So for each gate flood event the SAME overlay code is run twice over
the SAME pre-event OpenStreetMap snapshot, changing only the flood extent it is
given:

    ours       the pipeline's own flood extent for that event, at the
               configuration its leave-one-event-out fold was scored with
    reference  the event's reference polygons, selected with the event's own
               `reference_classes` (water UNION debris) - exactly the mask
               `eval/run_scoreboard.py` scores the flood map against

`building_f1` and `road_segment_f1` then compare the two FLAGGED SETS as sets
of OSM features: a building is a true positive when both extents flag it, a
false positive when only ours does, a miss when only the reference's does.

WHAT THIS NUMBER IS NOT

It is not a measurement of whether a building was really damaged. Nobody
inspected these buildings; both sides of the comparison are inferences from a
flood polygon, and both read the same OSM. What it measures is whether OUR
flood extent leads to the same damage conclusions as an expert flood extent -
which is the honest question an overlay with no damage reference can answer,
and it is why a high score here would NOT license the claim "we find damaged
buildings with F1 0.9x". Every row written carries that distinction in this
docstring and in the printed summary.

It also inherits the flood gate. The flood map currently reads FAIL, so these
numbers describe the damage consequences of a flood map that is itself not
accurate enough; they are not a separate success.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from eval import run_scoreboard as rs
from eval import scoreboard as sb
from eval.diagnose_errors import latest_loeo, params_from_score
from eval.fetch_dev_data import EVAL_RUNS, select_events
from strandline import damage, osm
from strandline.config import DamageParams, ExtentParams, OsmParams
from validation import compare

ROOT = Path(__file__).resolve().parents[1]


def _f1(tp: int, fp: int, fn: int) -> float:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return round(2 * precision * recall / (precision + recall), 4) if (precision + recall) else 0.0


def set_metrics(ours: set, theirs: set) -> dict:
    """Precision, recall and F1 of two flagged sets of feature ids.

    Both sets EMPTY is reported as f1 None, not 1.0: "neither extent flagged
    anything" is agreement, but calling it a perfect score would let an event
    with no features at all contribute a 1.0 to a gate average. The gate's
    `min_rows` then sees a row it cannot use, which is the honest outcome.
    """
    tp = len(ours & theirs)
    fp = len(ours - theirs)
    fn = len(theirs - ours)
    if not ours and not theirs:
        return {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None, "f1": None,
                "note": "neither extent flagged any feature; nothing to score"}
    precision = round(tp / (tp + fp), 4) if (tp + fp) else None
    recall = round(tp / (tp + fn), 4) if (tp + fn) else None
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall,
            "f1": _f1(tp, fp, fn)}


def feature_ids(gdf, id_column: str) -> set:
    """The ids of a flagged layer, for set comparison.

    `@osmId` for buildings (one OSM element each) and `segment_id` for roads
    (an element can split into several segments, and a segment is the thing
    that is cut). A feature with no id would silently collapse into one entry,
    so it is given a positional fallback that cannot collide.
    """
    if len(gdf) == 0 or id_column not in gdf.columns:
        return set()
    out = set()
    for i, value in enumerate(gdf[id_column]):
        out.add(str(value) if value is not None and str(value) != "nan" else f"#row{i}")
    return out


# --- one event ----------------------------------------------------------------

@dataclass
class DamageScore:
    event: str
    params_hash: str
    building_f1: float | None = None
    road_segment_f1: float | None = None
    detail: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and None not in (self.building_f1, self.road_segment_f1)


def reference_extent_geometry(event: dict, grid, dmgp: DamageParams):
    """The reference flood extent, as a geometry on OUR grid, buffered as ours is.

    The same `reference_classes` union (water + debris) that the flood gate is
    scored against, rasterised onto the analysis grid and then put through the
    identical buffer and polygonisation as our own extent. Identical treatment
    is the point: any difference in the two flagged sets must come from the
    extent, not from how the two were turned into geometry.
    """
    ref_path = ROOT / str(event["reference"])
    if not ref_path.exists():
        raise rs.pipeline.PipelineError(
            f"{event['name']}: reference {ref_path} is missing. Fetch it with "
            f"eval/fetch_references.py; scoring without it is not possible.")
    values = compare.reference_class_values(event.get("reference_classes") or {},
                                            "water", "debris")
    mask, _sel = compare.reference_mask(ref_path, grid, values)
    # A boolean mask straight into the same helper the pipeline uses, by giving
    # it a one-class "class map": True becomes the single extent class.
    classes = np.where(mask, int(ExtentParams().flood_extent_classes()[0]), 1).astype(np.uint8)
    return damage.extent_geometry(classes, grid, ExtentParams(), dmgp.buffer_px)


def detail_path(event_name: str, params_hash: str, runs_root: Path | None = None) -> Path:
    """Where one event's full damage evidence is persisted.

    Rule 3.8: the results CSV carries only the two gated F1s, but README and
    docs/open_items.md quote per-event counts ("ours flagged 7, the reference
    flagged 1"), the OSM feature totals and the parameters. Before this
    existed, those numbers lived nowhere on disk and the 2026-10-13 audit had
    to re-run the scorer to confirm them. The file is named and keyed exactly
    as `eval/run_scoreboard.score_path` names a flood score, so the two sit
    side by side under the same params_hash.
    """
    return ((runs_root or EVAL_RUNS) / event_name / "scores"
            / f"damage_{params_hash}.json")


def save_detail(score: DamageScore, runs_root: Path | None = None) -> Path | None:
    """Persist the evidence behind one event's two F1s. Returns the path."""
    if score.error is not None or not score.detail:
        return None
    path = detail_path(score.event, score.params_hash, runs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"event": score.event, "params_hash": score.params_hash,
         "building_f1": score.building_f1, "road_segment_f1": score.road_segment_f1,
         **score.detail}, indent=2, default=str), encoding="utf-8")
    return path


def score_event(event: dict, params_hash: str, runs_root: Path,
                osmp: OsmParams | None = None, dmgp: DamageParams | None = None,
                allow_osm_download: bool = True, log=print) -> DamageScore:
    """Both overlays for one event. Errors are returned, not raised."""
    name = str(event["name"])
    osmp = osmp if osmp is not None else OsmParams()
    dmgp = dmgp if dmgp is not None else DamageParams()
    score = DamageScore(event=name, params_hash=params_hash)
    stored = rs.load_score(name, params_hash, runs_root)
    if stored is None or not stored.ok:
        score.error = f"no usable flood score file {params_hash}.json for {name}"
        return score
    try:
        dp, rp, op, ep = params_from_score(stored)
        scene = rs.prepare_event(event, rp, dp, runs_root, allow_download=False, op=op,
                                 log=lambda *_: None)
        result = rs.pipeline.classify(scene, dp)
        grid = scene.grid

        snapshot = osm.load_snapshot(rs.event_bbox(event), rs.event_day(event),
                                     rs.cache_dir(name, runs_root), osmp,
                                     allow_download=allow_osm_download, log=log)
        ours = damage.assess(result.classes, grid, snapshot.layers, dmgp, ep, log=log)

        ref_geom = reference_extent_geometry(event, grid, dmgp)
        # The reference side reuses our already-split segments and already-read
        # buildings, so the two sides differ ONLY in the extent.
        ref_buildings = damage.damaged_buildings(
            osm.to_geodataframe(snapshot.layers.get("buildings") or {}, grid.crs), ref_geom)
        all_segments = damage.split_at_junctions(
            osm.to_geodataframe(snapshot.layers.get("roads") or {}, grid.crs),
            dmgp.min_segment_m)
        ref_roads = damage.cut_road_segments(all_segments, ref_geom)

        b_ours = feature_ids(ours.layers["damaged_buildings"], "@osmId")
        b_ref = feature_ids(ref_buildings, "@osmId")
        r_ours = feature_ids(ours.layers["cut_roads"], "segment_id")
        r_ref = feature_ids(ref_roads, "segment_id")

        buildings = set_metrics(b_ours, b_ref)
        roads = set_metrics(r_ours, r_ref)
        score.building_f1 = buildings["f1"]
        score.road_segment_f1 = roads["f1"]
        score.detail = {
            "osm": snapshot.report(),
            "damage_parameters": asdict(dmgp),
            "extent_parameters": asdict(ep),
            "ours": ours.report(),
            "buildings": buildings,
            "roads": roads,
            "counts": {
                "buildings_total": ours.buildings_total,
                "buildings_flagged_ours": len(b_ours),
                "buildings_flagged_reference": len(b_ref),
                "segments_total": ours.segments_total,
                "segments_cut_ours": len(r_ours),
                "segments_cut_reference": len(r_ref),
            },
            "what_this_measures": (
                "agreement between the damage conclusions drawn from OUR flood extent and "
                "from the reference flood extent, over the same pre-event OSM snapshot. It "
                "is NOT a measurement of real building damage: no building was inspected, "
                "and both sides are inferences from a flood polygon."),
        }
    except KeyboardInterrupt:
        raise
    # Deliberately broad, as in eval/run_scoreboard.score_event: one bad event
    # must be reported rather than losing the others, and nothing is scored in
    # its place.
    except Exception as err:
        score.error = f"{type(err).__name__}: {err}"
    return score


# --- rows ---------------------------------------------------------------------

def rows_for(scores: list[DamageScore], run_id: str) -> list[dict]:
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    commit = sb.git_commit()
    return [{"run_id": run_id, "timestamp": stamp, "git_commit": commit,
             "params_hash": s.params_hash, "event": s.event,
             "road_segment_f1": s.road_segment_f1, "building_f1": s.building_f1}
            for s in scores if s.ok]


def gate_plan_events(events: dict[str, dict] | None = None) -> list[str]:
    """Every event the damage gate is supposed to be measured over."""
    all_events = events if events is not None else sb.load_events()
    return sorted(str(e["name"]) for e in select_events("gates", None, all_events)
                  if sb.row_kind(e) == sb.KIND_FLOOD)


def write_rows(scores: list[DamageScore], planned: list[dict], run_id: str,
               results_dir: Path | None = None, log=print,
               gate_plan: list[str] | None = None) -> int:
    """Append rows only if EVERY planned event scored (as run_scoreboard does).

    A gate averaged over whichever events happened to work is Rule 3.2
    ("excluding hard pixels, areas or events"), so a partial set writes
    nothing and the gate stays NO DATA.

    AND only if the plan is the whole gate plan. `damage_overlay` in the gates
    file has `min_rows: 3` and no `where:` clause, so `check_gates` averages
    every row of the latest run_id: a run narrowed with `--events a b c` would
    become that latest run_id and the gate would silently be measured over a
    three-event subset somebody chose. That is the same Rule 3.2 violation as a
    partial result, arriving by a different door, and the 2026-10-13 audit
    found it open. `gate_plan=[]` disables the check for a test that supplies
    its own synthetic event names.
    """
    expected = gate_plan_events() if gate_plan is None else gate_plan
    if expected:
        names = sorted(str(e["name"]) for e in planned)
        if names != sorted(expected):
            log(f"  NOT WRITING: this run covers {names}, but the damage gate is defined "
                f"over {sorted(expected)}. A narrowed run would become the latest run_id "
                f"and the gate would be measured over a subset chosen by hand (Rule 3.2). "
                f"Re-run without --events, or use --no-write.")
            return 0
    done = {s.event for s in scores if s.ok}
    missing = [str(e["name"]) for e in planned if str(e["name"]) not in done]
    if missing:
        log(f"  NOT WRITING {len(done)} row(s): {len(missing)} of {len(planned)} planned "
            f"event(s) did not score ({', '.join(missing)}). A gate averaged over the rest "
            f"would be measured on a set chosen by what happened to work (Rule 3.2).")
        return 0
    rows = rows_for(scores, run_id)
    sb.append_rows("damage.csv", rows, results_dir=results_dir)
    return len(rows)


def print_table(scores: list[DamageScore], log=print) -> None:
    head = (f"{'event':<28} {'bld F1':>7} {'road F1':>8} {'bld ours':>9} {'bld ref':>8} "
            f"{'seg ours':>9} {'seg ref':>8}")
    log(head)
    log("-" * len(head))
    for s in scores:
        if s.error:
            log(f"{s.event:<28} FAILED  {s.error[:70]}")
            continue
        c = s.detail.get("counts", {})
        f = lambda v: "-" if v is None else f"{v:.4f}"  # noqa: E731
        log(f"{s.event:<28} {f(s.building_f1):>7} {f(s.road_segment_f1):>8} "
            f"{c.get('buildings_flagged_ours', '-'):>9} "
            f"{c.get('buildings_flagged_reference', '-'):>8} "
            f"{c.get('segments_cut_ours', '-'):>9} "
            f"{c.get('segments_cut_reference', '-'):>8}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--events", nargs="*", help="default: every gate flood event")
    ap.add_argument("--runs-root", type=Path, default=EVAL_RUNS)
    ap.add_argument("--results-dir", type=Path, default=None)
    ap.add_argument("--osm-buffer-km", type=float, default=OsmParams.buffer_km)
    ap.add_argument("--no-osm-download", action="store_true",
                    help="use only cached OSM; refuse rather than download")
    ap.add_argument("--no-write", action="store_true", help="print only, append no rows")
    args = ap.parse_args(argv)

    events = sb.load_events()
    planned = [e for e in select_events("gates", args.events, events)
               if sb.row_kind(e) == sb.KIND_FLOOD]
    if not planned:
        print("no gate flood events selected")
        return 1

    run_id, hashes = latest_loeo()
    osmp = OsmParams(buffer_km=args.osm_buffer_km)
    print(f"flood configuration from leave-one-event-out run {run_id}")
    print(f"OSM: pre-event snapshot per event, bbox + {osmp.buffer_km:g} km buffer")
    print(f"{len(planned)} event(s)\n")

    scores: list[DamageScore] = []
    for ev in planned:
        name = str(ev["name"])
        print(f"[{len(scores) + 1}/{len(planned)}] {name}")
        phash = hashes.get(name)
        if phash is None:
            scores.append(DamageScore(event=name, params_hash="-",
                                      error="no flood row for this event in the latest run"))
            print(f"      SKIPPED: {scores[-1].error}")
            continue
        scores.append(score_event(ev, phash, args.runs_root, osmp, DamageParams(),
                                  allow_osm_download=not args.no_osm_download,
                                  log=lambda *a: print("     ", *a)))
        if scores[-1].error:
            print(f"      FAILED: {scores[-1].error}")
        else:
            written = save_detail(scores[-1], args.runs_root)
            if written is not None:
                print(f"       evidence -> {written.relative_to(ROOT)}")

    print()
    print_table(scores)
    print("\nWhat these numbers mean: agreement between the damage conclusions drawn from "
          "OUR flood extent and from the REFERENCE flood extent, over the same pre-event "
          "OSM snapshot. No building was inspected; this is not real damage accuracy, and "
          "it inherits the flood map's accuracy.")

    if args.no_write:
        print("\n--no-write: no rows appended.")
    else:
        print()
        n = write_rows(scores, planned, sb.new_run_id(), args.results_dir)
        print(f"  {n} row(s) appended to eval/results/damage.csv")
        if n:
            print("  check the gates: venv/Scripts/python.exe -m eval.check_gates")

    failed = [s for s in scores if not s.ok]
    if failed:
        print(f"\n{len(failed)} event(s) did not score:")
        for s in failed:
            print(f"  {s.event}: {s.error or 'a metric was None (nothing flagged)'}")
        return 1
    return 0


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.exit(main())
