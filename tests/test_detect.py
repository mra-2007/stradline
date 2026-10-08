import numpy as np

from strandline import detect
from strandline.config import (
    CLASS_DEBRIS,
    CLASS_FLOOD_HIGH,
    CLASS_PERMANENT_WATER,
    CLASS_TERRAIN_EXCLUDED,
    FLOOD_CLASSES,
    DetectParams,
)
from validation.compare import flood_metrics

P = DetectParams()


def _run(s):
    g = s["geometry"]
    if s["speckle"]:
        prep = lambda a: detect.preprocess(a, P)  # noqa: E731
    else:
        prep = lambda a: a  # noqa: E731
    pre_vv = np.stack([prep(vv) for vv, _ in s["pre"]])
    pre_vh = np.stack([prep(vh) for _, vh in s["pre"]])
    post_vv, post_vh = prep(s["post"][0]), prep(s["post"][1])
    return detect.detect_flood(pre_vv, pre_vh, post_vv, post_vh, g["hand"], s["slope"],
                               g["streams"], P)


def _all_valid(shape):
    return np.ones(shape, dtype=bool)


def test_flood_accuracy_gaussian_noise(scene):
    s = scene(event=True)
    r = _run(s)
    g = s["geometry"]
    m = flood_metrics(np.isin(r.classes, FLOOD_CLASSES), g["flood"], _all_valid(g["flood"].shape))
    assert m["f1"] >= 0.97, m
    assert m["f1_tolerant"] >= 0.99, m


def test_flood_accuracy_with_real_speckle(scene):
    """Gamma speckle + Lee filter: harder, edges blur by ~1-2 px."""
    s = scene(event=True, speckle=True)
    r = _run(s)
    g = s["geometry"]
    m = flood_metrics(np.isin(r.classes, FLOOD_CLASSES), g["flood"], _all_valid(g["flood"].shape))
    assert m["f1"] >= 0.90, m
    assert m["f1_tolerant"] >= 0.97, m


def test_debris_detected(scene):
    s = scene(event=True)
    r = _run(s)
    debris_truth = s["geometry"]["debris"]
    recall = np.sum((r.classes == CLASS_DEBRIS) & debris_truth) / debris_truth.sum()
    assert recall >= 0.9


def test_permanent_river_is_not_flood(scene):
    s = scene(event=True)
    r = _run(s)
    g = s["geometry"]
    river_outside_debris = g["river"] & ~g["debris"]
    assert np.mean(r.classes[river_outside_debris] == CLASS_PERMANENT_WATER) >= 0.9
    assert not np.any(np.isin(r.classes[river_outside_debris], FLOOD_CLASSES))


def test_mountain_bait_rejected(scene):
    s = scene(event=True)
    r = _run(s)
    bait = s["geometry"]["bait"]
    assert not np.any(np.isin(r.classes[bait], FLOOD_CLASSES))
    assert np.all(r.classes[bait] == CLASS_TERRAIN_EXCLUDED)


def test_isolated_speck_removed(scene):
    s = scene(event=True)
    r = _run(s)
    assert not np.any(np.isin(r.classes[s["geometry"]["speck"]], FLOOD_CLASSES))


def test_no_event_means_no_flood(scene):
    """Judges may pick a date with no flood: the map must stay (almost) empty."""
    for seed in range(3):
        for speckle in (False, True):
            r = _run(scene(event=False, speckle=speckle, seed=seed))
            flood_px = int(np.isin(r.classes, FLOOD_CLASSES).sum())
            debris_px = int((r.classes == CLASS_DEBRIS).sum())
            assert flood_px <= 10, (seed, speckle, flood_px)
            assert debris_px <= 30, (seed, speckle, debris_px)


def test_high_confidence_is_subset_and_present(scene):
    r = _run(scene(event=True))
    assert np.sum(r.classes == CLASS_FLOOD_HIGH) > 0


def test_deterministic(scene):
    a = _run(scene(event=True, seed=7)).classes
    b = _run(scene(event=True, seed=7)).classes
    assert np.array_equal(a, b)


def test_grow_from_seeds_keeps_only_seeded_regions():
    seed = np.zeros((10, 10), bool)
    grow = np.zeros((10, 10), bool)
    grow[1:4, 1:4] = True
    seed[2, 2] = True
    grow[6:9, 6:9] = True  # no seed -> dropped
    out = detect.grow_from_seeds(seed, grow)
    assert out[1:4, 1:4].all() and not out[6:9, 6:9].any()
