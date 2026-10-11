"""The backscatter coefficient is a supported config choice, and the choice is
visible in every cache filename.

sigma0-ellipsoid and gamma0-terrain are different measurements of the same
scene. A pre/post stack that mixed them would show change that is an artefact
of the processing, not of the flood, so the two can never share a cache entry.
"""

import datetime as dt

import pytest

from strandline import ingest
from strandline.config import BACKSCATTER_COEFFICIENTS, RunParams


def test_default_coefficient_is_unchanged():
    # The default moves only on dev-event scoreboard evidence (docs/tuning_log.md).
    assert RunParams().backscatter_coefficient == "sigma0-ellipsoid"


def test_both_coefficients_are_supported():
    assert BACKSCATTER_COEFFICIENTS == ("sigma0-ellipsoid", "gamma0-terrain")
    for coefficient in BACKSCATTER_COEFFICIENTS:
        assert RunParams(backscatter_coefficient=coefficient).backscatter_coefficient == coefficient


@pytest.mark.parametrize("bad", ["gamma0", "sigma0", "beta0", "GAMMA0-TERRAIN", ""])
def test_unsupported_coefficient_is_refused_with_a_clear_message(bad):
    with pytest.raises(ValueError, match="not supported"):
        RunParams(backscatter_coefficient=bad)
    with pytest.raises(ValueError, match="not supported"):
        ingest.coefficient_slug(bad)


def test_cache_name_contains_the_coefficient():
    day = dt.date(2026, 8, 28)
    sigma = ingest.s1_cache_name("t85_ASCENDING", day, 20.0, "sigma0-ellipsoid")
    gamma = ingest.s1_cache_name("t85_ASCENDING", day, 20.0, "gamma0-terrain")

    assert sigma == "s1_sigma0_ellipsoid_t85_ASCENDING_2026-08-28_20m.tif"
    assert gamma == "s1_gamma0_terrain_t85_ASCENDING_2026-08-28_20m.tif"


def test_the_two_coefficients_can_never_share_a_cache_entry():
    """Same track, same day, same grid: the names must still differ."""
    args = ("t85_ASCENDING", dt.date(2026, 8, 28), 20.0)
    names = {ingest.s1_cache_name(*args, c) for c in BACKSCATTER_COEFFICIENTS}
    assert len(names) == len(BACKSCATTER_COEFFICIENTS)


def test_cache_name_still_separates_track_day_and_resolution():
    day = dt.date(2026, 8, 28)
    c = "sigma0-ellipsoid"
    base = ingest.s1_cache_name("t85_ASCENDING", day, 20.0, c)
    assert base != ingest.s1_cache_name("t85_DESCENDING", day, 20.0, c)
    assert base != ingest.s1_cache_name("t12_ASCENDING", day, 20.0, c)
    assert base != ingest.s1_cache_name("t85_ASCENDING", dt.date(2026, 8, 16), 20.0, c)
    assert base != ingest.s1_cache_name("t85_ASCENDING", day, 10.0, c)


def test_download_s1_refuses_an_unsupported_coefficient_before_any_network_call():
    sentinel = object()  # a connection that would explode if touched

    with pytest.raises(ValueError, match="not supported"):
        ingest.download_s1(sentinel, None, None, None, "beta0", False)


def test_cli_accepts_both_coefficients_and_defaults_to_sigma0(monkeypatch):
    from strandline import run as run_module

    captured = {}

    def fake_run(bbox, event_day, out, rp, dp, dry_run):
        captured["coefficient"] = rp.backscatter_coefficient
        return 0

    monkeypatch.setattr(run_module, "run", fake_run)
    argv = ["--bbox", "85.1", "27.85", "85.45", "28.3",
            "--event-date", "2026-08-26", "--out", "runs/x", "--dry-run"]

    run_module.main(argv)
    assert captured["coefficient"] == "sigma0-ellipsoid"

    run_module.main(argv + ["--backscatter-coefficient", "gamma0-terrain"])
    assert captured["coefficient"] == "gamma0-terrain"


def test_cli_rejects_an_unsupported_coefficient():
    from strandline import run as run_module

    argv = ["--bbox", "85.1", "27.85", "85.45", "28.3",
            "--event-date", "2026-08-26", "--out", "runs/x",
            "--backscatter-coefficient", "beta0"]
    with pytest.raises(SystemExit):
        run_module.main(argv)
