"""The dev-event reference wiring: independence flags, download URLs, held-out guard.

These guard the honesty of the scoreboard rather than the maths:
  - every flood event declares whether its reference is sensor-independent
  - unproven independence is recorded as false, never assumed
  - the reference fetcher refuses anything carrying a held-out name
"""

import yaml
import pytest

from pathlib import Path

from eval import fetch_references as fr

ROOT = Path(__file__).resolve().parents[1]
EVENTS = yaml.safe_load((ROOT / "eval" / "events.yaml").read_text(encoding="utf-8"))["events"]
FLOODS = [e for e in EVENTS if e["kind"] == "flood"]
CONTROLS = [e for e in EVENTS if e["kind"] == "control"]


def test_every_event_declares_independence():
    for ev in EVENTS:
        assert "independent_reference" in ev, f"{ev['name']}: independent_reference missing"


def test_flood_independence_is_an_explicit_boolean():
    """A flood event must say true or false. null would quietly skip the question."""
    for ev in FLOODS:
        assert isinstance(ev["independent_reference"], bool), (
            f"{ev['name']}: independent_reference must be true or false, "
            f"got {ev['independent_reference']!r}")


def test_controls_declare_no_independence():
    """Controls have no reference: false would imply a dependent one exists."""
    for ev in CONTROLS:
        assert ev["independent_reference"] is None, (
            f"{ev['name']}: a control has no reference, so independence is not a property it has")
        assert not ev.get("download_urls"), f"{ev['name']}: a control has nothing to download"


def test_independent_references_are_not_sentinel1():
    """The flag must agree with the recorded sensor: our input is Sentinel-1."""
    for ev in FLOODS:
        if ev["independent_reference"]:
            sensor = ev["ref_sensor"].lower()
            assert "sentinel-1" not in sensor.replace("our sentinel-1", ""), (
                f"{ev['name']}: claims independence but ref_sensor mentions Sentinel-1")
            assert "not established" not in sensor, (
                f"{ev['name']}: claims independence but its sensor was never established")


def test_unproven_sensor_is_never_counted_as_independent():
    for ev in FLOODS:
        if "not established" in ev["ref_sensor"].lower():
            assert ev["independent_reference"] is False, (
                f"{ev['name']}: sensor unproven, so independent_reference must be false")


def test_every_flood_event_has_download_urls_and_a_reference():
    for ev in FLOODS:
        assert ev.get("download_urls"), f"{ev['name']}: no download_urls"
        assert ev.get("reference"), f"{ev['name']}: no reference path"
        for url in ev["download_urls"]:
            assert url.startswith("https://"), f"{ev['name']}: {url} is not https"


def test_reference_path_lives_under_its_own_event_directory():
    for ev in FLOODS:
        expected = f"data/reference/{ev['name']}/"
        assert ev["reference"].startswith(expected), (
            f"{ev['name']}: reference must live in {expected}, got {ev['reference']}")


# --- Held-out guard (Rule 4) ---------------------------------------------------

def test_fetcher_refuses_a_held_out_name_anywhere():
    banned = fr.forbidden_names()
    assert banned, "forbidden_names() returned nothing"
    for name in banned:
        with pytest.raises(fr.HeldOutViolation):
            fr.guard(f"reference/{name}/observedEvent.shp", "test url", banned)


def test_fetcher_guard_allows_the_real_dev_events():
    banned = fr.forbidden_names()
    for ev in EVENTS:
        fr.guard(ev["name"], "event name", banned)
        for url in ev.get("download_urls") or []:
            fr.guard(url, "download url", banned)


def test_no_dev_event_download_url_mentions_a_held_out_activation():
    banned = fr.forbidden_names()
    blob = " ".join(
        str(ev.get(k, "")) for ev in EVENTS
        for k in ("name", "reference", "source", "ref_sensor", "notes")
    ).lower()
    for name in banned:
        assert name not in blob, f"held-out name {name!r} appears in the dev event file"
