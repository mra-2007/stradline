from datetime import date, datetime, timedelta, timezone

import numpy as np
from rasterio.crs import CRS
from rasterio.transform import from_origin
from shapely.geometry import box

from strandline import catalog, terrain
from strandline.config import RunParams
from validation.compare import flood_metrics

AOI = box(85.10, 27.85, 85.45, 28.30)
EVENT = date(2026, 8, 26)


def _scene(day: date, rel: int, direction: str, hour: int, cover=AOI):
    start = datetime(day.year, day.month, day.day, hour, 15, tzinfo=timezone.utc)
    return catalog.Scene(name=f"S1A_IW_GRDH_1SDV_{day:%Y%m%d}T{hour:02d}1500_x", start=start,
                         stop=start + timedelta(seconds=25), platform="S1A", relative_orbit=rel,
                         direction=direction, footprint=cover.buffer(0.2))


def test_choose_track_prefers_earliest_post_with_enough_pre():
    scenes = []
    for k in range(1, 6):  # track 85 ascending: pre every 12 days, post +2 d
        scenes.append(_scene(EVENT - timedelta(days=12 * k - 2), 85, "ASCENDING", 12))
    scenes.append(_scene(EVENT + timedelta(days=2), 85, "ASCENDING", 12))
    for k in range(1, 6):  # track 19 descending: post +5 d
        scenes.append(_scene(EVENT - timedelta(days=12 * k - 5), 19, "DESCENDING", 0))
    scenes.append(_scene(EVENT + timedelta(days=5), 19, "DESCENDING", 0))

    acqs = catalog.group_acquisitions(scenes, AOI)
    choice, log = catalog.choose_track(acqs, EVENT, RunParams())
    assert choice is not None, log
    assert choice.track[1] == 85 and choice.latency_days == 2
    # Five pre-event acquisitions are available on the chosen track; the stack
    # is capped at the configured depth and takes the LATEST of them, which is
    # what `catalog.choose_track` slices with [-n_pre:]. Asserted against the
    # config rather than a literal, so changing the shipped depth does not
    # require editing this test to keep saying the same thing.
    rp = RunParams()
    assert len(choice.pre) == rp.n_pre
    assert len(choice.pre) < 5, "the fixture must offer more pre images than the cap"
    assert choice.pre == sorted(choice.pre, key=lambda a: a.day)
    assert choice.pre[-1].day == EVENT - timedelta(days=10)  # the most recent pre image
    assert all(a.track == choice.track for a in choice.pre)  # never mixes tracks


def test_choose_track_refuses_without_enough_pre_images():
    scenes = [_scene(EVENT - timedelta(days=10), 85, "ASCENDING", 12),
              _scene(EVENT + timedelta(days=2), 85, "ASCENDING", 12)]
    choice, log = catalog.choose_track(catalog.group_acquisitions(scenes, AOI), EVENT, RunParams())
    assert choice is None
    assert any("pre-event" in line for line in log)


def test_partial_coverage_track_skipped():
    small = box(85.10, 27.85, 85.20, 27.95)
    scenes = [_scene(EVENT - timedelta(days=12 * k), 85, "ASCENDING", 12, cover=small) for k in range(1, 6)]
    scenes.append(_scene(EVENT + timedelta(days=2), 85, "ASCENDING", 12, cover=small))
    for s in scenes:
        object.__setattr__(s, "footprint", small)
    choice, _ = catalog.choose_track(catalog.group_acquisitions(scenes, AOI), EVENT, RunParams())
    assert choice is None


def test_relative_orbit_from_s1a_name():
    # absolute orbit 73 -> relative orbit 1 (S1A convention)
    assert catalog._relative_orbit_from_name("S1A_IW_GRDH_1SDV_a_b_000073_x_y") == 1
    assert catalog._relative_orbit_from_name("S1C_IW_GRDH_1SDV_a_b_000073_x_y") is None


def test_hand_zero_on_channel_and_rises_up_walls():
    h, w, res = 120, 120, 20.0
    rows, cols = np.mgrid[0:h, 0:w]
    d = np.abs(cols - 60)
    dem = 900.0 - 0.6 * rows + np.where(d <= 8, 0.3 * d, 2.4 + 6.0 * (d - 8))
    t = terrain.compute_terrain(dem, from_origin(500000, 3100000, res, res), CRS.from_epsg(32645), 0.5)
    # channel starts once upstream area exceeds the threshold (~11 rows here)
    assert t.streams[:, 55:66].any(axis=1)[20:-5].all()
    assert np.nanmedian(t.hand_m[20:100, 55:66]) < 3.0              # floor ~ 0 m
    assert np.nanmedian(t.hand_m[20:100, 100:110]) > 150.0          # high on the wall


def test_metrics_exact_and_tolerant():
    ref = np.zeros((20, 20), bool)
    ref[5:15, 5:10] = True
    pred = np.zeros_like(ref)
    pred[5:15, 6:11] = True  # shifted one pixel right
    m = flood_metrics(pred, ref, np.ones_like(ref), tolerance_px=1)
    assert m["tp"] == 40 and m["fp"] == 10 and m["fn"] == 10
    assert m["f1"] == 0.8
    assert m["f1_tolerant"] == 1.0
