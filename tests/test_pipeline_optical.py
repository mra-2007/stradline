"""End-to-end runs with Sentinel-2, network mocked.

SYNTHETIC: the radar stack, the DEM and both optical dates are generated, so
nothing here is a real-world accuracy figure (Rule 3.8). What is established is
the behaviour a live judging run depends on:

  - a clear optical pair is fused, written and reported, with per-pixel sensor
    provenance;
  - optical failure of ANY kind - no scene, too much cloud, a dead backend -
    costs the run nothing but the optical half, and the reason is recorded;
  - `--no-optical` produces exactly the radar-only configuration, hash included.
"""

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import rasterio
from dataclasses import replace
from shapely.geometry import box

from strandline import catalog, ingest, optical, run
from strandline.config import (
    CLASS_FLOOD_HIGH,
    CLASS_FLOOD_MEDIUM,
    SOURCE_BOTH,
    SOURCE_NONE,
    SOURCE_OPTICAL,
    SOURCE_SAR,
    FUSION_UNION,
    DetectParams,
    OpticalParams,
    RunParams,
    S2_REFLECTANCE_BANDS,
)
from tests.test_pipeline_offline import BBOX, EVENT, _fake_scenes, _pattern

# Reflectance, in S2_REFLECTANCE_BANDS order (B03, B04, B08, B11, B12).
VEGETATION = (0.08, 0.10, 0.35, 0.25, 0.18)
WATER = (0.07, 0.05, 0.04, 0.02, 0.01)

S2_PRE = EVENT - timedelta(days=9)
S2_POST = EVENT + timedelta(days=5)


def _s2_scene(day):
    start = datetime(day.year, day.month, day.day, 4, 50, tzinfo=timezone.utc)
    return catalog.S2Scene(
        name=f"S2A_MSIL2A_{day:%Y%m%d}T045021_N0510_R019_T45RUK_fake.SAFE",
        start=start, cloud_cover=3.0, footprint=box(*BBOX).buffer(0.5))


def _write(path, bands, dtype="float32"):
    data = np.stack(bands).astype(dtype)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=data.shape[1], width=data.shape[2],
                       count=data.shape[0], dtype=dtype, crs=_GRID.crs,
                       transform=_GRID.transform) as dst:
        dst.write(data)


_GRID = ingest.make_grid(BBOX, 20.0)


def _optical_zone(grid):
    """Valley floor beside the river, over the UPSTREAM quarter and the middle.

    The upstream part is water that the radar fixture never floods, so it can
    only come from optical; the middle overlaps the radar flood, so it is where
    the two sensors agree.
    """
    h, w = grid.height, grid.width
    cols = np.arange(w)[None, :].repeat(h, 0)
    rows = np.arange(h)[:, None].repeat(w, 1)
    d = np.abs(cols - w // 2)
    return (d <= 15) & (rows < 3 * h // 4)


def _fake_s2_download(scl_code=4, flooded=True):
    """A download stub that writes DN rasters, as the real backend does."""

    def download(conn, day, grid, paths, op):
        h, w = grid.height, grid.width
        refl = [np.full((h, w), value) for value in VEGETATION]
        if flooded and day == S2_POST:
            zone = _optical_zone(grid)
            for i, value in enumerate(WATER):
                refl[i][zone] = value
        # Digital numbers with the baseline-04 offset, exactly as L2A ships.
        _write(paths["refl"], [band * 10_000.0 + 1_000.0 for band in refl])
        _write(paths["scl"], [np.full((h, w), float(scl_code))])
        return paths

    return download


def _radar_fakes(monkeypatch, rng):
    post_day = EVENT + timedelta(days=4)

    def fake_s1(conn, acq, grid_, path, coefficient, with_mask,
                elevation_model="COPERNICUS_30"):
        _write(path, list(_pattern(grid_, acq.day == post_day, rng)))
        return path

    def fake_dem(conn, grid_, path):
        h, w = grid_.height, grid_.width
        d = np.abs(np.arange(w)[None, :] - w // 2).repeat(h, 0)
        dem = 700.0 - 0.5 * np.arange(h)[:, None] + np.where(d <= 20, 0.0, 7.0 * (d - 20))
        _write(path, [dem])
        return path

    monkeypatch.setattr(catalog, "search_s1_grd", _fake_scenes)
    monkeypatch.setattr(ingest, "connect", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "download_s1", fake_s1)
    monkeypatch.setattr(ingest, "download_dem", fake_dem)
    monkeypatch.setattr(catalog, "search_s2_l2a",
                        lambda *a, **k: [_s2_scene(S2_PRE), _s2_scene(S2_POST)])


RP = RunParams(stream_threshold_km2=0.5)

# The SHIPPED default is fusion `off` (optical computed and reported, but
# changing no pixel) until the leave-one-event-out optical pass has measured it,
# so a test of the fusion machinery must ask for `union` explicitly.
OP_UNION = replace(OpticalParams(), fusion=FUSION_UNION)


# --- the clear-sky path --------------------------------------------------------

def test_clear_optical_pair_is_fused_written_and_reported(tmp_path, monkeypatch):
    _radar_fakes(monkeypatch, np.random.default_rng(1))
    monkeypatch.setattr(optical, "download_s2_date", _fake_s2_download())

    assert run.run(BBOX, EVENT, tmp_path, RP, DetectParams(), dry_run=False,
                   op=OP_UNION) == 0
    report = json.loads((tmp_path / "run_report.json").read_text())
    opt = report["optical"]

    assert opt["status"] == "used"
    assert opt["fused_into_map"] is True
    assert opt["pair"]["pre"]["day"] == str(S2_PRE)
    assert opt["pair"]["post"]["day"] == str(S2_POST)
    assert opt["evidence"]["clear_fraction_pair"] == 1.0
    assert opt["evidence"]["optical_water_candidate_pixels"] > 1000
    # The reflectance convention that was actually applied is recorded, so it
    # can be checked against a real scene rather than assumed.
    conversion = opt["evidence"]["reflectance_conversion"]["post"]
    assert conversion["method"] == "dn_baseline"
    assert conversion["offset"] == -1000.0
    assert conversion["processing_baseline"] == 510
    assert conversion["implausible_fraction"] == 0.0

    assert (tmp_path / "flood_source.tif").exists()
    with rasterio.open(tmp_path / "flood_source.tif") as src:
        source = src.read(1)
    assert set(np.unique(source)) <= {SOURCE_NONE, SOURCE_SAR, SOURCE_OPTICAL, SOURCE_BOTH}
    assert (source == SOURCE_OPTICAL).sum() > 100, "optical-only reach not credited"
    assert (source == SOURCE_BOTH).sum() > 100, "agreement not credited"

    # The report's hectare table is built from the same layer.
    both_name = "Sentinel-1 and Sentinel-2 agree"
    assert opt["source_ha"][both_name] > 0
    assert opt["source_pixels"][both_name] == int((source == SOURCE_BOTH).sum())


def test_fusion_adds_the_reach_radar_missed(tmp_path, monkeypatch):
    """The same radar scene, with and without optical: optical only ever adds."""
    rng_seed = 7
    _radar_fakes(monkeypatch, np.random.default_rng(rng_seed))
    monkeypatch.setattr(optical, "download_s2_date", _fake_s2_download())
    assert run.run(BBOX, EVENT, tmp_path / "fused", RP, DetectParams(), dry_run=False,
                   op=OP_UNION) == 0

    _radar_fakes(monkeypatch, np.random.default_rng(rng_seed))
    assert run.run(BBOX, EVENT, tmp_path / "radar", RP, DetectParams(), dry_run=False,
                   op=replace(OpticalParams(), enabled=False)) == 0

    def affected(where):
        with rasterio.open(tmp_path / where / "flood_classes.tif") as src:
            classes = src.read(1)
        return int(np.isin(classes, [CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM]).sum())

    assert affected("fused") > affected("radar")


def test_the_shipped_default_reports_optical_but_changes_no_pixel(tmp_path, monkeypatch):
    """The default is fusion `off`: Sentinel-2 is chosen, downloaded, masked,
    written and reported, and the class map is the measured radar-only map.

    This is the configuration a judge gets by running the CLI with no flags, so
    it is the one that must match the numbers in the README."""
    _radar_fakes(monkeypatch, np.random.default_rng(3))
    monkeypatch.setattr(optical, "download_s2_date", _fake_s2_download())
    assert run.run(BBOX, EVENT, tmp_path / "default", RP, DetectParams(), dry_run=False) == 0

    _radar_fakes(monkeypatch, np.random.default_rng(3))
    assert run.run(BBOX, EVENT, tmp_path / "radar", RP, DetectParams(), dry_run=False,
                   op=replace(OpticalParams(), enabled=False)) == 0

    def classes(where):
        with rasterio.open(tmp_path / where / "flood_classes.tif") as src:
            return src.read(1)

    # Optical was really used as EVIDENCE ...
    opt = json.loads((tmp_path / "default" / "run_report.json").read_text())["optical"]
    assert opt["status"] == "used"
    assert opt["fusion"] == "off"
    assert opt["evidence"]["optical_water_candidate_pixels"] > 1000
    # ... and changed not one pixel of the map.
    assert opt["fused_into_map"] is False
    assert np.array_equal(classes("default"), classes("radar"))
    with rasterio.open(tmp_path / "default" / "flood_source.tif") as src:
        assert set(np.unique(src.read(1))) <= {SOURCE_NONE, SOURCE_SAR}


# --- every failure is a report, not a lost run --------------------------------

def test_a_broken_optical_download_does_not_cost_the_radar_map(tmp_path, monkeypatch):
    _radar_fakes(monkeypatch, np.random.default_rng(1))

    def boom(*a, **k):
        raise RuntimeError("openEO batch job failed: backend unavailable")

    monkeypatch.setattr(optical, "download_s2_date", boom)
    assert run.run(BBOX, EVENT, tmp_path, RP, DetectParams(), dry_run=False) == 0

    for name in ("flood_classes.tif", "zscore_combined.tif", "hand_m.tif",
                 "flood_polygons.geojson", "reliability_narrow_valley.tif",
                 "quicklook.png", "run_report.json"):
        assert (tmp_path / name).exists(), name
    opt = json.loads((tmp_path / "run_report.json").read_text())["optical"]
    assert opt["status"] == "failed"
    assert opt["error_type"] == "RuntimeError"
    assert "backend unavailable" in opt["message"]
    assert opt["fused_into_map"] is False


def test_a_dead_optical_catalogue_does_not_cost_the_radar_map(tmp_path, monkeypatch):
    _radar_fakes(monkeypatch, np.random.default_rng(1))

    def boom(*a, **k):
        raise OSError("name resolution failed")

    monkeypatch.setattr(catalog, "search_s2_l2a", boom)
    assert run.run(BBOX, EVENT, tmp_path, RP, DetectParams(), dry_run=False) == 0
    opt = json.loads((tmp_path / "run_report.json").read_text())["optical"]
    assert opt["status"] == "failed" and opt["error_type"] == "OSError"


def test_full_cloud_over_the_area_is_refused_with_its_reason(tmp_path, monkeypatch):
    """The catalogue said the tile was clear; the pixels say otherwise."""
    _radar_fakes(monkeypatch, np.random.default_rng(1))
    monkeypatch.setattr(optical, "download_s2_date", _fake_s2_download(scl_code=9))

    assert run.run(BBOX, EVENT, tmp_path, RP, DetectParams(), dry_run=False) == 0
    opt = json.loads((tmp_path / "run_report.json").read_text())["optical"]
    assert opt["status"] == "refused"
    assert "clear on BOTH" in opt["message"]
    assert opt["fused_into_map"] is False


def test_an_implausible_reflectance_convention_is_refused(tmp_path, monkeypatch):
    """Values that cannot be reflectance under any convention stop the stage."""
    _radar_fakes(monkeypatch, np.random.default_rng(1))

    def bad_download(conn, day, grid, paths, op):
        h, w = grid.height, grid.width
        _write(paths["refl"], [np.full((h, w), 90_000.0) for _ in S2_REFLECTANCE_BANDS])
        _write(paths["scl"], [np.full((h, w), 4.0)])
        return paths

    monkeypatch.setattr(optical, "download_s2_date", bad_download)
    assert run.run(BBOX, EVENT, tmp_path, RP, DetectParams(), dry_run=False) == 0
    opt = json.loads((tmp_path / "run_report.json").read_text())["optical"]
    assert opt["status"] == "refused"
    assert "not plausible" in opt["message"]


# --- radar only ----------------------------------------------------------------

def test_no_optical_is_exactly_the_radar_only_configuration(tmp_path, monkeypatch):
    _radar_fakes(monkeypatch, np.random.default_rng(1))

    def never(*a, **k):
        raise AssertionError("--no-optical must not touch Sentinel-2 at all")

    monkeypatch.setattr(catalog, "search_s2_l2a", never)
    monkeypatch.setattr(optical, "download_s2_date", never)

    op = replace(OpticalParams(), enabled=False)
    assert run.run(BBOX, EVENT, tmp_path, RP, DetectParams(), dry_run=False, op=op) == 0
    report = json.loads((tmp_path / "run_report.json").read_text())
    assert report["optical"]["status"] == "disabled"
    assert "optical" not in report["parameters"]
    assert report["parameters_hash"] == run.params_hash(DetectParams(), RP)


def test_a_warm_optical_cache_needs_no_login(tmp_path, monkeypatch):
    """Every dev-set score and every rerun reads the cache: it must not log in."""
    _radar_fakes(monkeypatch, np.random.default_rng(1))
    monkeypatch.setattr(optical, "download_s2_date", _fake_s2_download())
    assert run.run(BBOX, EVENT, tmp_path, RP, DetectParams(), dry_run=False) == 0

    # Second run over the same cache, with login made fatal.
    def no_login(*a, **k):
        raise AssertionError("a warm cache must not open a login")

    _radar_fakes(monkeypatch, np.random.default_rng(1))
    monkeypatch.setattr(ingest, "connect", no_login)

    def download_only_if_missing(conn, day, grid, paths, op):
        assert paths["refl"].exists() and paths["scl"].exists(), "cache miss on a warm cache"
        return paths

    monkeypatch.setattr(optical, "download_s2_date", download_only_if_missing)
    assert run.run(BBOX, EVENT, tmp_path, RP, DetectParams(), dry_run=False) == 0
    opt = json.loads((tmp_path / "run_report.json").read_text())["optical"]
    assert opt["status"] == "used"
