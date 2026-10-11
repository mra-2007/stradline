"""Sentinel-2 scene choice from catalogue metadata alone.

SYNTHETIC: every product here is fabricated, so these tests prove the
selection logic, not any real-world cloud statistics. No network is touched -
which is itself part of what is tested: a hopelessly clouded area must be
rejected from metadata, before a single download is attempted.
"""

from datetime import date, datetime, timedelta, timezone

from shapely.geometry import box

from strandline import catalog
from strandline.config import OpticalParams

AOI = box(11.00, 46.00, 11.20, 46.15)
EVENT = date(2024, 7, 10)


def _scene(day: date, cloud, cover=None, tile="T32TPS"):
    start = datetime(day.year, day.month, day.day, 10, 20, tzinfo=timezone.utc)
    return catalog.S2Scene(
        name=f"S2A_MSIL2A_{day:%Y%m%d}T102021_N0510_R022_{tile}_x.SAFE",
        start=start, cloud_cover=cloud,
        footprint=(cover if cover is not None else AOI.buffer(0.3)),
    )


def _days(scenes):
    return catalog.group_s2_days(scenes, AOI)


# --- grouping -----------------------------------------------------------------

def test_day_groups_tiles_and_weights_cloud_by_overlap():
    west = box(11.00, 46.00, 11.10, 46.15)      # half the AOI, clear
    east = box(11.10, 46.00, 11.20, 46.15)      # the other half, cloudy
    days = _days([_scene(EVENT, 0.0, cover=west, tile="T32TPS"),
                  _scene(EVENT, 80.0, cover=east, tile="T32TPT")])
    assert len(days) == 1
    day = days[0]
    assert day.coverage == 1.0                   # the two tiles together cover it
    assert day.cloud_cover == 40.0               # equal overlap -> plain mean
    assert len(day.names) == 2


def test_unknown_cloud_cover_counts_as_fully_cloudy():
    days = _days([_scene(EVENT, None)])
    assert days[0].cloud_cover == 100.0


def test_partial_coverage_is_measured_not_assumed():
    half = box(11.00, 46.00, 11.10, 46.15)
    days = _days([_scene(EVENT, 0.0, cover=half)])
    assert 0.45 < days[0].coverage < 0.55


# --- pair choice --------------------------------------------------------------

def test_chooses_clearest_pre_and_post():
    scenes = [_scene(EVENT - timedelta(days=d), cloud) for d, cloud in
              ((30, 5.0), (20, 60.0), (10, 2.0))]
    scenes += [_scene(EVENT + timedelta(days=d), cloud) for d, cloud in
               ((2, 55.0), (7, 1.0), (15, 20.0))]
    pair, log = catalog.choose_s2_pair(_days(scenes), EVENT, OpticalParams())
    assert pair is not None, log
    assert pair.pre.day == EVENT - timedelta(days=10)    # 2% cloud
    assert pair.post.day == EVENT + timedelta(days=7)    # 1% cloud
    assert pair.post_latency_days == 7


def test_ties_break_toward_the_date_closest_to_the_event():
    scenes = [_scene(EVENT - timedelta(days=d), 5.0) for d in (5, 40)]
    scenes += [_scene(EVENT + timedelta(days=d), 5.0) for d in (3, 18)]
    pair, _ = catalog.choose_s2_pair(_days(scenes), EVENT, OpticalParams())
    assert pair.pre.day == EVENT - timedelta(days=5)
    assert pair.post.day == EVENT + timedelta(days=3)


def test_all_dates_too_cloudy_gives_no_pair_and_says_so():
    scenes = [_scene(EVENT - timedelta(days=10), 95.0), _scene(EVENT + timedelta(days=3), 99.0)]
    pair, log = catalog.choose_s2_pair(_days(scenes), EVENT, OpticalParams())
    assert pair is None
    assert any("cloud" in line for line in log)


def test_post_window_is_longer_than_the_radar_one():
    """Debris and mud scars persist, so a +18 d clear scene is still usable."""
    scenes = [_scene(EVENT - timedelta(days=10), 1.0), _scene(EVENT + timedelta(days=18), 1.0)]
    pair, log = catalog.choose_s2_pair(_days(scenes), EVENT, OpticalParams())
    assert pair is not None, log
    assert pair.post_latency_days == 18
    assert pair.notes and "drained" in pair.notes[0]


def test_post_date_beyond_the_window_is_rejected():
    scenes = [_scene(EVENT - timedelta(days=10), 1.0), _scene(EVENT + timedelta(days=40), 1.0)]
    pair, log = catalog.choose_s2_pair(_days(scenes), EVENT, OpticalParams())
    assert pair is None
    assert any("after the event" in line for line in log)


def test_pre_date_beyond_the_search_window_is_rejected():
    scenes = [_scene(EVENT - timedelta(days=200), 1.0), _scene(EVENT + timedelta(days=3), 1.0)]
    pair, log = catalog.choose_s2_pair(_days(scenes), EVENT, OpticalParams())
    assert pair is None
    assert any("before the event" in line for line in log)


def test_same_day_as_the_event_is_not_used_as_post():
    scenes = [_scene(EVENT - timedelta(days=10), 1.0), _scene(EVENT, 1.0)]
    pair, _ = catalog.choose_s2_pair(_days(scenes), EVENT, OpticalParams())
    assert pair is None


def test_partially_covering_dates_are_rejected():
    half = box(11.00, 46.00, 11.10, 46.15)
    scenes = [_scene(EVENT - timedelta(days=10), 0.0, cover=half),
              _scene(EVENT + timedelta(days=3), 0.0, cover=half)]
    pair, log = catalog.choose_s2_pair(_days(scenes), EVENT, OpticalParams())
    assert pair is None
    assert any("of the AOI" in line for line in log)


def test_parse_s2_product_reads_cloud_cover():
    item = {"Name": "S2B_MSIL2A_20240710T102021_N0510_R022_T32TPS_x.SAFE",
            "ContentDate": {"Start": "2024-07-10T10:20:21.000Z",
                            "End": "2024-07-10T10:20:21.000Z"},
            "GeoFootprint": {"type": "Polygon", "coordinates": [
                [[11.0, 46.0], [11.3, 46.0], [11.3, 46.3], [11.0, 46.3], [11.0, 46.0]]]},
            "Attributes": [{"Name": "cloudCover", "Value": 12.5}]}
    scene = catalog.parse_s2_product(item)
    assert scene.cloud_cover == 12.5
    assert scene.start.date() == date(2024, 7, 10)


def test_parse_s2_product_survives_a_missing_cloud_attribute():
    item = {"Name": "x.SAFE",
            "ContentDate": {"Start": "2024-07-10T10:20:21.000Z",
                            "End": "2024-07-10T10:20:21.000Z"},
            "GeoFootprint": {"type": "Polygon", "coordinates": [
                [[11.0, 46.0], [11.3, 46.0], [11.3, 46.3], [11.0, 46.3], [11.0, 46.0]]]},
            "Attributes": []}
    assert catalog.parse_s2_product(item).cloud_cover is None
