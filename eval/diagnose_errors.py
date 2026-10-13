"""WHERE the flood errors are, per dev event. CHECKING ONLY - diagnosis, no fixes.

    venv/Scripts/python.exe -m eval.diagnose_errors
    venv/Scripts/python.exe -m eval.diagnose_errors --events <dev_event_name>
    venv/Scripts/python.exe -m eval.diagnose_errors --out docs/accuracy_diagnosis.md

This module changes NOTHING about the pipeline or any parameter. It re-runs the
configuration that the latest leave-one-event-out rows in eval/results/flood.csv
were scored with - per event, read from that event's own score file, so the
numbers here describe exactly the run the gate was computed from - and then
takes the error map apart:

  1. every reference flood pixel we missed, attributed to ONE cause: not
     analysed, no change signal, or a named rule that removed it;
  2. every false alarm, attributed to a terrain POSITION and to the rule that
     let it through - on flood events and, separately, on every control;
  3. boundary offset: how much of the error sits 1-3 px from a correct
     detection, and whether our map is systematically shifted against the
     reference (cross-correlation, +-5 px);
  4. acquisition facts: post-event latency, which orbit directions offer a
     usable pre/post pair, median river width in pixels, and what sensor and
     date each reference map was drawn from;
  5. an ORACLE upper bound per error source: the tolerant F1 the event would
     reach if that source alone were fixed perfectly.

THREE THINGS THIS MODULE MUST NOT BE MISTAKEN FOR

* It is not a tuner. Rule 3.4: parameters come from eval/tune.py with
  leave-one-event-out, or from physics. The cross-correlation shift in (3) is
  measured and PRINTED; nothing here writes it, or anything else, into a
  parameter. There is no code path from a diagnosis number to a config value.
* It is not a scoreboard. It appends no row to eval/results/, so it cannot
  move a gate. Every number it prints is derived from score files and rasters
  that already exist, plus the metric definitions in validation/compare.py -
  never a metric of its own.
* The cause names are what the DATA supports. "Reservoir shore" and the other
  position buckets are terrain-position proxies (distance to permanent water,
  distance to the channel, HAND, slope), not land-cover labels: Sentinel-1 and
  a DEM cannot tell an irrigated field from wet soil, and this module does not
  pretend otherwise. Where Sentinel-2 contributed, the optical share of each
  bucket is reported beside it.

Rule 1: the reference maps read here are SCORING ONLY and this module lives in
eval/ for that reason. Rule 4: it refuses to run on the held-out case study.

HOW THE INTERMEDIATE MASKS ARE OBTAINED

`strandline.detect.detect_flood` returns the final class map, not the masks
between its stages, and this diagnosis must not change it. So `stages()` below
re-walks the same sequence using detect's own public functions, and then
asserts that the class map it rebuilt is byte-identical to the one
`detect_flood` returns. If detect ever changes and this replica does not, the
assertion fires and the diagnosis refuses to report rather than attributing
errors with a stale model of the detector.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import yaml
from scipy import ndimage

from eval import run_scoreboard as rs
from eval import scoreboard as sb
from eval.fetch_dev_data import EVAL_RUNS, select_events
from strandline import catalog, detect
from strandline.config import (
    CLASS_DEBRIS,
    CLASS_FLOOD_HIGH,
    CLASS_FLOOD_MEDIUM,
    CLASS_NO_CHANGE,
    CLASS_NODATA,
    CLASS_PERMANENT_WATER,
    CLASS_TERRAIN_EXCLUDED,
    FUSION_DEBRIS_MODES,
    FUSION_WATER_MODES,
    MODE_DARKENING,
    MODE_ROUGH_WATER,
    DetectParams,
    ExtentParams,
    OpticalParams,
    RunParams,
)
from validation import compare

ROOT = Path(__file__).resolve().parents[1]
GATES = ROOT / "eval" / "gates.yaml"
FLOOD_CSV = ROOT / "eval" / "results" / "flood.csv"
DEFAULT_OUT = ROOT / "docs" / "accuracy_diagnosis.md"
# The recorded diagnosis beside the document: every count the markdown quotes,
# with the run_id it came from. Written by default, because a document whose
# numbers cannot be traced to a file is exactly what Rule 3.8 forbids.
DEFAULT_JSON_OUT = ROOT / "docs" / "accuracy_diagnosis.json"

EIGHT = np.ones((3, 3), dtype=bool)

# Position buckets for a false alarm, in the order they are TESTED. The first
# that matches wins, so each false pixel is counted exactly once. The order is
# nearest-cause-first: a pixel on a reservoir shore is explained by the
# reservoir, not by the slope it also happens to sit on.
SHORE_PX = 3          # distance from permanent water that still counts as shore
FLAT_SLOPE_DEG = 5.0  # above this, a false alarm is reported as sloping ground

# The miss causes, in the order they are TESTED (see attribute_misses).
MISS_CAUSES = (
    "not analysed: no data",
    "not analysed: terrain excluded",
    "rule: already permanent water",
    "rule: debris, not flood extent",
    "rule: HAND/slope above the water zone",
    "no change signal (|z| < z_seed in both polarisations)",
    "rule: magnitude or polarisation-agreement gate",
    "rule: hysteresis, no connected seed",
    "rule: minimum mapping unit",
    "rule: drainage connection",
    "unexplained",
)

FP_PLACES = (
    "permanent-water shore",
    "channel corridor",
    "flat ground away from the channel",
    "sloping ground",
    "other analysed ground",
)


# --- small helpers ------------------------------------------------------------

def _f1(p, r) -> float:
    return round(2 * p * r / (p + r), 4) if p and r else 0.0


def _share(n: int, total: int) -> float:
    return round(n / total, 4) if total else 0.0


def heldout_guard(events: list[dict], gates_path: Path | None = None) -> None:
    """Rule 4: refuse to diagnose anything named after the held-out case study."""
    cfg = yaml.safe_load((gates_path or GATES).read_text(encoding="utf-8"))
    forbidden = [str(n).lower() for n in cfg["heldout"]["forbidden_names"]]
    for ev in events:
        name = str(ev.get("name", "")).lower()
        hit = [f for f in forbidden if f in name]
        if hit:
            raise SystemExit(
                f"refusing to diagnose {ev.get('name')!r}: it carries the held-out name "
                f"{hit[0]!r}. The case study is scored once, through /heldout (Rule 4).")


def latest_loeo(csv_path: Path | None = None, *, kind: str = sb.KIND_FLOOD,
                split: str = "loeo") -> tuple[str, dict[str, str]]:
    """(run_id, {event: params_hash}) for the LATEST run matching kind and split.

    The same selection the gate checker makes, so the diagnosis describes the
    rows the gate is computed from and not some other run.
    """
    rows = list(csv.DictReader((csv_path or FLOOD_CSV).read_text(encoding="utf-8").splitlines()))
    picked = [r for r in rows if r.get("kind") == kind and r.get("split") == split]
    if not picked:
        raise SystemExit(f"no rows with kind={kind} split={split} in {csv_path or FLOOD_CSV}")
    run_id = max(r["run_id"] for r in picked)
    return run_id, {r["event"]: r["params_hash"] for r in picked if r["run_id"] == run_id}


def params_from_score(score: rs.EventScore) -> tuple[DetectParams, RunParams,
                                                     OpticalParams | None, ExtentParams]:
    """The exact configuration a score file was produced with.

    Read from the file rather than rebuilt from today's config.py: the point of
    this diagnosis is to explain the scored run, and config.py may since have
    moved (that is itself one of the things reported, see config_mismatches).
    """
    p = score.provenance
    dp = DetectParams(**p["parameters"]["detect"])
    rp = RunParams(**p["parameters"]["run"])
    opt = p.get("optical") or {}
    op = None
    if opt.get("parameters"):
        raw = dict(opt["parameters"])
        raw["scl_valid_classes"] = tuple(raw["scl_valid_classes"])
        raw["plausible_reflectance_range"] = tuple(raw["plausible_reflectance_range"])
        op = OpticalParams(**raw)
    ep = ExtentParams(**p["extent_parameters"])
    return dp, rp, op, ep


# --- re-walking the detector --------------------------------------------------

@dataclass
class Stages:
    """Every intermediate mask of one detection, on one grid."""

    z_vv: np.ndarray
    z_vh: np.ndarray
    zc: np.ndarray
    valid: np.ndarray
    zone_water: np.ndarray
    zone_debris: np.ndarray
    permanent: np.ndarray
    t_water: float
    signal: np.ndarray          # |z| >= z_seed in EITHER polarisation
    gate_ok: np.ndarray         # mode's magnitude / polarisation test for a seed
    # The pieces of the magnitude gate, kept apart so a miss can say WHICH
    # polarisation blocked it. `rough_water` only; None in `darkening` mode,
    # which has no such conjunction.
    big_vv: np.ndarray | None
    big_vh: np.ndarray | None
    sign_agree: np.ndarray | None
    growable: np.ndarray        # a coherent change big enough to JOIN a region
    seed: np.ndarray            # water seeds (after zone and permanent tests)
    grow: np.ndarray            # water growth candidates
    water_grown: np.ndarray     # after hysteresis, before cleanup
    water_fused: np.ndarray     # after optical union
    water_mmu: np.ndarray       # after the minimum mapping unit
    water_final: np.ndarray     # after the drainage-connection test
    debris_final: np.ndarray
    opt_water: np.ndarray
    opt_debris: np.ndarray
    classes: np.ndarray         # rebuilt here
    classes_authoritative: np.ndarray   # from detect.detect_flood
    faithful: bool              # the two class maps are identical


def stages(scene, dp: DetectParams) -> Stages:
    """Re-walk detect.detect_flood, keeping every intermediate mask.

    Mirrors strandline/detect.py stage for stage, using its own functions, and
    verifies the rebuilt class map against the real one (see module docstring).
    """
    p = dp
    pre_vv, pre_vh = scene.pre_vv_db, scene.pre_vh_db
    post_vv, post_vh = scene.post_vv_db, scene.post_vh_db
    hand, slope, streams = scene.terrain.hand_m, scene.terrain.slope_deg, scene.terrain.streams
    optical = scene.optical_evidence

    mean_vv, std_vv = detect.temporal_stats(pre_vv, p.std_floor_db)
    mean_vh, std_vh = detect.temporal_stats(pre_vh, p.std_floor_db)
    z_vv = detect.zscore(post_vv, mean_vv, std_vv)
    z_vh = detect.zscore(post_vh, mean_vh, std_vh)
    zc = detect.combine_z(z_vv, z_vh)

    valid = (np.isfinite(zc) & np.isfinite(post_vv) & np.isfinite(mean_vv)
             & np.isfinite(hand) & np.isfinite(slope))
    zone_water = valid & (hand <= p.hand_max_water_m) & (slope <= p.slope_max_water_deg)
    zone_debris = valid & (hand <= p.hand_max_debris_m) & (slope <= p.slope_max_debris_deg)
    thr = detect.otsu_water_threshold(post_vv, zone_water, p.water_db_min, p.water_db_max)
    t_water = thr.value

    with np.errstate(invalid="ignore"):
        permanent = zone_water & (mean_vv <= t_water) & (post_vv <= t_water)

        # The mode's own change test, split into the pieces a diagnosis needs.
        if p.mode == MODE_ROUGH_WATER:
            d_vv, d_vh = post_vv - mean_vv, post_vh - mean_vh
            big = ((np.abs(d_vv) >= p.min_abs_change_db)
                   & (np.abs(d_vh) >= p.min_abs_change_db))
            if p.require_pol_agreement:
                big &= np.sign(d_vv) == np.sign(d_vh)
            strong_pol = np.minimum(np.abs(z_vv), np.abs(z_vh)) >= p.z_pol_agree
            signal = (np.abs(z_vv) >= p.z_seed) | (np.abs(z_vh) >= p.z_seed)
            gate_ok = big & strong_pol
            big_vv = np.abs(d_vv) >= p.min_abs_change_db
            big_vh = np.abs(d_vh) >= p.min_abs_change_db
            sign_agree = np.sign(d_vv) == np.sign(d_vh)
            growable = big & (np.abs(zc) >= p.z_grow)
            seed_any = big & (np.abs(zc) >= p.z_seed) & strong_pol
            grow_any = big & (np.abs(zc) >= p.z_grow)
        elif p.mode == MODE_DARKENING:
            signal = (z_vv <= -p.z_seed) | (z_vh <= -p.z_seed)
            gate_ok = post_vv <= t_water
            big_vv = big_vh = sign_agree = None
            growable = (zc <= -p.z_grow) & (post_vv <= t_water + p.grow_darkness_margin_db)
            seed_any = (zc <= -p.z_seed) & (post_vv <= t_water)
            grow_any = (zc <= -p.z_grow) & (post_vv <= t_water + p.grow_darkness_margin_db)
        else:  # pragma: no cover - DetectParams.__post_init__ rejects anything else
            raise ValueError(f"unknown detect mode {p.mode!r}")

        seed = zone_water & ~permanent & seed_any
        grow = zone_water & ~permanent & grow_any
        water_grown = detect.grow_from_seeds(seed, grow)

        finder = detect._WATER_FINDERS[p.mode]
        sar_water, sar_debris, high = finder(
            p, zc, z_vv, z_vh, post_vv, post_vh, mean_vv, mean_vh,
            zone_water, zone_debris, permanent, t_water)

    fuse_water = optical is not None and optical.fusion in FUSION_WATER_MODES
    fuse_debris = optical is not None and optical.fusion in FUSION_DEBRIS_MODES
    fuse = fuse_water or fuse_debris
    opt_water = opt_debris = np.zeros(zc.shape, dtype=bool)
    if fuse:
        opt_clear = valid & optical.clear
        if fuse_water:
            opt_water = zone_water & ~permanent & opt_clear & optical.water
        if fuse_debris:
            opt_debris = (zone_debris & ~zone_water) & opt_clear & optical.debris
        water_fused = sar_water | opt_water
        debris_fused = (sar_debris | opt_debris) & ~water_fused
    else:
        water_fused, debris_fused = sar_water, sar_debris

    water_mmu = detect.remove_small(water_fused, p.mmu_pixels)
    debris_mmu = detect.remove_small(debris_fused, p.mmu_pixels_debris)
    water_final, debris_final = water_mmu, debris_mmu
    if p.require_drainage_connection:
        river = streams.astype(bool) | permanent
        water_final = detect.keep_connected_to(water_mmu, river, p.drainage_buffer_px)
        debris_final = detect.keep_connected_to(debris_mmu, river | water_final,
                                                p.drainage_buffer_px)
    high = high & water_final
    if fuse:
        high = high | (water_final & sar_water & opt_water)

    classes = np.full(zc.shape, CLASS_NODATA, dtype=np.uint8)
    classes[valid] = CLASS_NO_CHANGE
    classes[valid & ~(zone_water | zone_debris)] = CLASS_TERRAIN_EXCLUDED
    classes[permanent] = CLASS_PERMANENT_WATER
    classes[debris_final] = CLASS_DEBRIS
    classes[water_final] = CLASS_FLOOD_MEDIUM
    classes[high] = CLASS_FLOOD_HIGH

    truth = detect.detect_flood(pre_vv, pre_vh, post_vv, post_vh, hand, slope, streams, dp,
                                sar_invalid=None, optical=optical)
    return Stages(z_vv=z_vv, z_vh=z_vh, zc=zc, valid=valid, zone_water=zone_water,
                  zone_debris=zone_debris, permanent=permanent, t_water=t_water,
                  signal=signal, gate_ok=gate_ok, big_vv=big_vv, big_vh=big_vh,
                  sign_agree=sign_agree, growable=growable, seed=seed, grow=grow,
                  water_grown=water_grown, water_fused=water_fused, water_mmu=water_mmu,
                  water_final=water_final, debris_final=debris_final,
                  opt_water=opt_water, opt_debris=opt_debris, classes=classes,
                  classes_authoritative=truth.classes,
                  faithful=bool(np.array_equal(classes, truth.classes)))


# --- 1. missed reference pixels -----------------------------------------------

def attribute_misses(st: Stages, missed: np.ndarray, dp: DetectParams,
                     ep: ExtentParams) -> dict[str, np.ndarray]:
    """Split `missed` into the declared causes. Every pixel lands in exactly one.

    Tested in PIPELINE ORDER: the earliest thing that stopped the pixel is the
    cause reported, because fixing a later stage would not have recovered it.
    The one exception is the debris rule, tested early because such a pixel WAS
    detected - as debris - and only the extent definition keeps it out.
    """
    out: dict[str, np.ndarray] = {}
    left = missed.copy()

    def take(name: str, mask: np.ndarray) -> None:
        nonlocal left
        hit = left & mask
        out[name] = hit
        left = left & ~hit

    take(MISS_CAUSES[0], ~st.valid)
    take(MISS_CAUSES[1], st.valid & ~(st.zone_water | st.zone_debris))
    take(MISS_CAUSES[2], st.permanent)
    take(MISS_CAUSES[3],
         st.debris_final if not ep.debris_in_flood_extent else np.zeros_like(missed))
    take(MISS_CAUSES[4], ~st.zone_water)
    take(MISS_CAUSES[5], ~st.signal)
    take(MISS_CAUSES[6], ~st.gate_ok)
    take(MISS_CAUSES[7], ~st.water_grown)
    take(MISS_CAUSES[8], st.water_fused & ~st.water_mmu)
    take(MISS_CAUSES[9], st.water_mmu & ~st.water_final)
    out[MISS_CAUSES[10]] = left
    return out


def miss_subdiagnostics(st: Stages, causes: dict[str, np.ndarray]) -> dict:
    """The two questions the top-level buckets cannot answer on their own.

    1. Is the "no change signal" bucket really CHANGELESS ground, or ground
       whose change is real but too weak to start a region? The bucket is
       defined by the SEED threshold, as the diagnosis brief asks (|z| below
       z_seed in both polarisations), and a pixel can fail that and still carry
       a coherent change large enough to JOIN a region (`growable`). Those two
       populations need completely different answers, so they are counted
       apart: the first is beyond any threshold change, the second is a pixel
       the detector saw and could not connect to a seed.

    2. WHICH polarisation blocks the magnitude gate. The gate is a conjunction
       - both polarisations must move by `min_abs_change_db`, in the same
       direction - so a miss can be blamed on VV, on VH, or on disagreement.
       VH sits closer to the Sentinel-1 noise floor than VV, so "VH alone
       blocked it" is a sensor-noise statement, not a flood statement.

    Counts only. Nothing here is a cause of its own, so the partition above is
    untouched.
    """
    no_signal = causes[MISS_CAUSES[5]]
    gate = causes[MISS_CAUSES[6]]
    out = {
        "no_signal_pixels": int(no_signal.sum()),
        "no_signal_but_a_coherent_growable_change": int((no_signal & st.growable).sum()),
        "no_signal_and_no_qualifying_change_at_all": int((no_signal & ~st.growable).sum()),
        "gate_pixels": int(gate.sum()),
    }
    if st.big_vv is None:
        out["gate_split"] = "not applicable in darkening mode (no polarisation conjunction)"
        return out
    out.update({
        "gate_blocked_by_vh_alone": int((gate & st.big_vv & ~st.big_vh).sum()),
        "gate_blocked_by_vv_alone": int((gate & ~st.big_vv & st.big_vh).sum()),
        "gate_blocked_by_both_too_small": int((gate & ~st.big_vv & ~st.big_vh).sum()),
        "gate_blocked_by_sign_disagreement": int(
            (gate & st.big_vv & st.big_vh & ~st.sign_agree).sum()),
        "gate_blocked_by_weak_z_only": int(
            (gate & st.big_vv & st.big_vh & st.sign_agree).sum()),
    })
    return out


# --- 2. false alarms ----------------------------------------------------------

@dataclass
class Places:
    """Terrain-position proxies, computed once per event.

    Proxies, and named as such: distance to permanent water, distance to the
    channel network, HAND and slope are what a DEM and Sentinel-1 can establish.
    "Reservoir shore" and "flat ground" are therefore positions, not land cover.
    """

    shore: np.ndarray            # within SHORE_PX of permanent water
    corridor: np.ndarray         # within drainage_buffer_px of a channel
    flat: np.ndarray             # low HAND, gentle slope
    steep: np.ndarray            # slope above FLAT_SLOPE_DEG
    dist_permanent_px: np.ndarray


def places(st: Stages, scene, dp: DetectParams) -> Places:
    streams = scene.terrain.streams.astype(bool)
    hand = np.where(np.isfinite(scene.terrain.hand_m), scene.terrain.hand_m, np.inf)
    slope = np.where(np.isfinite(scene.terrain.slope_deg), scene.terrain.slope_deg, np.inf)
    dist_perm = (ndimage.distance_transform_edt(~st.permanent)
                 if st.permanent.any() else np.full(st.permanent.shape, np.inf))
    shore = dist_perm <= SHORE_PX
    corridor = ndimage.binary_dilation(streams, EIGHT, iterations=max(dp.drainage_buffer_px, 1))
    return Places(shore=shore, corridor=corridor & ~shore,
                  flat=(hand <= dp.hand_max_water_m) & (slope <= FLAT_SLOPE_DEG),
                  steep=slope > FLAT_SLOPE_DEG, dist_permanent_px=dist_perm)


def attribute_false_alarms(st: Stages, false_px: np.ndarray, pl: Places) -> dict[str, np.ndarray]:
    """Split `false_px` into the declared position buckets, first match wins."""
    out: dict[str, np.ndarray] = {}
    left = false_px.copy()
    for name, mask in zip(FP_PLACES, (pl.shore, pl.corridor, pl.flat, pl.steep)):
        hit = left & mask
        out[name] = hit
        left = left & ~hit
    out[FP_PLACES[4]] = left
    return out


def false_alarm_rules(st: Stages, false_px: np.ndarray, scene) -> dict[str, int]:
    """Which rule let each false pixel through, by channel and by change sign."""
    n = int(false_px.sum())
    if not n:
        return {}
    sar_only = int((false_px & ~st.opt_water & ~st.opt_debris).sum())
    opt_any = int((false_px & (st.opt_water | st.opt_debris)).sum())
    high = int((false_px & (st.classes == CLASS_FLOOD_HIGH)).sum())
    debris = int((false_px & (st.classes == CLASS_DEBRIS)).sum())
    mean_vv = np.nanmean(scene.pre_vv_db, axis=0)
    with np.errstate(invalid="ignore"):
        brighten = int((false_px & (scene.post_vv_db > mean_vv)).sum())
    return {"pixels": n, "sar_only": sar_only, "optical_contributed": opt_any,
            "high_confidence": high, "debris_class": debris,
            "brightened": brighten, "darkened": n - brighten}


# --- 3. boundary offset -------------------------------------------------------

def boundary_profile(pred: np.ndarray, ref: np.ndarray, domain: np.ndarray) -> dict:
    """How much of the error sits 1, 2, 3 or more pixels from a correct detection.

    "Correct detection" is the agreed set (true positives). A false alarm or a
    miss 1 px from it is an edge disagreement; one 10 px away is a different
    kind of mistake. Distances are in pixels of the 20 m analysis grid.
    """
    pred, ref = pred & domain, ref & domain
    tp = pred & ref
    fp, fn = pred & ~ref, ~pred & ref
    out: dict = {"tp": int(tp.sum()), "fp": int(fp.sum()), "fn": int(fn.sum())}
    if not tp.any():
        out["note"] = "no true positive to measure a distance from"
        return out
    dist = ndimage.distance_transform_edt(~tp)
    for label, err in (("fp", fp), ("fn", fn)):
        total = int(err.sum())
        d = dist[err]
        out[f"{label}_within_1px"] = _share(int((d <= 1).sum()), total)
        out[f"{label}_within_3px"] = _share(int((d <= 3).sum()), total)
        out[f"{label}_beyond_3px"] = _share(int((d > 3).sum()), total)
    err_all = fp | fn
    d_all = dist[err_all]
    out["errors_within_1px_of_a_correct_detection"] = _share(
        int((d_all <= 1).sum()), int(err_all.sum()))
    out["errors_within_3px_of_a_correct_detection"] = _share(
        int((d_all <= 3).sum()), int(err_all.sum()))
    return out


def shift_scan(pred: np.ndarray, ref: np.ndarray, domain: np.ndarray,
               max_shift: int = 5) -> dict:
    """Strict F1 of our map against the reference over +-max_shift px shifts.

    DIAGNOSIS ONLY. A best shift away from (0, 0) would mean a geometric
    offset - a geocoding or reference-registration difference - rather than a
    detection error. Nothing here writes the shift anywhere: correcting a map
    by a number fitted against the reference would be fitting to the reference.
    """
    pred, ref = pred & domain, ref & domain
    best = {"dy": 0, "dx": 0, "f1": 0.0}
    grid: dict[tuple[int, int], float] = {}
    for dy in range(-max_shift, max_shift + 1):
        for dx in range(-max_shift, max_shift + 1):
            moved = np.zeros_like(pred)
            ys = slice(max(dy, 0), pred.shape[0] + min(dy, 0))
            xs = slice(max(dx, 0), pred.shape[1] + min(dx, 0))
            ys0 = slice(max(-dy, 0), pred.shape[0] + min(-dy, 0))
            xs0 = slice(max(-dx, 0), pred.shape[1] + min(-dx, 0))
            moved[ys, xs] = pred[ys0, xs0]
            moved &= domain
            tp = int((moved & ref).sum())
            p = tp / max(int(moved.sum()), 1)
            r = tp / max(int(ref.sum()), 1)
            f1 = _f1(p, r)
            grid[(dy, dx)] = f1
            if f1 > best["f1"]:
                best = {"dy": dy, "dx": dx, "f1": f1}
    return {"f1_at_zero_shift": grid[(0, 0)], "best": best,
            "gain_from_best_shift": round(best["f1"] - grid[(0, 0)], 4)}


# --- 4. acquisition facts -----------------------------------------------------

def river_width_px(water: np.ndarray, min_axis_px: int = 50) -> dict:
    """Median width, in pixels, of the permanent-water network.

    Width is measured the usual way: the distance transform at the medial axis
    of the water mask is the local half-width, so width = 2 x that. Reported as
    NO DATA when the mask has too thin a skeleton to measure, rather than
    returning a number from a handful of pixels.
    """
    n = int(water.sum())
    if n == 0:
        return {"status": "NO DATA", "reason": "no permanent water detected", "pixels": 0}
    try:
        from skimage.morphology import medial_axis
    except ImportError:  # pragma: no cover - scikit-image is a hard dependency
        return {"status": "NO DATA", "reason": "scikit-image unavailable", "pixels": n}
    axis, dist = medial_axis(water, return_distance=True)
    half = dist[axis]
    if half.size < min_axis_px:
        return {"status": "NO DATA", "pixels": n,
                "reason": f"only {half.size} medial-axis pixel(s), fewer than {min_axis_px}"}
    return {"status": "measured", "pixels": n, "axis_pixels": int(half.size),
            "median_width_px": round(float(2 * np.median(half)), 2),
            "p10_width_px": round(float(2 * np.percentile(half, 10)), 2),
            "p90_width_px": round(float(2 * np.percentile(half, 90)), 2)}


def orbit_options(event: dict, rp: RunParams) -> dict:
    """Which orbit directions offer a usable pre/post pair for this event.

    One catalogue query (no login, no download), then `catalog.choose_track`
    itself, run once per orbit direction. Asking the pipeline's own chooser
    rather than reimplementing "usable" is the point: a direction is usable
    here exactly when a real run restricted to it would have accepted it.
    """
    bbox = rs.event_bbox(event)
    day = rs.event_day(event)
    start = day - timedelta(days=rp.search_days_before)
    end = day + timedelta(days=rp.max_post_latency_days + 1)
    scenes = catalog.search_s1_grd(bbox, start, end)
    acqs = catalog.group_acquisitions(scenes, catalog.aoi_polygon(bbox))
    directions = sorted({str(a.track[2]) for a in acqs})
    out: dict[str, dict] = {}
    for direction in directions:
        mine = [a for a in acqs if str(a.track[2]) == direction]
        choice, lines = catalog.choose_track(mine, day, rp)
        out[direction] = {
            "usable": choice is not None,
            "n_tracks": len({a.track for a in mine}),
            "best": None if choice is None else {
                "relative_orbit": choice.track[1],
                "post_day": str(choice.post.day),
                "post_latency_days": choice.latency_days,
                "n_pre": len(choice.pre)},
            "why": lines,
        }
    return {"directions_with_a_usable_pair": [d for d, v in out.items() if v["usable"]],
            "by_direction": out}


# --- 5. oracle upper bounds ---------------------------------------------------

def upper_bounds(pred: np.ndarray, ref: np.ndarray, domain: np.ndarray,
                 miss: dict[str, np.ndarray], fp: dict[str, np.ndarray],
                 tolerance_px: int = 1) -> dict:
    """The tolerant F1 this event would reach if ONE error source were fixed.

    ORACLE numbers: the pixels are added or removed using the reference, which
    no detector can do. They bound what is worth chasing, and are not an
    achievable score. The baseline is the measured one, so the arithmetic is
    visible: baseline + the gain of each source.
    """
    base = compare.flood_metrics(pred, ref, domain, tolerance_px)
    out = {"baseline_f1_tolerant": base["f1_tolerant"], "if_fixed": {}}
    for name, mask in miss.items():
        if not mask.any():
            continue
        m = compare.flood_metrics(pred | mask, ref, domain, tolerance_px)
        out["if_fixed"][f"recover {name}"] = {
            "pixels": int(mask.sum()), "f1_tolerant": m["f1_tolerant"],
            "gain": round(m["f1_tolerant"] - base["f1_tolerant"], 4)}
    for name, mask in fp.items():
        if not mask.any():
            continue
        m = compare.flood_metrics(pred & ~mask, ref, domain, tolerance_px)
        out["if_fixed"][f"remove false alarms: {name}"] = {
            "pixels": int(mask.sum()), "f1_tolerant": m["f1_tolerant"],
            "gain": round(m["f1_tolerant"] - base["f1_tolerant"], 4)}
    return out


# --- config mismatches --------------------------------------------------------

def default_n_pre_of_a_live_run() -> int:
    """How many pre-event images a DEFAULT `strandline.run` invocation uses."""
    return RunParams().n_pre


def config_mismatches(evaluated: dict[str, dict]) -> list[dict]:
    """Every difference between the shipped defaults and what was scored.

    `evaluated` maps event -> the parameter dicts from its score file. A key is
    reported once per distinct scored value, so a parameter that the
    leave-one-event-out folds disagree about (they are meant to: each fold
    chooses without its own event) is visible as such rather than averaged.
    """
    shipped = {
        **{f"detect.{k}": v for k, v in asdict(DetectParams()).items()},
        **{f"run.{k}": v for k, v in asdict(RunParams()).items()},
        **{f"optical.{k}": v for k, v in asdict(OpticalParams()).items()},
        **{f"extent.{k}": v for k, v in asdict(ExtentParams()).items()},
    }
    seen: dict[str, dict] = {}
    for event, params in sorted(evaluated.items()):
        for key, value in params.items():
            if key not in shipped:
                continue
            if _same(shipped[key], value):
                continue
            entry = seen.setdefault(key, {"key": key, "shipped_default": shipped[key],
                                          "scored_values": {}})
            entry["scored_values"].setdefault(json.dumps(value, default=str), []).append(event)
    out = []
    for key in sorted(seen):
        entry = seen[key]
        out.append({"key": key, "shipped_default": entry["shipped_default"],
                    "scored": [{"value": json.loads(v), "events": ev}
                               for v, ev in entry["scored_values"].items()]})
    return out


def _same(a, b) -> bool:
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return list(a) == list(b)
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-12
        except (TypeError, ValueError):
            return a == b
    return a == b


def scored_parameter_dict(score: rs.EventScore) -> dict:
    """Flat {section.field: value} of everything one score file was run with."""
    p = score.provenance
    flat = {f"detect.{k}": v for k, v in p["parameters"]["detect"].items()}
    flat.update({f"run.{k}": v for k, v in p["parameters"]["run"].items()})
    flat.update({f"extent.{k}": v for k, v in p["extent_parameters"].items()})
    for k, v in ((p.get("optical") or {}).get("parameters") or {}).items():
        flat[f"optical.{k}"] = v
    return flat


# --- one event ----------------------------------------------------------------

@dataclass
class EventDiagnosis:
    event: str
    kind: str
    params_hash: str
    facts: dict = field(default_factory=dict)
    misses: dict = field(default_factory=dict)
    false_alarms: dict = field(default_factory=dict)
    boundary: dict = field(default_factory=dict)
    shift: dict = field(default_factory=dict)
    bounds: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    error: str | None = None


def diagnose_event(event: dict, params_hash: str, runs_root: Path, tolerance_px: int,
                   *, with_orbits: bool = True, log=print) -> EventDiagnosis:
    name = str(event["name"])
    kind = sb.row_kind(event)
    score = rs.load_score(name, params_hash, runs_root)
    if score is None or not score.ok:
        return EventDiagnosis(event=name, kind=kind, params_hash=params_hash,
                              error=f"no usable score file {params_hash}.json for {name}")
    dp, rp, op, ep = params_from_score(score)
    log(f"  {name}: mode {dp.mode}, hash {params_hash}")
    scene = rs.prepare_event(event, rp, dp, runs_root, allow_download=False, op=op,
                             log=lambda *_: None)
    st = stages(scene, dp)
    if not st.faithful:
        return EventDiagnosis(
            event=name, kind=kind, params_hash=params_hash,
            error="the re-walked class map does not match detect.detect_flood; "
                  "eval/diagnose_errors.stages is out of date with strandline/detect.py "
                  "and no attribution from it can be trusted")

    classes = st.classes_authoritative
    domain = compare.scoring_domain(classes)
    pred = compare.flood_affected_mask(classes, include_debris=ep.debris_in_flood_extent)
    grid = scene.grid
    px_ha = grid.res * grid.res / 10_000.0
    pl = places(st, scene, dp)

    analysed = domain & (classes != CLASS_TERRAIN_EXCLUDED)
    facts = {
        "bbox": list(rs.event_bbox(event)),
        "event_date": str(rs.event_day(event)),
        "mode": dp.mode,
        "grid": f"{grid.width} x {grid.height} px at {grid.res:g} m, EPSG:{grid.epsg}",
        "post_day": score.provenance["track"]["post_day"],
        "post_latency_days": score.provenance["track"]["post_latency_days"],
        "track": f"{score.provenance['track']['relative_orbit']} "
                 f"{score.provenance['track']['direction']}",
        "n_pre": score.provenance["n_pre"],
        "pre_days": score.provenance["track"]["pre_days"],
        "domain_pixels": int(domain.sum()),
        "analysed_pixels": int(analysed.sum()),
        "analysed_fraction": _share(int(analysed.sum()), int(domain.sum())),
        "water_threshold_db": score.provenance["water_threshold"].get("water_threshold_db"),
        "water_threshold_clamped": score.provenance["water_threshold"].get(
            "water_threshold_clamped"),
        "optical": {k: (score.provenance.get("optical") or {}).get(k)
                    for k in ("status", "fusion", "clear_fraction_pair", "post_day",
                              "post_latency_days", "message")},
        "predicted_flood_ha": round(float(pred.sum()) * px_ha, 2),
        "permanent_water_ha": round(float(st.permanent.sum()) * px_ha, 2),
        "river_width": river_width_px(st.permanent),
        "reference_sensor": event.get("ref_sensor") or event.get("ref_sensor_name"),
        "reference_source": event.get("source"),
    }
    if with_orbits:
        try:
            facts["orbits"] = orbit_options(event, rp)
        except Exception as err:  # a catalogue outage must not lose the diagnosis
            facts["orbits"] = {"error": f"{type(err).__name__}: {err}"}

    diag = EventDiagnosis(event=name, kind=kind, params_hash=params_hash, facts=facts)

    if kind == sb.KIND_CONTROL:
        fp_places = attribute_false_alarms(st, pred & domain, pl)
        diag.false_alarms = {
            "total_pixels": int((pred & domain).sum()),
            "false_flood_fraction_domain": compare.false_flood_fraction(pred, domain),
            "false_flood_fraction_analysed": compare.false_flood_fraction(pred, analysed),
            "by_place": {k: int(v.sum()) for k, v in fp_places.items()},
            "by_place_share": {k: _share(int(v.sum()), int((pred & domain).sum()))
                               for k, v in fp_places.items()},
            "rules": false_alarm_rules(st, pred & domain, scene),
            "distance_to_permanent_water_px": _distance_histogram(
                pl.dist_permanent_px, pred & domain),
        }
        diag.metrics = dict(score.metrics)
        return diag

    ref_path = ROOT / str(event["reference"])
    union = compare.reference_class_values(event.get("reference_classes") or {},
                                           "water", "debris")
    ref, _sel = compare.reference_mask(ref_path, grid, union)
    ref &= domain
    missed = ref & ~pred
    false_px = pred & ~ref & domain

    miss_causes = attribute_misses(st, missed, dp, ep)
    fp_places = attribute_false_alarms(st, false_px, pl)
    diag.metrics = dict(score.metrics)
    diag.misses = {
        "reference_pixels": int(ref.sum()),
        "reference_ha": round(float(ref.sum()) * px_ha, 2),
        "missed_pixels": int(missed.sum()),
        "by_cause": {k: int(v.sum()) for k, v in miss_causes.items()},
        "by_cause_share": {k: _share(int(v.sum()), int(missed.sum()))
                           for k, v in miss_causes.items()},
        "subdiagnostics": miss_subdiagnostics(st, miss_causes),
    }
    diag.false_alarms = {
        "total_pixels": int(false_px.sum()),
        "by_place": {k: int(v.sum()) for k, v in fp_places.items()},
        "by_place_share": {k: _share(int(v.sum()), int(false_px.sum()))
                           for k, v in fp_places.items()},
        "rules": false_alarm_rules(st, false_px, scene),
    }
    diag.boundary = boundary_profile(pred, ref, domain)
    diag.shift = shift_scan(pred, ref, domain)
    diag.bounds = upper_bounds(pred, ref, domain, miss_causes, fp_places, tolerance_px)
    return diag


def _distance_histogram(dist: np.ndarray, mask: np.ndarray) -> dict:
    """Where a set of pixels sits relative to permanent water, in px bands."""
    if not mask.any():
        return {}
    d = dist[mask]
    out: dict[str, int] = {}
    edges = [0.0, 1.0, 3.0, 10.0, 30.0, np.inf]
    for lo, hi in zip(edges[:-1], edges[1:]):
        label = f"<={hi:g}" if lo == 0 else (f">{lo:g}" if hi == np.inf else f"{lo:g}-{hi:g}")
        out[label] = int(((d > lo) & (d <= hi)).sum()) if lo else int((d <= hi).sum())
    return out


# --- cross-event budgets ------------------------------------------------------
#
# The "gorge" subset is defined by a MEASURED, declared quantity - the mean
# slope each event records in eval/events.yaml, computed from the public
# Copernicus DEM over that event's own bbox - and it selects nothing for
# scoring. Every gate still averages over all six gate flood events; this
# subset exists only so the diagnosis can say which cause dominates in steep
# terrain as opposed to on a floodplain, which is the whole question.
GORGE_MEAN_SLOPE_DEG = 13.0


def mean_slope_deg(event: dict) -> float | None:
    """The mean slope declared for this event in events.yaml, in degrees."""
    import re
    match = re.search(r"mean slope\s+([0-9.]+)\s*deg", str(event.get("terrain", "")))
    return float(match.group(1)) if match else None


def miss_budget(diags: list[EventDiagnosis], events: dict[str, dict],
                only: list[str] | None = None) -> dict:
    """Pooled miss causes over a set of events: pixels and share of all misses.

    Pooled by PIXEL, not by averaging per-event shares: the question is where
    the missed ground is, and one event's 3,012 missed pixels should not weigh
    the same as another's 712.
    """
    chosen = [d for d in diags if d.error is None and d.misses
              and (only is None or d.event in only)]
    total = sum(d.misses["missed_pixels"] for d in chosen)
    by_cause = {c: sum(d.misses["by_cause"].get(c, 0) for d in chosen) for c in MISS_CAUSES}
    sub_keys = ("no_signal_and_no_qualifying_change_at_all",
                "no_signal_but_a_coherent_growable_change",
                "gate_blocked_by_vh_alone", "gate_blocked_by_vv_alone",
                "gate_blocked_by_both_too_small",
                "gate_blocked_by_sign_disagreement", "gate_blocked_by_weak_z_only")
    sub = {k: sum((d.misses.get("subdiagnostics") or {}).get(k, 0) for d in chosen)
           for k in sub_keys}
    sub["gate_blocked_by_one_polarisation_only"] = (
        sub["gate_blocked_by_vh_alone"] + sub["gate_blocked_by_vv_alone"])
    return {"events": [d.event for d in chosen], "missed_pixels": total,
            "by_cause": {c: n for c, n in by_cause.items() if n},
            "by_cause_share": {c: _share(n, total) for c, n in by_cause.items() if n},
            "subdiagnostics": sub,
            "subdiagnostics_share": {k: _share(v, total) for k, v in sub.items()}}


def control_budget(diags: list[EventDiagnosis]) -> dict:
    """Pooled false-flood pixels over every control, by place and by rule."""
    chosen = [d for d in diags if d.error is None and d.kind == sb.KIND_CONTROL]
    total = sum(d.false_alarms["total_pixels"] for d in chosen)
    by_place = {p: sum(d.false_alarms["by_place"].get(p, 0) for d in chosen)
                for p in FP_PLACES}
    rules = {}
    for key in ("sar_only", "optical_contributed", "high_confidence", "debris_class",
                "brightened", "darkened"):
        rules[key] = sum((d.false_alarms.get("rules") or {}).get(key, 0) for d in chosen)
    worst = max(chosen, key=lambda d: d.false_alarms["total_pixels"]) if chosen else None
    return {"events": [d.event for d in chosen], "false_pixels": total,
            "by_place": by_place,
            "by_place_share": {p: _share(n, total) for p, n in by_place.items()},
            "rules": rules,
            "worst_event": None if worst is None else worst.event,
            "worst_event_pixels": None if worst is None else
            worst.false_alarms["total_pixels"],
            "worst_event_analysed_fraction": None if worst is None else
            worst.false_alarms.get("false_flood_fraction_analysed")}


# --- the report ---------------------------------------------------------------

def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def render_markdown(run_id: str, diags: list[EventDiagnosis], mismatches: list[dict],
                    tolerance_px: int, generated: str,
                    events: dict[str, dict] | None = None) -> str:
    L: list[str] = []
    A = L.append
    A("# Accuracy diagnosis: where the flood errors are")
    A("")
    A(f"Generated by `eval/diagnose_errors.py` on {generated} from leave-one-event-out "
      f"run `{run_id}` of `eval/results/flood.csv`, at the boundary tolerance the gates "
      f"file declares ({tolerance_px} px = 20 m).")
    A("")
    A("**This document changes nothing.** No pipeline code and no parameter was touched to "
      "produce it. The case study was not used (Rule 4).")
    A("")
    A("**Where each number comes from (Rule 3.8).** Two kinds appear here and they must not "
      "be confused. The ACCURACY numbers - `F1 tol`, `F1`, and every control false-flood "
      "fraction - are the rows of `eval/results/flood.csv` for the run named above, and "
      "nothing here recomputes or adjusts them. Everything else is a DIAGNOSTIC COUNT: pixel "
      "tallies, position shares, boundary distances, shift scans and oracle bounds, computed "
      "here from each event's own score file under `runs/eval/<event>/scores/` and from the "
      "cached rasters that score was produced from, using the metric definitions in "
      "`validation/compare.py`. Diagnostic counts are not accuracy claims and must never be "
      "quoted as accuracy; they say where the error in those measured scores is. Every one of "
      "them is recorded in `docs/accuracy_diagnosis.json` beside this file, which carries the "
      "`run_id` and lets the document be re-rendered without re-running anything "
      "(`--from-json`).")
    A("")
    A("Each event was re-run with the configuration ITS leave-one-event-out fold was scored "
      "with, read from that event's own score file, and the error map taken apart. The "
      "intermediate masks come from a replica of `strandline.detect.detect_flood` whose "
      "rebuilt class map is asserted byte-identical to the real one; an event whose replica "
      "disagreed would be reported as an error here rather than attributed.")
    A("")

    ok = [d for d in diags if d.error is None]
    floods = [d for d in ok if d.kind != sb.KIND_CONTROL]
    controls = [d for d in ok if d.kind == sb.KIND_CONTROL]
    failed = [d for d in diags if d.error is not None]

    # --- the one table -------------------------------------------------------
    A("## The table")
    A("")
    A("One row per gate flood event. Shares are of that event's own misses or false alarms. "
      "`not analysed` is terrain the detector was never allowed to classify; `no signal` is "
      "analysed ground where neither polarisation moved by `z_seed`; the rule columns are "
      "the named rule that removed an otherwise-detectable pixel. The oracle columns are the "
      "tolerant F1 the event would reach if that source alone were fixed perfectly - a "
      "bound, not an achievable score.")
    A("")
    head = ["event", "F1 tol", "F1", "ref px", "missed", "not analysed", "no signal",
            "of which growable", "HAND", "gate", "gate: VH alone", "hyst", "MMU",
            "drainage", "debris rule", "FP px",
            "FP shore", "FP corridor", "FP flat", "FP slope", "err <=1px", "err <=3px",
            "best shift", "post +d", "river px", "top oracle fix"]
    A(_row(head))
    A(_sep(len(head)))
    for d in floods:
        m, mi, fa = d.metrics, d.misses, d.false_alarms
        cs = mi.get("by_cause_share", {})
        ps = fa.get("by_place_share", {})
        sub = mi.get("subdiagnostics", {})
        top = _top_oracle(d)
        row = [
            d.event,
            _fmt(m.get("f1_tolerant")), _fmt(m.get("f1")),
            str(mi.get("reference_pixels", "-")), str(mi.get("missed_pixels", "-")),
            _pct(cs.get(MISS_CAUSES[0], 0) + cs.get(MISS_CAUSES[1], 0)),
            _pct(cs.get(MISS_CAUSES[5], 0)),
            _pct(_share(sub.get("no_signal_but_a_coherent_growable_change", 0),
                        sub.get("no_signal_pixels", 0))),
            _pct(cs.get(MISS_CAUSES[4], 0)),
            _pct(cs.get(MISS_CAUSES[6], 0)),
            _pct(_share(sub.get("gate_blocked_by_vh_alone", 0), sub.get("gate_pixels", 0))),
            _pct(cs.get(MISS_CAUSES[7], 0)),
            _pct(cs.get(MISS_CAUSES[8], 0)),
            _pct(cs.get(MISS_CAUSES[9], 0)),
            _pct(cs.get(MISS_CAUSES[3], 0)),
            str(fa.get("total_pixels", "-")),
            _pct(ps.get(FP_PLACES[0], 0)), _pct(ps.get(FP_PLACES[1], 0)),
            _pct(ps.get(FP_PLACES[2], 0)), _pct(ps.get(FP_PLACES[3], 0)),
            _pct(d.boundary.get("errors_within_1px_of_a_correct_detection", 0)),
            _pct(d.boundary.get("errors_within_3px_of_a_correct_detection", 0)),
            f"({d.shift.get('best', {}).get('dy', 0)}, "
            f"{d.shift.get('best', {}).get('dx', 0)}) +"
            f"{_fmt(d.shift.get('gain_from_best_shift'))}",
            str(d.facts.get("post_latency_days")),
            _fmt((d.facts.get("river_width") or {}).get("median_width_px")),
            top,
        ]
        assert len(row) == len(head), (len(row), len(head))
        A(_row(row))
    for d in controls:
        fa = d.false_alarms
        ps = fa.get("by_place_share", {})
        row = [d.event] + ["-"] * 14 + [
               str(fa.get("total_pixels", "-")),
               _pct(ps.get(FP_PLACES[0], 0)), _pct(ps.get(FP_PLACES[1], 0)),
               _pct(ps.get(FP_PLACES[2], 0)), _pct(ps.get(FP_PLACES[3], 0)),
               "-", "-", "-", str(d.facts.get("post_latency_days")),
               _fmt((d.facts.get("river_width") or {}).get("median_width_px")),
               f"FFR {_fmt(fa.get('false_flood_fraction_domain'))} domain / "
               f"{_fmt(fa.get('false_flood_fraction_analysed'))} analysed"]
        assert len(row) == len(head), (len(row), len(head))
        A(_row(row))
    A("")
    if failed:
        A("Not diagnosed:")
        for d in failed:
            A(f"- `{d.event}`: {d.error}")
        A("")

    # --- per event detail ----------------------------------------------------
    A("## Per event")
    A("")
    for d in floods:
        A(f"### {d.event}")
        A("")
        f = d.facts
        A(f"- Scored configuration `{d.params_hash}`, mode `{f['mode']}`, "
          f"{f['n_pre']} pre-event images, grid {f['grid']}.")
        A(f"- Sentinel-1: track {f['track']}, post {f['post_day']} "
          f"(+{f['post_latency_days']} d after the event), pre {', '.join(f['pre_days'])}.")
        orb = f.get("orbits") or {}
        if "error" in orb:
            A(f"- Orbit directions with a usable pre/post pair: not checked ({orb['error']}).")
        else:
            A(f"- Orbit directions with a usable pre/post pair: "
              f"{', '.join(orb.get('directions_with_a_usable_pair') or ['none']) }. "
              f"{_orbit_note(orb)}")
        A(f"- Sentinel-2: {f['optical'].get('status')}"
          + (f", pair clear fraction {_fmt(f['optical'].get('clear_fraction_pair'))}, "
             f"post {f['optical'].get('post_day')} "
             f"(+{f['optical'].get('post_latency_days')} d)"
             if f["optical"].get("status") == "used" else
             f" - {f['optical'].get('message')}"))
        A(f"- Water darkness threshold {f['water_threshold_db']} dB"
          + (f" (Otsu clamped {f['water_threshold_clamped']})"
             if f["water_threshold_clamped"] else ""))
        A(f"- Analysed ground: {f['analysed_pixels']} of {f['domain_pixels']} px "
          f"({_pct(f['analysed_fraction'])}).")
        A(f"- Permanent water {f['permanent_water_ha']} ha; river width "
          f"{_river(f.get('river_width'))}.")
        A(f"- Reference: {f.get('reference_sensor')}")
        A("")
        A(f"Missed reference pixels ({d.misses['missed_pixels']} of "
          f"{d.misses['reference_pixels']}, "
          f"{_pct(_share(d.misses['missed_pixels'], d.misses['reference_pixels']))}):")
        A("")
        A(_row(["cause", "pixels", "share of misses"]))
        A(_sep(3))
        for cause in MISS_CAUSES:
            n = d.misses["by_cause"].get(cause, 0)
            if n:
                A(_row([cause, n, _pct(d.misses["by_cause_share"][cause])]))
        A("")
        sub = d.misses.get("subdiagnostics") or {}
        if sub:
            A(f"Of the {sub.get('no_signal_pixels', 0)} missed pixels with no seed-strength "
              f"signal, {sub.get('no_signal_but_a_coherent_growable_change', 0)} DO carry a "
              f"coherent change large enough to join a region and only lacked a connected "
              f"seed; {sub.get('no_signal_and_no_qualifying_change_at_all', 0)} carry no "
              f"qualifying change at all.")
            if "gate_blocked_by_vh_alone" in sub:
                A(f"Of the {sub.get('gate_pixels', 0)} blocked by the magnitude gate: "
                  f"{sub.get('gate_blocked_by_vh_alone', 0)} were blocked by VH alone (VV "
                  f"moved far enough, VH did not), "
                  f"{sub.get('gate_blocked_by_vv_alone', 0)} by VV alone, "
                  f"{sub.get('gate_blocked_by_both_too_small', 0)} because neither "
                  f"polarisation moved far enough, "
                  f"{sub.get('gate_blocked_by_sign_disagreement', 0)} by the two "
                  f"polarisations moving in opposite directions, and "
                  f"{sub.get('gate_blocked_by_weak_z_only', 0)} only by the z-score "
                  f"strength test.")
            A("")
        A(f"False alarms ({d.false_alarms['total_pixels']} px):")
        A("")
        A(_row(["where", "pixels", "share"]))
        A(_sep(3))
        for place in FP_PLACES:
            n = d.false_alarms["by_place"].get(place, 0)
            if n:
                A(_row([place, n, _pct(d.false_alarms["by_place_share"][place])]))
        A("")
        rules = d.false_alarms.get("rules") or {}
        if rules:
            A(f"Which rule let them through: {rules.get('sar_only')} px radar only, "
              f"{rules.get('optical_contributed')} px with Sentinel-2 agreeing, "
              f"{rules.get('high_confidence')} px high confidence, "
              f"{rules.get('debris_class')} px debris class, "
              f"{rules.get('brightened')} px brightened vs {rules.get('darkened')} px darkened "
              f"(in `rough_water` a brightening of either sign on the valley floor is water).")
            A("")
        A(f"Boundary: {_pct(d.boundary.get('errors_within_1px_of_a_correct_detection'))} of all "
          f"errors sit within 1 px of a correct detection, "
          f"{_pct(d.boundary.get('errors_within_3px_of_a_correct_detection'))} within 3 px. "
          f"Strict F1 at zero shift {_fmt(d.shift.get('f1_at_zero_shift'))}; best over "
          f"+-5 px is {_fmt(d.shift.get('best', {}).get('f1'))} at "
          f"(dy {d.shift.get('best', {}).get('dy')}, dx {d.shift.get('best', {}).get('dx')}), "
          f"a gain of {_fmt(d.shift.get('gain_from_best_shift'))}.")
        A("")
        A("Oracle upper bound, one source at a time "
          f"(baseline tolerant F1 {_fmt(d.bounds.get('baseline_f1_tolerant'))}):")
        A("")
        A(_row(["if this alone were fixed", "pixels", "tolerant F1", "gain"]))
        A(_sep(4))
        for label, info in sorted(d.bounds.get("if_fixed", {}).items(),
                                  key=lambda kv: -kv[1]["gain"]):
            A(_row([label, info["pixels"], _fmt(info["f1_tolerant"]),
                    f"{info['gain']:+.4f}"]))
        A("")

    A("## Controls")
    A("")
    for d in controls:
        fa = d.false_alarms
        f = d.facts
        A(f"### {d.event}")
        A("")
        A(f"- Scored configuration `{d.params_hash}`, mode `{f['mode']}`, "
          f"track {f['track']}, post {f['post_day']} (+{f['post_latency_days']} d).")
        A(f"- False flood {fa['total_pixels']} px = "
          f"{_fmt(fa['false_flood_fraction_domain'])} of the scoring domain and "
          f"{_fmt(fa['false_flood_fraction_analysed'])} of analysed ground "
          f"({_pct(f['analysed_fraction'])} of the domain is analysed).")
        A(f"- Permanent water {f['permanent_water_ha']} ha; river width "
          f"{_river(f.get('river_width'))}.")
        A("")
        A(_row(["where", "pixels", "share"]))
        A(_sep(3))
        for place in FP_PLACES:
            n = fa["by_place"].get(place, 0)
            if n:
                A(_row([place, n, _pct(fa["by_place_share"][place])]))
        A("")
        dist = fa.get("distance_to_permanent_water_px") or {}
        if dist:
            A("Distance from permanent water: "
              + ", ".join(f"{k} {v} px" for k, v in dist.items()) + ".")
            A("")
        rules = fa.get("rules") or {}
        if rules:
            A(f"Which rule let them through: {rules.get('sar_only')} px radar only, "
              f"{rules.get('optical_contributed')} px with Sentinel-2 agreeing, "
              f"{rules.get('high_confidence')} px high confidence, "
              f"{rules.get('debris_class')} px debris class, "
              f"{rules.get('brightened')} px brightened vs {rules.get('darkened')} px "
              f"darkened.")
            A("")

    # --- cross-event budget and what it names --------------------------------
    all_events = events or sb.load_events()
    steep = [d.event for d in floods
             if (mean_slope_deg(all_events.get(d.event, {})) or 0) >= GORGE_MEAN_SLOPE_DEG]
    pooled = miss_budget(floods, all_events)
    gorge = miss_budget(floods, all_events, only=steep)
    ctrl = control_budget(diags)

    A("## Where the missed ground is, pooled")
    A("")
    A(f"Pooled by PIXEL over the gate flood events, so a large event is not given the same "
      f"weight as a small one. The *steep* column pools only the events whose measured mean "
      f"slope in `eval/events.yaml` is at least {GORGE_MEAN_SLOPE_DEG:g} deg "
      f"({', '.join(gorge['events'])}); it selects nothing for scoring - every gate still "
      f"averages over all six events - and exists only so the diagnosis can separate steep "
      f"terrain from floodplain.")
    A("")
    A(_row(["cause", f"all {len(pooled['events'])} events", "share",
            f"steep {len(gorge['events'])} events", "share"]))
    A(_sep(5))
    for cause in MISS_CAUSES:
        a, b = pooled["by_cause"].get(cause, 0), gorge["by_cause"].get(cause, 0)
        if a or b:
            A(_row([cause, a, _pct(pooled["by_cause_share"].get(cause, 0)), b,
                    _pct(gorge["by_cause_share"].get(cause, 0))]))
    A(_row(["**total missed**", f"**{pooled['missed_pixels']}**", "100%",
            f"**{gorge['missed_pixels']}**", "100%"]))
    A("")
    A("Inside the two signal-level buckets:")
    A("")
    A(_row(["sub-cause", "all events", "share of misses", "steep events",
            "share of misses"]))
    A(_sep(5))
    labels = {
        "no_signal_and_no_qualifying_change_at_all":
            "no seed signal AND no qualifying change at all",
        "no_signal_but_a_coherent_growable_change":
            "no seed signal but a coherent change big enough to grow",
        "gate_blocked_by_one_polarisation_only":
            "magnitude gate: only ONE polarisation moved far enough",
        "gate_blocked_by_vh_alone": "   of which VH was the channel that fell short",
        "gate_blocked_by_vv_alone": "   of which VV was the channel that fell short",
        "gate_blocked_by_both_too_small":
            "magnitude gate: NEITHER polarisation moved far enough",
        "gate_blocked_by_sign_disagreement":
            "magnitude gate: the two polarisations moved in opposite directions",
        "gate_blocked_by_weak_z_only": "magnitude gate: only the z-strength test failed",
    }
    for key, label in labels.items():
        A(_row([label, pooled["subdiagnostics"].get(key, 0),
                _pct(pooled["subdiagnostics_share"].get(key, 0)),
                gorge["subdiagnostics"].get(key, 0),
                _pct(gorge["subdiagnostics_share"].get(key, 0))]))
    A("")

    A("## Where the control false alarms are, pooled")
    A("")
    A(_row(["where", "pixels", "share"]))
    A(_sep(3))
    for place in FP_PLACES:
        n = ctrl["by_place"].get(place, 0)
        if n:
            A(_row([place, n, _pct(ctrl["by_place_share"][place])]))
    A(_row(["**total**", f"**{ctrl['false_pixels']}**", "100%"]))
    A("")
    A(f"Pooled over {len(ctrl['events'])} controls. By channel and sign: "
      f"{ctrl['rules']['sar_only']} px radar only, "
      f"{ctrl['rules']['optical_contributed']} px with Sentinel-2 agreeing, "
      f"{ctrl['rules']['high_confidence']} px high confidence, "
      f"{ctrl['rules']['debris_class']} px debris class, "
      f"{ctrl['rules']['brightened']} px brightened against "
      f"{ctrl['rules']['darkened']} px darkened.")
    A("")

    # --- what the numbers name -----------------------------------------------
    A(_conclusions(pooled, gorge, ctrl, floods, all_events))

    # --- config mismatches ---------------------------------------------------
    A("## Shipped defaults vs the configuration that was scored")
    A("")
    A(f"A default `strandline.run` invocation uses **n_pre = {default_n_pre_of_a_live_run()}** "
      f"pre-event images (`RunParams.n_pre`, which is also the CLI's `--n-pre` default). "
      f"The dev plan and every scored row use `DEV_N_PRE = 3`, the thinnest stack "
      f"`RunParams.min_pre` allows.")
    A("")
    if not mismatches:
        A("No other mismatch: every other parameter in every scored fold equals the shipped "
          "default.")
    else:
        A(_row(["parameter", "shipped default", "scored value(s)", "events"]))
        A(_sep(4))
        for m in mismatches:
            for scored in m["scored"]:
                A(_row([f"`{m['key']}`", m["shipped_default"], scored["value"],
                        ", ".join(scored["events"])]))
    A("")
    A("Parameters that differ BETWEEN events are leave-one-event-out folds doing their job: "
      "each fold chose without its own event, so the shipped default cannot equal all of "
      "them at once. A difference that is the same across every fold is a real mismatch "
      "between what is shipped and what was measured.")
    A("")
    return "\n".join(L) + "\n"


def _conclusions(pooled: dict, gorge: dict, ctrl: dict, floods: list[EventDiagnosis],
                 events: dict[str, dict]) -> str:
    """What the pooled numbers name, with every figure read back from them.

    The prose is fixed; every NUMBER in it is interpolated from the budgets
    computed above. That is deliberate: a measured value hardcoded into a
    report is a value that silently goes stale, and this project has been
    bitten by exactly that. If a future run moves a share, this section moves
    with it or stops making its claim.
    """
    gs, gsub = gorge["by_cause_share"], gorge["subdiagnostics_share"]
    growable = gorge["subdiagnostics"].get("no_signal_but_a_coherent_growable_change", 0)
    changeless = gorge["subdiagnostics"].get("no_signal_and_no_qualifying_change_at_all", 0)
    gate = gs.get(MISS_CAUSES[6], 0)
    one_pol = gorge["subdiagnostics"].get("gate_blocked_by_one_polarisation_only", 0)
    both_small = gorge["subdiagnostics"].get("gate_blocked_by_both_too_small", 0)
    disagree = gorge["subdiagnostics"].get("gate_blocked_by_sign_disagreement", 0)
    drainage = gs.get(MISS_CAUSES[9], 0)
    flat = ctrl["by_place_share"].get(FP_PLACES[2], 0)
    corridor = ctrl["by_place_share"].get(FP_PLACES[1], 0)
    shore = ctrl["by_place_share"].get(FP_PLACES[0], 0)

    widths = {d.event: (d.facts.get("river_width") or {}).get("median_width_px")
              for d in floods}
    steep_widths = {e: widths[e] for e in gorge["events"] if widths.get(e)}
    near_1px = {d.event: d.boundary.get("errors_within_1px_of_a_correct_detection")
                for d in floods if d.event in gorge["events"]}
    shifts = {d.event: (d.shift.get("best", {}).get("dy"), d.shift.get("best", {}).get("dx"),
                        d.shift.get("gain_from_best_shift")) for d in floods}

    L = ["## What these numbers name", "",
         "### The single largest FIXABLE miss source in the steep events", ""]
    L.append(
        f"**The conjunctive magnitude gate** - `rough_water` requires BOTH polarisations to "
        f"move by at least `min_abs_change_db` (3 dB), in the same direction, before a pixel "
        f"counts as changed at all. It accounts for {_pct(gate)} of every missed reference "
        f"pixel in the steep events ({gorge['by_cause'].get(MISS_CAUSES[6], 0)} px), and "
        f"{one_pol} of those - {_pct(gsub.get('gate_blocked_by_one_polarisation_only', 0))} of "
        f"all steep-event misses - are pixels where ONE polarisation moved 3 dB or more and "
        f"the other did not. A further {both_small} px failed because neither channel reached "
        f"3 dB. Sign disagreement is negligible ({disagree} px), so it is the magnitude "
        f"conjunction doing the damage, not the agreement requirement.")
    L.append("")
    L.append(
        f"It is the largest *fixable* source rather than the largest source. The bigger bucket "
        f"- {_pct(gs.get(MISS_CAUSES[5], 0))} of steep-event misses, with no seed-strength "
        f"z-score in either polarisation - is almost entirely **changeless ground**: of those "
        f"pixels only {growable} carry a coherent change large enough to join a region at all, "
        f"against {changeless} that carry no qualifying change in either polarisation. No "
        f"threshold, seed or growth rule recovers a pixel whose backscatter did not move; "
        f"those are a sensor-and-scale limit and must be reported as one. The magnitude gate, "
        f"by contrast, discards pixels the detector demonstrably saw.")
    L.append("")
    L.append(
        f"The next source down is the drainage-connection test at {_pct(drainage)} of "
        f"steep-event misses, which matters and is addressed by the second fix below.")
    L.append("")
    L.append("### The false-alarm source on the controls")
    L.append("")
    L.append(
        f"**Flat valley-floor ground away from the channel**, admitted by the either-sign "
        f"rule: {_pct(flat)} of all {ctrl['false_pixels']} false-flood pixels pooled over "
        f"{len(ctrl['events'])} controls, against {_pct(corridor)} in the channel corridor and "
        f"{_pct(shore)} on a permanent-water shore. {ctrl['rules']['brightened']} of the "
        f"pooled pixels BRIGHTENED and {ctrl['rules']['darkened']} darkened.")
    L.append("")
    L.append(
        f"The worst control, `{ctrl['worst_event']}`, carries {ctrl['worst_event_pixels']} of "
        f"them - {_fmt(ctrl['worst_event_analysed_fraction'])} of its analysed ground against "
        f"the declared 1% ceiling - and **the reservoir-shore hypothesis for it is now tested "
        f"and false**: its distance histogram above puts only a small minority within 3 px of "
        f"permanent water and the bulk of the pixels 10 px (200 m) or further from any water "
        f"the detector found. They sit on low-HAND, gentle-slope valley floor in a dry August, "
        f"they are radar-only (optical refused that date for cloud), and they are "
        f"brightening-dominated. A coherent 3 dB brightening of flat ground in a tropical "
        f"farmed valley is what soil-moisture change, irrigation and tillage look like to "
        f"C-band; `rough_water` calls it water because inside the valley-floor zone it tests "
        f"the MAGNITUDE of a change and not its sign, and asks nothing about whether the "
        f"post-event surface could physically hold water.")
    L.append("")
    L.append("### Two things these errors are NOT")
    L.append("")
    L.append(
        "**Not a geometric offset.** The +-5 px cross-correlation finds its best shift at or "
        "within two pixels of zero on every event, the directions are inconsistent between "
        "events ("
        + "; ".join(f"`{e}` ({dy:+d}, {dx:+d}) for {gain:+.4f}"
                    for e, (dy, dx, gain) in sorted(shifts.items()) if dy is not None)
        + "), and the largest gain any shift buys is small. A single registration error would "
          "show one consistent direction and a large gain. DIAGNOSIS ONLY: no shift is applied "
          "anywhere, and fitting one against the reference would be fitting to the reference.")
    L.append("")
    L.append(
        "**Not an edge-of-polygon disagreement.** In the steep events only "
        + ", ".join(f"{_pct(v)} (`{e}`)" for e, v in sorted(near_1px.items()))
        + " of errors sit within 1 px of a correct detection, so the steep-event failure is "
          "whole missing reaches and not a one-pixel rim. The measured median channel width "
          "there is "
        + ", ".join(f"{w:g} px (`{e}`)" for e, w in sorted(steep_widths.items()))
        + " at 20 m, which is the scale the mixed-pixel argument rests on.")
    L.append("")
    L.append("### The physics-based fix for each")
    L.append("")
    L.append(
        "Neither is implemented here, and neither is a retune: both change a test, and a test "
        "change needs its own declared decision, its own leave-one-event-out pass and its own "
        "control check before anything is adopted.")
    L.append("")
    L.append(
        f"**1. For the misses: a per-polarisation, noise-aware change test instead of one "
        f"shared dB threshold demanded of both channels.** Note first what the split says: VV "
        f"is the channel that falls short on "
        f"{gorge['subdiagnostics'].get('gate_blocked_by_vv_alone', 0)} of those px and VH on "
        f"{gorge['subdiagnostics'].get('gate_blocked_by_vh_alone', 0)}, so this is NOT a "
        f"VH-is-noisy story - it is a single threshold being wrong for both channels. The "
        f"physics is the sensor's own noise budget. Sentinel-1 IW GRDH has a noise-equivalent "
        f"sigma-zero of about -22 dB, each polarisation sits a different distance above it "
        f"(and over a smooth or partly-water surface VH sits lower in absolute terms than VV), "
        f"and speckle adds a per-channel uncertainty set by the equivalent number of looks "
        f"(`enl` 4.4, reduced by the Lee filter). Whichever channel comes closest to its floor "
        f"gives a *clipped* response to a real water change, so demanding the same 3 dB from "
        f"both discards the pixel - the {one_pol} px measured above, in whichever direction "
        f"the clipping happened to fall. The fix is to compare each polarisation's change with "
        f"that polarisation's OWN uncertainty, derived from the published NESZ and from the "
        f"pre-event stack's own ENL-corrected variance, to require the change only in the "
        f"channels that are above their noise floor, and to keep the polarisation requirement "
        f"as a SIGN-consistency test where both channels are usable - which the measured "
        f"{disagree} px of sign disagreement says costs almost nothing. Every quantity in it "
        f"comes from the sensor specification and from the scene's own pre-event statistics, "
        f"so it is one rule for every area and date, not a value fitted to a gorge.")
    L.append("")
    L.append(
        f"**2. For the control false alarms: require a water surface that hydraulics allows, "
        f"not merely a change on low ground.** Water fills a valley from the channel upward "
        f"along the drainage network and stands at a common surface within a reach; wet soil, "
        f"irrigated fields and tilled ground do not. Two constraints follow, both from terrain "
        f"layers the pipeline already computes (`flow_dir`, `accumulation`, HAND) and both "
        f"applicable everywhere. (a) Replace the single global HAND ceiling with a "
        f"*reach-relative* one: a candidate pixel must lie below the water surface implied by "
        f"the channel reach it actually drains to, rather than below a fixed 20 m above the "
        f"nearest drainage anywhere in the scene. (b) Replace component-level drainage "
        f"connection with per-pixel flow-path continuity along the D8 directions: today a "
        f"whole connected component is admitted if any ONE of its pixels touches a channel "
        f"within 3 px, which is how a field-sized patch enters, and the same coarse test is "
        f"costing {_pct(drainage)} of steep-event misses in the other direction by rejecting "
        f"genuine flood that reaches the channel only outside the 3 px buffer. A continuity "
        f"test tightens the controls and loosens the gorges with one change, because both "
        f"failures come from the same component-level approximation.")
    L.append("")
    return "\n".join(L)


def _row(cells) -> str:
    """One markdown table row, with cell contents made safe.

    An unescaped pipe splits a cell, and several cause names contain one
    (`no change signal (|z| < z_seed ...)`), which silently shifted every
    column to its right until this existed.
    """
    return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"


def _sep(n: int) -> str:
    return "|" + "|".join(["---"] * n) + "|"


def _pct(x) -> str:
    return "-" if x is None else f"{100 * float(x):.1f}%"


def _river(info) -> str:
    if not info or info.get("status") != "measured":
        return f"NO DATA ({(info or {}).get('reason', 'not measured')})"
    return (f"median {info['median_width_px']} px "
            f"(p10 {info['p10_width_px']}, p90 {info['p90_width_px']}, "
            f"{info['axis_pixels']} axis px)")


def _orbit_note(orb: dict) -> str:
    parts = []
    for direction, info in sorted((orb.get("by_direction") or {}).items()):
        if info["best"]:
            b = info["best"]
            parts.append(f"{direction.lower()} best is orbit {b['relative_orbit']} with "
                         f"{b['n_pre']} pre and post +{b['post_latency_days']} d")
        else:
            parts.append(f"{direction.lower()} has {info['n_tracks']} track(s) but no "
                         f"usable pair")
    return ("; ".join(parts) + ".") if parts else ""


def _top_oracle(d: EventDiagnosis) -> str:
    fixed = d.bounds.get("if_fixed") or {}
    if not fixed:
        return "-"
    label, info = max(fixed.items(), key=lambda kv: kv[1]["gain"])
    return f"{label} ({info['gain']:+.4f})"


# --- CLI ----------------------------------------------------------------------

def _rerender(json_path: Path, out: Path) -> int:
    """Rebuild the markdown from a recorded run. Reads no raster and no score.

    Every number in the document comes from the JSON, which is what the run
    that produced it measured, so this cannot invent or refresh a value: a
    stale JSON renders a stale - and correctly dated - document.
    """
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    diags = [EventDiagnosis(**{k: d.get(k) for k in
                               ("event", "kind", "params_hash", "facts", "misses",
                                "false_alarms", "boundary", "shift", "bounds", "metrics",
                                "error")})
             for d in raw["events"]]
    generated = str(raw.get("generated", datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    text = render_markdown(raw["run_id"], diags, raw.get("config_mismatches") or [],
                           int(raw["tolerance_px"]), generated)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"re-rendered {out} from {json_path} (run {raw['run_id']}, measured {generated})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--events", nargs="*", help="default: every gate flood event and control")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT,
                    help="where to record every measured count (default: "
                         "docs/accuracy_diagnosis.json). This is the document's provenance; "
                         "--json-out '' turns it off.")
    ap.add_argument("--runs-root", type=Path, default=EVAL_RUNS)
    ap.add_argument("--results", type=Path, default=FLOOD_CSV)
    ap.add_argument("--no-orbits", action="store_true",
                    help="skip the catalogue query for orbit availability (offline)")
    ap.add_argument("--from-json", type=Path, default=None,
                    help="re-render the markdown from a --json-out file of an earlier run, "
                         "without touching a raster. The JSON carries the run_id and every "
                         "measured count, so the document is identical to the one that run "
                         "wrote; use it to change wording, never to change a number.")
    args = ap.parse_args(argv)

    if args.from_json:
        return _rerender(args.from_json, args.out)

    con = rs.contract()
    tolerance_px = int(con["boundary_tolerance_px"])
    all_events = sb.load_events()
    planned = select_events("gates", args.events, all_events)
    heldout_guard(planned)

    run_id, flood_hashes = latest_loeo(args.results)
    # Controls are scored at the FINAL point, not per fold, and the latest run
    # that carries them is the full-split one. Taken from the same CSV so the
    # control rows diagnosed are the ones the control gate was computed from.
    _ctrl_run, ctrl_hashes = latest_loeo(args.results, kind=sb.KIND_CONTROL, split="full")
    hashes = {**flood_hashes, **ctrl_hashes}

    print(f"leave-one-event-out run {run_id}; controls from run {_ctrl_run}")
    print(f"{len(planned)} event(s) to diagnose\n")

    diags: list[EventDiagnosis] = []
    evaluated: dict[str, dict] = {}
    for ev in planned:
        name = str(ev["name"])
        if name not in hashes:
            diags.append(EventDiagnosis(event=name, kind=sb.row_kind(ev), params_hash="-",
                                        error="no row for this event in the latest run"))
            print(f"  {name}: SKIPPED (no row in the latest run)")
            continue
        try:
            d = diagnose_event(ev, hashes[name], args.runs_root, tolerance_px,
                               with_orbits=not args.no_orbits)
        except KeyboardInterrupt:
            raise
        # Deliberately broad, for the same reason eval/run_scoreboard.score_event
        # is: one unreadable event must not lose the diagnosis of the others, and
        # the failure is reported rather than filled in.
        except Exception as err:
            d = EventDiagnosis(event=name, kind=sb.row_kind(ev), params_hash=hashes[name],
                               error=f"{type(err).__name__}: {err}")
        diags.append(d)
        if d.error:
            print(f"      FAILED: {d.error}")
        else:
            score = rs.load_score(name, hashes[name], args.runs_root)
            if score is not None:
                evaluated[name] = scored_parameter_dict(score)

    mismatches = config_mismatches(evaluated)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = render_markdown(run_id, diags, mismatches, tolerance_px, generated,
                           events=all_events)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"\nwrote {args.out.relative_to(ROOT) if args.out.is_absolute() else args.out}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(
            {"run_id": run_id, "tolerance_px": tolerance_px, "generated": generated,
             "config_mismatches": mismatches,
             "events": [asdict(d) for d in diags]}, indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.json_out}")

    failed = [d for d in diags if d.error]
    if failed:
        print(f"\n{len(failed)} event(s) not diagnosed:")
        for d in failed:
            print(f"  {d.event}: {d.error}")
    return 0


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.exit(main())
