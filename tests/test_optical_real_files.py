"""The reflectance convention, checked against the REAL downloaded Sentinel-2 files.

NOT SYNTHETIC. This is the only test in the suite that reads downloaded
Sentinel-2 data, and it exists because the bug it guards could not have been
caught on synthetic input: the fixtures in tests/test_pipeline_optical.py write
digital numbers in the archive convention (reflectance * 10000 + 1000), which is
exactly the convention this openEO backend does NOT use. Every synthetic test
passed while all 20 real files were being converted with the offset applied
twice.

It skips when the dev-set cache is absent (a fresh clone, or CI), so it never
blocks the suite; it is a check on data, and the data is not in the repo.

    venv/Scripts/python.exe -m eval.fetch_dev_data --plan gates --optical

What is asserted is the PHYSICS, not the backend's current habit: whichever
offset `dn_offset` picks, the result must be plausible, non-negative
reflectance. A test that asserted "this backend pre-applies the offset" would
have to be edited if the backend changed, which is precisely the kind of
assertion that hides a regression.
"""

from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")

from strandline import optical
from strandline.config import OpticalParams, S2_REFLECTANCE_BANDS

ROOT = Path(__file__).resolve().parents[1]
EVAL_RUNS = ROOT / "runs" / "eval"


def _refl_files() -> list[Path]:
    if not EVAL_RUNS.exists():
        return []
    return sorted(EVAL_RUNS.glob("*/cache/s2_refl_*.tif"))


FILES = _refl_files()
needs_cache = pytest.mark.skipif(
    not FILES, reason="no Sentinel-2 dev cache; run eval.fetch_dev_data --optical")


def _read(path: Path) -> tuple[np.ndarray, tuple, tuple]:
    with rasterio.open(path) as src:
        return src.read().astype(np.float64), src.scales, src.offsets


@needs_cache
@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.parent.name}/{p.stem[-14:]}")
def test_every_real_file_converts_to_plausible_reflectance(path):
    """The conversion a real run would apply, with its own physical test."""
    op = OpticalParams()
    values, scales, offsets = _read(path)
    assert values.shape[0] == len(S2_REFLECTANCE_BANDS)

    refl, info = optical.to_reflectance(values, op, file_scales=scales,
                                        file_offsets=offsets)
    assert info["negative_fraction"] <= op.max_negative_fraction, (
        f"{path.name}: {info['negative_fraction']:.1%} of valid pixels are negative "
        f"reflectance after conversion ({info['method']}, offset {info['offset']}). "
        f"Reflectance is not negative: the scale/offset convention is wrong.")
    median = info["median_after"]
    assert 0.0 < median < 1.0, f"{path.name}: implausible median reflectance {median}"


def _clear_fraction(refl_path: Path, op: OpticalParams) -> float:
    """The clear fraction of this date, from its own scene-classification layer."""
    day = refl_path.stem.split("_")[-2]
    scl_path = refl_path.parent / f"s2_scl_{day}_20m.tif"
    with rasterio.open(scl_path) as src:
        scl = src.read(1).astype(np.float64)
    return float(np.mean(optical.clear_mask(scl, op)))


@needs_cache
@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.parent.name}/{p.stem[-14:]}")
def test_each_real_file_is_either_decidable_or_too_cloudy_to_be_used(path):
    """The dark tail is what decides, so a scene with no dark pixels cannot.

    On 19 of the 20 dev-set files the two conventions are two orders of
    magnitude apart - applying the specification offset drives 9.1-62.8% of
    pixels negative against 0.0-2.0% without it - and `dn_offset` separates them
    outright.

    The twentieth is `ecuador_poza_honda_control_2024` 2024-08-31, and it is
    worth stating exactly rather than hiding: that scene is 87% cloud, its
    median reflectance is 0.56 and its 1st percentile is 0.087, so it contains
    no pixel dark enough to place the zero point and BOTH conventions give
    plausible, non-negative reflectance. There is no pixel test that can decide
    it, and this one picks the specification convention, which on this backend
    is the wrong one.

    It costs nothing, and the reason is this assertion's second branch: a date
    that bright is a date under cloud, so its clear fraction (12.7%) is below
    `min_clear_fraction` (20%) and `build_evidence` refuses the pair before any
    index is computed. The wrong conversion never reaches a map.

    The two branches together are the real guarantee, which is why they are one
    test: a file is EITHER distinguishable by its dark tail, OR too cloudy to be
    used at all. A file that is neither is a genuine gap and must fail here.
    """
    op = OpticalParams()
    values, _, _ = _read(path)
    valid = np.isfinite(values)
    offset, _, method, decision = optical.dn_offset(
        values, valid, op, scene_names=["_N0510_"])
    decidable = decision["negative_fraction_with_offset"] > op.max_negative_fraction

    if decidable:
        assert decision["negative_fraction_without_offset"] <= op.max_negative_fraction
        assert (offset, method) == (0.0, "dn_offset_already_applied")
        return

    clear = _clear_fraction(path, op)
    assert clear < op.min_clear_fraction, (
        f"{path.name}: the two offset conventions are indistinguishable on this file "
        f"({decision}), so the conversion is a guess - and at {clear:.1%} clear this "
        f"date is usable, so the guess would reach a map. Needs a decision rule that "
        f"does not rest on the scene having dark pixels.")


@needs_cache
def test_the_whole_dev_set_was_downloaded_for_both_dates():
    """Two dates per event, two files per date: anything else is a partial fetch."""
    scl = sorted(EVAL_RUNS.glob("*/cache/s2_scl_*.tif"))
    assert len(FILES) == len(scl), "a reflectance file has no scene-classification layer"
    per_event: dict[str, int] = {}
    for path in FILES:
        per_event[path.parent.parent.name] = per_event.get(path.parent.parent.name, 0) + 1
    odd = {k: v for k, v in per_event.items() if v != 2}
    assert not odd, f"events without exactly one pre/post pair: {odd}"
