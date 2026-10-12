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
from eval.fetch_dev_data import (
    EVAL_RUNS,
    cache_dir,
    coverage,
    dev_run_params,
    select_events,
)
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
from strandline import optical, run as pipeline
from strandline.config import (
    DETECT_MODES,
    DEV_N_PRE,
    FUSION_OFF,
    FUSION_UNION,
    OPTICAL_ADOPTION,
    OPTICAL_FUSION_MODES,
    OPTICAL_TUNE_GRIDS,
    TUNE_GRIDS,
    DetectParams,
    OpticalParams,
    RunParams,
)

ROOT = Path(__file__).resolve().parents[1]
GATES = ROOT / "eval" / "gates.yaml"
CHOSEN = ROOT / "eval" / "chosen_params.json"

OBJECTIVE_METRIC = "f1_tolerant"
CONTROL_METRIC = "false_flood_fraction"

SECTIONS = {"detect": DetectParams, "run": RunParams}

# The optical grid is searched in its own pass, so it has its own sections.
# `run` is deliberately absent: nothing about the Sentinel-2 channel changes a
# Sentinel-1 download.
OPTICAL_SECTIONS = {"detect": DetectParams, "optical": OpticalParams}


# --- the grid -----------------------------------------------------------------

def validate_grid(grids: dict[str, dict[str, tuple]] | None = None
                  ) -> dict[str, dict[str, tuple]]:
    """Reject a grid the search cannot honour, before anything expensive runs."""
    grids = TUNE_GRIDS if grids is None else grids
    for mode, grid in grids.items():
        if mode not in DETECT_MODES:
            raise ValueError(f"TUNE_GRIDS has a grid for unknown detection mode {mode!r}; "
                             f"known: {', '.join(DETECT_MODES)}")
        if "detect.mode" in grid:
            raise ValueError("TUNE_GRIDS keys ARE the modes; do not also put detect.mode "
                             "on an axis, or a grid could score a mode it is not for")
        for key, values in grid.items():
            section, _, field_name = key.partition(".")
            if section == "optical":
                raise ValueError(
                    f"TUNE_GRIDS[{mode!r}] key {key!r}: optical axes belong in "
                    f"OPTICAL_TUNE_GRIDS, which is searched in its own pass. The radar "
                    f"search runs without any Sentinel-2 data cached.")
            if section not in SECTIONS:
                raise ValueError(
                    f"TUNE_GRIDS[{mode!r}] key {key!r}: section must be one of "
                    f"{sorted(SECTIONS)}")
            known = {f.name for f in fields(SECTIONS[section])}
            if field_name not in known:
                raise ValueError(f"TUNE_GRIDS[{mode!r}] key {key!r}: "
                                 f"{SECTIONS[section].__name__} has no field {field_name!r}")
            if section == "detect" and field_name in pipeline.PREPROCESS_FIELDS:
                raise ValueError(
                    f"TUNE_GRIDS[{mode!r}] key {key!r} is applied before a scene is prepared, "
                    f"so varying it would need the whole stack re-filtered per point. "
                    f"Prepare-time fields ({', '.join(pipeline.PREPROCESS_FIELDS)}) cannot be "
                    f"tuned by this search.")
            if not values:
                raise ValueError(f"TUNE_GRIDS[{mode!r}] key {key!r}: no values")
    return grids


def validate_optical_grids(grids: dict[str, dict[str, tuple]] | None = None
                           ) -> dict[str, dict[str, tuple]]:
    """Reject an optical grid the search could not honour.

    Checked here rather than only in config.py so that a declared grid is
    MACHINE-verified - every axis names a real field of a real section, and no
    point varies a parameter its fusion mode ignores - from the moment it is
    declared, which is before any optical score exists.
    """
    grids = OPTICAL_TUNE_GRIDS if grids is None else grids
    for fusion, grid in grids.items():
        if fusion not in OPTICAL_FUSION_MODES:
            raise ValueError(f"OPTICAL_TUNE_GRIDS has a grid for unknown fusion mode "
                             f"{fusion!r}; known: {', '.join(OPTICAL_FUSION_MODES)}")
        if "optical.fusion" in grid:
            raise ValueError("OPTICAL_TUNE_GRIDS keys ARE the fusion modes; do not also "
                             "put optical.fusion on an axis, or a grid could score "
                             "a mode it is not for")
        for key, values in grid.items():
            section, _, field_name = key.partition(".")
            if section not in OPTICAL_SECTIONS:
                raise ValueError(f"OPTICAL_TUNE_GRIDS[{fusion!r}] key {key!r}: section must "
                                 f"be one of {sorted(OPTICAL_SECTIONS)}")
            known = {f.name for f in fields(OPTICAL_SECTIONS[section])}
            if field_name not in known:
                raise ValueError(f"OPTICAL_TUNE_GRIDS[{fusion!r}] key {key!r}: "
                                 f"{OPTICAL_SECTIONS[section].__name__} has no field "
                                 f"{field_name!r}")
            if section == "detect" and field_name in pipeline.PREPROCESS_FIELDS:
                raise ValueError(f"OPTICAL_TUNE_GRIDS[{fusion!r}] key {key!r} is applied "
                                 f"before a scene is prepared and cannot be tuned here.")
            if section == "optical" and field_name in optical.READ_FIELDS:
                raise ValueError(
                    f"OPTICAL_TUNE_GRIDS[{fusion!r}] key {key!r} is applied before the pair "
                    f"is read - it chooses the scenes or converts the digital numbers - so "
                    f"varying it would need both dates read again per point, and the search "
                    f"reads them once. Read-time fields "
                    f"({', '.join(optical.READ_FIELDS)}) cannot be tuned by this search.")
            if not values:
                raise ValueError(f"OPTICAL_TUNE_GRIDS[{fusion!r}] key {key!r}: no values")
    return grids


def optical_grid_points(grids: dict[str, dict[str, tuple]] | None = None) -> list[dict]:
    """Every point of every fusion mode's grid, in declaration order.

    These points are crossed with the SAR point already chosen for each
    leave-one-event-out fold, rather than searched jointly with the SAR grid.
    That is a declared COST decision, recorded in docs/tuning_log.md before any
    optical score existed; each fold's SAR point is still chosen without the
    held-out event, so the result stays out of sample.

    Scored by `build_optical_matrix` / `leave_one_out_optical` below, which
    cross these points with each fold's own SAR point.
    """
    grids = validate_optical_grids(grids)
    points: list[dict] = []
    for fusion, grid in grids.items():
        keys = list(grid)
        for combo in itertools.product(*(grid[k] for k in keys)):
            point = {"optical.fusion": fusion}
            point.update(dict(zip(keys, combo)))
            points.append(point)
    return points


def grid_points(grids: dict[str, dict[str, tuple]] | None = None) -> list[dict]:
    """Every point of every mode's grid, in declaration order. Deterministic.

    One grid per mode, so a point never varies a parameter its mode ignores.
    `detect.mode` is added from the grid's key, which is why it must not also
    appear as an axis.
    """
    grids = validate_grid(grids)
    points: list[dict] = []
    for mode, grid in grids.items():
        keys = list(grid)
        for combo in itertools.product(*(grid[k] for k in keys)):
            point = {"detect.mode": mode}
            point.update(dict(zip(keys, combo)))
            points.append(point)
    return points


def mode_of(point: dict) -> str:
    return str(point.get("detect.mode", DetectParams().mode))


def points_by_mode(points: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in points:
        out.setdefault(mode_of(p), []).append(p)
    return out


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


def materialise_optical(point: dict, base_op: OpticalParams | None = None) -> OpticalParams:
    """The `optical.*` keys of one point -> the OpticalParams a run would use.

    `enabled=True` always: a point of this grid is by definition a run that
    consulted Sentinel-2. Fusion `off` is still such a run - it gathers,
    converts and reports optical evidence and then changes no pixel - which is
    exactly what makes `off` an honest control in the comparison rather than a
    synonym for the radar-only configuration.
    """
    op = base_op or OpticalParams()
    changes = {k.split(".", 1)[1]: v for k, v in point.items() if k.startswith("optical.")}
    return replace(op, enabled=True, **changes)


def materialise_full(point: dict, n_pre: int = DEV_N_PRE
                     ) -> tuple[DetectParams, RunParams, OpticalParams | None]:
    """A combined SAR + optical point -> every parameter set a run would use.

    Returns `op=None` when the point carries no `optical.*` key at all, which
    is the radar-only configuration and must keep hashing as it always has.
    """
    dp, rp = materialise(point, n_pre)
    has_optical = any(k.startswith("optical.") for k in point)
    return dp, rp, (materialise_optical(point) if has_optical else None)


def full_point_hash(point: dict, n_pre: int = DEV_N_PRE) -> str:
    """The identity of a combined point. Radar-only points hash as before."""
    return pipeline.params_hash(*materialise_full(point, n_pre))


def combine(sar_point: dict, optical_point: dict) -> dict:
    """One SAR point crossed with one optical point, SAR keys first."""
    return {**sar_point, **optical_point}


def fusion_of(point: dict) -> str:
    return str(point.get("optical.fusion", FUSION_OFF))


def point_label(point: dict) -> str:
    return " ".join(f"{k.split('.', 1)[1]}={v}" for k, v in point.items())


def best_per_mode(sel: "Selection") -> dict[str, dict]:
    """The best feasible candidate of each mode, for the A/B comparison.

    Reported so a mode is never adopted on a training-set margin alone: the
    two modes have different numbers of candidates, so the count of candidates
    and the best objective of each are both recorded.
    """
    out: dict[str, dict] = {}
    for cand in sel.candidates:
        mode = mode_of(cand.point)
        row = out.setdefault(mode, {"candidates": 0, "feasible": 0, "best_objective": None,
                                    "best_point": None, "rejected_by_control": 0})
        row["candidates"] += 1
        if cand.rejected and "false-flood" in cand.rejected:
            row["rejected_by_control"] += 1
        if not cand.feasible or cand.objective is None:
            continue
        row["feasible"] += 1
        if row["best_objective"] is None or cand.objective > row["best_objective"]:
            row["best_objective"] = cand.objective
            row["best_point"] = dict(cand.point)
    return out


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
    # Grouped by coefficient only: the detection mode is a classify-time
    # choice, so every mode reuses the same prepared scene.
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
            "by_mode": best_per_mode(self),
            "candidates": [asdict(c) for c in self.candidates],
        }


def select(matrix: Matrix, train: list[str], controls: list[str], points: list[dict],
           ceiling: float, n_pre: int = DEV_N_PRE, hash_of=point_hash) -> Selection:
    """Best feasible grid point on `train`, with every control under `ceiling`.

    A point is usable only when every training event AND every control has a
    score for it: comparing points over different event subsets would make the
    objective meaningless.

    `hash_of` is how a point is keyed in the matrix - `point_hash` for the radar
    search, `full_point_hash` for the optical pass, whose points also carry
    `optical.*` keys. The objective, the control constraint and the tie-break
    are identical for both, deliberately: the optical question is "does this
    help", judged by the same rule that chose the radar parameters.
    """
    out = Selection(train_events=list(train), control_events=list(controls), ceiling=ceiling)
    for point in points:
        phash = hash_of(point, n_pre)
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
                  points: list[dict], ceiling: float, n_pre: int = DEV_N_PRE,
                  hash_of=point_hash, points_for=None) -> dict[str, Selection]:
    """For each flood event: choose on the others, keep the choice.

    `points_for(held)` lets each fold search a DIFFERENT candidate list, which
    is what the optical pass needs: fold `held` crosses the optical grid with
    the SAR point the radar pass chose without `held`, so the SAR half of the
    configuration is still out of sample for that event.
    """
    out: dict[str, Selection] = {}
    for held in flood_events:
        train = [e for e in flood_events if e != held]
        candidates = points if points_for is None else points_for(held)
        out[held] = select(matrix, train, controls, candidates, ceiling, n_pre, hash_of)
    return out


# --- the optical pass ---------------------------------------------------------
#
# A SECOND leave-one-event-out pass, crossing the optical grid with the SAR
# point already chosen for each fold. Declared as a cost decision in
# docs/tuning_log.md before any optical score existed: a joint search would be
# 12 x 5 = 60 detections per event per fold instead of 5.


def optical_sar_points(selections: dict[str, Selection],
                       final: "Selection | None" = None) -> dict[str, dict]:
    """Which SAR point each fold uses in the optical pass: its own radar choice.

    The `""` key carries the final (whole-pool) SAR point, so the final optical
    choice is crossed with the final SAR choice.
    """
    out: dict[str, dict] = {}
    for held, sel in selections.items():
        if sel.ok:
            out[held] = dict(sel.chosen.point)
    if final is not None and final.ok:
        out[""] = dict(final.chosen.point)
    return out


def optical_points_needed(sar_points: dict[str, dict],
                          optical_points: list[dict]) -> list[dict]:
    """Every combined point the optical pass must score, deduplicated.

    Several folds usually choose the same SAR point, so the matrix is far
    smaller than folds x optical points.
    """
    seen: dict[str, dict] = {}
    for sar in sar_points.values():
        for opt in optical_points:
            point = combine(sar, opt)
            seen.setdefault(full_point_hash(point), point)
    return list(seen.values())


def build_optical_matrix(events: list[dict], points: list[dict],
                         runs_root: Path | None = None, *, n_pre: int = DEV_N_PRE,
                         tolerance_px: int = 1, reuse: bool = True,
                         allow_download: bool = False, max_pixels: int = MAX_SCENE_PIXELS,
                         log=print) -> Matrix:
    """Score every event at every combined (SAR x optical) point.

    Two things are read once per event and reused across every point, because
    both cost far more than a classification does:

      the radar scene   downloaded, speckle-filtered, terrain-derived;
      the optical pair  four GeoTIFFs read and converted to reflectance.

    Only `build_evidence` (two normalised indices over arrays already in
    memory) and `classify` run per point. An event whose optical stage REFUSED
    - too much cloud on the pair, or no clear pair at all - is still scored at
    every point: the configuration asked for Sentinel-2 and got none, so the
    map is radar-only and the score says so. That is the honest reading and it
    is why those events are not dropped (Rule 3.2).
    """
    matrix: Matrix = {}
    for i, event in enumerate(events, start=1):
        name = str(event["name"])
        log(f"[{i}/{len(events)}] {name}")
        scene = read = None
        for point in points:
            dp, rp, op = materialise_full(point, n_pre)
            phash = pipeline.params_hash(dp, rp, op)
            if reuse:
                cached = load_score(name, phash, runs_root)
                if cached is not None and cached.ok:
                    matrix[(name, phash)] = cached
                    log(f"      {point_label(point):<64} cached "
                        f"{cached.metrics.get(OBJECTIVE_METRIC, cached.metrics.get(CONTROL_METRIC))}")
                    continue
            try:
                if scene is None:
                    scene = prepare_event(event, rp, dp, runs_root, allow_download,
                                          max_pixels, op=op, log=lambda *a: None)
                    read = optical_read_of(event, scene, op, runs_root)
                    log(f"      optical: {optical_status_line(scene)}")
                point_scene = scene_with_optical(scene, read, op)
                score = score_with_scene(event, point_scene, dp, tolerance_px, op=op,
                                         log=lambda *a: None)
            except KeyboardInterrupt:
                raise
            # Broad on purpose, as in build_matrix: one unusable event must not
            # lose the whole matrix. It is recorded as failed, which blocks any
            # gate that needs it rather than quietly shrinking the set.
            except Exception as err:
                score = EventScore(event=name, kind=sb.row_kind(event), params_hash=phash,
                                   error=f"{type(err).__name__}: {err}")
                log(f"      {point_label(point):<64} FAILED {score.error[:70]}")
                save_score(score, runs_root)
                matrix[(name, phash)] = score
                if scene is None:   # the event itself is unusable
                    break
                continue
            save_score(score, runs_root)
            matrix[(name, phash)] = score
            key = OBJECTIVE_METRIC if score.kind != sb.KIND_CONTROL else CONTROL_METRIC
            log(f"      {point_label(point):<64} {key}={score.metrics.get(key)}")
        del scene, read
    return matrix


def optical_status_line(scene) -> str:
    """One line saying what Sentinel-2 gave this event. Printed per event."""
    report = scene.optical_report
    status = report.get("status")
    evidence = report.get("evidence") or {}
    if "clear_fraction_pair" in evidence:
        return (f"{status}, {evidence['clear_fraction_pair']:.1%} of the area clear on both "
                f"dates ({evidence.get('pre_day')} -> {evidence.get('post_day')})")
    return f"{status}: {str(report.get('message', ''))[:140]}"


def optical_read_of(event: dict, scene, op: OpticalParams | None,
                    runs_root: Path | None = None):
    """The read optical pair for this event, or None if the stage produced none.

    Read from the same cache and for exactly the pair the prepared scene chose
    (`scene.optical_pair`) - asking the catalogue again could pick a different
    one - and only when that scene's own optical stage succeeded, so a refusal
    (too cloudy, no clear pair) stays a refusal here and is never worked around.
    """
    if op is None or scene.optical_pair is None or scene.optical_evidence is None:
        return None
    cache = cache_dir(str(event["name"]), runs_root)
    return optical.read_pair(cache, scene.grid, scene.optical_pair, op)


def scene_with_optical(scene, read, op: OpticalParams | None):
    """The prepared scene with evidence rebuilt for ONE optical point.

    `replace` rather than mutation: the arrays are shared by reference (no copy),
    but the scene handed to each point is its own object, so a point can never
    see evidence built for another.
    """
    if op is None:
        return scene
    if read is None:
        # The stage refused or found nothing. The configuration still asked for
        # optical, and the map is radar-only: that is what gets scored.
        return replace(scene, optical_evidence=None, optical_params=op)
    evidence = optical.evidence_from_pair(read, op)
    return replace(scene, optical_evidence=evidence, optical_params=op)


# --- the adoption decision ----------------------------------------------------

def adoption_verdict(off: dict[str, float], on: dict[str, float],
                     controls_off: dict[str, float], controls_on: dict[str, float],
                     ceiling: float, rule: dict | None = None) -> dict:
    """Does optical fusion earn the default? The declared rule, applied.

    `off` / `on` are held-out tolerant F1 per flood event, `controls_*` are
    false-flood fractions per control, each measured with the SAME SAR point -
    so the only difference between the two columns is Sentinel-2.

    Every criterion is reported with the number it was decided on, pass or
    fail, so the verdict is checkable rather than announced
    (config.OPTICAL_ADOPTION, declared before this ran).
    """
    rule = rule or OPTICAL_ADOPTION
    shared = [e for e in off if e in on]
    mean_off = (sum(off[e] for e in shared) / len(shared)) if shared else None
    mean_on = (sum(on[e] for e in shared) / len(shared)) if shared else None
    gain = None if mean_off is None else round(mean_on - mean_off, 6)

    regressions = {e: round(on[e] - off[e], 6) for e in shared
                   if on[e] - off[e] < -rule["max_event_regression"]}
    shared_controls = [c for c in controls_off if c in controls_on]
    control_regressions = {c: round(controls_on[c] - controls_off[c], 6)
                           for c in shared_controls
                           if controls_on[c] - controls_off[c] > rule["max_event_regression"]}
    over_ceiling = {c: controls_on[c] for c in shared_controls if controls_on[c] > ceiling}

    criteria = {
        "mean_tolerant_f1_improves": {
            "measured_off": None if mean_off is None else round(mean_off, 6),
            "measured_on": None if mean_on is None else round(mean_on, 6),
            "gain": gain,
            "required": f"> {rule['min_mean_gain']}",
            "pass": bool(gain is not None and gain > rule["min_mean_gain"]),
        },
        "no_event_regresses_more_than_allowed": {
            "limit": rule["max_event_regression"],
            "events_worse": regressions,
            "pass": not regressions and bool(shared),
        },
        "no_control_regresses_more_than_allowed": {
            "limit": rule["max_event_regression"],
            "controls_worse": control_regressions,
            "pass": not control_regressions,
        },
        "every_control_under_the_gate_ceiling": {
            "ceiling": ceiling,
            "controls_over": over_ceiling,
            "pass": not over_ceiling,
        },
    }
    # A mean can be carried by one event. That is not a reason to reject the
    # change - the declared rule decides that - but it IS something a reader
    # must be told, so it is computed here rather than left to be noticed.
    gains = {e: on[e] - off[e] for e in shared}
    positive = {e: g for e, g in gains.items() if g > 0}
    total_gain = sum(gains.values())
    largest = max(positive, key=lambda e: positive[e]) if positive else None
    concentration = {
        "summed_gain": round(total_gain, 6),
        "largest_single_gain_event": largest,
        "largest_single_gain": None if largest is None else round(gains[largest], 6),
        "share_of_summed_gain": (None if largest is None or total_gain <= 0
                                 else round(gains[largest] / total_gain, 4)),
        "events_improved": sorted(positive),
        "events_unchanged": sorted(e for e, g in gains.items() if g == 0),
        "events_worse": sorted(e for e, g in gains.items() if g < 0),
    }
    if concentration["share_of_summed_gain"] is not None and \
            concentration["share_of_summed_gain"] >= 0.5:
        concentration["warning"] = (
            f"{concentration['share_of_summed_gain']:.0%} of the summed gain is one event "
            f"({largest}). The mean improvement is real but is NOT evidence that the change "
            f"helps broadly; quote the per-event column, not the mean, when describing it.")

    return {
        "rule": dict(rule),
        "control_ceiling": ceiling,
        "concentration": concentration,
        "events_compared": shared,
        "controls_compared": shared_controls,
        "per_event": {e: {"off": off[e], "on": on[e], "delta": round(on[e] - off[e], 6)}
                      for e in shared},
        "per_control": {c: {"off": controls_off[c], "on": controls_on[c],
                            "delta": round(controls_on[c] - controls_off[c], 6)}
                        for c in shared_controls},
        "criteria": criteria,
        "keep_fusion": all(c["pass"] for c in criteria.values()),
    }


def optical_fold_selections(matrix: Matrix, sar_points: dict[str, dict],
                            optical_points: list[dict], floods: list[str],
                            controls: list[str], ceiling: float,
                            n_pre: int = DEV_N_PRE, fusion: str = FUSION_UNION
                            ) -> dict[str, Selection]:
    """Per fold: the best point of ONE fusion mode, chosen on the other events.

    Restricting the candidates to a single fusion mode is what makes the
    comparison a comparison: `off` and `union` are each given their own best
    shot on the same training events and the same SAR point, and then judged on
    the held-out event. A free search over all five points at once would answer
    a different (and weaker) question - "did anything win" - and could pick
    `union` on one fold and `off` on the next, which no default can express.
    """
    folds = [e for e in floods if e in sar_points]
    candidates = [p for p in optical_points if fusion_of(p) == fusion]
    return leave_one_out(
        matrix, folds, controls, [], ceiling, n_pre, full_point_hash,
        points_for=lambda held: [combine(sar_points[held], p) for p in candidates])


def held_out_values(matrix: Matrix, selections: dict[str, Selection],
                    metric: str = OBJECTIVE_METRIC) -> dict[str, float]:
    """Each fold's held-out value under its own chosen point. Skips what failed."""
    out: dict[str, float] = {}
    for held, sel in selections.items():
        if not sel.ok:
            continue
        score = matrix.get((held, sel.chosen.params_hash))
        if score is not None and score.ok and score.value(metric) is not None:
            out[held] = score.value(metric)
    return out


def control_values(matrix: Matrix, point: dict, controls: list[str],
                   n_pre: int = DEV_N_PRE) -> dict[str, float]:
    """Every control's false-flood fraction at one point. Skips what failed."""
    phash = full_point_hash(point, n_pre)
    out: dict[str, float] = {}
    for c in controls:
        score = matrix.get((c, phash))
        if score is not None and score.ok and score.value(CONTROL_METRIC) is not None:
            out[c] = score.value(CONTROL_METRIC)
    return out


def print_optical_table(matrix: Matrix, off: dict[str, Selection], on: dict[str, Selection],
                        sar_points: dict[str, dict], events: list[str], log=print) -> None:
    """Per event: what optical could SEE, and what it did to the score both ways.

    The clear fraction is the first column for a reason: on an event where
    Sentinel-2 saw nothing, the two columns are identical BY CONSTRUCTION, and a
    reader must be able to tell that apart from a genuine dead heat.
    """
    head = (f"  {'event':<32} {'clear pair':>10} {'F1 off':>7} {'F1 on':>7} {'delta':>7}  "
            f"{'strict off':>10} {'strict on':>9}  {'chosen union point':<34}")
    log(head)
    log("  " + "-" * (len(head) - 2))
    for event in events:
        rows = []
        for sel in (off.get(event), on.get(event)):
            score = (None if sel is None or not sel.ok
                     else matrix.get((event, sel.chosen.params_hash)))
            rows.append(score)
        clear = None
        for score in rows:
            if score is not None and score.ok:
                clear = score.provenance.get("optical", {}).get("clear_fraction_pair", clear)
        f = lambda s, k: ("-" if s is None or not s.ok or s.value(k) is None  # noqa: E731
                          else f"{s.value(k):.4f}")
        delta = ("-" if any(s is None or not s.ok or s.value(OBJECTIVE_METRIC) is None
                            for s in rows)
                 else f"{rows[1].value(OBJECTIVE_METRIC) - rows[0].value(OBJECTIVE_METRIC):+.4f}")
        chosen = on.get(event)
        label = ("none feasible" if chosen is None or not chosen.ok
                 else " ".join(f"{k.split('.', 1)[1]}={v}" for k, v in chosen.chosen.point.items()
                               if k.startswith("optical.") and k != "optical.fusion"))
        log(f"  {event:<32} {('-' if clear is None else f'{clear:.3f}'):>10} "
            f"{f(rows[0], OBJECTIVE_METRIC):>7} {f(rows[1], OBJECTIVE_METRIC):>7} {delta:>7}  "
            f"{f(rows[0], 'f1'):>10} {f(rows[1], 'f1'):>9}  {label:<34}")


def print_verdict(verdict: dict, log=print) -> None:
    log("\n=== ADOPTION RULE (declared in config.OPTICAL_ADOPTION before this ran) ===")
    for name, c in verdict["criteria"].items():
        mark = "PASS" if c["pass"] else "FAIL"
        detail = ", ".join(f"{k}={v}" for k, v in c.items() if k != "pass")
        log(f"  [{mark}] {name}: {detail}")
    conc = verdict.get("concentration") or {}
    if conc.get("warning"):
        log(f"\n  CONCENTRATION: {conc['warning']}")
        log(f"    improved: {', '.join(conc['events_improved']) or 'none'}")
        log(f"    unchanged: {', '.join(conc['events_unchanged']) or 'none'}")
        log(f"    worse: {', '.join(conc['events_worse']) or 'none'}")
    log(f"\n  VERDICT: {'KEEP' if verdict['keep_fusion'] else 'DO NOT KEEP'} optical fusion"
        + ("" if verdict["keep_fusion"] else
           " - optical stays separate evidence in run outputs, fusion stays `off`"))


# --- the config diff ----------------------------------------------------------

def config_diff(dp: DetectParams, rp: RunParams,
                op: OpticalParams | None = None) -> list[dict]:
    """Chosen values against what strandline/config.py declares today.

    `op` is included only when the optical pass endorsed a configuration, so a
    run that did not score optical never suggests an optical edit.
    """
    diff = []
    pairs = [("detect", dp, DetectParams()), ("run", rp, RunParams())]
    if op is not None:
        pairs.append(("optical", op, OpticalParams()))
    for section, chosen, current in pairs:
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


# --- the optical pass, end to end ---------------------------------------------

def run_optical_pass(planned: list[dict], floods: list[str], controls: list[str],
                     selections: dict[str, Selection], final: Selection,
                     ceiling: float, args, tolerance_px: int,
                     radar_matrix: Matrix | None = None
                     ) -> tuple[dict, list[EventScore], list[EventScore]]:
    """Score the optical grid, compare fusion off against on, apply the rule.

    Returns (report, held-out flood scores, control scores) for the configuration
    the rule actually endorses - which is the `off` column unless the rule says
    KEEP, so a refusal to adopt writes the numbers that were already true.
    """
    print("\n=== OPTICAL PASS (second leave-one-event-out, over OPTICAL_TUNE_GRIDS) ===")
    optical_points = optical_grid_points()
    sar_points = optical_sar_points(selections, final)
    if not sar_points:
        print("  the radar pass chose nothing, so there is no SAR point to cross. Skipped.")
        return {"status": "skipped", "reason": "no SAR point chosen by the radar pass"}, [], []

    needed = optical_points_needed(sar_points, optical_points)
    print(f"  {len(optical_points)} optical point(s) x {len(set(map(str, sar_points.values())))} "
          f"distinct SAR point(s) -> {len(needed)} configuration(s), "
          f"{len(needed) * len(planned)} classification(s) at most")
    for fusion, axes in OPTICAL_TUNE_GRIDS.items():
        print(f"      {fusion}: " + (", ".join(f"{k.split('.', 1)[1]}={list(v)}"
                                               for k, v in axes.items()) or "(one point)"))

    matrix = build_optical_matrix(planned, needed, args.runs_root, n_pre=args.n_pre,
                                 tolerance_px=tolerance_px, reuse=not args.no_reuse,
                                 allow_download=args.allow_download,
                                 max_pixels=args.max_pixels, log=lambda *a: print(*a))

    off_sel = optical_fold_selections(matrix, sar_points, optical_points, floods, controls,
                                      ceiling, args.n_pre, FUSION_OFF)
    on_sel = optical_fold_selections(matrix, sar_points, optical_points, floods, controls,
                                     ceiling, args.n_pre, FUSION_UNION)
    off_held = held_out_values(matrix, off_sel)
    on_held = held_out_values(matrix, on_sel)

    # Controls are compared at the FINAL SAR point, one configuration each way,
    # because a control has no held-out notion: it constrains every choice.
    final_sar = sar_points.get("")
    off_point = combine(final_sar, {"optical.fusion": FUSION_OFF}) if final_sar else None
    final_on = select(matrix, floods, controls,
                      [combine(final_sar, p) for p in optical_points
                       if fusion_of(p) == FUSION_UNION],
                      ceiling, args.n_pre, full_point_hash) if final_sar else None
    off_controls = control_values(matrix, off_point, controls, args.n_pre) if off_point else {}
    on_controls = (control_values(matrix, final_on.chosen.point, controls, args.n_pre)
                   if final_on is not None and final_on.ok else {})

    print("\n=== FUSION OFF vs UNION, per event (same SAR point, held-out) ===")
    print("  'clear pair' is the share of the AOI Sentinel-2 could see on BOTH dates.")
    print("  Where it is absent, optical contributed nothing and the two columns are")
    print("  identical by construction, not by a dead heat.\n")
    print_optical_table(matrix, off_sel, on_sel, sar_points,
                        [e for e in floods if e in sar_points])
    if off_controls or on_controls:
        print(f"\n  controls (false-flood fraction, ceiling {ceiling}):")
        for c in controls:
            a, b = off_controls.get(c), on_controls.get(c)
            delta = ("-" if a is None or b is None else f"{b - a:+.4f}")
            print(f"  {c:<32} {'-' if a is None else f'{a:.4f}':>10} "
                  f"{'-' if b is None else f'{b:.4f}':>10} {delta:>8}"
                  + ("   OVER CEILING" if b is not None and b > ceiling else ""))

    # The `off` column must BE the radar-only configuration, numerically.
    # Checked on the real dev set before the verdict rests on it.
    equivalence = fusion_off_matches_radar_only(
        matrix, radar_matrix or {}, sar_points, floods + controls, args.n_pre)
    print(f"\n  fusion `off` against radar-only at the same SAR point: "
          f"{equivalence['identical']}/{equivalence['compared']} identical")
    if equivalence["mismatched"]:
        print("  WARNING: fusion `off` did NOT reproduce the radar-only numbers on "
              f"{len(equivalence['mismatched'])} event/point pair(s). The comparison below "
              f"is not trustworthy until that is explained:")
        for k, v in list(equivalence["mismatched"].items())[:4]:
            print(f"    {k}: off={v['fusion_off']} radar_only={v['radar_only']}")

    verdict = adoption_verdict(off_held, on_held, off_controls, on_controls, ceiling)
    print_verdict(verdict)

    keep = verdict["keep_fusion"]
    chosen_sel = on_sel if keep else off_sel
    chosen_point = (final_on.chosen.point if keep and final_on is not None and final_on.ok
                    else off_point)
    flood_scores = [matrix[(e, s.chosen.params_hash)] for e, s in chosen_sel.items()
                    if s.ok and (e, s.chosen.params_hash) in matrix
                    and matrix[(e, s.chosen.params_hash)].ok]
    control_scores = ([matrix[(c, full_point_hash(chosen_point, args.n_pre))]
                       for c in controls
                       if (c, full_point_hash(chosen_point, args.n_pre)) in matrix
                       and matrix[(c, full_point_hash(chosen_point, args.n_pre))].ok]
                      if chosen_point else [])

    report = {
        "status": "scored",
        "method": {
            "split": "leave-one-event-out over the flood events, SAR point fixed per fold "
                     "to the one the radar pass chose WITHOUT that event",
            "candidates_per_fold": {FUSION_OFF: 1, FUSION_UNION: len(
                [p for p in optical_points if fusion_of(p) == FUSION_UNION])},
            "controls": "compared at the final SAR point; IN-SAMPLE, as in the radar pass",
            "objective": f"maximise mean {OBJECTIVE_METRIC} over the training events",
            "constraint": f"every control {CONTROL_METRIC} <= {ceiling} (from eval/gates.yaml)",
        },
        "grid": {f: {k: list(v) for k, v in axes.items()}
                 for f, axes in OPTICAL_TUNE_GRIDS.items()},
        "sar_point_per_fold": sar_points,
        "configurations_scored": len(needed),
        "optical_coverage": optical_coverage(matrix, planned),
        "fusion_off_equals_radar_only": equivalence,
        "fusion_off": {held: sel.report() for held, sel in off_sel.items()},
        "fusion_union": {held: sel.report() for held, sel in on_sel.items()},
        "held_out_f1_tolerant": {"off": off_held, "on": on_held},
        "controls_false_flood_fraction": {"off": off_controls, "on": on_controls},
        "adoption": verdict,
        "chosen_configuration": chosen_point,
        "chosen_params_hash": (full_point_hash(chosen_point, args.n_pre)
                               if chosen_point else None),
        "caveats": [
            "Every number here was measured with the reflectance conversion CORRECTED on "
            "2026-10-12: the Copernicus openEO backend returns L2A already offset-corrected, "
            "and the earlier code applied BOA_ADD_OFFSET a second time. Any optical number "
            "from before that date is void.",
            "Four of the ten gate-plan events have no usable Sentinel-2 pair (clear fraction "
            "below min_clear_fraction), so on those the two columns are identical by "
            "construction. They are kept in the pool, not dropped: dropping them would be "
            "Rule 3.2.",
            "`off` is NOT the radar-only configuration: it consulted Sentinel-2 and then "
            "changed no pixel, so it has its own params_hash. Its metrics are identical to "
            "the radar-only ones, which is asserted, not assumed.",
            f"Measured with n_pre={args.n_pre}, as the radar pass was.",
        ],
    }
    return report, flood_scores, control_scores


def fusion_off_matches_radar_only(optical_matrix: Matrix, radar_matrix: Matrix,
                                  sar_points: dict[str, dict], events: list[str],
                                  n_pre: int = DEV_N_PRE) -> dict:
    """Does fusion `off` reproduce the radar-only numbers, pixel for pixel?

    It must. `off` gathers Sentinel-2, converts it, builds the evidence and
    then adds no pixel, so its class map has to be byte-identical to the
    radar-only map at the same SAR point - and therefore every metric identical
    too. The claim is asserted in tests on synthetic scenes
    (tests/test_detect_optical_fusion.py); this checks it on the REAL dev set,
    where a mistake in the eval plumbing - the wrong scene handed to a point,
    evidence leaking between points - would show up and a synthetic test would
    not.

    A mismatch is reported, never smoothed over: it would mean the `off` column
    of the comparison is not the configuration the published numbers describe,
    and the comparison would be meaningless.
    """
    compared, mismatched = 0, {}
    for sar in {str(sorted(p.items())): p for p in sar_points.values()}.values():
        for event in events:
            off_hash = full_point_hash(combine(sar, {"optical.fusion": FUSION_OFF}), n_pre)
            radar_hash = point_hash(sar, n_pre)
            a = optical_matrix.get((event, off_hash))
            b = radar_matrix.get((event, radar_hash))
            if a is None or b is None or not a.ok or not b.ok:
                continue
            compared += 1
            if a.metrics != b.metrics:
                mismatched[f"{event}@{radar_hash}"] = {"fusion_off": a.metrics,
                                                       "radar_only": b.metrics}
    return {"compared": compared, "identical": compared - len(mismatched),
            "mismatched": mismatched, "pass": compared > 0 and not mismatched}


def optical_coverage(matrix: Matrix, planned: list[dict]) -> dict:
    """Per event: what Sentinel-2 actually gave, read back off the score files.

    The gate numbers mean something different on an event optical could not see,
    so this table travels with them.
    """
    out: dict[str, dict] = {}
    for event in planned:
        name = str(event["name"])
        for (ev, _), score in matrix.items():
            if ev != name or not score.ok:
                continue
            opt = score.provenance.get("optical") or {}
            if not opt:
                continue
            out[name] = {
                "status": opt.get("status"),
                "clear_fraction_pair": opt.get("clear_fraction_pair"),
                "pre_day": opt.get("pre_day"),
                "post_day": opt.get("post_day"),
                "post_latency_days": opt.get("post_latency_days"),
                "message": opt.get("message"),
            }
            break
    return out


# --- main ---------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--plan", default="gates", choices=["gates", "all"])
    ap.add_argument("--events", nargs="*", help="explicit event names instead of a plan")
    ap.add_argument("--n-pre", type=int, default=DEV_N_PRE)
    ap.add_argument("--no-write", action="store_true", help="append no rows to flood.csv")
    ap.add_argument("--optical", action="store_true",
                    help="after the radar pass, run the SECOND leave-one-event-out pass over "
                         "OPTICAL_TUNE_GRIDS, crossing each fold's own SAR point with every "
                         "optical point, and apply the declared adoption rule. Needs the "
                         "Sentinel-2 dev cache (eval.fetch_dev_data --optical). Rows are "
                         "appended only if the rule says KEEP")
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
    per_mode = points_by_mode(points)
    print(f"grid: {len(points)} point(s) over {len(per_mode)} mode(s): "
          + ", ".join(f"{m} {len(ps)}" for m, ps in per_mode.items()))
    for mode, axes in TUNE_GRIDS.items():
        print(f"      {mode}: " + ", ".join(f"{k.split('.', 1)[1]}={list(v)}"
                                            for k, v in axes.items()))
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

    print("\n=== MODE COMPARISON (training mean, all pool events) ===")
    print("A mode with more candidates has more chances to fit the training set,")
    print("so a narrow win here means little: the held-out column decides.")
    ab = best_per_mode(final)
    head = f"  {'mode':<14} {'candidates':>10} {'feasible':>9} {'rejected by control':>20} {'best train F1 tol':>18}"
    print(head)
    for mode in sorted(ab):
        row = ab[mode]
        best = row["best_objective"]
        print(f"  {mode:<14} {row['candidates']:>10} {row['feasible']:>9} "
              f"{row['rejected_by_control']:>20} "
              f"{('-' if best is None else f'{best:.4f}'):>18}")
    held_by_mode: dict[str, list[float]] = {}
    for held, sel in selections.items():
        if sel.ok and (held, sel.chosen.params_hash) in matrix:
            sc = matrix[(held, sel.chosen.params_hash)]
            if sc.ok and sc.value(OBJECTIVE_METRIC) is not None:
                held_by_mode.setdefault(mode_of(sel.chosen.point), []).append(
                    sc.value(OBJECTIVE_METRIC))
    if held_by_mode:
        print("\n  held-out events per chosen mode (this is the number that decides):")
        for mode, vals in sorted(held_by_mode.items()):
            print(f"    {mode:<14} {len(vals)} event(s), mean held-out "
                  f"{OBJECTIVE_METRIC} {sum(vals) / len(vals):.4f}, "
                  f"worst {min(vals):.4f}")

    control_scores: list[EventScore] = []
    diff: list[dict] = []
    dp_final = rp_final = None
    if final.ok:
        dp_final, rp_final = materialise(final.chosen.point, args.n_pre)
        diff = config_diff(dp_final, rp_final)
        # Recomputed below if the optical pass endorses a fusion configuration.
        control_scores = [matrix[(c, final.chosen.params_hash)] for c in controls
                          if (c, final.chosen.params_hash) in matrix
                          and matrix[(c, final.chosen.params_hash)].ok]

    print("\n=== PER-EVENT SCORES ===")
    print("flood events: leave-one-event-out (parameters never saw the event)")
    print("controls:     final parameters; IN-SAMPLE, every control constrained the choice\n")
    print_table(loeo_scores + control_scores)
    print_coverage(loeo_scores + control_scores, args.plan)

    # --- the optical pass ----------------------------------------------------
    optical_report: dict | None = None
    optical_scores: list[EventScore] = []
    optical_controls: list[EventScore] = []
    if args.optical:
        optical_report, optical_scores, optical_controls = run_optical_pass(
            planned, floods, controls, selections, final, ceiling, args, tolerance_px,
            radar_matrix=matrix)
        # The config edit a human has to make now includes the optical section,
        # but ONLY if the declared rule endorsed fusion. A pass that said no
        # must not leave a suggestion to turn it on.
        if (optical_report.get("status") == "scored"
                and optical_report["adoption"]["keep_fusion"]
                and optical_report.get("chosen_configuration") and final.ok):
            _, _, op_final = materialise_full(optical_report["chosen_configuration"],
                                              args.n_pre)
            diff = config_diff(dp_final, rp_final, op_final)

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
        "grid": {mode: {k: list(v) for k, v in axes.items()}
                 for mode, axes in TUNE_GRIDS.items()},
        "modes_compared": sorted(points_by_mode(points)),
        "pool": {"flood_events": floods, "gate_eligible": gate_floods, "controls": controls},
        "coverage": coverage([s.event for s in loeo_scores + control_scores], args.plan),
        "loeo": {held: sel.report() for held, sel in selections.items()},
        "held_out_scores": {s.event: s.metrics for s in loeo_scores},
        "final": final.report(),
        "chosen_parameters": (None if not final.ok else
                              {"detect": asdict(dp_final), "run": asdict(rp_final),
                               "params_hash": final.chosen.params_hash}),
        "config_diff": diff,
        "mode_comparison": best_per_mode(final),
        "optical_pass": optical_report,
        "caveats": [
            "The two modes have different numbers of grid candidates "
            f"({', '.join(f'{m}: {len(ps)}' for m, ps in points_by_mode(points).items())}), "
            "so the mode with more candidates has more chances to fit the training events. "
            "Only the held-out numbers decide which mode is adopted.",
            "The primary metric is flood-affected area (our flood classes UNION debris) "
            "against each reference's flooded + debris/mass-movement classes, decided in "
            "docs/tuning_log.md before this mode was scored. Water-only scores are in each "
            "event's score file.",
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

    # Which configuration's rows become the gate numbers. Exactly ONE set is
    # written per run: check_gates averages every row of the latest run_id, so
    # writing both the radar-only and the optical rows would silently average
    # two different configurations together.
    adopted = "radar-only (optical not consulted)"
    flood_rows, control_rows = loeo_scores, control_scores
    if optical_report is not None and optical_report.get("status") == "scored":
        if optical_report["adoption"]["keep_fusion"]:
            adopted = "optical fusion `union` (adoption rule: KEEP)"
            flood_rows, control_rows = optical_scores, optical_controls
        else:
            adopted = ("radar-only (adoption rule: DO NOT KEEP optical fusion; the optical "
                       "pass changed no published number)")

    if args.no_write:
        print("\n--no-write: no rows appended to eval/results/flood.csv.")
    else:
        print(f"\n  rows describe: {adopted}")
        written = write_rows(flood_rows, [e for e in planned
                                          if sb.row_kind(e) != sb.KIND_CONTROL],
                             events_all, run_id, "loeo", args.results_dir)
        if control_rows:
            written.update(write_rows(control_rows,
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
