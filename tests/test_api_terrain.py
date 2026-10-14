"""Tests for the dashboard's extra API surface: terrain tiles, the radar
quicklook and the run listing the run switcher reads."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from PIL import Image
from rasterio.transform import from_origin

from api import store, terrain
from api.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test_runs.db"
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", test_db)
    store.init_db(test_db)
    with TestClient(app) as test_client:
        yield test_client


def _write_dem(path: Path, value: float = 1234.0, nodata_corner: bool = False) -> Path:
    """A tiny DEM over a known lon/lat box, constant elevation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.full((32, 32), value, dtype="float32")
    if nodata_corner:
        data[0, 0] = np.nan
    transform = from_origin(85.10, 28.30, 0.01, 0.01)
    with rasterio.open(path, "w", driver="GTiff", height=32, width=32, count=1,
                       dtype="float32", crs="EPSG:4326", transform=transform,
                       nodata=float("nan")) as dst:
        dst.write(data, 1)
    return path


def _decode_terrarium(png_bytes: bytes) -> np.ndarray:
    rgb = np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB")).astype("float64")
    return rgb[:, :, 0] * 256.0 + rgb[:, :, 1] + rgb[:, :, 2] / 256.0 - 32768.0


# --- the encoding ------------------------------------------------------------


def test_encode_terrarium_round_trips_known_elevations():
    elevations = np.array([[0.0, 1.0, -10.0, 8848.0, 1234.5]], dtype="float32")
    rgb = terrain.encode_terrarium(elevations)
    decoded = rgb[:, :, 0].astype("float64") * 256.0 + rgb[:, :, 1] + rgb[:, :, 2] / 256.0 - 32768.0
    assert np.allclose(decoded, elevations, atol=1.0 / 256.0)


def test_encode_terrarium_sea_level_is_128_0_0():
    rgb = terrain.encode_terrarium(np.zeros((2, 2), dtype="float32"))
    assert rgb.tolist() == [[[128, 0, 0], [128, 0, 0]], [[128, 0, 0], [128, 0, 0]]]


def test_encode_terrarium_treats_nodata_as_flat():
    rgb = terrain.encode_terrarium(np.array([[np.nan, np.inf]], dtype="float32"))
    assert rgb.tolist() == [[[128, 0, 0], [128, 0, 0]]]


def test_flat_tile_png_is_a_256px_tile_at_the_elevation_asked_for():
    decoded = _decode_terrarium(terrain.flat_tile_png())
    assert decoded.shape == (256, 256)
    assert np.allclose(decoded, 0.0)
    assert np.allclose(_decode_terrarium(terrain.flat_tile_png(1200.0)), 1200.0, atol=0.01)


def test_terrarium_tile_png_reads_the_dem(tmp_path):
    dem = _write_dem(tmp_path / "cache" / "dem_20m.tif", value=1234.0)
    # z=11 tile containing roughly 85.27 E, 28.05 N (inside the DEM box).
    png = terrain.terrarium_tile_png(dem, 11, 1509, 857)
    decoded = _decode_terrarium(png)
    assert decoded.shape == (256, 256)
    # A constant DEM: inside and outside sit at the same elevation, so the
    # tile is that elevation everywhere and there is no cliff at the edge.
    assert np.allclose(decoded, 1234.0, atol=0.01)


def test_terrarium_tile_outside_the_dem_is_flat_at_the_dem_base(tmp_path):
    dem = _write_dem(tmp_path / "cache" / "dem_20m.tif", value=980.0)
    png = terrain.terrarium_tile_png(dem, 2, 0, 0)  # north Atlantic, far away
    # Flat, and at the DEM's own lowest elevation rather than at sea level:
    # the surround must meet the mapped area flush.
    assert np.allclose(_decode_terrarium(png), 980.0, atol=0.01)


def test_base_elevation_is_the_dems_minimum(tmp_path):
    dem = _write_dem(tmp_path / "cache" / "dem_20m.tif", value=2500.0, nodata_corner=True)
    assert abs(terrain.base_elevation(dem) - 2500.0) < 0.01


def test_find_dem_prefers_the_cached_20m_dem(tmp_path):
    assert terrain.find_dem(tmp_path) is None
    dem = _write_dem(tmp_path / "cache" / "dem_20m.tif")
    assert terrain.find_dem(tmp_path) == dem


# --- the endpoints -----------------------------------------------------------


def test_terrain_endpoint_serves_a_png(client, tmp_path):
    run_dir = tmp_path / "runs" / "terrain_run"
    _write_dem(run_dir / "cache" / "dem_20m.tif", value=2000.0)
    store.create_run("terrain_run", [85.10, 27.85, 85.45, 28.30], "2026-08-26", str(run_dir))

    resp = client.get("/runs/terrain_run/terrain/11/1509/857.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["x-attribution"] == "Copernicus DEM"
    decoded = _decode_terrarium(resp.content)
    assert np.any(np.abs(decoded - 2000.0) < 0.01)


def test_terrain_endpoint_without_a_dem_is_a_404_with_a_hint(client, tmp_path):
    run_dir = tmp_path / "runs" / "no_dem_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_run("no_dem_run", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(run_dir))

    resp = client.get("/runs/no_dem_run/terrain/11/1509/857.png")
    assert resp.status_code == 404
    body = resp.json()
    assert "DEM" in body["error"]
    assert body["hint"]


def test_terrain_endpoint_unknown_run_is_a_404(client):
    resp = client.get("/runs/nope/terrain/11/1509/857.png")
    assert resp.status_code == 404
    assert resp.json()["error"] == "Run not found"


def test_quicklook_endpoint_serves_the_png(client, tmp_path):
    run_dir = tmp_path / "runs" / "ql_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "quicklook.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    store.create_run("ql_run", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(run_dir))

    resp = client.get("/runs/ql_run/quicklook")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"\x89PNG\r\n\x1a\nfake"


def test_quicklook_missing_is_a_404_json(client, tmp_path):
    run_dir = tmp_path / "runs" / "ql_missing"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_run("ql_missing", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(run_dir))

    resp = client.get("/runs/ql_missing/quicklook")
    assert resp.status_code == 404
    body = resp.json()
    assert "quicklook.png" in body["error"]
    assert body["hint"]


# --- the run listing ---------------------------------------------------------


def test_list_runs_includes_case_smoke_and_created_runs(client, tmp_path):
    run_dir = tmp_path / "runs" / "listed_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_run("listed_run", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(run_dir))

    resp = client.get("/runs")
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    ids = [r["run_id"] for r in runs]
    assert "listed_run" in ids
    assert "case_smoke" in ids
    entry = next(r for r in runs if r["run_id"] == "listed_run")
    assert entry["event_date"] == "2026-08-26"
    assert entry["bbox"] == [85.1, 27.8, 85.4, 28.3]
    assert entry["state"] == "queued"


def test_discover_runs_registers_directories_with_a_report(tmp_path):
    db = tmp_path / "discover.db"
    store.init_db(db)
    base = tmp_path / "base"
    nested = base / "runs" / "eval" / "some_event"
    nested.mkdir(parents=True)
    (nested / "run_report.json").write_text(
        json.dumps({"event_date": "2023-08-09", "bbox_wgs84": [10.0, 61.0, 10.3, 61.3]}),
        encoding="utf-8")
    flat = base / "runs" / "another_event"
    flat.mkdir(parents=True)
    (flat / "run_report.json").write_text(
        json.dumps({"event_date": "2024-05-01", "bbox_wgs84": [89.8, 27.4, 90.1, 27.7]}),
        encoding="utf-8")
    # A directory with no report is not a run.
    (base / "runs" / "empty_dir").mkdir(parents=True)

    added = store.discover_runs(base_dir=base, db_path=db)
    assert sorted(added) == ["another_event", "some_event"]
    assert "empty_dir" not in added

    item = store.get_run("some_event", db_path=db)
    assert item["state"] == "done"
    assert item["event_date"] == "2023-08-09"
    assert item["bbox"] == [10.0, 61.0, 10.3, 61.3]

    # Idempotent: a second pass adds nothing and changes nothing.
    assert store.discover_runs(base_dir=base, db_path=db) == []


def test_prune_missing_runs_forgets_rows_with_no_directory(tmp_path):
    db = tmp_path / "prune.db"
    store.init_db(db)
    alive = tmp_path / "alive"
    alive.mkdir()
    store.create_run("alive_run", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(alive), db_path=db)
    store.create_run(
        "gone_run", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(tmp_path / "gone"), db_path=db
    )

    assert store.prune_missing_runs(db_path=db) == ["gone_run"]
    assert store.get_run("gone_run", db_path=db) is None
    assert store.get_run("alive_run", db_path=db) is not None
    # Nothing left to prune.
    assert store.prune_missing_runs(db_path=db) == []


def test_discover_runs_leaves_existing_rows_alone(tmp_path):
    db = tmp_path / "discover2.db"
    store.init_db(db)
    base = tmp_path / "base2"
    run_dir = base / "runs" / "live_run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_report.json").write_text(
        json.dumps({"event_date": "2026-08-26", "bbox_wgs84": [85.1, 27.8, 85.4, 28.3]}),
        encoding="utf-8")
    store.create_run("live_run", [85.1, 27.8, 85.4, 28.3], "2026-08-26", str(run_dir), db_path=db)
    store.update_run("live_run", state="running", db_path=db)

    assert store.discover_runs(base_dir=base, db_path=db) == []
    assert store.get_run("live_run", db_path=db)["state"] == "running"
