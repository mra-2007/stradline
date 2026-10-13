"""Score the cut-off analysis: our flood extent vs the reference extent.

    venv/Scripts/python.exe -m eval.cutoff_eval
    venv/Scripts/python.exe -m eval.cutoff_eval --events <dev_event_name> --no-write

CHECKING ONLY. Reference maps are read here and never by `strandline/`.

WHAT IS MEASURED

For each gate flood event the SAME cut-off analysis runs twice over the SAME
pre-event OpenStreetMap snapshot and the same road graphs, changing only the
flood extent that removes edges:

    ours       the pipeline's own flood extent, at the configuration its
               leave-one-event-out fold was scored with
    reference  the event's reference polygons under its own `reference_classes`
               (water UNION debris) - the mask the flood gate is scored against

`settlement_f1` compares the two CUT-OFF SETS as sets of settlements, under the
one declared definition in `config.CUTOFF_GATE_*` (no vehicle route to the
nearest health facility, bridges intact).

WHAT THIS NUMBER IS NOT

It is not a measurement of whether anybody was really cut off. No road was
driven and no village was visited; both sides are inferences from a flood
polygon over the same OSM. It says whether OUR extent leads to the same
isolation conclusions as an expert extent, and it inherits the flood gate,
which currently FAILS.

THIN OSM IS A LIMITATION, NEVER A REASON TO DROP AN EVENT

Every row is published with how many settlements and how many kilometres of
road OSM actually has for that area, because a cut-off F1 computed over three
settlements means something different from one over ninety, and because the
damage evaluation already met an area with 65 buildings mapped in total. An
event is never excluded for thin mapping: that would be Rule 3.2. The
completeness figures are published beside the score instead.
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
from eval.damage_eval import gate_plan_events, set_metrics
from eval.diagnose_errors import latest_loeo, params_from_score
from eval.fetch_dev_data import EVAL_RUNS, select_events
from strandline import damage, network, osm
from strandline.config import (
    CUTOFF_GATE_GRAPH,
    CUTOFF_GATE_SCENARIO,
    CUTOFF_GATE_TARGET,
    STATUS_CUT_OFF,
    DamageParams,
    ExtentParams,
    MonteCarloParams,
    NetworkParams,
    OsmParams,
)

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class CutoffScore:
    event: str
    params_hash: str
    settlement_f1: float | None = None
    detail: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.settlement_f1 is not None


def detail_path(event_name: str, params_hash: str, runs_root: Path | None = None) -> Path:
    """Where one event's cut-off evidence is persisted (Rule 3.8).

    Beside the flood score and the damage evidence, under the same hash, so a
    published settlement count or road length can be checked without re-running
    anything.
    """
    return ((runs_root or EVAL_RUNS) / event_name / "scores"
            / f"cutoff_{params_hash}.json")


def save_detail(score: CutoffScore, runs_root: Path | None = None) -> Path | None:
    if score.error is not None or not score.detail:
        return None
    path = detail_path(score.event, score.params_hash, runs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"event": score.event, "params_hash": score.params_hash,
         "settlement_f1": score.settlement_f1, **score.detail}, indent=2, default=str),
        encoding="utf-8")
    return path


def cut_off_set(result, statuses) -> set:
    """Settlement names cut off, by the gated definition, given a status list."""
    return {name for name, s in zip(result.settlements.names, statuses)
            if s == STATUS_CUT_OFF}


def _statuses_for_extent(result, graph, flat_mask, bridge_out, np_: NetworkParams,
                         sources) -> list:
    """Re-run just the routing for one extent, reusing the built graph."""
    flooded = graph.flooded_edges(flat_mask)
    removed = network._removed(graph, flooded, CUTOFF_GATE_SCENARIO, bridge_out)
    none_removed = np.zeros(graph.n_edges, dtype=bool)
    before = network.distance_to_nearest(graph, sources, none_removed)
    after = network.distance_to_nearest(graph, sources, removed)
    return before, after, removed


def score_event(event: dict, params_hash: str, runs_root: Path,
                osmp: OsmParams | None = None, np_: NetworkParams | None = None,
                mc: MonteCarloParams | None = None, dmgp: DamageParams | None = None,
                allow_osm_download: bool = True, log=print) -> CutoffScore:
    """Both cut-off sets for one event. Errors are returned, not raised."""
    from eval.damage_eval import reference_extent_geometry

    name = str(event["name"])
    osmp = osmp if osmp is not None else OsmParams()
    np_ = np_ if np_ is not None else NetworkParams()
    mc = mc if mc is not None else MonteCarloParams()
    dmgp = dmgp if dmgp is not None else DamageParams()
    score = CutoffScore(event=name, params_hash=params_hash)
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

        # OUR side: the full analysis, which also builds the graphs and the
        # settlements both sides share.
        ours = network.assess(result.classes, result.z_combined, None, grid,
                              snapshot.layers, z_seed=dp.z_seed, np_=np_, mc=mc,
                              dmgp=dmgp, extent=ep, log=log)
        our_set = set(ours.cut_off_names())

        # REFERENCE side: the same graph, the same settlements, the same
        # scenario - only the extent differs. Rasterised through the identical
        # helper so neither side gets a different notion of "the flood is here".
        ref_geom = reference_extent_geometry(event, grid, dmgp)
        from rasterio.features import rasterize

        ref_mask = (rasterize([(ref_geom, 1)], out_shape=(grid.height, grid.width),
                              transform=grid.transform, fill=0, dtype="uint8")
                    .astype(bool).ravel() if not ref_geom.is_empty
                    else np.zeros(grid.height * grid.width, dtype=bool))
        graph = ours.graphs[CUTOFF_GATE_GRAPH]
        # The SAME destination nodes our own routing used, taken from the
        # result rather than recomputed: if the two sides could disagree about
        # where the clinics are, part of the F1 would be an artefact of that.
        sources = ours.target_nodes[(CUTOFF_GATE_GRAPH, CUTOFF_GATE_TARGET)]
        before, after, _removed_edges = _statuses_for_extent(
            ours, graph, ref_mask, np.zeros(graph.n_edges, dtype=bool), np_, sources)
        ref_statuses = network.statuses(before, after, ours.attached[CUTOFF_GATE_GRAPH],
                                        np_.detour_factor)
        ref_set = cut_off_set(ours, ref_statuses)

        metrics = set_metrics(our_set, ref_set)
        score.settlement_f1 = metrics["f1"]

        roads = osm.to_geodataframe(snapshot.layers.get("roads") or {}, grid.crs)
        segments = damage.split_at_junctions(roads, dmgp.min_segment_m)
        score.detail = {
            "osm": snapshot.report(),
            "network_parameters": asdict(np_),
            "monte_carlo_parameters": asdict(mc),
            "extent_parameters": asdict(ep),
            "gated_definition": {"graph": CUTOFF_GATE_GRAPH,
                                 "destination": CUTOFF_GATE_TARGET,
                                 "scenario": CUTOFF_GATE_SCENARIO},
            "settlements": ours.settlements.report(),
            "cut_off_ours": sorted(our_set),
            "cut_off_reference": sorted(ref_set),
            "metrics": metrics,
            "osm_completeness": {
                "settlements": len(ours.settlements),
                "settlements_from_osm_place_nodes":
                    ours.settlements.sources.count("osm_place"),
                "settlements_from_building_clusters":
                    ours.settlements.sources.count("building_cluster"),
                "settlements_not_attached_to_any_graph": int(ours.settlements.not_attached),
                "road_segments": int(len(segments)),
                "road_km": round(float(segments.geometry.length.sum()) / 1000.0, 2)
                           if len(segments) else 0.0,
                "graph_km": {k: round(g.total_length_m() / 1000.0, 2)
                             for k, g in sorted(ours.graphs.items())},
                "health_facilities": ours.targets_found.get("health_facilities"),
                "note": ("published beside the score because a cut-off F1 over three "
                         "settlements means something different from one over ninety. "
                         "Thin mapping is a limitation, NEVER a reason to drop an event "
                         "(Rule 3.2)."),
            },
            "what_this_measures": (
                "agreement between the isolation conclusions drawn from OUR flood extent "
                "and from the reference flood extent, over the same pre-event OSM road "
                "network. NOT a measurement of whether anybody was really cut off: no "
                "road was driven, and both sides are inferences from a flood polygon."),
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

def rows_for(scores: list[CutoffScore], run_id: str) -> list[dict]:
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    commit = sb.git_commit()
    return [{"run_id": run_id, "timestamp": stamp, "git_commit": commit,
             "params_hash": s.params_hash, "event": s.event,
             "settlement_f1": s.settlement_f1}
            for s in scores if s.ok]


def write_rows(scores: list[CutoffScore], planned: list[dict], run_id: str,
               results_dir: Path | None = None, log=print,
               gate_plan: list[str] | None = None) -> int:
    """Append rows only for a COMPLETE run over the WHOLE gate plan.

    Both halves matter and both are Rule 3.2: a partial result would make the
    gate an average over whichever events worked, and a run narrowed with
    `--events` would become the latest run_id and the gate would read a
    hand-picked subset (`cutoff_analysis` has min_rows 3 and no `where:`).
    """
    expected = gate_plan_events() if gate_plan is None else gate_plan
    if expected:
        names = sorted(str(e["name"]) for e in planned)
        if names != sorted(expected):
            log(f"  NOT WRITING: this run covers {names}, but the cut-off gate is defined "
                f"over {sorted(expected)}. A narrowed run would become the latest run_id "
                f"and the gate would be measured over a subset chosen by hand (Rule 3.2).")
            return 0
    done = {s.event for s in scores if s.ok}
    missing = [str(e["name"]) for e in planned if str(e["name"]) not in done]
    if missing:
        log(f"  NOT WRITING {len(done)} row(s): {len(missing)} of {len(planned)} planned "
            f"event(s) did not score ({', '.join(missing)}).")
        return 0
    rows = rows_for(scores, run_id)
    sb.append_rows("cutoff.csv", rows, results_dir=results_dir)
    return len(rows)


def print_table(scores: list[CutoffScore], log=print) -> None:
    head = (f"{'event':<28} {'settl F1':>9} {'cut ours':>9} {'cut ref':>8} "
            f"{'settlements':>12} {'road km':>9} {'clinics':>8}")
    log(head)
    log("-" * len(head))
    for s in scores:
        if s.error:
            log(f"{s.event:<28} FAILED  {s.error[:70]}")
            continue
        c = s.detail.get("osm_completeness", {})
        log(f"{s.event:<28} {s.settlement_f1:>9.4f} "
            f"{len(s.detail.get('cut_off_ours', [])):>9} "
            f"{len(s.detail.get('cut_off_reference', [])):>8} "
            f"{c.get('settlements', '-'):>12} {c.get('road_km', '-'):>9} "
            f"{c.get('health_facilities', '-'):>8}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--events", nargs="*", help="default: every gate flood event")
    ap.add_argument("--runs-root", type=Path, default=EVAL_RUNS)
    ap.add_argument("--results-dir", type=Path, default=None)
    ap.add_argument("--mc-runs", type=int, default=MonteCarloParams.n_runs)
    ap.add_argument("--no-osm-download", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    events = sb.load_events()
    planned = [e for e in select_events("gates", args.events, events)
               if sb.row_kind(e) == sb.KIND_FLOOD]
    if not planned:
        print("no gate flood events selected")
        return 1

    run_id, hashes = latest_loeo()
    from dataclasses import replace as dc_replace

    mc = dc_replace(MonteCarloParams(), n_runs=args.mc_runs)
    print(f"flood configuration from leave-one-event-out run {run_id}")
    print(f"cut-off definition: graph {CUTOFF_GATE_GRAPH}, destination "
          f"{CUTOFF_GATE_TARGET}, scenario {CUTOFF_GATE_SCENARIO}")
    print(f"{len(planned)} event(s)\n")

    scores: list[CutoffScore] = []
    for ev in planned:
        name = str(ev["name"])
        print(f"[{len(scores) + 1}/{len(planned)}] {name}")
        phash = hashes.get(name)
        if phash is None:
            scores.append(CutoffScore(event=name, params_hash="-",
                                      error="no flood row for this event in the latest run"))
            print(f"      SKIPPED: {scores[-1].error}")
            continue
        scores.append(score_event(ev, phash, args.runs_root, mc=mc,
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
    print("\nWhat these numbers mean: agreement between the isolation conclusions drawn "
          "from OUR flood extent and from the REFERENCE extent, over the same pre-event "
          "OSM road network. Nobody was visited; this is not real cut-off accuracy, and "
          "it inherits the flood map's accuracy. The settlement and road counts are "
          "published because a score over few settlements means little - thin mapping is "
          "a limitation, never a reason to drop an event.")

    if args.no_write:
        print("\n--no-write: no rows appended.")
    else:
        print()
        n = write_rows(scores, planned, sb.new_run_id(), args.results_dir)
        print(f"  {n} row(s) appended to eval/results/cutoff.csv")
        if n:
            print("  check the gates: venv/Scripts/python.exe -m eval.check_gates")

    failed = [s for s in scores if not s.ok]
    if failed:
        print(f"\n{len(failed)} event(s) did not score:")
        for s in failed:
            reason = s.error or "settlement_f1 was None (neither side cut anything off)"
            print(f"  {s.event}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.exit(main())
