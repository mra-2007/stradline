"""strandline.ml: CPU Kuro Siwo U-Net inference.

SYNTHETIC checkpoints and arrays throughout (Rule 3.8): there is no trained
Kuro Siwo checkpoint in this repository (models/strandline_kurosiwo_unet.pt is
gitignored), so every "model" here is a tiny randomly-initialised
segmentation_models_pytorch.Unet saved in the documented checkpoint format.
Nothing here is a real-world accuracy figure; what is established is that the
LOADING, CONVERSION and TILING MACHINERY does what it claims.
"""

from pathlib import Path

import numpy as np
import pytest
import rasterio

from strandline import ml

torch = pytest.importorskip("torch")
smp = pytest.importorskip("segmentation_models_pytorch")

ROOT = Path(__file__).resolve().parents[1]
CACHED_SCENE = (ROOT / "runs" / "eval" / "bosnia_konjic_2024" / "cache"
                / "s1_sigma0_ellipsoid_copernicus_30_t73_ASCENDING_2024-09-11_20m.tif")


def _tiny_checkpoint(path: Path, mean=None, std=None, clip_lo=None, clip_hi=None) -> dict:
    """A real, tiny, randomly-initialised net saved in the documented format."""
    net = smp.Unet("resnet34", encoder_weights=None, in_channels=ml.N_CHANNELS, classes=3)
    ckpt = {
        "state_dict": net.state_dict(),
        "mean": np.asarray(mean if mean is not None else [0.1] * ml.N_CHANNELS),
        "std": np.asarray(std if std is not None else [0.05] * ml.N_CHANNELS),
        "clip_lo": np.asarray(clip_lo if clip_lo is not None else [0.0] * ml.N_CHANNELS),
        "clip_hi": np.asarray(clip_hi if clip_hi is not None else [1.0] * ml.N_CHANNELS),
    }
    torch.save(ckpt, str(path))
    return ckpt


# --- unit conversion -----------------------------------------------------

def test_sigma0_db_to_linear_is_the_inverse_of_10log10():
    db = np.array([-30.0, -20.0, -10.0, 0.0, 5.0, np.nan])
    linear = ml.sigma0_db_to_linear(db)
    back = 10.0 * np.log10(linear[np.isfinite(linear)])
    assert np.allclose(back, db[np.isfinite(db)])
    assert np.isnan(linear[-1])


def test_normalise_applies_clip_then_zscore_per_channel():
    channels = np.array([[[5.0, -5.0], [2.0, 2.0]]] * ml.N_CHANNELS)  # (6, 2, 2), same per channel
    clip_lo = np.full(ml.N_CHANNELS, -1.0)
    clip_hi = np.full(ml.N_CHANNELS, 1.0)
    mean = np.full(ml.N_CHANNELS, 0.0)
    std = np.full(ml.N_CHANNELS, 2.0)
    out = ml.normalise(channels, clip_lo, clip_hi, mean, std)
    # 5.0 clips to 1.0, then (1.0 - 0.0) / 2.0 = 0.5; -5.0 clips to -1.0 -> -0.5;
    # 2.0 clips to 1.0 -> 0.5 (both occurrences).
    assert np.allclose(out[0], [[0.5, -0.5], [0.5, 0.5]])


def test_clip_coverage_reports_the_inside_fraction_per_channel():
    channels = np.zeros((ml.N_CHANNELS, 2, 2))
    channels[0] = [[0.5, 0.5], [0.5, 5.0]]       # 3/4 inside [0, 1]
    channels[1] = [[np.nan, 2.0], [2.0, 2.0]]    # 0/3 finite-inside
    clip_lo = np.zeros(ml.N_CHANNELS)
    clip_hi = np.ones(ml.N_CHANNELS)
    cov = ml.clip_coverage(channels, clip_lo, clip_hi)
    assert cov[ml.CHANNELS[0]] == pytest.approx(0.75)
    assert cov[ml.CHANNELS[1]] == pytest.approx(0.0)


@pytest.mark.skipif(not CACHED_SCENE.exists(), reason="dev-event cache not present")
def test_our_linear_conversion_matches_a_cached_dev_scenes_own_range():
    """Round-tripping a REAL cached Sentinel-1 scene through dB and back must
    not change its order of magnitude. this is a conversion sanity check, not a
    claim about the real checkpoint's clip_lo/clip_hi (unknown: the trained
    checkpoint is not in this repository; see docs/open_items.md).

    A representative Sentinel-1 IW GRDH linear-sigma0 range of
    [10**(-30/10), 10**(0/10)] (-30..0 dB, wider than DetectParams'
    water_db_min/max to allow for debris and dry land, not just water) is used
    as the plausible-range stand-in. Most finite pixels of a real scene must
    fall inside it once our dB round-trip is applied, or the conversion in
    `strandline.ml` disagrees with the conversion `strandline.detect` already
    uses on the exact same files.
    """
    with rasterio.open(CACHED_SCENE) as ds:
        linear = ds.read().astype(np.float64)
    from strandline.detect import preprocess
    from strandline.config import DetectParams

    dp = DetectParams()
    db = np.stack([preprocess(linear[0], dp), preprocess(linear[1], dp)])
    back_to_linear = ml.sigma0_db_to_linear(db)
    clip_lo, clip_hi = 10 ** (-30.0 / 10.0), 10 ** (0.0 / 10.0)
    for pol in range(2):
        x = back_to_linear[pol]
        finite = np.isfinite(x)
        inside = finite & (x >= clip_lo) & (x <= clip_hi)
        frac = float(inside.sum()) / float(finite.sum())
        assert frac > 0.5, f"only {frac:.1%} of pixels land in the plausible range"


# --- load_model / missing-model fallback ----------------------------------

def test_load_model_missing_file_is_unavailable_not_an_exception(tmp_path):
    loaded, status = ml.load_model(tmp_path / "does_not_exist.pt")
    assert loaded is None
    assert status["available"] is False
    assert "not found" in status["reason"]


def test_load_model_checkpoint_missing_required_keys_is_unavailable(tmp_path):
    path = tmp_path / "bad.pt"
    torch.save({"state_dict": {}}, str(path))   # no mean/std/clip_lo/clip_hi
    loaded, status = ml.load_model(path)
    assert loaded is None
    assert status["available"] is False
    assert "expected format" in status["reason"]


def test_load_model_accepts_the_documented_checkpoint_format(tmp_path):
    path = tmp_path / "ok.pt"
    _tiny_checkpoint(path)
    loaded, status = ml.load_model(path)
    assert loaded is not None
    assert status["available"] is True
    assert status["sha256"] == ml.file_sha256(path)


# --- tiled inference --------------------------------------------------------

def test_predict_tiles_handles_a_non_multiple_of_tile_size(tmp_path):
    path = tmp_path / "ok.pt"
    _tiny_checkpoint(path, clip_lo=[0.0] * ml.N_CHANNELS, clip_hi=[1.0] * ml.N_CHANNELS)
    loaded, _ = ml.load_model(path)
    h, w = 150, 170   # neither dimension is a multiple of 128
    channels = np.full((ml.N_CHANNELS, h, w), 0.2)
    p_flood, p_perm = ml.predict_tiles(loaded, channels, tile=128, overlap=32)
    assert p_flood.shape == (h, w)
    assert p_perm.shape == (h, w)
    finite = np.isfinite(p_flood)
    assert finite.all()   # every pixel was covered by some tile
    assert np.all(p_flood[finite] >= 0) and np.all(p_flood[finite] <= 1)
    assert np.all(p_perm[finite] >= 0) and np.all(p_perm[finite] <= 1)


def test_predict_tiles_leaves_fully_invalid_pixels_as_nan(tmp_path):
    path = tmp_path / "ok.pt"
    _tiny_checkpoint(path)
    loaded, _ = ml.load_model(path)
    channels = np.full((ml.N_CHANNELS, 40, 40), np.nan)
    p_flood, p_perm = ml.predict_tiles(loaded, channels, tile=128, overlap=32)
    assert np.all(np.isnan(p_flood))
    assert np.all(np.isnan(p_perm))


def test_predict_tiles_is_deterministic(tmp_path):
    path = tmp_path / "ok.pt"
    _tiny_checkpoint(path)
    loaded, _ = ml.load_model(path)
    rng = np.random.default_rng(0)
    channels = rng.uniform(0.0, 1.0, size=(ml.N_CHANNELS, 160, 140))
    a = ml.predict_tiles(loaded, channels, tile=128, overlap=32)
    b = ml.predict_tiles(loaded, channels, tile=128, overlap=32)
    assert np.array_equal(a[0], b[0], equal_nan=True)
    assert np.array_equal(a[1], b[1], equal_nan=True)


# --- run_model: the stage-level contract ------------------------------------

def test_run_model_with_physics_mode_never_touches_torch(monkeypatch, tmp_path):
    """mode='physics' must not even try to import torch or read a path."""
    from strandline.config import ModelParams

    def _boom(*a, **k):
        raise AssertionError("load_model must not be called in physics mode")

    monkeypatch.setattr(ml, "load_model", _boom)
    h, w = 20, 20
    pre_vv = np.zeros((3, h, w))
    pre_vh = np.zeros((3, h, w))
    post_vv = np.zeros((h, w))
    post_vh = np.zeros((h, w))
    evidence, report = ml.run_model(pre_vv, pre_vh, post_vv, post_vh, ModelParams())
    assert evidence is None
    assert report["status"] == ml.MODEL_DISABLED


def test_run_model_falls_back_cleanly_when_the_file_is_missing(tmp_path):
    from strandline.config import ModelParams

    mp = ModelParams(mode="model", model_path=str(tmp_path / "missing.pt"))
    h, w = 20, 20
    pre_vv = np.zeros((3, h, w))
    pre_vh = np.zeros((3, h, w))
    post_vv = np.zeros((h, w))
    post_vh = np.zeros((h, w))
    evidence, report = ml.run_model(pre_vv, pre_vh, post_vv, post_vh, mp)
    assert evidence is None
    assert report["status"] == ml.MODEL_UNAVAILABLE
    assert "not found" in report["reason"]


def test_run_model_used_end_to_end_is_deterministic(tmp_path):
    from strandline.config import ModelParams

    path = tmp_path / "ok.pt"
    _tiny_checkpoint(path)
    mp = ModelParams(mode="fusion", model_path=str(path), tile_px=64, tile_overlap_px=16)
    h, w = 80, 96
    rng = np.random.default_rng(1)
    pre_vv = rng.uniform(-20, -5, size=(3, h, w))
    pre_vh = rng.uniform(-25, -10, size=(3, h, w))
    post_vv = rng.uniform(-20, -5, size=(h, w))
    post_vh = rng.uniform(-25, -10, size=(h, w))

    ev1, r1 = ml.run_model(pre_vv, pre_vh, post_vv, post_vh, mp)
    ev2, r2 = ml.run_model(pre_vv, pre_vh, post_vv, post_vh, mp)
    assert r1["status"] == ml.MODEL_USED
    assert ev1.available and ev2.available
    assert np.array_equal(ev1.p_flood, ev2.p_flood, equal_nan=True)
    assert np.array_equal(ev1.p_perm, ev2.p_perm, equal_nan=True)
    assert ev1.sha256 == ml.file_sha256(path)
