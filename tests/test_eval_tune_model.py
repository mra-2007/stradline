"""The model leave-one-event-out pass: hashing, contamination exclusion, and
end-to-end scoring with a TINY FAKE CHECKPOINT (SYNTHETIC - Rule 3.8; no
number here is a real-world accuracy figure).

What is checked:
  - a point with no `model.*` key is still the radar(+optical/extent)-only
    configuration (same invariant the optical pass is held to);
  - `model_path` is part of the hash, so a re-trained checkpoint at the same
    mode/t_gorge/fusion_weight cannot reuse a cached score written under a
    different checkpoint file;
  - an event in KS_TRAINED_EVENTS is scored but never chosen FROM and never
    held OUT by `leave_one_out`/`select`, and is marked `contaminated` in its
    score's provenance;
  - `build_model_matrix` prepares a scene (and therefore runs inference)
    exactly ONCE per event, however many model grid points are scored - every
    other point only re-thresholds the cached evidence.
"""

import weakref
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from eval import tune
from eval.run_scoreboard import EventScore
from strandline import ml
from strandline.config import ModelParams

torch = pytest.importorskip("torch")
smp = pytest.importorskip("segmentation_models_pytorch")

from tests.test_eval_scoreboard import prepared  # noqa: E402  (reuse the fake-scene builder)

N_PRE = 3
CEILING = 0.01

MODEL = {"model.mode": "model", "model.t_gorge": 0.0}
FUSION = {"model.mode": "fusion", "model.t_gorge": 0.0, "model.fusion_weight": 1.0}
PHYSICS: dict = {}


def _tiny_checkpoint(path: Path) -> None:
    net = smp.Unet("resnet34", encoder_weights=None, in_channels=ml.N_CHANNELS, classes=3)
    torch.save({
        "state_dict": net.state_dict(),
        "mean": np.array([0.1] * ml.N_CHANNELS, dtype=np.float32),
        "std": np.array([0.05] * ml.N_CHANNELS, dtype=np.float32),
        "clip_lo": np.array([0.0] * ml.N_CHANNELS, dtype=np.float32),
        "clip_hi": np.array([1.0] * ml.N_CHANNELS, dtype=np.float32),
    }, str(path))


# --- hashing and materialisation ---------------------------------------------

def test_a_point_with_no_model_key_is_the_radar_only_configuration():
    dp, rp, op, ep, mp = tune.materialise_full_model(PHYSICS, N_PRE)
    assert mp is None
    from strandline import run as pipeline
    assert pipeline.params_hash(dp, rp, op, ep, mp) == pipeline.params_hash(dp, rp)


def test_different_model_paths_at_the_same_mode_hash_differently():
    h1 = tune.full_point_hash_model(MODEL, N_PRE, model_path="models/model_1.pt")
    h2 = tune.full_point_hash_model(MODEL, N_PRE, model_path="models/model_2.pt")
    assert h1 != h2, "a re-trained checkpoint must never reuse another one's cached score"


def test_no_model_path_falls_back_to_the_shipped_default():
    dp, rp, op, ep, mp = tune.materialise_full_model(MODEL, N_PRE)
    assert mp.model_path == ModelParams.model_path


def test_materialise_model_sets_the_given_model_path():
    dp, rp, op, ep, mp = tune.materialise_full_model(MODEL, N_PRE, model_path="models/x.pt")
    assert mp.model_path == "models/x.pt"
    assert mp.mode == "model"
    assert mp.t_gorge == 0.0


# --- contamination exclusion (synthetic matrix, no model needed) ------------

def _entry(event, kind, phash, f1_tol) -> EventScore:
    return EventScore(event=event, kind=kind, params_hash=phash,
                      metrics={"f1_tolerant": f1_tol, "false_flood_fraction": 0.0})


def _matrix_of(events: dict[str, float], points: list[dict]) -> tune.Matrix:
    """events: {name: f1_tolerant}. Every point scores identically (f1 fixed per
    event) so which point `select` prefers is decided purely by the grid order
    tie-break, keeping the test about EXCLUSION, not about which point wins."""
    out: tune.Matrix = {}
    for name, f1 in events.items():
        for point in points:
            phash = tune.full_point_hash_model(point, N_PRE)
            out[(name, phash)] = _entry(name, "flood", phash, f1)
    return out


def test_a_contaminated_event_is_never_the_held_out_fold():
    contaminated = "pakistan_larkana_2022"
    clean = ["event_a", "event_b", "event_c"]
    points = [MODEL, FUSION]
    matrix = _matrix_of({**{e: 0.8 for e in clean}, contaminated: 0.99}, points)

    selections = tune.leave_one_out(matrix, clean, [], points, CEILING, N_PRE,
                                    tune.full_point_hash_model)
    assert contaminated not in selections
    assert set(selections) == set(clean)


def test_a_contaminated_event_cannot_move_the_final_choice():
    """Its score (0.99, deliberately the best) must not appear in the training
    mean that decides `select`'s winner - only the clean events' mean does."""
    contaminated = "pakistan_larkana_2022"
    matrix = _matrix_of({"event_a": 0.70, "event_b": 0.70, contaminated: 0.99}, [MODEL, FUSION])
    final = tune.select(matrix, ["event_a", "event_b"], [], [MODEL, FUSION], CEILING, N_PRE,
                        tune.full_point_hash_model)
    assert final.ok
    # Both points score identically among the clean events (0.70 each), so the
    # grid-order tie-break picks MODEL (first); if the contaminated event's
    # 0.99 had leaked in, this assertion is not what proves it - the objective
    # value is what would differ.
    assert final.chosen.objective == pytest.approx(0.70)


def test_mark_contamination_stamps_every_score_in_place():
    matrix = _matrix_of({"event_a": 0.8, "pakistan_larkana_2022": 0.9}, [MODEL])
    tune.mark_contamination(matrix, ("pakistan_larkana_2022",))
    for (event, _), score in matrix.items():
        assert score.provenance["contaminated"] == (event == "pakistan_larkana_2022")


def test_mark_contamination_accepts_a_plain_list_too():
    scores = [_entry("event_a", "flood", "h1", 0.8),
             _entry("pakistan_larkana_2022", "flood", "h2", 0.9)]
    tune.mark_contamination(scores, ("pakistan_larkana_2022",))
    assert scores[0].provenance["contaminated"] is False
    assert scores[1].provenance["contaminated"] is True


# --- end-to-end with a tiny fake checkpoint: inference runs once per event --

@pytest.fixture
def two_scenes(scene, tmp_path):
    """Two synthetic events (one standing in for the 'contaminated' one), each
    with its own prepared scene, reusing the conftest valley fixture."""
    from tests.test_eval_scoreboard import write_reference, EVENT_DAY

    def build(name, seed):
        data = scene(event=True, n_pre=N_PRE, seed=seed)
        scn = prepared(data, n_pre=N_PRE)
        g = data["geometry"]
        ref = write_reference(tmp_path / name / "ref.shp", g["flood"], scn.grid)
        event = {
            "name": name, "kind": "flood", "independent_reference": True,
            "event_date": str(EVENT_DAY), "bbox": [85.30, 27.90, 85.36, 27.96],
            "reference": str(ref),
            "reference_classes": {"water": ["Flooded area"], "debris": []},
        }
        return event, scn

    return dict(a=build("model_pass_event_a", 1), b=build("model_pass_event_b", 2))


def test_build_model_matrix_infers_once_per_event_then_rethresholds(
        monkeypatch, tmp_path, two_scenes):
    path = tmp_path / "tiny.pt"
    _tiny_checkpoint(path)

    events = [two_scenes["a"][0], two_scenes["b"][0]]
    scenes_by_name = {e["name"]: s for e, s in two_scenes.values()}

    prepare_calls = {"n": 0}
    real_run_model = ml.run_model

    def fake_prepare_event(event, rp, dp, runs_root, allow_download, max_pixels, *,
                           op=None, mp=None, log=print):
        prepare_calls["n"] += 1
        scn = scenes_by_name[event["name"]]
        evidence, report = real_run_model(scn.pre_vv_db, scn.pre_vh_db, scn.post_vv_db,
                                          scn.post_vh_db, mp)
        return replace(scn, model_evidence=evidence, model_params=mp, model_report=report)

    monkeypatch.setattr(tune, "prepare_event", fake_prepare_event)

    points = [tune.combine({}, {"model.mode": "model", "model.t_gorge": v})
              for v in (-1.0, 0.0, 1.0)]
    matrix = tune.build_model_matrix(events, points, tmp_path / "runs", n_pre=N_PRE,
                                     reuse=False, allow_download=False,
                                     model_path=str(path), log=lambda *a: None)

    for event in events:
        for point in points:
            phash = tune.full_point_hash_model(point, N_PRE, model_path=str(path))
            assert (event["name"], phash) in matrix
            assert matrix[(event["name"], phash)].ok, matrix[(event["name"], phash)].error

    # One `prepare_event` call per event, regardless of 3 grid points each -
    # the probe-then-rethreshold design (`scene_with_model`) must not re-run
    # inference per point.
    assert prepare_calls["n"] == len(events)


def test_build_model_matrix_releases_each_scene_before_the_next_event(
        monkeypatch, tmp_path, two_scenes):
    """Each event's scene (a loaded torch model plus several full-resolution
    SAR arrays) must be unreachable once the next event's scene is being
    prepared - checked directly with a weakref, rather than trusted to
    CPython's refcounting timing. Does NOT reproduce the 2026-10-15 OOM kill
    (this synthetic scene is too small, and the leak did not reproduce even
    with `gc.collect()` removed) - it only pins the intended memory contract
    of `build_model_matrix`'s per-event loop."""
    path = tmp_path / "tiny.pt"
    _tiny_checkpoint(path)
    events = [two_scenes["a"][0], two_scenes["b"][0]]
    scenes_by_name = {e["name"]: s for e, s in two_scenes.values()}
    refs: list[weakref.ReferenceType] = []
    real_run_model = ml.run_model

    def fake_prepare_event(event, rp, dp, runs_root, allow_download, max_pixels, *,
                           op=None, mp=None, log=print):
        scn = scenes_by_name[event["name"]]
        evidence, report = real_run_model(scn.pre_vv_db, scn.pre_vh_db, scn.post_vv_db,
                                          scn.post_vh_db, mp)
        full = replace(scn, model_evidence=evidence, model_params=mp, model_report=report)
        refs.append(weakref.ref(full))
        return full

    monkeypatch.setattr(tune, "prepare_event", fake_prepare_event)
    points = [{"model.mode": "model", "model.t_gorge": 0.0}]
    tune.build_model_matrix(events, points, tmp_path / "runs", n_pre=N_PRE, reuse=False,
                            allow_download=False, model_path=str(path), log=lambda *a: None)

    assert len(refs) == 2
    assert refs[0]() is None, "the first event's scene is still alive after the second ran"
