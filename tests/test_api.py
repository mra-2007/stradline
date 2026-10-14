"""Tests for Strandline FastAPI application (api/main.py and api/store.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api import store
from api.main import LAYER_FILE_MAP, app


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test_runs.db"
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", test_db)
    store.init_db(test_db)
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_cors_headers(client):
    resp = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_preflight_success(client, monkeypatch):
    mock_choice = MagicMock()
    mock_choice.track = ("orbit", 123, "ASCENDING")
    mock_choice.post.day = "2026-08-28"
    mock_choice.latency_days = 2
    mock_pre = MagicMock()
    mock_pre.day = "2026-08-16"
    mock_choice.pre = [mock_pre]
    mock_choice.notes = ["track note"]

    monkeypatch.setattr("api.main.strandline_preflight", lambda bbox, event_day, rp: mock_choice)

    resp = client.get("/preflight?bbox=85.1,27.8,85.4,28.3&date=2026-08-26")
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "usable track found"
    assert data["chosen"]["latency_days"] == 2
    assert data["chosen"]["track"] == ["orbit", 123, "ASCENDING"]


def test_preflight_no_track(client, monkeypatch):
    monkeypatch.setattr("api.main.strandline_preflight", lambda bbox, event_day, rp: None)

    resp = client.get("/preflight?bbox=85.1,27.8,85.4,28.3&date=2026-08-26")
    assert resp.status_code == 200
    data = resp.json()
    assert data["chosen"] is None
    assert data["message"] == "no usable track"


def test_preflight_invalid_inputs(client):
    resp = client.get("/preflight?bbox=85.1,27.8&date=2026-08-26")
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data and "hint" in data

    resp = client.get("/preflight?bbox=85.1,27.8,85.4,28.3&date=not-a-date")
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data and "hint" in data


def test_runs_creation_and_status(client, monkeypatch, tmp_path):
    monkeypatch.setattr("api.main.strandline_run", lambda *args, **kwargs: 0)

    # Mock thread so test runs synchronously
    class SyncThread:
        def __init__(self, target, args=(), daemon=True):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("threading.Thread", SyncThread)

    payload = {"bbox": [80.10, 26.85, 80.45, 27.30], "event_date": "2026-05-15"}
    resp = client.post("/runs", json=payload)
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    assert run_id != "case_smoke"

    # Check status
    resp = client.get(f"/runs/{run_id}/status")
    assert resp.status_code == 200
    status_data = resp.json()
    assert status_data["state"] == "done"
    assert "completed" in status_data["stages"]
    assert status_data["error"] is None

    # Idempotent: same bbox and date returns existing run_id
    resp2 = client.post("/runs", json=payload)
    assert resp2.status_code == 200
    assert resp2.json()["run_id"] == run_id


def test_runs_refused_code_2(client, monkeypatch):
    monkeypatch.setattr("api.main.strandline_run", lambda *args, **kwargs: 2)

    class SyncThread:
        def __init__(self, target, args=(), daemon=True):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("threading.Thread", SyncThread)

    payload = {"bbox": [80.0, 20.0, 80.5, 20.5], "event_date": "2026-01-01"}
    resp = client.post("/runs", json=payload)
    run_id = resp.json()["run_id"]

    resp = client.get(f"/runs/{run_id}/status")
    assert resp.status_code == 200
    status_data = resp.json()
    assert status_data["state"] == "refused"
    assert status_data["error"] == "no usable same-track Sentinel-1 pair for this area and date"


def test_runs_unknown_status(client):
    resp = client.get("/runs/non_existent_id/status")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "Run not found"
    assert "hint" in data


def test_runs_report(client, tmp_path):
    run_dir = tmp_path / "runs" / "api_test_rep"
    run_dir.mkdir(parents=True, exist_ok=True)
    report_file = run_dir / "run_report.json"
    dummy_report = {"event_date": "2026-08-26", "areas_ha": {"water": 12.5}}
    report_file.write_text(json.dumps(dummy_report), encoding="utf-8")

    store.create_run("test_rep", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(run_dir))

    resp = client.get("/runs/test_rep/report")
    assert resp.status_code == 200
    assert resp.json() == dummy_report


def test_runs_layers_mapping(client, tmp_path):
    run_dir = tmp_path / "runs" / "api_test_layers"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_run("test_layers", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(run_dir))

    for layer_name, filename in LAYER_FILE_MAP.items():
        layer_file = run_dir / filename
        layer_content = {"type": "FeatureCollection", "layer": layer_name, "features": []}
        layer_file.write_text(json.dumps(layer_content), encoding="utf-8")

        resp = client.get(f"/runs/test_layers/layers/{layer_name}")
        assert resp.status_code == 200
        assert resp.json() == layer_content

    # Unknown layer
    resp = client.get("/runs/test_layers/layers/unknown_layer")
    assert resp.status_code == 404
    assert "Unknown layer" in resp.json()["error"]


def test_runs_ask(client, monkeypatch, tmp_path):
    run_dir = tmp_path / "runs" / "api_test_ask"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_run("test_ask", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(run_dir))

    expected_answer = {
        "text": "12.5 ha flooded",
        "numbers": [],
        "trace": [],
        "refused": False,
        "reason": None,
        "lang": "en",
    }
    monkeypatch.setattr(
        "api.main.copilot_answer", lambda rd, q, lang, use_llm=True: expected_answer
    )

    resp = client.post("/runs/test_ask/ask", json={"question": "how much is flooded?", "lang": "en"})
    assert resp.status_code == 200
    assert resp.json() == expected_answer


def test_runs_ask_offline_skips_the_language_model(client, monkeypatch, tmp_path):
    """offline=True must reach the copilot as use_llm=False (CLAUDE.md: the
    dashboard falls back to the offline templates when the model-backed call
    times out, and that fallback must never touch the network)."""
    run_dir = tmp_path / "runs" / "api_test_ask_offline"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_run("test_ask_offline", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(run_dir))

    seen_use_llm = []

    def fake_answer(rd, q, lang, use_llm=True):
        seen_use_llm.append(use_llm)
        return {
            "text": "12.5 ha flooded",
            "numbers": [],
            "trace": [],
            "refused": False,
            "reason": None,
            "lang": "en",
        }

    monkeypatch.setattr("api.main.copilot_answer", fake_answer)

    resp = client.post(
        "/runs/test_ask_offline/ask",
        json={"question": "how much is flooded?", "lang": "en", "offline": True},
    )
    assert resp.status_code == 200
    assert seen_use_llm == [False]


def test_runs_sitrep_reuse_and_build(client, monkeypatch, tmp_path):
    run_dir = tmp_path / "runs" / "api_test_sitrep"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_run("test_sitrep", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(run_dir))

    pdf_file = run_dir / "sitrep_en.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 existing pdf content")

    # Reuses existing
    resp = client.get("/runs/test_sitrep/sitrep?lang=en")
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4 existing pdf content"

    # Build when absent
    def mock_build(rd, lang, out):
        Path(out).write_bytes(b"%PDF-1.4 newly built pdf")
        return Path(out), MagicMock()

    monkeypatch.setattr("api.main.copilot_sitrep.build", mock_build)

    resp2 = client.get("/runs/test_sitrep/sitrep?lang=ne")
    assert resp2.status_code == 200
    assert resp2.content == b"%PDF-1.4 newly built pdf"


def test_startup_case_smoke_registered(client):
    run_item = store.get_run("case_smoke")
    assert run_item is not None
    assert run_item["run_id"] == "case_smoke"
    assert run_item["state"] == "done"
