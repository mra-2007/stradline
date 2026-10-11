"""Pre-download the Sentinel-1 and DEM rasters the dev scoreboard needs.

Run this in your own terminal, once, before scoring. It only downloads
allowed pipeline inputs (Sentinel-1 GRD and the Copernicus DEM via openEO)
and never touches reference data.

    venv/Scripts/python.exe -m eval.fetch_dev_data --estimate      # plan only, no network
    venv/Scripts/python.exe -m eval.fetch_dev_data                 # download
    venv/Scripts/python.exe -m eval.fetch_dev_data --optical       # + Sentinel-2
    venv/Scripts/python.exe -m eval.fetch_dev_data --events norway_selsverket_2023

Everything is cached under runs/eval/<event>/cache with the same names the
pipeline uses, so this script is only a warm-up: `eval.run_scoreboard` and
`eval.tune` find the files and download nothing. It is resumable - stop it
with Ctrl-C and run it again, and it picks up the files that are missing.

WHAT IS IN THE PLAN (and what is not)

`--plan gates` (the default) covers what the accuracy gates actually rest on:
every flood event whose reference is independent of our own Sentinel-1 input,
plus the hardest no-flood controls. Flood events with a Sentinel-1-derived
reference are written as kind=flood_dependent and can never move a gate
(see eval/scoreboard.py), so they are not worth a download yet; `--plan all`
fetches the whole dev set when there is time.

This makes the control gate rest on fewer controls than events.yaml declares.
That is a cost decision, taken on area and terrain and never on a score, and
the scoreboard prints the coverage (scored of declared) next to every number
so the gap is always visible. It is not a licence to drop an event that scores
badly: Rule 3.2.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from eval import scoreboard as sb
from strandline import ingest, optical, run as pipeline
from strandline.catalog import TrackChoice
from strandline.config import BACKSCATTER_COEFFICIENTS, DEV_N_PRE, OpticalParams, RunParams

ROOT = Path(__file__).resolve().parents[1]
EVAL_RUNS = ROOT / "runs" / "eval"

# The controls kept in the gate plan, and why. Chosen on terrain and area,
# before any score existed - the four steepest paired controls that are small
# enough to download. The flat-ground controls (Prum, Larkana) and the two
# large ones (Kranj at 2602 km2, Kuruwita's monsoon pair) come in with
# `--plan all`.
GATE_PLAN_CONTROLS = (
    "bosnia_konjic_control_2024",        # mean slope 21.8 deg, deep gorge shadow
    "bhutan_punakha_control_2024",       # mean slope 24.4 deg, Himalayan snow line
    "norway_ringebu_control_2023",       # snowmelt freshet, same track as its flood
    "ecuador_poza_honda_control_2024",   # permanent reservoir inside the bbox
)

# Measured from the runs/case_smoke cache: 43.0 MB for a 4.375 Mpx two-band
# Sentinel-1 GeoTIFF and 19.75 MB for the one-band DEM on the same grid.
BYTES_PER_PIXEL_S1 = 9.8
BYTES_PER_PIXEL_DEM = 4.5

# Sentinel-2: DERIVED, NOT MEASURED. Half of BYTES_PER_PIXEL_S1 per band (that
# figure covers two bands), plus a little for the single-byte class layer, which
# compresses well. No Sentinel-2 file has been downloaded yet, so this is the
# one part of the estimate not grounded in a real file. Recalibrate both numbers
# from the first real download, and say which they are when quoting a size.
BYTES_PER_PIXEL_S2_BAND = 4.9
BYTES_PER_PIXEL_S2_SCL = 1.3
S2_DATES_PER_EVENT = 2          # one clear pre-event date, one clear post-event

ESTIMATE_CAVEAT = (
    "\nEstimate only. The Sentinel-1 and DEM bytes per pixel are MEASURED from the"
    "\nruns/case_smoke cache; the Sentinel-2 figures are DERIVED from them and not yet"
    "\nconfirmed against a real file. The acquisition count assumes every event finds a"
    "\nfull stack, and the S2 column assumes a clear pair exists - pre-flight confirms"
    "\nboth, and an event with no clear pair downloads no Sentinel-2 at all."
)

def dev_run_params(coefficient: str = RunParams.backscatter_coefficient,
                   n_pre: int = DEV_N_PRE) -> RunParams:
    """The RunParams every dev-event run uses. One definition, shared."""
    return replace(RunParams(), n_pre=n_pre, backscatter_coefficient=coefficient)


def cache_dir(event_name: str, runs_root: Path | None = None) -> Path:
    """Where one event's rasters live. Shared by both coefficients, because
    the coefficient is already part of each Sentinel-1 filename."""
    return (runs_root or EVAL_RUNS) / event_name / "cache"


def select_events(plan: str = "gates", only: list[str] | None = None,
                  events: dict[str, dict] | None = None) -> list[dict]:
    """The events in a plan, in a fixed order (floods first, then controls)."""
    all_events = events if events is not None else sb.load_events()
    if only:
        unknown = [n for n in only if n not in all_events]
        if unknown:
            raise SystemExit(f"unknown event(s): {', '.join(unknown)}\n"
                             f"known: {', '.join(all_events)}")
        chosen = [all_events[n] for n in only]
    elif plan == "all":
        chosen = list(all_events.values())
    elif plan == "gates":
        chosen = [e for e in all_events.values() if sb.gate_eligible(e)]
        chosen += [all_events[n] for n in GATE_PLAN_CONTROLS if n in all_events]
    else:
        raise SystemExit(f"unknown plan {plan!r}; choose gates or all")
    order = {sb.KIND_FLOOD: 0, sb.KIND_FLOOD_DEPENDENT: 1, sb.KIND_CONTROL: 2}
    return sorted(chosen, key=lambda e: (order[sb.row_kind(e)], str(e["name"])))


def coverage(scored: list[str], plan: str = "gates", events: dict[str, dict] | None = None
             ) -> dict[str, dict]:
    """How many events of each CSV kind were scored, of how many declared.

    Printed next to every aggregate so a partial dev set is never mistaken for
    the whole one.
    """
    all_events = events if events is not None else sb.load_events()
    done = set(scored)
    out: dict[str, dict] = {}
    planned = {str(e["name"]) for e in select_events(plan, events=all_events)}
    for kind in (sb.KIND_FLOOD, sb.KIND_FLOOD_DEPENDENT, sb.KIND_CONTROL):
        names = [n for n, e in all_events.items() if sb.row_kind(e) == kind]
        out[kind] = {
            "declared": len(names),
            "planned": len([n for n in names if n in planned]),
            "scored": len([n for n in names if n in done]),
            "missing": sorted(n for n in names if n not in done),
        }
    return out


# --- the download plan --------------------------------------------------------

@dataclass
class Task:
    event: str
    kind: str                 # "s1" | "dem" | "s2"
    coefficient: str | None
    day: date | None
    path: Path
    grid: ingest.Grid
    acq: object = None
    # "s2" only: both files of one date (reflectance and the class layer), and
    # the parameters that name them.
    s2_paths: dict | None = None
    op: OpticalParams | None = None

    @property
    def label(self) -> str:
        if self.kind == "dem":
            return f"{self.event} DEM"
        if self.kind == "s2":
            return f"{self.event} S2 {self.day}"
        return f"{self.event} {self.coefficient} {self.day}"

    @property
    def paths(self) -> list[Path]:
        if self.kind == "s2" and self.s2_paths is not None:
            return [self.s2_paths["refl"], self.s2_paths["scl"]]
        return [self.path]

    @property
    def done(self) -> bool:
        """Every file this task produces is already on disk."""
        return all(q.exists() for q in self.paths)

    @property
    def size_on_disk(self) -> int:
        return sum(q.stat().st_size for q in self.paths if q.exists())

    @property
    def expected_bytes(self) -> int:
        px = self.grid.width * self.grid.height
        if self.kind == "s2":
            return int(px * (len(optical.S2_REFLECTANCE_BANDS) * BYTES_PER_PIXEL_S2_BAND
                             + BYTES_PER_PIXEL_S2_SCL))
        per_px = BYTES_PER_PIXEL_S1 if self.kind == "s1" else BYTES_PER_PIXEL_DEM
        return int(px * per_px)


def plan_event(event: dict, coefficients: list[str], runs_root: Path | None = None,
               n_pre: int = DEV_N_PRE, choice: TrackChoice | None = None,
               op: OpticalParams | None = None, log=print) -> list[Task]:
    """Pre-flight one event and list the files its runs will need.

    With `op`, the Sentinel-2 pair is pre-flighted too. An event with no clear
    pair is NOT a failure - it is the normal state of a clouded monsoon flood,
    and scoring it radar-only is the honest outcome - so it is logged and
    skipped rather than refused.
    """
    bbox = tuple(float(v) for v in event["bbox"])
    event_day = date.fromisoformat(str(event["event_date"]))
    name = str(event["name"])
    cache = cache_dir(name, runs_root)
    tasks: list[Task] = []

    for i, coefficient in enumerate(coefficients):
        rp = dev_run_params(coefficient, n_pre)
        grid = ingest.make_grid(bbox, rp.resolution_m)
        if choice is None:
            choice = pipeline.preflight(bbox, event_day, rp, log=log)
            if choice is None:
                raise pipeline.PipelineError(
                    f"{name}: no usable same-track pair with n_pre={n_pre}; "
                    "this event cannot be scored and must be reported as such, not dropped.")
        paths = pipeline.scene_cache_paths(choice, cache, grid.res, coefficient,
                                           rp.elevation_model)
        for acq in list(choice.pre) + [choice.post]:
            tasks.append(Task(name, "s1", coefficient, acq.day, paths["s1"][acq.day], grid, acq))
        if i == 0:  # the DEM does not depend on the coefficient
            tasks.append(Task(name, "dem", None, None, paths["dem"], grid))

    if op is not None and op.enabled:
        pair = pipeline.optical_preflight(bbox, event_day, op, log=log)
        if pair is None:
            log(f"  {name}: no clear Sentinel-2 pair; this event will be scored "
                f"radar-only")
        else:
            for day in (pair.pre.day, pair.post.day):
                s2_paths = optical.s2_cache_paths(cache, day, grid.res)
                tasks.append(Task(name, "s2", None, day, s2_paths["refl"], grid,
                                  s2_paths=s2_paths, op=op))
    return tasks


# --- running it ---------------------------------------------------------------

_local = threading.local()
_print_lock = threading.Lock()


def _connection():
    """One openEO connection per worker thread (the client is not thread-safe)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = ingest.connect()
    return conn


def _download(task: Task) -> tuple[Task, float, str]:
    t0 = time.time()
    if task.done:
        return task, 0.0, "cached"
    conn = _connection()
    if task.kind == "dem":
        ingest.download_dem(conn, task.grid, task.path)
    elif task.kind == "s2":
        optical.download_s2_date(conn, task.day, task.grid, task.s2_paths, task.op)
    else:
        rp = dev_run_params(task.coefficient)
        ingest.download_s1(conn, task.acq, task.grid, task.path, task.coefficient,
                           rp.use_sar_mask, rp.elevation_model)
    return task, time.time() - t0, "downloaded"


def _mb(n: float) -> str:
    return f"{n / 1e6:,.0f} MB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--plan", default="gates", choices=["gates", "all"],
                    help="gates: what the accuracy gates rest on (default). all: every dev event")
    ap.add_argument("--events", nargs="*", help="explicit event names instead of a plan")
    ap.add_argument("--coefficients", nargs="*", default=list(BACKSCATTER_COEFFICIENTS),
                    choices=list(BACKSCATTER_COEFFICIENTS))
    ap.add_argument("--n-pre", type=int, default=DEV_N_PRE,
                    help="pre-event images per stack (default from config.DEV_N_PRE)")
    ap.add_argument("--workers", type=int, default=3, help="parallel openEO downloads")
    ap.add_argument("--runs-root", type=Path, default=EVAL_RUNS)
    ap.add_argument("--optical", action="store_true",
                    help="also fetch the Sentinel-2 pair for each event, so the dev set can "
                         "be scored with the optical evidence the live pipeline uses")
    ap.add_argument("--optical-post-days", type=int,
                    default=OpticalParams.max_post_latency_days,
                    help="days after the event to look for a clear Sentinel-2 scene "
                         "(default: %(default)s)")
    ap.add_argument("--estimate", action="store_true",
                    help="print the size estimate from the grids alone and exit (no network)")
    args = ap.parse_args(argv)

    op = (replace(OpticalParams(), enabled=True, max_post_latency_days=args.optical_post_days)
          if args.optical else None)

    events = select_events(args.plan, args.events)
    print(f"Plan '{args.plan}': {len(events)} event(s), coefficients "
          f"{', '.join(args.coefficients)}, n_pre={args.n_pre}, "
          f"sentinel-2 {'ON' if op else 'off'}\n")

    if args.estimate:
        total = s2_total = 0
        print(f"{'event':<34} {'kind':<16} {'grid':>13} {'Mpx':>7} {'S1+DEM':>11} "
              f"{'S2':>11} {'total':>11}")
        print("-" * 108)
        for ev in events:
            grid = ingest.make_grid(tuple(float(v) for v in ev["bbox"]), RunParams.resolution_m)
            px = grid.width * grid.height
            radar = (px * BYTES_PER_PIXEL_S1 * (args.n_pre + 1) * len(args.coefficients)
                     + px * BYTES_PER_PIXEL_DEM)
            s2 = (px * (len(optical.S2_REFLECTANCE_BANDS) * BYTES_PER_PIXEL_S2_BAND
                        + BYTES_PER_PIXEL_S2_SCL) * S2_DATES_PER_EVENT) if op else 0
            total += radar + s2
            s2_total += s2
            print(f"{ev['name']:<34} {sb.row_kind(ev):<16} {grid.width:>6}x{grid.height:<6} "
                  f"{px / 1e6:>7.2f} {_mb(radar):>11} {_mb(s2):>11} {_mb(radar + s2):>11}")
        per_event_files = len(args.coefficients) * (args.n_pre + 1) + 1
        if op:
            per_event_files += 2 * S2_DATES_PER_EVENT    # reflectance + class layer
        n_files = per_event_files * len(events)
        print("-" * 108)
        print(f"{'TOTAL':<34} {'':<16} {'':>13} {'':>7} {_mb(total - s2_total):>11} "
              f"{_mb(s2_total):>11} {_mb(total):>11}  in {n_files} files")
        print(ESTIMATE_CAVEAT)
        return 0

    print("Pre-flight (catalogue only, no login) ...")
    tasks: list[Task] = []
    failed: list[str] = []
    for ev in events:
        try:
            tasks += plan_event(ev, args.coefficients, args.runs_root, args.n_pre,
                                op=op, log=lambda *_: None)
        except pipeline.PipelineError as err:
            print(f"  REFUSED {ev['name']}: {err}")
            failed.append(str(ev["name"]))
    todo = [t for t in tasks if not t.done]
    have = len(tasks) - len(todo)
    print(f"  {len(tasks)} file(s) needed, {have} already cached, {len(todo)} to download "
          f"(~{_mb(sum(t.expected_bytes for t in todo))})\n")
    if not todo:
        print("Nothing to do: the dev cache is complete for this plan.")
        return 1 if failed else 0

    t0 = time.time()
    done = bytes_got = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(_download, t): t for t in todo}
        try:
            for fut in as_completed(futures):
                task = futures[fut]
                try:
                    task, seconds, how = fut.result()
                except Exception as err:  # one bad file must not lose the rest
                    with _print_lock:
                        print(f"  FAILED  {task.label}: {type(err).__name__}: {err}")
                    failed.append(task.label)
                    continue
                done += 1
                size = task.size_on_disk
                bytes_got += size
                elapsed = time.time() - t0
                rate = done / max(elapsed, 1e-9)
                with _print_lock:
                    print(f"  [{done:>3}/{len(todo)}] {task.label:<52} {_mb(size):>9} "
                          f"{seconds:>6.0f}s  {how}   eta "
                          f"{(len(todo) - done) / max(rate, 1e-9) / 60:>5.0f} min")
        except KeyboardInterrupt:
            print("\nInterrupted. Files already written stay cached; run again to resume.")
            return 130

    print(f"\n{done}/{len(todo)} downloaded, {_mb(bytes_got)} in "
          f"{(time.time() - t0) / 60:.0f} min -> {args.runs_root}")
    if failed:
        print(f"{len(failed)} FAILED (run again to retry):")
        for f in failed:
            print(f"  {f}")
        return 1
    print("Dev cache complete for this plan. Next: "
          "venv/Scripts/python.exe -m eval.tune")
    return 0


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.exit(main())
