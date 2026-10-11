"""Every PUBLISHED params_hash must still be reproducible by this code.

`params_hash` is the primary key that links a row in `eval/results/*.csv`, a
cached score under `runs/eval/<event>/scores/<hash>.json`, and every number
quoted in README.md and docs/tuning_log.md, to the exact parameter set that
produced it. If the hashing of a radar-only configuration ever changes, all of
that silently comes apart: `eval/run_scoreboard.py` rejects a cached score whose
hash differs, and the published numbers cite a file this code can no longer
emit.

This happened once, during the Sentinel-2 work: a new field on `DetectParams`
re-hashed every recorded configuration (d4f9b1791c82 -> 4d050525b109) while
provably changing no output pixel. The fix was to keep every optical parameter
in `OpticalParams`, whose section `config.params_dict` includes only when
optical is enabled. These tests exist so that cannot recur unnoticed.

A failure here is NOT fixed by updating the expected hash. It means either a
parameter was added to the wrong dataclass, or a genuine change of meaning needs
a re-score and a new recorded hash.
"""

import csv
import json
from pathlib import Path

from strandline.config import DetectParams, OpticalParams, RunParams, params_dict
from strandline.run import params_hash

ROOT = Path(__file__).resolve().parents[1]
CHOSEN = ROOT / "eval" / "chosen_params.json"
FLOOD_CSV = ROOT / "eval" / "results" / "flood.csv"


def _chosen() -> dict:
    return json.loads(CHOSEN.read_text(encoding="utf-8"))


def test_the_recorded_chosen_configuration_still_hashes_to_its_recorded_hash():
    """The configuration the dev set was scored at must re-hash to its own key."""
    chosen = _chosen()
    params = chosen["chosen_parameters"]
    dp = DetectParams(**params["detect"])
    rp = RunParams(**params["run"])
    assert params_hash(dp, rp) == params["params_hash"]


def test_radar_only_hashing_ignores_optical_entirely():
    """Optical parameters may not touch the hash of a radar-only run."""
    dp, rp = DetectParams(), RunParams()
    bare = params_hash(dp, rp)
    assert params_hash(dp, rp, None) == bare
    # Disabled optical: not consulted, so not part of the configuration.
    assert params_hash(dp, rp, OpticalParams(enabled=False)) == bare
    # A different optical threshold cannot move the radar-only hash either.
    assert params_hash(dp, rp, OpticalParams(enabled=False, mndwi_water=0.4)) == bare
    assert "optical" not in params_dict(dp, rp, OpticalParams(enabled=False))


def test_enabling_optical_is_a_different_configuration():
    """Asking for Sentinel-2 IS a different configuration, and must not hide it."""
    dp, rp = DetectParams(), RunParams()
    assert params_hash(dp, rp, OpticalParams(enabled=True)) != params_hash(dp, rp)
    assert "optical" in params_dict(dp, rp, OpticalParams(enabled=True))


def test_no_optical_field_leaked_into_the_radar_dataclasses():
    """The structural reason the hashes above are stable. Keep it that way."""
    radar_fields = ({f for f in DetectParams().__dataclass_fields__}
                    | {f for f in RunParams().__dataclass_fields__})
    optical_only = {"fusion", "enabled", "mndwi_water", "mndwi_rise_min", "ndvi_drop_min",
                    "swir_rise_min", "scl_valid_classes", "max_scene_cloud_cover",
                    "min_clear_fraction", "reflectance_scale", "boa_add_offset"}
    leaked = radar_fields & optical_only
    assert not leaked, (
        f"{sorted(leaked)} belongs in OpticalParams: a field here re-hashes every "
        f"configuration already recorded in eval/results/")


def test_every_hash_in_the_results_csv_is_a_radar_only_hash():
    """No published row may carry a hash this code cannot produce radar-only."""
    import itertools
    from dataclasses import replace
    from strandline.config import TUNE_GRIDS
    from eval.fetch_dev_data import dev_run_params
    from strandline.config import DEV_N_PRE

    reachable = set()
    for mode, grid in TUNE_GRIDS.items():
        keys = list(grid)
        for combo in itertools.product(*(grid[k] for k in keys)):
            point = {"detect.mode": mode}
            point.update(dict(zip(keys, combo)))
            d = {k.split(".", 1)[1]: v for k, v in point.items() if k.startswith("detect.")}
            r = {k.split(".", 1)[1]: v for k, v in point.items() if k.startswith("run.")}
            dp = replace(DetectParams(), **d)
            rp = replace(dev_run_params(n_pre=DEV_N_PRE), **r) if r else dev_run_params(
                n_pre=DEV_N_PRE)
            reachable.add(params_hash(dp, rp))

    with FLOOD_CSV.open(encoding="utf-8") as f:
        hashes = {row["params_hash"] for row in csv.DictReader(f) if row.get("params_hash")}
    assert hashes, "no rows to check"
    missing = sorted(hashes - reachable)
    assert not missing, (
        f"params_hash {missing} appears in eval/results/flood.csv but no point of the "
        f"declared radar grid reproduces it with this code")
