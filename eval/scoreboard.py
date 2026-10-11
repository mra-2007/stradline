"""Append scored rows to eval/results/*.csv.

The one job worth stating plainly: a dev event whose reference was drawn from
our own input sensor must never move an accuracy gate. Scoring such an event
measures how well we reproduce another Sentinel-1 flood map, not how well we
map a flood, so a gate resting on it would be self-congratulatory.

That is enforced here, at the point rows are written. `events.yaml` keeps
`kind: flood` for every flood event - it is a flood, and the file should say so
- while `independent_reference` records whether its reference is independent of
our input. This module maps the two onto the CSV's `kind` column:

    events.yaml                                    ->  CSV kind
    kind: flood,   independent_reference: true     ->  flood
    kind: flood,   independent_reference: false    ->  flood_dependent
    kind: control                                  ->  control

The gate for flood_mapping selects `where: {kind: flood, split: loeo}`, so
flood_dependent rows are excluded from every gate automatically, with no change
to the gates file and no new state to keep in sync. They still land in the same
CSV, so they stay visible on the scoreboard and in leave-one-event-out tuning.

Rule 3.5: these files are append-only. Nothing here rewrites or deletes a row.
"""

from __future__ import annotations

import csv
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "eval" / "events.yaml"
RESULTS = ROOT / "eval" / "results"

KIND_FLOOD = "flood"
KIND_FLOOD_DEPENDENT = "flood_dependent"
KIND_CONTROL = "control"

# Column order per results file (see the contract comment in the gates file).
COLUMNS = {
    "flood.csv": ["run_id", "timestamp", "git_commit", "params_hash", "event", "kind", "split",
                  "precision", "recall", "f1", "iou",
                  "precision_tolerant", "recall_tolerant", "f1_tolerant",
                  "false_flood_fraction"],
    "damage.csv": ["run_id", "timestamp", "git_commit", "params_hash", "event",
                   "road_segment_f1", "building_f1"],
    "cutoff.csv": ["run_id", "timestamp", "git_commit", "params_hash", "event", "settlement_f1"],
    "repro.csv": ["run_id", "timestamp", "git_commit", "params_hash", "event", "identical_output"],
}


def load_events(path: Path | None = None) -> dict[str, dict]:
    raw = yaml.safe_load((path or EVENTS).read_text(encoding="utf-8")) or {}
    return {str(e["name"]): e for e in raw.get("events", [])}


def row_kind(event: dict) -> str:
    """The `kind` written to the CSV, which is what gate filters select on.

    A flood event is downgraded to flood_dependent unless its reference is
    explicitly marked independent. Anything else - false, null, or the field
    missing entirely - is treated as not independent, so a new event cannot
    reach a gate by forgetting to answer the question.
    """
    kind = str(event.get("kind", "")).strip()
    if kind == KIND_CONTROL:
        return KIND_CONTROL
    if kind != KIND_FLOOD:
        raise ValueError(
            f"{event.get('name')!r}: kind must be {KIND_FLOOD!r} or {KIND_CONTROL!r}, got {kind!r}")
    return KIND_FLOOD if event.get("independent_reference") is True else KIND_FLOOD_DEPENDENT


def gate_eligible(event: dict) -> bool:
    """True when this event's rows can move the flood_mapping gate."""
    return row_kind(event) == KIND_FLOOD


def new_run_id(when: datetime | None = None) -> str:
    return (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def append_rows(filename: str, rows: list[dict], results_dir: Path | None = None) -> Path:
    """Append rows to a results CSV, writing the header only when creating it.

    Append-only by construction: the file is opened with mode "a" and no code
    path here truncates or rewrites it.
    """
    if filename not in COLUMNS:
        raise ValueError(f"unknown results file {filename!r}; expected one of {sorted(COLUMNS)}")
    out_dir = results_dir or RESULTS
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    cols = COLUMNS[filename]
    for row in rows:
        unknown = set(row) - set(cols)
        if unknown:
            raise ValueError(f"{filename}: unexpected column(s) {sorted(unknown)}")
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="raise")
        if is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in cols})
    return path


def flood_row(event: dict, metrics: dict, *, run_id: str, params_hash: str,
              split: str = "loeo", commit: str | None = None,
              timestamp: str | None = None) -> dict:
    """Build one flood.csv row, with `kind` derived from the event, never passed in."""
    row = {
        "run_id": run_id,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": commit or git_commit(),
        "params_hash": params_hash,
        "event": str(event["name"]),
        "kind": row_kind(event),
        "split": split,
    }
    for key in ("precision", "recall", "f1", "iou", "precision_tolerant", "recall_tolerant",
                "f1_tolerant", "false_flood_fraction"):
        if key in metrics:
            row[key] = metrics[key]
    return row


def summarise(path: Path | None = None) -> dict[str, list[str]]:
    """Which dev events can move a gate, and which only appear on the scoreboard."""
    events = load_events(path)
    out: dict[str, list[str]] = {KIND_FLOOD: [], KIND_FLOOD_DEPENDENT: [], KIND_CONTROL: []}
    for name, ev in events.items():
        out[row_kind(ev)].append(name)
    return out


def main(argv=None) -> int:
    groups = summarise()
    print(f"{len(groups[KIND_FLOOD])} flood event(s) count toward the flood_mapping gate:")
    for n in groups[KIND_FLOOD]:
        print(f"   {n}")
    print(f"\n{len(groups[KIND_FLOOD_DEPENDENT])} flood event(s) are scored but EXCLUDED from "
          f"gates (reference not independent of our Sentinel-1 input):")
    for n in groups[KIND_FLOOD_DEPENDENT]:
        print(f"   {n}")
    print(f"\n{len(groups[KIND_CONTROL])} control(s):")
    for n in groups[KIND_CONTROL]:
        print(f"   {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
