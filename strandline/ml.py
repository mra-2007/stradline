"""CPU inference with the Kuro Siwo U-Net flood model (optional evidence).

Pure, deterministic, and OPTIONAL at two levels: `torch` /
`segmentation_models_pytorch` may not be importable, and the trained weights
(gitignored, not part of this repo: `models/strandline_kurosiwo_unet.pt`) may
not be present on disk. Neither is fatal: `load_model` returns `(None, status)`
rather than raising, and every caller falls back to physics-only detection,
with the reason recorded in run_report.json (Rule 5: a refusal, not a silent
wrong map; Rule 6: never claim a model contributed when it did not run).

Checkpoint format (torch.load(..., map_location="cpu", weights_only=False)):
a dict with keys `state_dict` (for
segmentation_models_pytorch.Unet("resnet34", encoder_weights=None,
in_channels=6, classes=3)), and per-channel normalisation statistics `mean`,
`std`, `clip_lo`, `clip_hi` (each length 6, one entry per element of
CHANNELS), exactly as applied at training time:

    x = clip(x, clip_lo, clip_hi)
    x = (x - mean) / std

Channel order: [post_vv, post_vh, pre1_vv, pre1_vh, pre2_vv, pre2_vh] - post
VV/VH, then the two pre-event dates nearest the event, VV/VH each. Class
codes: 0 none, 1 permanent water, 2 flood.

Model inputs are LINEAR sigma0, not dB: this pipeline's own stacks are dB
(strandline.detect.preprocess), so `sigma0_db_to_linear` converts back before
normalisation.
"""

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

CHANNELS = ("post_vv", "post_vh", "pre1_vv", "pre1_vh", "pre2_vv", "pre2_vh")
N_CHANNELS = len(CHANNELS)

# How many tiles go through one net() forward pass. Pure speed: batching does
# not change a single tile's inputs, padding or softmax - each tile's output
# is exactly the row of the batch it occupies, so this is the same computation
# as the one-tile-at-a-time loop it replaces, just fewer Python-level forward
# calls on a CPU that has threads to spare.
DEFAULT_BATCH_SIZE = 16

MODEL_CLASS_NONE = 0
MODEL_CLASS_PERMANENT = 1
MODEL_CLASS_FLOOD = 2

# What `load_model` / `run_model` reported. Always one of these in
# run_report.json, so a report can never be silent about whether the model
# contributed (same contract as strandline.run's OPTICAL_* / OSM_* statuses).
MODEL_DISABLED = "disabled"
MODEL_UNAVAILABLE = "unavailable"   # torch/smp missing, file missing, or bad checkpoint
MODEL_FAILED = "failed"              # loaded, but inference itself broke
MODEL_USED = "used"


def sigma0_db_to_linear(db: np.ndarray) -> np.ndarray:
    """dB -> linear sigma0. NaN-preserving."""
    out = np.full(np.shape(db), np.nan, dtype=np.float64)
    finite = np.isfinite(db)
    out[finite] = np.power(10.0, np.asarray(db)[finite] / 10.0)
    return out


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class LoadedModel:
    net: object               # torch.nn.Module, CPU, eval mode
    mean: np.ndarray          # (N_CHANNELS,)
    std: np.ndarray           # (N_CHANNELS,)
    clip_lo: np.ndarray       # (N_CHANNELS,)
    clip_hi: np.ndarray       # (N_CHANNELS,)
    sha256: str
    path: str


def load_model(path, device: str = "cpu") -> tuple["LoadedModel | None", dict]:
    """Returns (LoadedModel | None, status). Never raises.

    `status["available"]` is False whenever `LoadedModel` is None, with
    `status["reason"]` saying why: no torch, no file, or a checkpoint that
    does not match the documented format. A missing or broken model is a
    normal, expected state (Rule 5), not a bug to loop on.
    """
    p = Path(path)
    if not p.exists():
        return None, {"available": False, "reason": f"model file not found: {p}"}
    try:
        import torch  # noqa: F401
        import segmentation_models_pytorch as smp
    except ImportError as err:
        return None, {"available": False,
                      "reason": f"torch/segmentation_models_pytorch not importable: {err}"}
    try:
        ckpt = torch.load(str(p), map_location=device, weights_only=False)
    except Exception as err:
        return None, {"available": False, "reason": f"failed to load checkpoint: {err}"}
    try:
        state_dict = ckpt["state_dict"] if isinstance(ckpt, dict) and "state_dict" in ckpt else ckpt
        mean = np.asarray(ckpt["mean"], dtype=np.float64).reshape(N_CHANNELS)
        std = np.asarray(ckpt["std"], dtype=np.float64).reshape(N_CHANNELS)
        clip_lo = np.asarray(ckpt["clip_lo"], dtype=np.float64).reshape(N_CHANNELS)
        clip_hi = np.asarray(ckpt["clip_hi"], dtype=np.float64).reshape(N_CHANNELS)
        if np.any(std <= 0):
            raise ValueError(f"checkpoint std must be positive, got {std.tolist()}")
        net = smp.Unet("resnet34", encoder_weights=None, in_channels=N_CHANNELS, classes=3)
        net.load_state_dict(state_dict)
        net.to(device)
        net.eval()
    except Exception as err:
        return None, {"available": False,
                      "reason": f"checkpoint does not match the expected format: {err}"}
    sha = file_sha256(p)
    loaded = LoadedModel(net=net, mean=mean, std=std, clip_lo=clip_lo, clip_hi=clip_hi,
                         sha256=sha, path=str(p))
    return loaded, {"available": True, "reason": None, "sha256": sha, "path": str(p)}


def normalise(channels: np.ndarray, clip_lo: np.ndarray, clip_hi: np.ndarray,
             mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    """channels: (N_CHANNELS, H, W) LINEAR sigma0. Exactly the training recipe:
    clip to [clip_lo, clip_hi] per channel, then z-score per channel."""
    out = np.empty_like(channels, dtype=np.float64)
    for c in range(channels.shape[0]):
        x = np.clip(channels[c], clip_lo[c], clip_hi[c])
        out[c] = (x - mean[c]) / std[c]
    return out


def clip_coverage(channels: np.ndarray, clip_lo: np.ndarray, clip_hi: np.ndarray
                  ) -> dict[str, float]:
    """Per-channel fraction of FINITE pixels already inside [clip_lo, clip_hi],
    measured BEFORE clipping. A unit-conversion test, not a detection: if our
    linear-sigma0 conversion does not match what the checkpoint was trained on,
    most pixels land outside the clip range and get clamped to one edge, which
    silently destroys the signal rather than raising. Reported so that
    regression is visible in run_report.json rather than only in a test.
    """
    out: dict[str, float] = {}
    for c, name in enumerate(CHANNELS):
        x = channels[c]
        finite = np.isfinite(x)
        if not finite.any():
            out[name] = float("nan")
            continue
        inside = finite & (x >= clip_lo[c]) & (x <= clip_hi[c])
        out[name] = float(inside.sum()) / float(finite.sum())
    return out


def _tile_starts(size: int, tile: int, stride: int) -> list[int]:
    if size <= tile:
        return [0]
    starts = list(range(0, size - tile + 1, stride))
    if starts[-1] != size - tile:
        starts.append(size - tile)
    return starts


def predict_tiles(loaded: LoadedModel, channels_linear: np.ndarray,
                  tile: int, overlap: int, batch_size: int = DEFAULT_BATCH_SIZE
                  ) -> tuple[np.ndarray, np.ndarray]:
    """channels_linear: (N_CHANNELS, H, W) linear sigma0, NaN where invalid.

    Runs the net over `tile`-px windows with `overlap`-px stride reduction,
    averaging softmax probabilities over every window a pixel falls in - the
    standard way to remove tile-edge artefacts from a patch-based CNN. A pixel
    never covered by a finite window (all NaN) stays NaN in both outputs.

    Tiles are padded and normalised exactly as before, then grouped into
    batches of up to `batch_size` and sent through the net in one call per
    batch (CPU threads otherwise sit idle on a batch of 1); batchnorm is in
    eval mode (running statistics, not batch statistics), so this changes
    nothing about any one tile's output, only how many Python-level forward
    calls it takes to get there.

    Returns (p_flood, p_perm), each (H, W) float64 in [0, 1].
    """
    import torch
    torch.set_num_threads(os.cpu_count() or 1)

    c, h, w = channels_linear.shape
    if c != N_CHANNELS:
        raise ValueError(f"expected {N_CHANNELS} channels, got {c}")
    stride = max(tile - overlap, 1)
    rows = _tile_starts(h, tile, stride)
    cols = _tile_starts(w, tile, stride)
    positions = [(r0, c0) for r0 in rows for c0 in cols]

    sum_flood = np.zeros((h, w), dtype=np.float64)
    sum_perm = np.zeros((h, w), dtype=np.float64)
    count = np.zeros((h, w), dtype=np.float64)

    net = loaded.net
    with torch.inference_mode():
        for start in range(0, len(positions), batch_size):
            batch_x = []
            batch_dims = []   # (r0, c0, r1, c1, th, tw, valid)
            for r0, c0 in positions[start:start + batch_size]:
                r1, c1 = min(r0 + tile, h), min(c0 + tile, w)
                patch = channels_linear[:, r0:r1, c0:c1]
                th, tw = patch.shape[1], patch.shape[2]
                valid = np.isfinite(patch).all(axis=0)
                if not valid.any():
                    continue
                filled = np.where(np.isfinite(patch), patch, 0.0)
                padded = np.zeros((c, tile, tile), dtype=np.float64)
                padded[:, :th, :tw] = filled
                x = normalise(padded, loaded.clip_lo, loaded.clip_hi, loaded.mean, loaded.std)
                batch_x.append(x.astype(np.float32))
                batch_dims.append((r0, c0, r1, c1, th, tw, valid))
            if not batch_x:
                continue
            t = torch.from_numpy(np.stack(batch_x))
            logits = net(t)
            probs = torch.softmax(logits, dim=1).numpy()
            for i, (r0, c0, r1, c1, th, tw, valid) in enumerate(batch_dims):
                p_flood_patch = probs[i, MODEL_CLASS_FLOOD, :th, :tw]
                p_perm_patch = probs[i, MODEL_CLASS_PERMANENT, :th, :tw]
                sum_flood[r0:r1, c0:c1] += np.where(valid, p_flood_patch, 0.0)
                sum_perm[r0:r1, c0:c1] += np.where(valid, p_perm_patch, 0.0)
                count[r0:r1, c0:c1] += valid.astype(np.float64)

    with np.errstate(invalid="ignore"):
        covered = count > 0
        p_flood = np.where(covered, sum_flood / np.maximum(count, 1), np.nan)
        p_perm = np.where(covered, sum_perm / np.maximum(count, 1), np.nan)
    return p_flood, p_perm


# --- disk cache: one inference per (model, scene, tiling), ever -------------
#
# The expensive step is the forward pass, not the grid of mode/fusion_weight/
# t_gorge points that follow it (those only re-threshold p_flood/p_perm - see
# strandline.detect._model_masks). Keying on the model's own sha256 plus a
# hash of the exact LINEAR input array plus the tiling parameters means the
# cache is self-invalidating: any change to the checkpoint, to upstream
# preprocessing, or to tile_px/tile_overlap_px produces a new key rather than
# a stale hit, with no separate "cache version" to remember to bump.

def _scene_cache_key(channels_linear: np.ndarray, tile: int, overlap: int,
                     model_sha256: str) -> str:
    h = hashlib.sha256()
    h.update(model_sha256.encode("utf-8"))
    h.update(f"|{tile}|{overlap}|".encode("utf-8"))
    h.update(np.ascontiguousarray(channels_linear, dtype=np.float64).tobytes())
    return h.hexdigest()


def _load_cached_prediction(cache_dir: Path, key: str) -> tuple[np.ndarray, np.ndarray] | None:
    path = Path(cache_dir) / f"{key}.npz"
    if not path.exists():
        return None
    try:
        with np.load(path) as npz:
            return npz["p_flood"], npz["p_perm"]
    except (OSError, ValueError, KeyError, EOFError):
        # A partial or corrupt cache file is not fatal (Rule 5): recompute.
        return None


def _save_cached_prediction(cache_dir: Path, key: str, p_flood: np.ndarray,
                            p_perm: np.ndarray) -> None:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # np.savez appends ".npz" itself when the given name lacks it, so the temp
    # name must already end in ".npz" or the later rename targets a path that
    # was never written.
    tmp = cache_dir / f".tmp-{os.getpid()}-{key}.npz"
    np.savez(tmp, p_flood=p_flood, p_perm=p_perm)
    tmp.replace(cache_dir / f"{key}.npz")


@dataclass
class ModelEvidence:
    """What the Kuro Siwo model contributed, duck-typed for strandline.detect
    (the same pattern as strandline.optical.OpticalEvidence): carries the
    fusion behaviour (`mode`, `fusion_weight`, `t_gorge`) alongside the
    per-pixel probabilities, so detect.py needs no separate ModelParams import.
    """

    p_flood: np.ndarray | None     # (H, W), [0, 1], NaN where not covered
    p_perm: np.ndarray | None      # (H, W), [0, 1], NaN where not covered
    available: bool
    mode: str                       # ModelParams.mode at the time evidence was built
    fusion_weight: float
    t_gorge: float
    sha256: str | None
    stats: dict


def run_model(pre_vv_db: np.ndarray, pre_vh_db: np.ndarray,
             post_vv_db: np.ndarray, post_vh_db: np.ndarray,
             params, model_path=None, cache_dir: Path | None = None,
             log=lambda *a: None) -> tuple[ModelEvidence | None, dict]:
    """Build model evidence for one prepared scene. NEVER raises.

    `params` is a strandline.config.ModelParams. `pre_*_db` are (n_dates, H, W)
    dB stacks (strandline.detect.preprocess output); the two dates nearest the
    event (the last two of the stack, which is built oldest-to-newest) are used
    as pre1/pre2, matching the checkpoint's documented channel order.

    `cache_dir`, when given, is checked for a prior (model, scene, tiling)
    inference before running the net, and written to afterwards - so the same
    scene scored again (a repeat live run, or a second pass over the same
    event in eval/tune.py) pays for the forward pass at most once, ever.
    `cache_dir=None` (the default) skips caching entirely; already-recorded
    scores stay reproducible either way, since caching only changes WHERE
    `p_flood`/`p_perm` come from, never their values.

    Returns (ModelEvidence | None, report). `report["status"]` is always one of
    MODEL_DISABLED / MODEL_UNAVAILABLE / MODEL_FAILED / MODEL_USED, so a run
    report can never be silent about whether the model ran.
    """
    from strandline.config import MODEL_MODE_PHYSICS

    report: dict = {"status": MODEL_DISABLED, "mode": params.mode if params else MODEL_MODE_PHYSICS}
    if params is None or params.mode == MODEL_MODE_PHYSICS:
        return None, report

    if pre_vv_db.shape[0] < 2:
        report["status"] = MODEL_UNAVAILABLE
        report["reason"] = (f"need at least 2 pre-event dates for pre1/pre2, got "
                            f"{pre_vv_db.shape[0]}")
        return None, report

    path = model_path or params.model_path
    loaded, load_status = load_model(path)
    report.update({k: v for k, v in load_status.items() if k != "available"})
    if loaded is None:
        report["status"] = MODEL_UNAVAILABLE
        report["reason"] = load_status["reason"]
        return None, report

    t0 = time.perf_counter()
    try:
        channels_db = np.stack([
            post_vv_db, post_vh_db,
            pre_vv_db[-1], pre_vh_db[-1],
            pre_vv_db[-2], pre_vh_db[-2],
        ])
        channels_linear = sigma0_db_to_linear(channels_db)
        coverage = clip_coverage(channels_linear, loaded.clip_lo, loaded.clip_hi)

        cache_key = None
        cached = None
        if cache_dir is not None:
            cache_key = _scene_cache_key(channels_linear, params.tile_px,
                                         params.tile_overlap_px, loaded.sha256)
            cached = _load_cached_prediction(Path(cache_dir), cache_key)
        if cached is not None:
            p_flood, p_perm = cached
            report["model_cache"] = "hit"
        else:
            p_flood, p_perm = predict_tiles(loaded, channels_linear,
                                            params.tile_px, params.tile_overlap_px)
            if cache_dir is not None:
                _save_cached_prediction(Path(cache_dir), cache_key, p_flood, p_perm)
            report["model_cache"] = "miss" if cache_dir is not None else "disabled"
    except Exception as err:
        report["status"] = MODEL_FAILED
        report["error_type"] = type(err).__name__
        report["reason"] = str(err)
        return None, report

    report["status"] = MODEL_USED
    report["sha256"] = loaded.sha256
    report["clip_coverage"] = {k: round(v, 4) for k, v in coverage.items()}
    report["p_flood_coverage"] = round(float(np.mean(np.isfinite(p_flood))), 4)
    report["inference_s"] = round(time.perf_counter() - t0, 2)
    log(f"      model inference: {report['model_cache']} in {report['inference_s']:.1f}s")
    evidence = ModelEvidence(p_flood=p_flood, p_perm=p_perm, available=True, mode=params.mode,
                             fusion_weight=params.fusion_weight, t_gorge=params.t_gorge,
                             sha256=loaded.sha256, stats=report)
    return evidence, report
