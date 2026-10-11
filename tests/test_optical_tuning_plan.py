"""The declared optical parameter grid, and the dev-set download plan for it.

These tests exist so the grid declared in strandline/config.py BEFORE any
optical score existed cannot drift from the fields it claims to set, and so the
radar search can never silently acquire an optical axis.
"""

import pytest
from datetime import date, timedelta

from eval import fetch_dev_data as fetch
from eval import tune
from strandline import catalog, optical
from strandline.config import (
    FUSION_OFF,
    FUSION_UNION,
    OPTICAL_FUSION_MODES,
    OPTICAL_TUNE_GRIDS,
    OpticalParams,
)


# --- the declared grid ---------------------------------------------------------

def test_the_declared_optical_grid_is_valid():
    assert tune.validate_optical_grids() is OPTICAL_TUNE_GRIDS


def test_every_fusion_mode_is_in_the_grid_including_off():
    """`off` must be a candidate: the comparison has to be able to say no."""
    assert set(OPTICAL_TUNE_GRIDS) == set(OPTICAL_FUSION_MODES)
    points = tune.optical_grid_points()
    modes = {p["optical.fusion"] for p in points}
    assert modes == set(OPTICAL_FUSION_MODES)


def test_fusion_off_is_a_single_point():
    """With fusion off an index threshold cannot change a pixel, so one point."""
    points = tune.optical_grid_points()
    off = [p for p in points if p["optical.fusion"] == FUSION_OFF]
    assert len(off) == 1
    assert off[0] == {"optical.fusion": FUSION_OFF}


def test_the_grid_is_small_and_deterministic():
    first = tune.optical_grid_points()
    assert len(first) == 5, "a declared grid must not grow without a written reason"
    assert first == tune.optical_grid_points()


def test_a_grid_axis_naming_an_unknown_field_is_rejected():
    with pytest.raises(ValueError, match="has no field"):
        tune.validate_optical_grids({FUSION_UNION: {"optical.not_a_field": (1,)}})


def test_a_grid_axis_in_an_unknown_section_is_rejected():
    with pytest.raises(ValueError, match="section must be one of"):
        tune.validate_optical_grids({FUSION_UNION: {"run.resolution_m": (20.0,)}})


def test_an_unknown_fusion_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown fusion mode"):
        tune.validate_optical_grids({"maybe": {}})


def test_the_fusion_mode_may_not_also_be_an_axis():
    with pytest.raises(ValueError, match="keys ARE the fusion modes"):
        tune.validate_optical_grids(
            {FUSION_UNION: {"optical.fusion": OPTICAL_FUSION_MODES}})


def test_an_empty_axis_is_rejected():
    with pytest.raises(ValueError, match="no values"):
        tune.validate_optical_grids({FUSION_UNION: {"optical.mndwi_water": ()}})


def test_the_radar_grid_may_never_carry_an_optical_axis():
    """The radar search runs with no Sentinel-2 cached, so it must refuse one."""
    from strandline.config import MODE_ROUGH_WATER
    with pytest.raises(ValueError, match="OPTICAL_TUNE_GRIDS"):
        tune.validate_grid({MODE_ROUGH_WATER: {"optical.mndwi_water": (0.0,)}})


def test_the_shipped_radar_grid_is_still_valid():
    tune.validate_grid()


# --- the download plan ---------------------------------------------------------

EVENT = {"name": "synthetic_event", "kind": "flood", "independent_reference": True,
         "event_date": "2024-07-10", "bbox": [11.00, 46.00, 11.08, 46.06]}
EVENT_DAY = date(2024, 7, 10)


def _s2_scene(day, cloud=2.0):
    import datetime as dt
    from shapely.geometry import box
    start = dt.datetime(day.year, day.month, day.day, 10, 20, tzinfo=dt.timezone.utc)
    return catalog.S2Scene(name=f"S2A_MSIL2A_{day:%Y%m%d}T102021_N0510_R022_T32TPS_x.SAFE",
                           start=start, cloud_cover=cloud,
                           footprint=box(*EVENT["bbox"]).buffer(0.3))


def _choice():
    """A minimal TrackChoice, so planning needs no Sentinel-1 catalogue call."""
    from shapely.geometry import box
    import datetime as dt

    def acq(day):
        start = dt.datetime(day.year, day.month, day.day, 5, 0, tzinfo=dt.timezone.utc)
        return catalog.Acquisition(track=("orbit", 15, "ASCENDING"), day=day, start=start,
                                   stop=start + dt.timedelta(seconds=25), names=["x"],
                                   footprint=box(*EVENT["bbox"]), coverage=1.0)

    pre = [acq(EVENT_DAY - timedelta(days=12 * k)) for k in (3, 2, 1)]
    return catalog.TrackChoice(track=("orbit", 15, "ASCENDING"),
                               post=acq(EVENT_DAY + timedelta(days=2)), pre=pre,
                               latency_days=2)


def test_without_optical_the_plan_is_unchanged(tmp_path, monkeypatch):
    def never(*a, **k):
        raise AssertionError("no Sentinel-2 catalogue call without --optical")

    monkeypatch.setattr(catalog, "search_s2_l2a", never)
    tasks = fetch.plan_event(EVENT, ["sigma0-ellipsoid"], tmp_path, 3, choice=_choice(),
                             log=lambda *_: None)
    assert {t.kind for t in tasks} == {"s1", "dem"}


def test_with_optical_two_dates_are_planned_as_two_files_each(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "search_s2_l2a",
                        lambda *a, **k: [_s2_scene(EVENT_DAY - timedelta(days=8)),
                                         _s2_scene(EVENT_DAY + timedelta(days=6))])
    tasks = fetch.plan_event(EVENT, ["sigma0-ellipsoid"], tmp_path, 3, choice=_choice(),
                             op=OpticalParams(), log=lambda *_: None)
    s2 = [t for t in tasks if t.kind == "s2"]
    assert len(s2) == 2
    assert [t.day for t in s2] == [EVENT_DAY - timedelta(days=8),
                                   EVENT_DAY + timedelta(days=6)]
    for task in s2:
        assert len(task.paths) == 2                       # reflectance + class layer
        assert task.paths[0].name.startswith("s2_refl_")
        assert task.paths[1].name.startswith("s2_scl_")
        assert not task.done
        assert task.expected_bytes > 0


def test_an_event_with_no_clear_pair_plans_no_optical_download(tmp_path, monkeypatch):
    """A clouded monsoon flood is scored radar-only, not refused."""
    monkeypatch.setattr(catalog, "search_s2_l2a",
                        lambda *a, **k: [_s2_scene(EVENT_DAY - timedelta(days=8), cloud=99.0),
                                         _s2_scene(EVENT_DAY + timedelta(days=6), cloud=98.0)])
    lines = []
    tasks = fetch.plan_event(EVENT, ["sigma0-ellipsoid"], tmp_path, 3, choice=_choice(),
                             op=OpticalParams(), log=lines.append)
    assert not [t for t in tasks if t.kind == "s2"]
    assert any("radar-only" in line for line in lines)


def test_an_s2_task_is_done_only_when_both_files_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "search_s2_l2a",
                        lambda *a, **k: [_s2_scene(EVENT_DAY - timedelta(days=8)),
                                         _s2_scene(EVENT_DAY + timedelta(days=6))])
    task = [t for t in fetch.plan_event(EVENT, ["sigma0-ellipsoid"], tmp_path, 3,
                                        choice=_choice(), op=OpticalParams(),
                                        log=lambda *_: None) if t.kind == "s2"][0]
    task.paths[0].parent.mkdir(parents=True, exist_ok=True)
    task.paths[0].write_bytes(b"not really a geotiff")
    assert not task.done, "a half-downloaded date must not count as cached"
    task.paths[1].write_bytes(b"nor is this")
    assert task.done


def test_cache_paths_are_shared_with_the_pipeline(tmp_path):
    """The fetcher must write exactly where the pipeline reads."""
    day = date(2024, 7, 2)
    assert (optical.s2_cache_paths(tmp_path, day, 20.0)["refl"].name
            == optical.s2_refl_cache_name(day, 20.0))
