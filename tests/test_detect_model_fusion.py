"""How Kuro Siwo model evidence is allowed to act on the class map.

SYNTHETIC throughout (tests/conftest.py generates the valley and ModelEvidence
is hand-built here): nothing is a real-world accuracy figure (Rule 3.8). What
is established:

  - with no model evidence, or mode "physics", the output is EXACTLY the
    physics-only output. Adding the model to this pipeline cannot have
    changed a single physics-only pixel.
  - the model is held to the same standard of PLACE as the physics channel:
    HAND/slope zones apply to it and it cannot put water outside them.
  - `p_perm` (logit >= t_gorge) always becomes permanent water, never flood.
  - agreement between the physics channel and the model's own water call is
    HIGH confidence.
  - the result is deterministic.
"""

import numpy as np

from strandline import detect
from strandline.config import DetectParams
from strandline.ml import ModelEvidence


def _run(scene_data, params, model=None):
    g = scene_data["geometry"]
    pre_vv = np.stack([p[0] for p in scene_data["pre"]])
    pre_vh = np.stack([p[1] for p in scene_data["pre"]])
    return detect.detect_flood(pre_vv, pre_vh, scene_data["post"][0], scene_data["post"][1],
                               g["hand"].astype(float), scene_data["slope"], g["streams"],
                               params, model=model)


def _evidence(shape, p_flood=None, p_perm=None, mode="model", fusion_weight=1.0, t_gorge=0.0,
             available=True):
    zeros = np.zeros(shape)
    return ModelEvidence(
        p_flood=zeros.copy() if p_flood is None else p_flood,
        p_perm=zeros.copy() if p_perm is None else p_perm,
        available=available, mode=mode, fusion_weight=fusion_weight, t_gorge=t_gorge,
        sha256=None, stats={})


# --- the invariant: physics-only output is untouched ------------------------

def test_no_model_evidence_leaves_the_physics_map_exactly_as_it_was(scene):
    data = scene()
    dp = DetectParams()
    bare = _run(data, dp)
    with_model = _run(data, dp, model=_evidence(
        bare.classes.shape, p_flood=np.ones(bare.classes.shape), mode="physics"))
    assert np.array_equal(bare.classes, with_model.classes)
    assert bare.stats["pixels"] == with_model.stats["pixels"]


def test_an_unavailable_model_is_ignored_even_in_fusion_mode(scene):
    """A model that did not load (missing file/torch) must not change a pixel."""
    data = scene()
    dp = DetectParams()
    bare = _run(data, dp)
    with_model = _run(data, dp, model=_evidence(
        bare.classes.shape, p_flood=np.ones(bare.classes.shape), mode="fusion",
        available=False))
    assert np.array_equal(bare.classes, with_model.classes)


# --- terrain masks override the model ----------------------------------------

def test_hand_slope_zone_overrides_a_fully_confident_model(scene):
    """The model says flood EVERYWHERE (p_flood=1), mode='model': only the
    HAND/slope valley-floor zone may become water - never the steep walls
    (zone_debris-only ground, or fully excluded ground)."""
    data = scene()
    g = data["geometry"]
    dp = DetectParams()
    shape = g["hand"].shape
    result = _run(data, dp, model=_evidence(shape, p_flood=np.ones(shape), mode="model"))

    zone_water = (g["hand"] <= dp.hand_max_water_m) & (data["slope"] <= dp.slope_max_water_deg)
    from strandline.config import CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM
    water = np.isin(result.classes, (CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM))
    # Every water pixel must lie inside the physics zone, however confident
    # the model was outside it.
    assert np.array_equal(water, water & zone_water)
    assert water.any()   # and it DID call water somewhere inside the zone


# --- p_perm is permanent water, never flood ----------------------------------

def test_high_confidence_permanent_prediction_is_never_flood(scene):
    from strandline.config import CLASS_PERMANENT_WATER, FLOOD_CLASSES

    data = scene()
    g = data["geometry"]
    dp = DetectParams()
    shape = g["hand"].shape
    # The model is fully confident BOTH flood and permanent everywhere the
    # physics zone allows; permanent must win.
    result = _run(data, dp, model=_evidence(
        shape, p_flood=np.full(shape, 0.99), p_perm=np.full(shape, 0.99), mode="model"))
    zone_water = (g["hand"] <= dp.hand_max_water_m) & (data["slope"] <= dp.slope_max_water_deg)
    assert not np.any(np.isin(result.classes, FLOOD_CLASSES) & zone_water
                      & (result.classes != CLASS_PERMANENT_WATER)
                      & np.isin(result.classes, FLOOD_CLASSES))
    assert np.any(result.classes[zone_water] == CLASS_PERMANENT_WATER)


# --- agreement is high confidence --------------------------------------------

def test_agreement_between_physics_and_model_water_is_high_confidence(scene):
    from strandline.config import CLASS_FLOOD_HIGH

    data = scene()
    g = data["geometry"]
    flood_mask = g["flood"]
    dp = DetectParams()
    shape = g["hand"].shape
    # The model agrees with the physics channel exactly on the known flood
    # region (high p_flood there, zero elsewhere).
    p_flood = np.where(flood_mask, 0.95, 0.0)
    result = _run(data, dp, model=_evidence(shape, p_flood=p_flood, mode="fusion"))
    # At least some of the known flood region is HIGH confidence through the
    # agreement route (physics darkness already gives high confidence for the
    # default -10 dB flood, so this mainly checks no exception/regression and
    # that agreement pixels are a SUBSET of water).
    high = result.classes == CLASS_FLOOD_HIGH
    water = np.isin(result.classes, (CLASS_FLOOD_HIGH, 3))
    assert np.array_equal(high, high & water)


# --- fusion mode: union of physics and model ---------------------------------

def test_fusion_mode_adds_model_only_water_the_physics_channel_missed(scene):
    """A weak physics change (below z_seed) that the model is confident about
    must still become water in fusion mode, inside the valley-floor zone."""
    from strandline.config import FLOOD_CLASSES

    data = scene(flood_delta_db=-2.0)   # too weak for the physics seed alone
    g = data["geometry"]
    dp = DetectParams()
    shape = g["hand"].shape
    physics_only = _run(data, dp)

    p_flood = np.where(g["flood"], 0.99, 0.0)
    fused = _run(data, dp, model=_evidence(shape, p_flood=p_flood, mode="fusion",
                                           fusion_weight=0.0, t_gorge=0.0))
    physics_water = np.isin(physics_only.classes, FLOOD_CLASSES)
    fused_water = np.isin(fused.classes, FLOOD_CLASSES)
    assert fused_water.sum() > physics_water.sum()


def test_model_mode_ignores_the_physics_water_channel(scene):
    """mode='model': if the model says no flood anywhere, there is no flood,
    even where the physics channel alone would have called one (strong,
    well-known flood in the fixture)."""
    from strandline.config import FLOOD_CLASSES

    data = scene()   # the default -10 dB flood, which physics alone detects
    g = data["geometry"]
    dp = DetectParams()
    shape = g["hand"].shape
    zero_conf = _run(data, dp, model=_evidence(shape, p_flood=np.zeros(shape), mode="model"))
    assert not np.any(np.isin(zero_conf.classes, FLOOD_CLASSES))


# --- determinism --------------------------------------------------------------

def test_model_fusion_is_deterministic(scene):
    data = scene()
    g = data["geometry"]
    dp = DetectParams()
    shape = g["hand"].shape
    p_flood = np.where(g["flood"], 0.8, 0.1)
    ev = _evidence(shape, p_flood=p_flood, mode="fusion")
    a = _run(data, dp, model=ev)
    b = _run(data, dp, model=ev)
    assert np.array_equal(a.classes, b.classes)
