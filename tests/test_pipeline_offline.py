"""End-to-end run with the network mocked: catalogue search and openEO
downloads are replaced by synthetic GeoTIFFs on the real analysis grid.
Checks that every output is produced and consistent."""

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import rasterio
from shapely.geometry import box

from strandline import catalog, ingest, run
from strandline.config import CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM, DetectParams, RunParams

BBOX = (85.30, 27.90, 85.36, 27.96)
EVENT = date(2026, 8, 26)


def _fake_scenes(*_args, **_kwargs):
    out = []
    days = [EVENT - timedelta(days=12 * k - 4) for k in range(5, 0, -1)] + [EVENT + timedelta(days=4)]
    for d in days:
        start = datetime(d.year, d.month, d.day, 12, 15, tzinfo=timezone.utc)
        out.append(catalog.Scene(name=f"S1A_IW_GRDH_1SDV_{d:%Y%m%d}_fake", start=start,
                                 stop=start + timedelta(seconds=25), platform="S1A",
                                 relative_orbit=85, direction="ASCENDING",
                                 footprint=box(*BBOX).buffer(0.5)))
    return out


def _pattern(grid, flood: bool, rng):
    h, w = grid.height, grid.width
    cols = np.arange(w)[None, :].repeat(h, 0)
    rows = np.arange(h)[:, None].repeat(w, 1)
    d = np.abs(cols - w // 2)
    vv, vh = np.full((h, w), -9.0), np.full((h, w), -16.0)
    vv[d <= 3], vh[d <= 3] = -21.0, -28.0
    if flood:
        zone = (d > 3) & (d <= 15) & (rows > h // 4) & (rows < 3 * h // 4)
        vv[zone], vh[zone] = -19.0, -26.0
    lin = [10 ** (b / 10) * rng.gamma(4.4, 1 / 4.4, size=b.shape) for b in (vv, vh)]
    return np.stack(lin).astype(np.float32)


def test_full_run_offline(tmp_path, monkeypatch):
    rng = np.random.default_rng(1)
    grid = ingest.make_grid(BBOX, 20.0)
    post_day = EVENT + timedelta(days=4)

    def fake_s1(conn, acq, grid_, path, coefficient, with_mask,
                elevation_model="COPERNICUS_30"):
        assert elevation_model == "COPERNICUS_30", "SRTM would fail above 60N"
        path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(path, "w", driver="GTiff", height=grid_.height, width=grid_.width,
                           count=2, dtype="float32", crs=grid_.crs, transform=grid_.transform) as dst:
            dst.write(_pattern(grid_, acq.day == post_day, rng))
        return path

    def fake_dem(conn, grid_, path):
        h, w = grid_.height, grid_.width
        d = np.abs(np.arange(w)[None, :] - w // 2).repeat(h, 0)
        dem = 700.0 - 0.5 * np.arange(h)[:, None] + np.where(d <= 20, 0.0, 7.0 * (d - 20))
        with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                           crs=grid_.crs, transform=grid_.transform) as dst:
            dst.write(dem.astype(np.float32)[None])
        return path

    monkeypatch.setattr(catalog, "search_s1_grd", _fake_scenes)
    monkeypatch.setattr(ingest, "connect", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "download_s1", fake_s1)
    monkeypatch.setattr(ingest, "download_dem", fake_dem)

    rp = RunParams(stream_threshold_km2=0.5)
    assert run.run(BBOX, EVENT, tmp_path, rp, DetectParams(), dry_run=False) == 0

    for name in ("flood_classes.tif", "zscore_combined.tif", "hand_m.tif",
                 "flood_polygons.geojson", "quicklook.png", "run_report.json"):
        assert (tmp_path / name).exists(), name

    report = json.loads((tmp_path / "run_report.json").read_text())
    assert report["track"]["relative_orbit"] == 85
    assert len(report["track"]["pre"]) == 5
    assert report["attribution"][0].startswith("Contains modified Copernicus Sentinel data")

    with rasterio.open(tmp_path / "flood_classes.tif") as src:
        classes = src.read(1)
        assert src.crs.to_epsg() == grid.epsg
    assert np.isin(classes, [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM]).sum() > 1000

    gj = json.loads((tmp_path / "flood_polygons.geojson").read_text())
    assert gj["features"], "no flood polygons written"
    lon, lat = gj["features"][0]["geometry"]["coordinates"][0][0]
    assert BBOX[0] - 0.01 < lon < BBOX[2] + 0.01 and BBOX[1] - 0.01 < lat < BBOX[3] + 0.01


def test_dry_run_needs_no_login(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "search_s1_grd", _fake_scenes)

    def boom(*a, **k):
        raise AssertionError("dry run must not log in")

    monkeypatch.setattr(ingest, "connect", boom)
    assert run.run(BBOX, EVENT, tmp_path, RunParams(), DetectParams(), dry_run=True) == 0


def test_refuses_when_no_usable_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "search_s1_grd", lambda *a, **k: _fake_scenes()[-2:])
    assert run.run(BBOX, EVENT, tmp_path, RunParams(), DetectParams(), dry_run=True) == 2
