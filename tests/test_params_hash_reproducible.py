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
    """The configuration the dev set was scored at must re-hash to its own key.

    `chosen_parameters` is null when the radar pass found no feasible point,
    which is the state since the stricter control criterion was declared. The
    adopted configuration is then recorded by the second pass instead, and that
    is what must re-hash.
    """
    from eval import tune

    chosen = _chosen()
    params = chosen.get("chosen_parameters")
    if params is not None:
        dp = DetectParams(**params["detect"])
        rp = RunParams(**params["run"])
        assert params_hash(dp, rp) == params["params_hash"]
        return

    optical_pass = chosen.get("optical_pass") or {}
    point = optical_pass.get("chosen_configuration")
    assert point, ("neither chosen_parameters nor optical_pass.chosen_configuration is "
                   "recorded, so nothing says what the published rows describe")
    assert tune.full_point_hash(point, 3) == optical_pass["chosen_params_hash"]


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
    optical_only = {f for f in OpticalParams().__dataclass_fields__} - {
        # Names an optical parameter may legitimately SHARE with a radar one,
        # because both dataclasses have their own: excluded so this test cannot
        # fail on a coincidence of naming rather than a real leak.
        "search_days_before", "max_post_latency_days", "post_same_day_ok",
        "min_aoi_coverage"}
    assert {"fusion", "mndwi_water", "boa_add_offset", "max_negative_fraction"} <= optical_only
    leaked = radar_fields & optical_only
    assert not leaked, (
        f"{sorted(leaked)} belongs in OpticalParams: a field here re-hashes every "
        f"configuration already recorded in eval/results/")


def _radar_reachable() -> dict[str, dict]:
    """Every params_hash a point of the declared RADAR grid produces."""
    from eval import tune

    return {tune.point_hash(p): dict(p) for p in tune.grid_points()}


def _extent_reachable() -> dict[str, dict]:
    """Radar grid x the declared EXTENT grid, with optical not consulted.

    That is what `eval.run_scoreboard --no-optical` and a `--no-optical` CLI run
    produce once the shipped extent is not the hash baseline, so such a row is
    traceable to declared values and belongs in the reachable set.
    """
    from eval import tune

    out: dict[str, dict] = {}
    for sar in tune.grid_points():
        for extent in tune.extent_points():
            point = tune.combine(sar, extent)
            out[tune.full_point_hash(point)] = point
    return out


def _optical_reachable() -> dict[str, dict]:
    """Every params_hash a radar point CROSSED WITH an optical point produces.

    The optical pass scores exactly these configurations, so a row it writes
    must be one of them. They are listed separately from the radar-only ones so
    a reader of a failure can see at once which kind of configuration a
    published row claims to be.
    """
    from eval import tune

    out: dict[str, dict] = {}
    for sar in tune.grid_points():
        for opt in tune.optical_grid_points():
            point = tune.combine(sar, opt)
            out[tune.full_point_hash(point)] = point
    return out


def test_every_hash_in_the_results_csv_is_reproducible_from_a_declared_grid_point():
    """No published row may carry a hash this code cannot produce.

    A row is reproducible if some point of the declared radar grid, or of that
    grid crossed with the declared optical grid, hashes to it. Anything else
    means a number was published from a configuration this code can no longer
    emit - which is Rule 3.8 (reporting a number that cannot be traced).
    """
    radar, optical_too = _radar_reachable(), _optical_reachable()
    extent_too = _extent_reachable()
    with FLOOD_CSV.open(encoding="utf-8") as f:
        hashes = {row["params_hash"] for row in csv.DictReader(f) if row.get("params_hash")}
    assert hashes, "no rows to check"
    missing = sorted(hashes - set(radar) - set(optical_too) - set(extent_too))
    assert not missing, (
        f"params_hash {missing} appears in eval/results/flood.csv but no point of the "
        f"declared radar grid, or of it crossed with the optical grid, reproduces it "
        f"with this code")


def test_no_run_id_mixes_a_radar_only_and_an_optical_configuration():
    """One run_id, one configuration. The gate averages a run_id's rows.

    `eval/check_gates.py` takes the LATEST run_id matching a module's `where`
    and averages every row of it, so a run that wrote both its radar-only rows
    and its optical rows would publish the mean of two different detectors
    under one number - and nothing downstream could tell. `eval/tune.py`
    prevents it by choosing one set of rows (see the `adopted` branch in
    `main`), but that is one assignment standing between the project and a
    meaningless gate, so the invariant is also checked here against the data
    that actually got published.

    Mixed `params_hash` values within a run_id are EXPECTED and fine -
    leave-one-event-out gives each fold its own - so what is checked is the
    coarser thing that must never be mixed: whether the row consulted
    Sentinel-2 at all.
    """
    radar = _radar_reachable()
    with FLOOD_CSV.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    by_run: dict[str, set[bool]] = {}
    for row in rows:
        phash = row.get("params_hash")
        if not phash:
            continue
        by_run.setdefault(row["run_id"], set()).add(phash in radar)
    mixed = sorted(run for run, kinds in by_run.items() if len(kinds) > 1)
    assert not mixed, (
        f"run_id {mixed} contains BOTH radar-only and optical rows. check_gates averages "
        f"every row of the latest run_id, so this gate number is the mean of two different "
        f"configurations. One run must publish one configuration.")


def test_an_optical_row_implies_a_recorded_keep_verdict():
    """A published row that consulted Sentinel-2 must have a verdict behind it.

    Kept apart from the reachability test on purpose: that one says a row is
    TRACEABLE to a declared grid point, this one says a row produced by the
    more permissive configuration was actually ENDORSED by the adoption rule.
    Rows and verdict can only be made consistent by re-scoring, not by editing
    either one alone.

    Fusion was adopted on 2026-10-12, so the optical branch below is the live
    one; the radar-only branch stays because `eval/results/flood.csv` is
    append-only and still holds the pre-adoption rows, which must keep passing.
    """
    radar = _radar_reachable()
    with FLOOD_CSV.open(encoding="utf-8") as f:
        hashes = {row["params_hash"] for row in csv.DictReader(f) if row.get("params_hash")}
    optical_rows = sorted(hashes - set(radar))
    if not optical_rows:
        return
    optical_pass = (_chosen().get("optical_pass") or {})
    adoption = optical_pass.get("adoption") or {}
    dominance = optical_pass.get("dominance") or {}
    endorsed = (adoption.get("keep_fusion") is True
                or dominance.get("chosen") is not None)
    assert endorsed, (
        f"rows {optical_rows} were written by a configuration that consulted Sentinel-2, "
        f"but eval/chosen_params.json records neither an adoption verdict of KEEP nor a "
        f"Pareto-dominance winner. Either the rows or the record is wrong.")


# --- ExtentParams: changes the scored quantity, not the class map -------------

def test_the_baseline_extent_hashes_exactly_as_no_extent_at_all():
    """Baseline in, nothing added. Every params_hash published survives.

    The comparison is against EXTENT_HASH_BASELINE, never against
    `ExtentParams()`: the shipped default moved to debris-OUT on 2026-10-12, and
    a configuration that scores a different quantity must not share a hash with
    the rows published before the option existed.
    """
    from strandline.config import EXTENT_HASH_BASELINE

    dp, rp = DetectParams(), RunParams()
    bare = params_hash(dp, rp)
    assert params_hash(dp, rp, None, None) == bare
    assert params_hash(dp, rp, None, EXTENT_HASH_BASELINE) == bare
    assert "extent" not in params_dict(dp, rp, None, EXTENT_HASH_BASELINE)
    # And with optical enabled, the same property holds on top of it.
    op = OpticalParams()
    assert params_hash(dp, rp, op, EXTENT_HASH_BASELINE) == params_hash(dp, rp, op)


def test_taking_debris_out_of_the_extent_is_a_different_configuration():
    """It changes the scored quantity, so it MUST change the hash."""
    from strandline.config import ExtentParams

    dp, rp = DetectParams(), RunParams()
    out = ExtentParams(debris_in_flood_extent=False)
    assert params_hash(dp, rp, None, out) != params_hash(dp, rp)
    assert params_dict(dp, rp, None, out)["extent"] == {"debris_in_flood_extent": False}


def test_no_extent_field_leaked_into_the_radar_dataclasses():
    """The structural reason the hash above is stable. Same rule as optical."""
    from strandline.config import ExtentParams

    radar_fields = ({f for f in DetectParams().__dataclass_fields__}
                    | {f for f in RunParams().__dataclass_fields__})
    leaked = radar_fields & {f for f in ExtentParams().__dataclass_fields__}
    assert not leaked, (
        f"{sorted(leaked)} belongs in ExtentParams: a field on DetectParams or "
        f"RunParams re-hashes every configuration already recorded in eval/results/")


def test_the_extent_hash_baseline_is_a_constant_not_the_live_default():
    """The published hashes must depend on what was MEASURED, not on what ships.

    `params_dict` omits the `extent` section exactly when the configuration
    equals `EXTENT_HASH_BASELINE`. If that comparison keyed off `ExtentParams()`
    instead, then the day the shipped default moved - which it did, to
    debris-OUT, on 2026-10-12 - a configuration scoring a DIFFERENT quantity
    would silently hash like the rows already published. This test is the guard.
    """
    from strandline.config import EXTENT_HASH_BASELINE, ExtentParams

    assert EXTENT_HASH_BASELINE.debris_in_flood_extent is True, (
        "every hash published before this option existed was measured with debris "
        "INSIDE the flood extent; moving this constant invalidates all of them")
    dp, rp = DetectParams(), RunParams()
    bare = params_hash(dp, rp)
    assert params_hash(dp, rp, None, EXTENT_HASH_BASELINE) == bare
    # The shipped default is NOT the baseline any more, and must not be treated as it.
    if ExtentParams() != EXTENT_HASH_BASELINE:
        assert params_hash(dp, rp, None, ExtentParams()) != bare, (
            "the shipped default scores a different quantity from the published rows, "
            "so it must not share their hash")
        assert "extent" in params_dict(dp, rp, None, ExtentParams())


def test_a_scorer_hashes_the_extent_it_actually_used():
    """Passing None must not hash as the baseline while scoring the default."""
    import inspect
    from eval import run_scoreboard as rs

    src = inspect.getsource(rs.score_with_scene)
    assert "params_hash(dp, rp, op, extent)" in src, (
        "score_with_scene must hash the RESOLVED extent, not the caller's None")
    src = inspect.getsource(rs.score_event)
    assert "ep if ep is not None else ExtentParams()" in src, (
        "score_event must resolve the extent before hashing")
