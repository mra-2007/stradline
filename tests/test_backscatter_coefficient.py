"""The backscatter coefficient and the elevation model are config choices, and
both are visible in every cache filename.

Two different coefficients, or the same coefficient over two different
elevation models, are different measurements of the same scene: one changes the
radiometric reference area, the other changes where each pixel is placed during
orthorectification. A pre/post stack that mixed either would show change that
is an artefact of the processing, not of the flood, so they can never share a
cache entry.

On gamma0-terrain: it was a supported choice here until 2026-10-11 and has been
removed because the Copernicus Data Space backend does not offer it (its
sar_backscatter process declares coefficient as enum ["sigma0-ellipsoid"]).
These tests assert the CURRENT measured capability, and the "cannot share a
cache entry" property is now checked over the elevation-model axis, which is
real, rather than over a coefficient the backend refuses. See
docs/tuning_log.md, 2026-10-11.
"""

import datetime as dt

import pytest

from strandline import ingest
from strandline.config import BACKSCATTER_COEFFICIENTS, ELEVATION_MODELS, RunParams

DAY = dt.date(2026, 8, 28)
SIGMA = "sigma0-ellipsoid"
COP30 = "COPERNICUS_30"


def test_default_coefficient_is_unchanged():
    # The default moves only on dev-event scoreboard evidence (docs/tuning_log.md).
    assert RunParams().backscatter_coefficient == SIGMA


def test_every_declared_coefficient_is_accepted():
    assert BACKSCATTER_COEFFICIENTS == (SIGMA,)
    for coefficient in BACKSCATTER_COEFFICIENTS:
        assert RunParams(backscatter_coefficient=coefficient).backscatter_coefficient == coefficient


def test_gamma0_terrain_is_refused_because_the_backend_does_not_offer_it():
    """Measured: the backend's own process schema is enum ["sigma0-ellipsoid"].

    Refused here, before any network call, so the failure says why instead of
    dying inside a batch job that was never created.
    """
    assert "gamma0-terrain" not in BACKSCATTER_COEFFICIENTS
    with pytest.raises(ValueError, match="not supported"):
        RunParams(backscatter_coefficient="gamma0-terrain")


@pytest.mark.parametrize("bad", ["gamma0", "sigma0", "beta0", "SIGMA0-ELLIPSOID", ""])
def test_unsupported_coefficient_is_refused_with_a_clear_message(bad):
    with pytest.raises(ValueError, match="not supported"):
        RunParams(backscatter_coefficient=bad)
    with pytest.raises(ValueError, match="not supported"):
        ingest.coefficient_slug(bad)


# --- the elevation model ------------------------------------------------------

def test_default_elevation_model_is_global():
    """SRTMGL1, the backend default, has no data above 60 deg N: every
    Norwegian dev event failed on tile N61E009 until this was set."""
    assert RunParams().elevation_model == COP30
    assert ELEVATION_MODELS == (COP30,)


@pytest.mark.parametrize("bad", ["SRTMGL1", "srtm", "COPERNICUS_90", "", None])
def test_unsupported_elevation_model_is_refused(bad):
    with pytest.raises(ValueError, match="not supported"):
        RunParams(elevation_model=bad)
    with pytest.raises(ValueError, match="not supported"):
        ingest.elevation_model_slug(bad)


def test_download_passes_the_elevation_model_to_sar_backscatter(tmp_path):
    """The whole point of the fix: it must reach the openEO call."""
    captured = {}

    class FakeCube:
        def sar_backscatter(self, **kwargs):
            captured.update(kwargs)
            return self

        def resample_spatial(self, **kwargs):
            return self

        def reduce_dimension(self, **kwargs):
            return self

        def download(self, path, format=None):
            open(path, "wb").write(b"fake")

    class FakeConn:
        def load_collection(self, *a, **k):
            return FakeCube()

    acq = type("A", (), {"start": dt.datetime(2026, 8, 28, 5, 50),
                         "stop": dt.datetime(2026, 8, 28, 5, 51)})()
    grid = ingest.make_grid((85.3, 27.9, 85.33, 27.93), 20.0)
    out = tmp_path / ingest.s1_cache_name("t85_ASCENDING", DAY, 20.0, SIGMA, COP30)

    ingest.download_s1(FakeConn(), acq, grid, out, SIGMA, False, COP30)

    assert captured["elevation_model"] == COP30, \
        "sar_backscatter was called without an elevation model: SRTM would be used"
    assert captured["coefficient"] == SIGMA
    assert captured["mask"] is False


def test_download_refuses_an_unsupported_elevation_model_before_any_network_call():
    sentinel = object()  # a connection that would explode if touched
    with pytest.raises(ValueError, match="not supported"):
        ingest.download_s1(sentinel, None, None, None, SIGMA, False, "SRTMGL1")


# --- cache names --------------------------------------------------------------

def test_cache_name_contains_the_coefficient_and_the_elevation_model():
    name = ingest.s1_cache_name("t85_ASCENDING", DAY, 20.0, SIGMA, COP30)
    assert name == "s1_sigma0_ellipsoid_copernicus_30_t85_ASCENDING_2026-08-28_20m.tif"


def test_the_elevation_model_is_required_so_a_mixed_cache_cannot_happen():
    """No default: a caller cannot omit it and silently reuse SRTM files."""
    with pytest.raises(TypeError):
        ingest.s1_cache_name("t85_ASCENDING", DAY, 20.0, SIGMA)


def test_old_srtm_based_names_can_never_be_read_as_new_ones():
    """The pre-fix name must not collide with the post-fix name."""
    new = ingest.s1_cache_name("t85_ASCENDING", DAY, 20.0, SIGMA, COP30)
    old = f"s1_sigma0_ellipsoid_t85_ASCENDING_{DAY}_20m.tif"
    assert new != old
    assert old not in new and new not in old


def test_cache_name_still_separates_track_day_and_resolution():
    base = ingest.s1_cache_name("t85_ASCENDING", DAY, 20.0, SIGMA, COP30)
    assert base != ingest.s1_cache_name("t85_DESCENDING", DAY, 20.0, SIGMA, COP30)
    assert base != ingest.s1_cache_name("t12_ASCENDING", DAY, 20.0, SIGMA, COP30)
    assert base != ingest.s1_cache_name("t85_ASCENDING", dt.date(2026, 8, 16), 20.0, SIGMA, COP30)
    assert base != ingest.s1_cache_name("t85_ASCENDING", DAY, 10.0, SIGMA, COP30)


def test_every_supported_processing_combination_gets_its_own_cache_entry():
    names = {ingest.s1_cache_name("t85_ASCENDING", DAY, 20.0, c, e)
             for c in BACKSCATTER_COEFFICIENTS for e in ELEVATION_MODELS}
    assert len(names) == len(BACKSCATTER_COEFFICIENTS) * len(ELEVATION_MODELS)


def test_the_elevation_model_reaches_the_params_hash():
    """A re-processed scene must not be served a score cached from the old one."""
    from dataclasses import replace

    from strandline import run as pipeline
    from strandline.config import DetectParams

    rp = RunParams()
    assert "elevation_model" in pipeline.params_dict(DetectParams(), rp)["run"]
    # The hash must change if the field ever changes, so widen ELEVATION_MODELS
    # and this stays true without an edit here.
    other = [e for e in ELEVATION_MODELS if e != rp.elevation_model]
    for e in other:
        assert pipeline.params_hash(DetectParams(), rp) != \
            pipeline.params_hash(DetectParams(), replace(rp, elevation_model=e))


def test_download_s1_refuses_an_unsupported_coefficient_before_any_network_call():
    sentinel = object()
    with pytest.raises(ValueError, match="not supported"):
        ingest.download_s1(sentinel, None, None, None, "beta0", False)


def test_cli_accepts_the_supported_coefficient_and_defaults_to_sigma0(monkeypatch):
    from strandline import run as run_module

    captured = {}

    def fake_run(bbox, event_day, out, rp, dp, dry_run, op=None):
        captured["coefficient"] = rp.backscatter_coefficient
        captured["elevation_model"] = rp.elevation_model
        captured["optical"] = op
        return 0

    monkeypatch.setattr(run_module, "run", fake_run)
    argv = ["--bbox", "85.1", "27.85", "85.45", "28.3",
            "--event-date", "2026-08-26", "--out", "runs/x", "--dry-run"]

    run_module.main(argv)
    assert captured["coefficient"] == SIGMA
    assert captured["elevation_model"] == COP30
    # Track B requires Sentinel-2 where the sky is clear, so the CLI asks for it
    # unless told not to.
    assert captured["optical"].enabled is True

    run_module.main(argv + ["--backscatter-coefficient", SIGMA])
    assert captured["coefficient"] == SIGMA

    run_module.main(argv + ["--no-optical"])
    assert captured["optical"].enabled is False

    run_module.main(argv + ["--optical-post-days", "30"])
    assert captured["optical"].max_post_latency_days == 30


@pytest.mark.parametrize("bad", ["beta0", "gamma0-terrain"])
def test_cli_rejects_an_unsupported_coefficient(bad):
    from strandline import run as run_module

    argv = ["--bbox", "85.1", "27.85", "85.45", "28.3",
            "--event-date", "2026-08-26", "--out", "runs/x",
            "--backscatter-coefficient", bad]
    with pytest.raises(SystemExit):
        run_module.main(argv)
