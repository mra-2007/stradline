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
