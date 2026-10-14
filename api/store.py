"""SQLite job store for Strandline API runs.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "api" / "runs.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                bbox_json TEXT NOT NULL,
                event_date TEXT NOT NULL,
                state TEXT NOT NULL,
                stages_json TEXT NOT NULL,
                error TEXT,
                out_dir TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_date ON runs (event_date, state)")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "run_id": row["run_id"],
        "bbox": json.loads(row["bbox_json"]),
        "event_date": row["event_date"],
        "state": row["state"],
        "stages": json.loads(row["stages_json"]),
        "error": row["error"],
        "out_dir": row["out_dir"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def find_finished_run(bbox: list[float] | tuple[float, ...], event_date: str,
                      db_path: Path | None = None) -> dict | None:
    target_bbox = [round(float(x), 5) for x in bbox]
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT * FROM runs WHERE event_date = ? AND state = 'done' ORDER BY updated_at DESC",
            (str(event_date),)
        )
        for row in cursor.fetchall():
            row_bbox = [round(float(x), 5) for x in json.loads(row["bbox_json"])]
            if row_bbox == target_bbox:
                return _row_to_dict(row)
    return None


def create_run(run_id: str, bbox: list[float] | tuple[float, ...], event_date: str,
               out_dir: str, db_path: Path | None = None) -> dict:
    now = _now_iso()
    bbox_json = json.dumps([float(x) for x in bbox])
    stages_json = json.dumps([])
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO runs (run_id, bbox_json, event_date, state, stages_json, error, out_dir, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', ?, NULL, ?, ?, ?)
            """,
            (run_id, bbox_json, str(event_date), stages_json, str(out_dir), now, now)
        )
    return get_run(run_id, db_path=db_path)  # type: ignore[return-value]


def update_run(run_id: str, state: str | None = None, stage: str | None = None,
               error: str | None = None, db_path: Path | None = None) -> dict | None:
    now = _now_iso()
    with get_connection(db_path) as conn:
        cursor = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        if not row:
            return None
        current = _row_to_dict(row)
        new_state = state if state is not None else current["state"]
        new_stages = list(current["stages"])
        if stage and stage not in new_stages:
            new_stages.append(stage)
        new_error = error if error is not None else current["error"]

        conn.execute(
            """
            UPDATE runs
            SET state = ?, stages_json = ?, error = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (new_state, json.dumps(new_stages), new_error, now, run_id)
        )
    return get_run(run_id, db_path=db_path)


def get_run(run_id: str, db_path: Path | None = None) -> dict | None:
    with get_connection(db_path) as conn:
        cursor = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return _row_to_dict(row)


def list_runs(db_path: Path | None = None) -> list[dict]:
    """Every registered run, newest first."""
    with get_connection(db_path) as conn:
        cursor = conn.execute("SELECT * FROM runs ORDER BY created_at DESC, run_id ASC")
        return [_row_to_dict(row) for row in cursor.fetchall()]


def prune_missing_runs(db_path: Path | None = None) -> list[str]:
    """Forget runs whose output directory is no longer on disk.

    These are rows left behind by deleted or temporary runs. They have no
    report, no layers and no quicklook, so every endpoint would 404 on them;
    carrying them in the run switcher only offers the user dead ends. Nothing
    under `eval/results/` is touched — this is the API's own job store.
    """
    removed: list[str] = []
    for item in list_runs(db_path=db_path):
        if Path(item["out_dir"]).is_dir():
            continue
        with get_connection(db_path) as conn:
            conn.execute("DELETE FROM runs WHERE run_id = ?", (item["run_id"],))
        removed.append(item["run_id"])
    return removed


def discover_runs(base_dir: Path = ROOT, db_path: Path | None = None) -> list[str]:
    """Register finished runs that are on disk but not in the database.

    A run directory counts as finished when it holds a `run_report.json`; the
    bbox and the event date are read from that report, so nothing here is keyed
    to a particular area, date or event. Runs nested one level deeper (the
    `runs/eval/<event>` and `runs/copilot_dev/<event>` trees) are included, which
    is what gives the dashboard's run switcher something to switch between.

    Already-registered runs are left exactly as they are.
    """
    runs_root = Path(base_dir) / "runs"
    if not runs_root.is_dir():
        return []

    candidates: list[Path] = []
    for report in sorted(runs_root.glob("*/run_report.json")):
        candidates.append(report.parent)
    for report in sorted(runs_root.glob("*/*/run_report.json")):
        candidates.append(report.parent)

    added: list[str] = []
    for run_dir in candidates:
        run_id = run_dir.name
        if get_run(run_id, db_path=db_path) is not None:
            continue
        try:
            report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        bbox = report.get("bbox_wgs84")
        event_date = str(report.get("event_date", ""))
        if not (isinstance(bbox, list) and len(bbox) == 4) or not event_date:
            continue
        now = _now_iso()
        mtime = datetime.fromtimestamp(
            (run_dir / "run_report.json").stat().st_mtime, tz=timezone.utc).isoformat()
        with get_connection(db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO runs
                    (run_id, bbox_json, event_date, state, stages_json, error, out_dir,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'done', ?, NULL, ?, ?, ?)
                """,
                (run_id, json.dumps(bbox), event_date,
                 json.dumps(["preflight", "fetch", "prepare", "classify", "write"]),
                 str(run_dir.resolve()), mtime, now)
            )
        added.append(run_id)
    return added


def register_case_smoke(base_dir: Path = ROOT, db_path: Path | None = None) -> dict | None:
    case_dir = base_dir / "runs" / "case_smoke"
    if not case_dir.exists() or not case_dir.is_dir():
        return None

    report_path = case_dir / "run_report.json"
    bbox = [85.10, 27.85, 85.45, 28.30]
    event_date = "2026-08-26"
    if report_path.exists():
        try:
            report_data = json.loads(report_path.read_text(encoding="utf-8"))
            bbox = report_data.get("bbox_wgs84", bbox)
            event_date = str(report_data.get("event_date", event_date))
        except Exception:
            pass

    existing = get_run("case_smoke", db_path=db_path)
    now = _now_iso()
    stages = ["preflight", "fetch", "prepare", "classify", "write"]
    with get_connection(db_path) as conn:
        if existing is None:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs (run_id, bbox_json, event_date, state, stages_json, error, out_dir, created_at, updated_at)
                VALUES ('case_smoke', ?, ?, 'done', ?, NULL, ?, ?, ?)
                """,
                (json.dumps(bbox), event_date, json.dumps(stages), str(case_dir.resolve()), now, now)
            )
        else:
            conn.execute(
                """
                UPDATE runs
                SET bbox_json = ?, event_date = ?, state = 'done', stages_json = ?, out_dir = ?, updated_at = ?
                WHERE run_id = 'case_smoke'
                """,
                (json.dumps(bbox), event_date, json.dumps(stages), str(case_dir.resolve()), now)
            )
    return get_run("case_smoke", db_path=db_path)
