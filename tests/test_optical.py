"""Sentinel-2 masking, index and reflectance-conversion behaviour.

SYNTHETIC: every array here is hand-built from plausible reflectance values,
so these tests prove the LOGIC - which pixels are used, which signature fires,
what is refused - and not any real-world accuracy. No real-world number may be
quoted from this file.
"""

import numpy as np
import pytest

from strandline import optical
from strandline.config import OpticalParams, S2_REFLECTANCE_BANDS

H = W = 12

# Hand-built spectra, reflectance units, in S2_REFLECTANCE_BANDS order
# (B03 green, B04 red, B08 NIR, B11 SWIR1, B12 SWIR2).
VEGETATION = (0.08, 0.10, 0.35, 0.25, 0.18)   # MNDWI -0.52, NDVI  0.56
WATER = (0.07, 0.05, 0.04, 0.02, 0.01)        # MNDWI  0.56, NDVI -0.11
MUD = (0.20, 0.22, 0.26, 0.33, 0.30)          # MNDWI -0.25, NDVI  0.08, SWIR up


def _scene(base=VEGETATION):
    arr = np.empty((len(S2_REFLECTANCE_BANDS), H, W), dtype=np.float64)
    for i, value in enumerate(base):
        arr[i] = value
    return arr


def _put(scene, spectrum, rows, cols):
    for i, value in enumerate(spectrum):
        scene[i][rows, cols] = value


def _scl(code=4):
    return np.full((H, W), float(code))


# --- clear mask ---------------------------------------------------------------

def test_clear_mask_keeps_only_the_declared_classes():
    op = OpticalParams()
    scl = np.arange(12, dtype=np.float64).reshape(1, 12) * np.ones((1, 1))
    keep = optical.clear_mask(scl, op)
    kept = sorted(int(c) for c in scl[keep])
    assert kept == [4, 5, 6, 7]


def test_clear_mask_drops_cloud_shadow_cirrus_and_snow():
    op = OpticalParams()
    for code in (0, 1, 2, 3, 8, 9, 10, 11):
        assert not optical.clear_mask(_scl(code), op).any(), f"SCL {code} must be masked"


def test_clear_mask_keeps_unclassified_because_turbid_flood_water_lands_there():
    assert optical.clear_mask(_scl(7), OpticalParams()).all()


def test_clear_mask_treats_absent_pixels_as_not_clear():
    scl = _scl(4)
    scl[0, 0] = np.nan
    assert not optical.clear_mask(scl, OpticalParams())[0, 0]


# --- indices ------------------------------------------------------------------

def test_index_band_order_is_the_documented_one():
    scene = _scene(VEGETATION)
    green, red, nir, swir1 = VEGETATION[0], VEGETATION[1], VEGETATION[2], VEGETATION[3]
    assert optical.mndwi(scene)[0, 0] == pytest.approx((green - swir1) / (green + swir1))
    assert optical.ndvi(scene)[0, 0] == pytest.approx((nir - red) / (nir + red))


def test_normalised_difference_is_nan_where_the_denominator_vanishes():
    a = np.array([0.5, 0.0, np.nan])
    b = np.array([0.5, 0.0, 0.2])
    out = optical.normalised_difference(a, b)
    assert out[0] == pytest.approx(0.0)
    assert np.isnan(out[1]) and np.isnan(out[2])


# --- evidence -----------------------------------------------------------------

def _pair_with(water_rows=None, mud_rows=None, permanent_rows=None):
    pre, post = _scene(), _scene()
    if permanent_rows is not None:
        _put(pre, WATER, permanent_rows, slice(None))
        _put(post, WATER, permanent_rows, slice(None))
    if water_rows is not None:
        _put(post, WATER, water_rows, slice(None))
    if mud_rows is not None:
        _put(post, MUD, mud_rows, slice(None))
    return pre, post


def test_new_water_is_detected_and_permanent_water_is_not():
    pre, post = _pair_with(water_rows=slice(2, 4), permanent_rows=slice(8, 10))
    ev = optical.build_evidence(pre, _scl(), post, _scl(), OpticalParams())
    assert ev.water[2:4].all()
    assert not ev.water[8:10].any(), "water on BOTH dates must not read as flood"
    assert not ev.water[5].any()


def test_deposit_signature_needs_both_an_ndvi_drop_and_a_swir_rise():
    pre, post = _pair_with(mud_rows=slice(2, 4))
    op = OpticalParams()
    ev = optical.build_evidence(pre, _scl(), post, _scl(), op)
    assert ev.debris[2:4].all()
    assert not ev.water[2:4].any(), "mud is not water"

    # NDVI drops the same way but SWIR does not brighten: not a deposit.
    pre2, post2 = _pair_with()
    dark_mud = (MUD[0], MUD[1], MUD[2], VEGETATION[3], MUD[4])
    _put(post2, dark_mud, slice(2, 4), slice(None))
    ev2 = optical.build_evidence(pre2, _scl(), post2, _scl(), op)
    assert not ev2.debris.any()


def test_water_and_deposit_classes_are_disjoint():
    pre, post = _pair_with(water_rows=slice(2, 4), mud_rows=slice(6, 8))
    ev = optical.build_evidence(pre, _scl(), post, _scl(), OpticalParams())
    assert not (ev.water & ev.debris).any()


def test_a_clouded_pixel_contributes_nothing_even_when_the_index_fires():
    pre, post = _pair_with(water_rows=slice(2, 6))
    post_scl = _scl()
    post_scl[2:4] = 9.0                      # cloud, high probability
    ev = optical.build_evidence(pre, _scl(), post, post_scl, OpticalParams())
    assert not ev.water[2:4].any()
    assert ev.water[4:6].all()
    assert not ev.clear[2:4].any()


def test_cloud_on_the_pre_date_also_removes_the_pixel():
    pre, post = _pair_with(water_rows=slice(2, 6))
    pre_scl = _scl()
    pre_scl[2:4] = 8.0                       # cloud, medium probability
    ev = optical.build_evidence(pre, pre_scl, post, _scl(), OpticalParams())
    assert not ev.water[2:4].any()


def test_too_much_cloud_is_a_refusal_not_a_partial_answer():
    pre, post = _pair_with(water_rows=slice(2, 4))
    post_scl = _scl()
    post_scl[2:] = 9.0                       # only 2 of 12 rows clear -> 17%
    with pytest.raises(optical.OpticalError, match="clear on BOTH"):
        optical.build_evidence(pre, _scl(), post, post_scl, OpticalParams())


def test_clear_fraction_is_reported():
    pre, post = _pair_with(water_rows=slice(2, 4))
    post_scl = _scl()
    post_scl[:3] = 9.0
    ev = optical.build_evidence(pre, _scl(), post, post_scl, OpticalParams())
    assert ev.stats["clear_fraction_post"] == pytest.approx(0.75)
    assert ev.stats["clear_fraction_pair"] == pytest.approx(0.75)
    assert ev.clear_fraction == pytest.approx(0.75)


def test_evidence_is_deterministic():
    pre, post = _pair_with(water_rows=slice(2, 4), mud_rows=slice(6, 8))
    a = optical.build_evidence(pre, _scl(), post, _scl(), OpticalParams())
    b = optical.build_evidence(pre, _scl(), post, _scl(), OpticalParams())
    assert np.array_equal(a.water, b.water) and np.array_equal(a.debris, b.debris)
    assert a.stats == b.stats


def test_mndwi_threshold_is_honoured():
    """A pixel that rises a lot but still reads as land is not water."""
    pre, post = _pair_with()
    # MNDWI pre -0.52, post -0.05: a 0.47 rise, but still below the 0.0 threshold.
    _put(post, (0.18, 0.10, 0.20, 0.20, 0.15), slice(2, 4), slice(None))
    ev = optical.build_evidence(pre, _scl(), post, _scl(), OpticalParams())
    assert not ev.water.any()


# --- reflectance conversion ---------------------------------------------------

def test_digital_numbers_with_the_modern_baseline_get_the_boa_offset():
    op = OpticalParams()
    dn = np.full((2, 4, 4), 2000.0)
    out, info = optical.to_reflectance(
        dn, op, scene_names=["S2A_MSIL2A_20240710T102021_N0510_R022_T32TPS_x.SAFE"])
    assert info["method"] == "dn_baseline"
    assert info["processing_baseline"] == 510
    assert out[0, 0, 0] == pytest.approx((2000.0 - 1000.0) / 10000.0)


def test_digital_numbers_from_an_older_baseline_get_no_offset():
    op = OpticalParams()
    dn = np.full((2, 4, 4), 2000.0)
    out, info = optical.to_reflectance(
        dn, op, scene_names=["S2A_MSIL2A_20200710T102021_N0214_R022_T32TPS_x.SAFE"])
    assert info["offset"] == 0.0
    assert out[0, 0, 0] == pytest.approx(0.2)


def test_values_that_are_already_reflectance_are_left_alone():
    op = OpticalParams()
    refl = np.full((2, 4, 4), 0.21)
    out, info = optical.to_reflectance(refl, op)
    assert info["method"] == "already_reflectance"
    assert np.allclose(out, refl)


def test_file_declared_scale_and_offset_win():
    op = OpticalParams()
    dn = np.full((1, 4, 4), 2500.0)
    out, info = optical.to_reflectance(dn, op, file_scales=(1e-4,), file_offsets=(-0.1,))
    assert info["method"] == "file_metadata"
    assert out[0, 0, 0] == pytest.approx(2500.0 * 1e-4 - 0.1)


def test_an_implausible_conversion_is_refused():
    op = OpticalParams()
    # 50000 DN: with scale 10000 this lands at ~4.9, far outside reflectance.
    with pytest.raises(optical.OpticalError, match="not plausible"):
        optical.to_reflectance(np.full((1, 4, 4), 50_000.0), op,
                               file_scales=(1.0,), file_offsets=(0.0,))


def test_conversion_records_the_negative_fraction_for_later_verification():
    """A double-applied offset shows up as negative reflectance, so it is recorded."""
    op = OpticalParams()
    dn = np.full((1, 4, 4), 400.0)   # below the offset -> negative after conversion
    out, info = optical.to_reflectance(
        dn, op, scene_names=["S2A_MSIL2A_20240710T102021_N0510_R022_T32TPS_x.SAFE"])
    assert info["negative_fraction"] == 1.0
    assert out.max() < 0


def test_all_absent_reflectance_is_refused():
    with pytest.raises(optical.OpticalError, match="no valid pixels"):
        optical.to_reflectance(np.full((1, 3, 3), np.nan), OpticalParams())


def test_cache_names_carry_day_resolution_and_bands():
    import datetime as dt
    name = optical.s2_refl_cache_name(dt.date(2024, 7, 10), 20.0)
    assert name == "s2_refl_b03b04b08b11b12_2024-07-10_20m.tif"
    assert optical.s2_scl_cache_name(dt.date(2024, 7, 10), 20.0) == "s2_scl_2024-07-10_20m.tif"
