"""Choose detection parameters by leave-one-event-out, and score the left-out event.

    venv/Scripts/python.exe -m eval.tune                  # tune, score, write rows
    venv/Scripts/python.exe -m eval.tune --no-write       # print only
    venv/Scripts/python.exe -m eval.tune --apply-check    # just show the config diff

WHY LEAVE-ONE-EVENT-OUT

A parameter set chosen on an event and then scored on the same event measures
nothing: it measures the fit, not the method (Rule 3.4, in-sample tuning). So
for each flood event the parameters are chosen using only the OTHER events,
and the event itself is scored once with those parameters. Each row therefore
says "with parameters chosen without ever seeing this event, this is how well
we mapped it" - which is the question the judges will actually ask.

THE OBJECTIVE

Maximise the mean boundary-tolerant F1 over the training events, subject to
every no-flood control staying at or below the false-flood ceiling the gates
file declares (1%). The constraint is not negotiable and is not traded off
against F1: a parameter set that floods a dry valley is rejected however well
it scores on the floods. The ceiling is read from eval/gates.yaml rather than
written here, so the two can never drift apart.

Ties are broken by grid order, which is declaration order in
`strandline/config.py`, so the choice is reproducible.

WHAT IS AND IS NOT MEASURED HONESTLY HERE

- The per-event flood numbers are out-of-sample. That is the point.
- The control numbers are IN-SAMPLE with respect to the feasibility
  constraint: every control was used to reject parameter sets, so the
  false-flood rate is not an out-of-sample estimate. It is reported as what it
  is, and chosen_params.json says so.
- Final parameters are chosen on all the events in the pool. They are written
  to eval/chosen_params.json and printed as a diff against config.py.
  NOTHING IS APPLIED: a human edits config.py.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

import yaml

from eval import scoreboard as sb
from eval.fetch_dev_data import EVAL_RUNS, coverage, dev_run_params, select_events
from eval.run_scoreboard import (
    EventScore,
    _rel,
    contract,
    load_score,
    prepare_event,
    print_coverage,
    print_table,
    save_score,
    score_with_scene,
    write_rows,
    MAX_SCENE_PIXELS,
)
from strandline import run as pipeline
from strandline.config import DEV_N_PRE, TUNE_GRID, DetectParams, RunParams

ROOT = Path(__file__).resolve().parents[1]
GATES = ROOT / "eval" / "gates.yaml"
CHOSEN = ROOT / "eval" / "chosen_params.json"

OBJECTIVE_METRIC = "f1_tolerant"
CONTROL_METRIC = "false_flood_fraction"

SECTIONS = {"detect": DetectParams, "run": RunParams}


# --- the grid -----------------------------------------------------------------

def validate_grid(grid: dict[str, tuple] | None = None) -> dict[str, tuple]:
    """Reject a grid the search cannot honour, before anything expensive runs."""
    grid = TUNE_GRID if grid is None else grid
    for key, values in grid.items():
        section, _, field_name = key.partition(".")
        if section not in SECTIONS:
            raise ValueError(f"TUNE_GRID key {key!r}: section must be one of {sorted(SECTIONS)}")
        known = {f.name for f in fields(SECTIONS[section])}
        if field_name not in known:
            raise ValueError(f"TUNE_GRID key {key!r}: {SECTIONS[section].__name__} has no "
                             f"field {field_name!r}")
        if section == "detect" and field_name in pipeline.PREPROCESS_FIELDS:
            raise ValueError(
                f"TUNE_GRID key {key!r} is applied before a scene is prepared, so varying it "
                f"would need the whole stack re-filtered per point. Prepare-time fields "
                f"({', '.join(pipeline.PREPROCESS_FIELDS)}) cannot be tuned by this search.")
        if not values:
            raise ValueError(f"TUNE_GRID key {key!r}: no values")
    return grid


def grid_points(grid: dict[str, tuple] | None = None) -> list[dict]:
    """The cartesian product, in declaration order. Deterministic."""
    grid = validate_grid(grid)
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))]


def materialise(point: dict, n_pre: int = DEV_N_PRE, base_dp: DetectParams | None = None,
                base_rp: RunParams | None = None) -> tuple[DetectParams, RunParams]:
    """One grid point -> the (DetectParams, RunParams) a run would use."""
    dp = base_dp or DetectParams()
    rp = base_rp or dev_run_params(n_pre=n_pre)
    d_changes = {k.split(".", 1)[1]: v for k, v in point.items() if k.startswith("detect.")}
    r_changes = {k.split(".", 1)[1]: v for k, v in point.items() if k.startswith("run.")}
    return (replace(dp, **d_changes) if d_changes else dp,
            replace(rp, **r_changes) if r_changes else rp)


def point_hash(point: dict, n_pre: int = DEV_N_PRE) -> str:
    dp, rp = materialise(point, n_pre)
    return pipeline.params_hash(dp, rp)


def point_label(point: dict) -> str:
    return " ".join(f"{k.split('.', 1)[1]}={v}" for k, v in point.items())


def coefficient_of(point: dict, n_pre: int = DEV_N_PRE) -> str:
    return materialise(point, n_pre)[1].backscatter_coefficient


# --- the gate contract --------------------------------------------------------

def control_ceiling(path: Path | None = None) -> float:
    """The false-flood ceiling, read from the human-owned gates file."""
    cfg = yaml.safe_load((path or GATES).read_text(encoding="utf-8"))
    for g in cfg["modules"]["no_flood_controls"]["gates"]:
        if g["metric"] == CONTROL_METRIC and g.get("op") == "<=":
            return float(g["value"])
    raise ValueError("no_flood_controls declares no false_flood_fraction ceiling")


# --- the score matrix ---------------------------------------------------------

Matrix = dict[tuple[str, str], EventScore]


def build_matrix(events: list[dict], points: list[dict], runs_root: Path | None = None,
                 *, n_pre: int = DEV_N_PRE, tolerance_px: int = 1, reuse: bool = True,
                 allow_download: bool = False, max_pixels: int = MAX_SCENE_PIXELS,
                 log=print) -> Matrix:
    """Score every event at every grid point.

    One prepared scene serves every point that shares its backscatter
    coefficient, because preparing it (download, speckle filter, terrain) costs
    minutes and classifying costs seconds. Scores already on disk are reused,
    so an interrupted run resumes.
    """
    matrix: Matrix = {}
    by_coefficient: dict[str, list[dict]] = {}
    for p in points:
        by_coefficient.setdefault(coefficient_of(p, n_pre), []).append(p)

    for i, event in enumerate(events, start=1):
        name = str(event["name"])
        log(f"[{i}/{len(events)}] {name}")
        for coefficient, pts in by_coefficient.items():
            scene = None
            for point in pts:
                dp, rp = materialise(point, n_pre)
                phash = pipeline.params_hash(dp, rp)
                if reuse:
                    cached = load_score(name, phash, runs_root)
                    if cached is not None and cached.ok:
                        matrix[(name, phash)] = cached
                        log(f"      {coefficient} {point_label(point):<40} cached "
                            f"{cached.metrics.get(OBJECTIVE_METRIC, cached.metrics.get(CONTROL_METRIC))}")
                        continue
                try:
                    if scene is None:
                        scene = prepare_event(event, rp, dp, runs_root, allow_download,
                                              max_pixels, log=lambda *a: None)
                    score = score_with_scene(event, scene, dp, tolerance_px, log=lambda *a: None)
                except KeyboardInterrupt:
                    raise
                # Broad on purpose: one unusable event must not lose the whole
                # matrix. It is recorded as failed, which blocks any gate that
                # needs it rather than quietly shrinking the set (Rules 2, 6).
                except Exception as err:
                    score = EventScore(event=name, kind=sb.row_kind(event), params_hash=phash,
                                       error=f"{type(err).__name__}: {err}")
                    log(f"      {coefficient} {point_label(point):<40} FAILED {score.error[:90]}")
                    save_score(score, runs_root)
                    matrix[(name, phash)] = score
                    if scene is None:   # the event itself is unusable: skip its other points
                        break
                    continue
                save_score(score, runs_root)
                matrix[(name, phash)] = score
                key = OBJECTIVE_METRIC if score.kind != sb.KIND_CONTROL else CONTROL_METRIC
                log(f"      {coefficient} {point_label(point):<40} {key}="
                    f"{score.metrics.get(key)}")
            del scene
    return matrix


# --- selection ----------------------------------------------------------------

@dataclass
class Candidate:
    point: dict
    params_hash: str
    objective: float | None = None
    per_event: dict[str, float] = field(default_factory=dict)
    worst_control: float | None = None
    worst_control_event: str | None = None
    feasible: bool = False
    rejected: str | None = None


@dataclass
class Selection:
    """The outcome of one parameter choice, with every candidate kept."""

    train_events: list[str]
    control_events: list[str]
    ceiling: float
    candidates: list[Candidate] = field(default_factory=list)
    chosen: Candidate | None = None
    ties: int = 0

    @property
    def ok(self) -> bool:
        return self.chosen is not None

    def report(self) -> dict:
        return {
            "train_events": self.train_events,
            "control_events": self.control_events,
            "control_ceiling": self.ceiling,
            "chosen": None if self.chosen is None else {
                "parameters": self.chosen.point,
                "params_hash": self.chosen.params_hash,
                "mean_train_f1_tolerant": self.chosen.objective,
                "train_per_event_f1_tolerant": self.chosen.per_event,
                "worst_control_false_flood_fraction": self.chosen.worst_control,
                "worst_control_event": self.chosen.worst_control_event,
            },
            "ties_at_best_objective": self.ties,
            "candidates": [asdict(c) for c in self.candidates],
        }


def select(matrix: Matrix, train: list[str], controls: list[str], points: list[dict],
           ceiling: float, n_pre: int = DEV_N_PRE) -> Selection:
    """Best feasible grid point on `train`, with every control under `ceiling`.

    A point is usable only when every training event AND every control has a
    score for it: comparing points over different event subsets would make the
    objective meaningless.
    """
    out = Selection(train_events=list(train), control_events=list(controls), ceiling=ceiling)
    for point in points:
        phash = point_hash(point, n_pre)
        cand = Candidate(point=dict(point), params_hash=phash)

        missing = [e for e in list(train) + list(controls)
                   if (e, phash) not in matrix or not matrix[(e, phash)].ok]
        if missing:
            cand.rejected = f"no score for {', '.join(missing[:4])}"
            out.candidates.append(cand)
            continue

        values = {e: matrix[(e, phash)].value(OBJECTIVE_METRIC) for e in train}
        if any(v is None for v in values.values()):
            cand.rejected = f"{OBJECTIVE_METRIC} missing on a training event"
            out.candidates.append(cand)
            continue
        cand.per_event = {e: round(v, 4) for e, v in values.items()}
        cand.objective = round(sum(values.values()) / len(values), 6) if values else None

        ffr = {e: matrix[(e, phash)].value(CONTROL_METRIC) for e in controls}
        if any(v is None for v in ffr.values()):
            cand.rejected = f"{CONTROL_METRIC} missing on a control"
            out.candidates.append(cand)
            continue
        if ffr:
            cand.worst_control_event = max(ffr, key=lambda e: ffr[e])
            cand.worst_control = round(ffr[cand.worst_control_event], 6)
        if cand.worst_control is not None and cand.worst_control > ceiling:
            cand.rejected = (f"control {cand.worst_control_event} false-flood "
                             f"{cand.worst_control:.4f} > {ceiling}")
            out.candidates.append(cand)
            continue
        cand.feasible = True
        out.candidates.append(cand)

    feasible = [c for c in out.candidates if c.feasible and c.objective is not None]
    if not feasible:
        return out
    best = max(c.objective for c in feasible)
    tied = [c for c in feasible if c.objective == best]
    out.ties = len(tied)
    out.chosen = tied[0]      # grid order = declaration order in config.py
    return out


def leave_one_out(matrix: Matrix, flood_events: list[str], controls: list[str],
                  points: list[dict], ceiling: float, n_pre: int = DEV_N_PRE
                  ) -> dict[str, Selection]:
    """For each flood event: choose on the others, keep the choice."""
    out: dict[str, Selection] = {}
    for held in flood_events:
        train = [e for e in flood_events if e != held]
        out[held] = select(matrix, train, controls, points, ceiling, n_pre)
    return out


# --- the config diff ----------------------------------------------------------

def config_diff(dp: DetectParams, rp: RunParams) -> list[dict]:
    """Chosen values against what strandline/config.py declares today."""
    diff = []
    for section, chosen, current in (("detect", dp, DetectParams()), ("run", rp, RunParams())):
        for f in fields(current):
            a, b = getattr(current, f.name), getattr(chosen, f.name)
            if a != b:
                diff.append({"key": f"{section}.{f.name}", "config_py": a, "chosen": b})
    return diff


def print_diff(diff: list[dict], log=print) -> None:
    log("\nDIFF against strandline/config.py (NOT applied - a human edits config.py):")
    if not diff:
        log("  no change: the tuner chose the values config.py already declares.")
        return
    width = max(len(d["key"]) for d in diff)
    for d in diff:
        log(f"  {d['key']:<{width}}  {d['config_py']!r}  ->  {d['chosen']!r}")


# --- main ---------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--plan", default="gates", choices=["gates", "all"])
    ap.add_argument("--events", nargs="*", help="explicit event names instead of a plan")
    ap.add_argument("--n-pre", type=int, default=DEV_N_PRE)
    ap.add_argument("--no-write", action="store_true", help="append no rows to flood.csv")
    ap.add_argument("--apply-check", action="store_true",
                    help="read eval/chosen_params.json and print its diff against config.py")
    ap.add_argument("--allow-download", action="store_true")
    ap.add_argument("--no-reuse", action="store_true", help="recompute every score")
    ap.add_argument("--max-pixels", type=int, default=MAX_SCENE_PIXELS)
    ap.add_argument("--runs-root", type=Path, default=EVAL_RUNS)
    ap.add_argument("--results-dir", type=Path, default=None)
    ap.add_argument("--chosen-out", type=Path, default=CHOSEN)
    args = ap.parse_args(argv)

    if args.apply_check:
        if not args.chosen_out.exists():
            print(f"{args.chosen_out} does not exist: run the tuner first.")
            return 1
        saved = json.loads(args.chosen_out.read_text(encoding="utf-8"))
        print(f"from {args.chosen_out.name}, run_id {saved.get('run_id')}")
        print_diff(saved.get("config_diff", []))
        return 0

    con = contract()
    tolerance_px = int(con["boundary_tolerance_px"])
    ceiling = control_ceiling()
    points = grid_points()
    events_all = sb.load_events()
    planned = select_events(args.plan, args.events, events_all)

    floods = [str(e["name"]) for e in planned if sb.row_kind(e) != sb.KIND_CONTROL]
    gate_floods = [str(e["name"]) for e in planned if sb.gate_eligible(e)]
    controls = [str(e["name"]) for e in planned if sb.row_kind(e) == sb.KIND_CONTROL]
    run_id = sb.new_run_id()

    print(f"run_id {run_id}")
    print(f"grid: {len(points)} point(s) over {len(TUNE_GRID)} axis/axes "
          f"{list(TUNE_GRID)}")
    print(f"pool: {len(floods)} flood event(s) ({len(gate_floods)} gate-eligible), "
          f"{len(controls)} control(s); n_pre {args.n_pre}; tolerance {tolerance_px} px; "
          f"control ceiling {ceiling}")
    print(f"work: up to {len(points) * len(planned)} classifications "
          f"({len(points)} points x {len(planned)} events)\n")

    matrix = build_matrix(planned, points, args.runs_root, n_pre=args.n_pre,
                          tolerance_px=tolerance_px, reuse=not args.no_reuse,
                          allow_download=args.allow_download, max_pixels=args.max_pixels,
                          log=lambda *a: print(*a))

    # --- leave-one-event-out ------------------------------------------------
    print("\n=== LEAVE-ONE-EVENT-OUT ===")
    selections = leave_one_out(matrix, floods, controls, points, ceiling, args.n_pre)
    loeo_scores: list[EventScore] = []
    infeasible: list[str] = []
    for held, sel in selections.items():
        if not sel.ok:
            why = {}
            for c in sel.candidates:
                why[c.rejected or "feasible"] = why.get(c.rejected or "feasible", 0) + 1
            print(f"  {held:<32} NO FEASIBLE PARAMETER SET. {len(sel.candidates)} candidate(s): "
                  + "; ".join(f"{k} x{v}" for k, v in sorted(why.items())))
            infeasible.append(held)
            continue
        score = matrix.get((held, sel.chosen.params_hash))
        if score is None or not score.ok:
            print(f"  {held:<32} chosen {sel.chosen.params_hash} but the held-out event has "
                  f"no score for it: {None if score is None else score.error}")
            infeasible.append(held)
            continue
        loeo_scores.append(score)
        print(f"  {held:<32} chose [{point_label(sel.chosen.point)}] on {len(sel.train_events)} "
              f"other event(s), train mean {OBJECTIVE_METRIC} {sel.chosen.objective:.4f} "
              f"-> held-out {OBJECTIVE_METRIC} {score.value(OBJECTIVE_METRIC)}")

    # --- final parameters, chosen on the whole pool -------------------------
    print("\n=== FINAL PARAMETERS (all pool events) ===")
    final = select(matrix, floods, controls, points, ceiling, args.n_pre)
    if not final.ok:
        print("  NO FEASIBLE PARAMETER SET over the whole pool. Nothing is chosen and nothing "
              "is written. The grid, the controls or the method must change; see the candidate "
              "table in the report.")
    else:
        print(f"  {point_label(final.chosen.point)}")
        print(f"  mean {OBJECTIVE_METRIC} over {len(floods)} event(s): "
              f"{final.chosen.objective:.4f}   worst control "
              f"{final.chosen.worst_control} ({final.chosen.worst_control_event}) "
              f"<= {ceiling}")
        if final.ties > 1:
            print(f"  {final.ties} point(s) tied at this objective; grid order decided")

    control_scores: list[EventScore] = []
    diff: list[dict] = []
    dp_final = rp_final = None
    if final.ok:
        dp_final, rp_final = materialise(final.chosen.point, args.n_pre)
        diff = config_diff(dp_final, rp_final)
        control_scores = [matrix[(c, final.chosen.params_hash)] for c in controls
                          if (c, final.chosen.params_hash) in matrix
                          and matrix[(c, final.chosen.params_hash)].ok]

    print("\n=== PER-EVENT SCORES ===")
    print("flood events: leave-one-event-out (parameters never saw the event)")
    print("controls:     final parameters; IN-SAMPLE, every control constrained the choice\n")
    print_table(loeo_scores + control_scores)
    print_coverage(loeo_scores + control_scores, args.plan)

    # --- artefacts -----------------------------------------------------------
    report = {
        "run_id": run_id,
        "git_commit": sb.git_commit(),
        "generated_by": "eval/tune.py",
        "method": {
            "objective": f"maximise mean {OBJECTIVE_METRIC} over the training events",
            "constraint": f"every control {CONTROL_METRIC} <= {ceiling} (from eval/gates.yaml)",
            "tie_break": "grid order, i.e. declaration order in strandline/config.py TUNE_GRID",
            "split": "leave-one-event-out over the flood events in the pool",
            "tolerance_px": tolerance_px,
            "n_pre": args.n_pre,
        },
        "grid": {k: list(v) for k, v in TUNE_GRID.items()},
        "pool": {"flood_events": floods, "gate_eligible": gate_floods, "controls": controls},
        "coverage": coverage([s.event for s in loeo_scores + control_scores], args.plan),
        "loeo": {held: sel.report() for held, sel in selections.items()},
        "held_out_scores": {s.event: s.metrics for s in loeo_scores},
        "final": final.report(),
        "chosen_parameters": (None if not final.ok else
                              {"detect": asdict(dp_final), "run": asdict(rp_final),
                               "params_hash": final.chosen.params_hash}),
        "config_diff": diff,
        "caveats": [
            f"Measured with n_pre={args.n_pre} pre-event images, not the shipped default "
            f"{RunParams.n_pre}: a thinner reference stack gives noisier per-pixel std.",
            "Control false-flood rates are in-sample: every control was used as a feasibility "
            "constraint when choosing parameters, so they are not an out-of-sample estimate.",
            "Flood events whose reference came from Sentinel-1 are written as kind="
            "flood_dependent and cannot move a gate; they can still appear in the pool.",
            "Nothing here is applied to strandline/config.py. A human makes that edit.",
        ],
    }
    if not args.no_write:
        args.chosen_out.parent.mkdir(parents=True, exist_ok=True)
        args.chosen_out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {_rel(args.chosen_out)}")

    if final.ok:
        print_diff(diff)

    if args.no_write:
        print("\n--no-write: no rows appended to eval/results/flood.csv.")
    else:
        print()
        written = write_rows(loeo_scores, [e for e in planned
                                           if sb.row_kind(e) != sb.KIND_CONTROL],
                             events_all, run_id, "loeo", args.results_dir)
        if control_scores:
            written.update(write_rows(control_scores,
                                      [e for e in planned
                                       if sb.row_kind(e) == sb.KIND_CONTROL],
                                      events_all, run_id, "full", args.results_dir))
        total = sum(written.values())
        print(f"  {total} row(s) appended to eval/results/flood.csv"
              + (f" ({', '.join(f'{k}={v}' for k, v in written.items())})" if written else ""))
        if total:
            print("  check the gates: venv/Scripts/python.exe -m eval.check_gates")

    if infeasible or not final.ok:
        print(f"\nSTOP: {len(infeasible)} event(s) had no feasible parameter set"
              if infeasible else "\nSTOP: no feasible parameter set over the pool")
        return 1
    return 0


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.exit(main())
