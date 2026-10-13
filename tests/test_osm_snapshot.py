"""The OSM snapshot guard: never read OSM from on or after the event.

This is a Rule 1 test, not a convenience test. After a flood, mappers trace the
damage - buildings get flood tags, washed-out bridges get deleted, humanitarian
campaigns redraw whole valleys. An OSM read dated on or after the event day
therefore contains somebody else's damage assessment, and feeding it to the
pipeline would be reading the answer. Nothing here touches the network: the date
rule, the buffer arithmetic, the cache key and the full-history reduction are all
pure functions, and the transport is exercised against a fake session.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from strandline import osm
from strandline.config import OSM_LAYERS, OsmParams

CAP = date.fromisoformat(OsmParams().latest_snapshot)


# --- the date rule ------------------------------------------------------------

@pytest.mark.parametrize("event", [
    date(2021, 7, 14), date(2023, 8, 4), date(2024, 10, 4), date(2025, 2, 26),
    date(2026, 7, 27), date(2026, 7, 28), date(2026, 8, 26), date(2026, 10, 13),
])
def test_the_snapshot_is_always_strictly_before_the_event(event):
    chosen = osm.snapshot_date(event)
    assert chosen < event, f"{chosen} is not strictly before {event}"
    assert chosen <= CAP, f"{chosen} is after the latest available snapshot {CAP}"


def test_the_snapshot_is_the_day_before_when_that_is_available():
    assert osm.snapshot_date(date(2024, 10, 4)) == date(2024, 10, 3)


def test_an_event_after_the_cap_falls_back_to_the_cap():
    """A future or very recent event cannot read OSM it does not have."""
    assert osm.snapshot_date(CAP + timedelta(days=60)) == CAP
    # The day immediately after the cap: the cap itself is strictly before it.
    assert osm.snapshot_date(CAP + timedelta(days=1)) == CAP


def test_an_event_on_the_cap_itself_steps_back_a_day():
    """`<= cap` must not beat `< event`: the strict rule wins."""
    assert osm.snapshot_date(CAP) == CAP - timedelta(days=1)


def test_an_event_before_osm_history_is_refused_not_guessed():
    with pytest.raises(osm.OsmError) as err:
        osm.snapshot_date(date(2005, 1, 1))
    message = str(err.value)
    assert "NOT AVAILABLE" in message, "a refusal must say what to report instead"
    assert str(osm.OHSOME_HISTORY_START) in message


def test_the_cap_matches_the_snapshot_the_project_declares():
    """CLAUDE.md Rule 1 names 2026-07-27 as the latest available snapshot."""
    assert OsmParams().latest_snapshot == "2026-07-27"


def test_a_malformed_cap_fails_at_construction_not_at_request_time():
    with pytest.raises(ValueError):
        OsmParams(latest_snapshot="not-a-date")


# --- the buffer ---------------------------------------------------------------

def test_the_buffer_grows_the_box_by_at_least_the_requested_distance():
    """Checked against a real geodesic distance, not against the formula."""
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    bbox = (85.10, 27.85, 85.45, 28.30)
    w, s, e, n = osm.buffered_bbox(bbox, 15.0)
    assert (w, s, e, n) != bbox
    # North edge, east edge: the distance added must be >= 15 km everywhere.
    _, _, north_m = geod.inv(bbox[0], bbox[3], bbox[0], n)
    _, _, east_m = geod.inv(bbox[2], bbox[1], e, bbox[1])
    _, _, east_top_m = geod.inv(bbox[2], bbox[3], e, bbox[3])
    for name, metres in (("north", north_m), ("east", east_m), ("east at top", east_top_m)):
        assert metres >= 15_000, f"{name} buffer is only {metres:.0f} m"
        assert metres < 15_000 * 1.6, f"{name} buffer is wastefully large: {metres:.0f} m"


def test_a_zero_buffer_is_the_identity():
    bbox = (1.0, 2.0, 3.0, 4.0)
    assert osm.buffered_bbox(bbox, 0.0) == bbox


def test_the_buffer_stays_inside_valid_coordinates():
    w, s, e, n = osm.buffered_bbox((-179.99, -89.9, 179.99, 89.9), 50.0)
    assert -180.0 <= w and e <= 180.0 and -90.0 <= s and n <= 90.0


def test_a_negative_buffer_is_rejected():
    with pytest.raises(ValueError):
        OsmParams(buffer_km=-1.0)


# --- the cache key ------------------------------------------------------------

def test_the_cache_key_covers_bbox_and_snapshot():
    a = osm.cache_path(Path("c"), "roads", date(2024, 1, 1), (1, 2, 3, 4))
    assert a != osm.cache_path(Path("c"), "roads", date(2024, 1, 2), (1, 2, 3, 4))
    assert a != osm.cache_path(Path("c"), "roads", date(2024, 1, 1), (1, 2, 3, 5))
    assert a != osm.cache_path(Path("c"), "buildings", date(2024, 1, 1), (1, 2, 3, 4))
    assert "2024-01-01" in a.name, "the snapshot must be readable in the filename"


def test_float_noise_does_not_miss_the_cache():
    assert (osm.bbox_key((1.0, 2.0, 3.0, 4.0))
            == osm.bbox_key((1.0 + 1e-12, 2.0, 3.0, 4.0)))


# --- reducing a full-history window to a snapshot -----------------------------

def _feat(oid, valid_from, valid_to, name=None):
    return {"type": "Feature",
            "properties": {osm.OSM_ID: oid, osm.VALID_FROM: valid_from,
                           osm.VALID_TO: valid_to, "name": name},
            "geometry": {"type": "Point", "coordinates": [0, 0]}}


END = "2024-10-03T00:00:00Z"


def test_an_element_edited_inside_the_window_is_counted_once_at_its_latest_version():
    features = [_feat("way/1", "2024-10-02T00:00:00Z", "2024-10-02T12:00:00Z", "old"),
                _feat("way/1", "2024-10-02T12:00:00Z", END, "new")]
    out = osm.snapshot_features(features, END)
    assert len(out) == 1
    assert out[0]["properties"]["name"] == "new"


def test_an_element_deleted_inside_the_window_is_absent_from_the_snapshot():
    """It did not exist at the snapshot instant, so it must not be mapped."""
    features = [_feat("way/1", "2024-10-02T00:00:00Z", "2024-10-02T12:00:00Z", "gone"),
                _feat("way/2", "2024-10-02T00:00:00Z", END, "stays")]
    out = osm.snapshot_features(features, END)
    assert [f["properties"]["@osmId"] for f in out] == ["way/2"]


def test_without_a_window_end_every_id_still_appears_exactly_once():
    features = [_feat("way/1", "a", "b"), _feat("way/1", "c", "d"), _feat("way/2", "a", "b")]
    out = osm.snapshot_features(features)
    assert sorted(f["properties"]["@osmId"] for f in out) == ["way/1", "way/2"]


def test_the_reduction_is_deterministic_in_output_order():
    """Rule 5: the same inputs must give byte-identical output."""
    features = [_feat("way/9", "a", END), _feat("way/1", "a", END), _feat("way/5", "a", END)]
    first = osm.snapshot_features(features, END)
    second = osm.snapshot_features(list(reversed(features)), END)
    assert [f["properties"]["@osmId"] for f in first] == ["way/1", "way/5", "way/9"]
    assert first == second


def test_features_without_an_osm_id_are_kept_rather_than_dropped():
    plain = {"type": "Feature", "properties": {"highway": "track"},
             "geometry": {"type": "Point", "coordinates": [0, 0]}}
    out = osm.snapshot_features([plain], END)
    assert out == [plain]


# --- transport: a refusal is never an empty layer -----------------------------

class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or json.dumps(body or {})
        self.content = self.text.encode()

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Answers per endpoint, and records what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        for key, resp in self.answers.items():
            if key in url:
                return resp() if callable(resp) else resp
        raise AssertionError(f"unexpected url {url}")


def _ok(features):
    return FakeResponse(200, {"type": "FeatureCollection", "features": features})


def test_a_blocked_snapshot_endpoint_falls_back_to_full_history():
    """The measured 403 on /elements/geometry must not lose the layer."""
    session = FakeSession({
        "v1/elements/geometry": FakeResponse(403, None, "Forbidden"),
        "elementsFullHistory/geometry": _ok([_feat("way/1", "x", END, "road")]),
    })
    body, prov = osm.fetch_layer("roads", (1, 2, 3, 4), date(2024, 10, 3),
                                 session=session, log=lambda *a: None)
    assert prov["endpoint"] == "elementsFullHistory/geometry"
    assert len(body["features"]) == 1
    assert prov["snapshot"] == "2024-10-03"
    assert prov["filter"] == OSM_LAYERS["roads"]


def test_the_snapshot_endpoint_is_preferred_when_it_works():
    session = FakeSession({
        "v1/elements/geometry": _ok([_feat("way/1", "x", END, "road")]),
        "elementsFullHistory/geometry": _ok([]),
    })
    _body, prov = osm.fetch_layer("roads", (1, 2, 3, 4), date(2024, 10, 3),
                                  session=session, log=lambda *a: None)
    assert prov["endpoint"] == "elements/geometry"


def test_every_endpoint_failing_is_a_refusal_naming_both():
    session = FakeSession({
        "v1/elements/geometry": FakeResponse(403, None, "Forbidden"),
        "elementsFullHistory/geometry": FakeResponse(500, None, "boom"),
    })
    with pytest.raises(osm.OsmError) as err:
        osm.fetch_layer("roads", (1, 2, 3, 4), date(2024, 10, 3),
                        session=session, log=lambda *a: None)
    message = str(err.value)
    assert "elements/geometry" in message and "elementsFullHistory/geometry" in message
    assert "NOT AVAILABLE rather than zero" in message, (
        "a refusal must tell the reader not to treat this as no damage")


def test_a_too_large_area_is_refused_with_what_to_do():
    session = FakeSession({"geometry": FakeResponse(413, None, "too big")})
    with pytest.raises(osm.OsmError) as err:
        osm.fetch_layer("buildings", (1, 2, 3, 4), date(2024, 10, 3),
                        session=session, log=lambda *a: None)
    assert "smaller bbox" in str(err.value)


def test_a_200_without_features_is_refused_not_read_as_empty():
    session = FakeSession({"geometry": FakeResponse(200, {"attribution": {}})})
    with pytest.raises(osm.OsmError) as err:
        osm.fetch_layer("roads", (1, 2, 3, 4), date(2024, 10, 3),
                        session=session, log=lambda *a: None)
    assert "Refusing to treat it as an empty area" in str(err.value)


def test_an_unknown_layer_is_refused():
    with pytest.raises(osm.OsmError) as err:
        osm.fetch_layer("airports", (1, 2, 3, 4), date(2024, 10, 3),
                        session=FakeSession({}), log=lambda *a: None)
    assert "airports" in str(err.value)


def test_the_request_asks_for_the_snapshot_date_and_identifies_itself():
    session = FakeSession({"v1/elements/geometry": _ok([])})
    osm.fetch_layer("roads", (1.5, 2.5, 3.5, 4.5), date(2024, 10, 3),
                    session=session, log=lambda *a: None)
    call = session.calls[0]
    assert call["params"]["time"] == "2024-10-03"
    assert call["params"]["bboxes"].startswith("1.500000,2.500000")
    assert "strandline" in call["headers"]["User-Agent"]


# --- caching ------------------------------------------------------------------

def test_a_layer_is_cached_by_bbox_and_snapshot_and_reused(tmp_path):
    session = FakeSession({"v1/elements/geometry": _ok([_feat("way/1", "x", END)])})
    bbox, snap = (1, 2, 3, 4), date(2024, 10, 3)
    osm.load_layer("roads", bbox, snap, tmp_path, session=session, log=lambda *a: None)
    assert osm.cache_path(tmp_path, "roads", snap, bbox).exists()
    calls_before = len(session.calls)
    _body, prov = osm.load_layer("roads", bbox, snap, tmp_path, session=session,
                                 log=lambda *a: None)
    assert len(session.calls) == calls_before, "a cached layer must not be re-downloaded"
    assert prov["endpoint"] == "cache"


def test_a_missing_cache_with_downloads_disabled_is_refused(tmp_path):
    with pytest.raises(osm.OsmError) as err:
        osm.load_layer("roads", (1, 2, 3, 4), date(2024, 10, 3), tmp_path,
                       allow_download=False, log=lambda *a: None)
    assert "not cached" in str(err.value)


def test_a_corrupt_cache_file_is_refused_with_how_to_fix_it(tmp_path):
    bbox, snap = (1, 2, 3, 4), date(2024, 10, 3)
    path = osm.cache_path(tmp_path, "roads", snap, bbox)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(osm.OsmError) as err:
        osm.load_layer("roads", bbox, snap, tmp_path, log=lambda *a: None)
    assert "Delete it and run again" in str(err.value)


def test_load_snapshot_records_the_snapshot_and_the_buffered_bbox(tmp_path):
    session = FakeSession({"v1/elements/geometry": _ok([_feat("way/1", "x", END)])})
    snap = osm.load_snapshot((17.9, 43.66, 17.92, 43.68), date(2024, 10, 4), tmp_path,
                             OsmParams(buffer_km=15.0), layers=["roads", "waterways"],
                             session=session, log=lambda *a: None)
    report = snap.report()
    assert report["snapshot"] == "2024-10-03"
    assert report["buffer_km"] == 15.0
    assert report["bbox_queried"][0] < report["bbox_requested"][0]
    assert report["feature_counts"] == {"roads": 1, "waterways": 1}
    assert report["attribution"] == "© OpenStreetMap contributors"
    assert "Rule 1" in report["snapshot_rule"]


def test_the_written_cache_is_byte_identical_across_runs(tmp_path):
    """Rule 5: deterministic outputs."""
    features = [_feat("way/9", "a", END), _feat("way/1", "a", END)]
    one, two = tmp_path / "a", tmp_path / "b"
    for out in (one, two):
        osm.load_layer("roads", (1, 2, 3, 4), date(2024, 10, 3), out,
                       session=FakeSession({"v1/elements/geometry": _ok(list(features))}),
                       log=lambda *a: None)
    first = osm.cache_path(one, "roads", date(2024, 10, 3), (1, 2, 3, 4)).read_bytes()
    second = osm.cache_path(two, "roads", date(2024, 10, 3), (1, 2, 3, 4)).read_bytes()
    assert first == second
