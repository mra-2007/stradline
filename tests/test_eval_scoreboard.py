"""The scoreboard, on a synthetic event with KNOWN truth.

SYNTHETIC, and labelled as such: the valley, the flood and the reference map
here are generated, so the numbers these tests assert are properties of the
scoring code, never measurements of real-world accuracy (Rule 3.8, Rule 6).

What is checked:
  - the reference_classes mapping selects the right polygons, through a real
    vector file, including re-projection onto the analysis grid;
  - flood events get strict AND tolerant precision/recall/F1/IoU, controls get
    false_flood_fraction;
  - the scoring domain is not quietly shrunk (terrain-excluded ground stays in);
  - rows match the column contract at the end of eval/gates.yaml exactly;
  - a kind with a failed event gets NO rows written at all;
  - a missing raster is refused with the command that fetches it, and nothing
    is downloaded.
"""

import csv
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import geopandas as gpd
import numpy as np
import pytest
import yaml
from pathlib import Path
from shapely.geometry import box, shape

from eval import scoreboard as sb
from eval import run_scoreboard as rs
from strandline import catalog, ingest, run as pipeline, terrain as terr_mod
from strandline.config import (
    CLASS_NODATA,
    CLASS_TERRAIN_EXCLUDED,
    DetectParams,
    RunParams,
)
from validation import compare

ROOT = Path(__file__).resolve().parents[1]
GATES = yaml.safe_load((ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8"))

RES = 20.0
EPSG = 32645
WEST, SOUTH = 500_000.0, 3_000_000.0
EVENT_DAY = date(2024, 7, 10)


# --- a prepared scene with no network and no files ---------------------------

def _grid(h, w):
    return ingest.Grid(EPSG, RES, WEST, SOUTH, WEST + w * RES, SOUTH + h * RES)


def _acq(day, track=("orbit", 85, "ASCENDING")):
    start = datetime(day.year, day.month, day.day, 12, 15, tzinfo=timezone.utc)
    return catalog.Acquisition(track=track, day=day, start=start,
                               stop=start + timedelta(seconds=25),
                               names=[f"S1A_{day:%Y%m%d}_synthetic"], footprint=box(0, 0, 1, 1),
                               coverage=1.0)


def prepared(scene_data, n_pre=3) -> pipeline.PreparedScene:
    """Wrap the synthetic valley from conftest as a PreparedScene."""
    g = scene_data["geometry"]
    pre = scene_data["pre"][:n_pre]
    h, w = g["dem"].shape
    grid = _grid(h, w)
    terrain = terr_mod.Terrain(
        slope_deg=scene_data["slope"], hand_m=g["hand"].astype(float),
        streams=g["streams"], flow_dir=np.zeros_like(g["dem"], dtype=np.uint8),
        accumulation=np.zeros_like(g["dem"]), hand_fallback_fraction=0.0)
    choice = catalog.TrackChoice(
        track=("orbit", 85, "ASCENDING"), post=_acq(EVENT_DAY + timedelta(days=1)),
        pre=[_acq(EVENT_DAY - timedelta(days=12 * (k + 1))) for k in reversed(range(n_pre))],
        latency_days=1)
    dp = DetectParams()
    return pipeline.PreparedScene(
        grid=grid, choice=choice,
        pre_vv_db=np.stack([p[0] for p in pre]), pre_vh_db=np.stack([p[1] for p in pre]),
        post_vv_db=scene_data["post"][0], post_vh_db=scene_data["post"][1],
        terrain=terrain, run_params=replace(RunParams(), n_pre=n_pre),
        preprocess_params={f: getattr(dp, f) for f in pipeline.PREPROCESS_FIELDS})


def write_reference(path: Path, mask: np.ndarray, grid, *, value="Flooded area",
                    decoys: np.ndarray | None = None, to_epsg: int | None = None) -> Path:
    """Vectorise a truth mask into a reference layer with an attribute column.

    `decoys` are polygons of another class in the SAME file, so the test can
    prove that reference_classes filtering happens rather than everything
    being swept in.
    """
    from rasterio.features import shapes

    def polys(m):
        return [shape(geom) for geom, v in
                shapes(m.astype("uint8"), mask=m, transform=grid.transform) if v == 1]

    geoms = polys(mask)
    rows = [{"notation": value, "obj_desc": "Riverine flood"} for _ in geoms]
    if decoys is not None:
        extra = polys(decoys)
        geoms += extra
        rows += [{"notation": "Not Applicable", "obj_desc": "Mudflow"} for _ in extra]
    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs=grid.crs)
    if to_epsg is not None:
        gdf = gdf.to_crs(f"EPSG:{to_epsg}")
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path)
    return path


@pytest.fixture
def synthetic(scene, tmp_path):
    """A flood event, its prepared scene and a reference drawn from the truth."""
    data = scene(event=True, n_pre=3, seed=3)
    scn = prepared(data)
    g = data["geometry"]
    ref = write_reference(tmp_path / "ref" / "event.shp", g["flood"], scn.grid,
                          decoys=g["debris"])
    event = {
        "name": "synthetic_valley_2024",
        "kind": "flood",
        "independent_reference": True,
        "event_date": str(EVENT_DAY),
        "bbox": [85.30, 27.90, 85.36, 27.96],
        "reference": str(ref),
        "reference_classes": {"water": ["Flooded area"], "debris": ["Not Applicable"]},
        "ref_sensor_name": "synthetic",
    }
    return {"event": event, "scene": scn, "data": data, "ref": ref}


# --- reference_classes --------------------------------------------------------

def test_reference_classes_selects_only_the_declared_class(synthetic):
    """The decoy polygons in the same file must not enter the water reference."""
    grid = synthetic["scene"].grid
    water, sel_w = compare.reference_mask(synthetic["ref"], grid, ["Flooded area"])
    everything, sel_all = compare.reference_mask(synthetic["ref"], grid, [compare.ALL_POLYGONS])
    truth = synthetic["data"]["geometry"]["flood"]

    assert sel_w.matched_columns == {"notation": sel_w.n_selected}
    assert sel_w.n_selected < sel_all.n_selected, "filtering selected everything"
    assert water.sum() == pytest.approx(truth.sum(), rel=0.02)
    assert everything.sum() > water.sum(), "the decoy class was not in the file"


def test_reference_classes_all_polygons_takes_the_whole_layer(synthetic):
    _, sel = compare.reference_mask(synthetic["ref"], synthetic["scene"].grid, ["__all__"])
    assert sel.all_polygons and sel.n_selected == sel.n_total


def test_unknown_reference_class_is_refused_not_scored_as_zero(synthetic):
    """A typo must fail loudly: an empty reference would look like 0 recall."""
    with pytest.raises(compare.ReferenceClassError) as err:
        compare.reference_mask(synthetic["ref"], synthetic["scene"].grid, ["flooded area"])
    assert "Did you mean" in str(err.value)


def test_reference_is_reprojected_onto_the_analysis_grid(scene, tmp_path):
    """References arrive in EPSG:4326 or 3857; the mask must still line up."""
    data = scene(event=True, n_pre=3, seed=4)
    scn = prepared(data)
    truth = data["geometry"]["flood"]
    ref = write_reference(tmp_path / "ll" / "event.geojson", truth, scn.grid, to_epsg=4326)
    mask, _ = compare.reference_mask(ref, scn.grid, ["Flooded area"])
    overlap = (mask & truth).sum() / truth.sum()
    assert overlap > 0.97, f"re-projected reference misaligned: {overlap:.3f} overlap"


# --- metrics ------------------------------------------------------------------

def test_flood_event_gets_strict_and_tolerant_metrics(synthetic):
    score = rs.score_with_scene(synthetic["event"], synthetic["scene"], DetectParams(),
                                tolerance_px=1, log=lambda *a: None)
    assert score.ok, score.error
    for key in ("precision", "recall", "f1", "iou",
                "precision_tolerant", "recall_tolerant", "f1_tolerant"):
        assert key in score.metrics, f"{key} not reported"
        assert 0.0 <= float(score.metrics[key]) <= 1.0
    assert "false_flood_fraction" not in score.metrics, "a flood event has no control metric"
    assert score.metrics["f1_tolerant"] >= score.metrics["f1"], "tolerance must not lower F1"


def test_tolerant_and_strict_are_both_reported_by_the_gate_contract():
    """Rule 2: strict scores are always reported next to the tolerant ones."""
    gates = GATES["modules"]["flood_mapping"]["gates"]
    reported = {g["metric"] for g in gates}
    assert {"f1", "f1_tolerant", "iou"} <= reported
    assert set(rs.METRIC_COLUMNS) >= reported - {"false_flood_fraction"}


def test_a_perfect_map_scores_one(synthetic):
    """Sanity floor on the metric code itself: prediction == reference => 1.0."""
    scn = synthetic["scene"]
    result = pipeline.classify(scn, DetectParams())
    pred = compare.flood_mask(result.classes)
    domain = compare.scoring_domain(result.classes)
    m = compare.flood_metrics(pred, pred, domain, tolerance_px=1)
    assert m["precision"] == m["recall"] == m["f1"] == m["iou"] == 1.0
    assert m["f1_tolerant"] == 1.0


def test_control_is_scored_on_false_flood_fraction_only(synthetic):
    control = {**synthetic["event"], "name": "synthetic_valley_control_2024",
               "kind": "control", "independent_reference": None}
    control.pop("reference")
    score = rs.score_with_scene(control, synthetic["scene"], DetectParams(), log=lambda *a: None)
    assert score.kind == sb.KIND_CONTROL
    assert list(score.metrics) == ["false_flood_fraction"]
    assert 0.0 <= float(score.metrics["false_flood_fraction"]) <= 1.0


def test_control_on_a_dry_scene_reports_almost_no_flood(scene):
    """The control metric must actually respond to there being no flood."""
    data = scene(event=False, n_pre=3, seed=5)
    control = {"name": "dry_control", "kind": "control", "independent_reference": None,
               "event_date": str(EVENT_DAY), "bbox": [85.3, 27.9, 85.36, 27.96]}
    score = rs.score_with_scene(control, prepared(data), DetectParams(), log=lambda *a: None)
    assert float(score.metrics["false_flood_fraction"]) <= 0.01


# --- the scoring domain is not quietly shrunk --------------------------------

def test_terrain_excluded_ground_stays_in_the_scoring_domain(synthetic):
    """Rule 3.2: steep ground we refuse to analyse is still scored against us.

    If it were dropped from the domain, a reference flood there would stop
    counting as a miss and recall would rise for free.
    """
    result = pipeline.classify(synthetic["scene"], DetectParams())
    domain = compare.scoring_domain(result.classes)
    excluded = result.classes == CLASS_TERRAIN_EXCLUDED
    assert excluded.sum() > 0, "the synthetic valley has no steep ground to test with"
    assert (domain & excluded).sum() == excluded.sum(), "terrain-excluded pixels left the domain"
    assert domain.sum() == int((result.classes != CLASS_NODATA).sum())


def test_domain_fraction_is_reported(synthetic):
    """A shrinking domain is the easiest way to flatter a score, so it is visible."""
    score = rs.score_with_scene(synthetic["event"], synthetic["scene"], DetectParams(),
                                log=lambda *a: None)
    assert 0.0 < score.provenance["domain_fraction"] <= 1.0
    assert score.provenance["domain_pixels"] > 0
    assert score.provenance["reference"]["reference_ha"] > 0


def test_provenance_records_how_the_reference_was_selected(synthetic):
    score = rs.score_with_scene(synthetic["event"], synthetic["scene"], DetectParams(),
                                log=lambda *a: None)
    ref = score.provenance["reference"]
    assert "notation" in ref["selected_by"]
    assert ref["credit"] == compare.REFERENCE_CREDIT
    assert ref["independent_reference"] is True
    assert score.provenance["n_pre"] == 3
    assert score.provenance["parameters"]["detect"]["z_seed"] == DetectParams().z_seed


def test_the_primary_metric_is_the_flood_affected_union(synthetic):
    """Decided in docs/tuning_log.md before the new mode was scored: the gate
    columns carry our flood classes UNION debris, scored against the
    reference's flooded + debris/mass-movement classes."""
    score = rs.score_with_scene(synthetic["event"], synthetic["scene"], DetectParams(),
                                log=lambda *a: None)
    assert score.provenance["scored_quantity"].startswith("flood_affected")
    assert score.provenance["reference"]["scored_classes"] == ["Flooded area", "Not Applicable"]


def test_water_only_and_debris_only_are_reported_next_to_the_union(synthetic):
    score = rs.score_with_scene(synthetic["event"], synthetic["scene"], DetectParams(),
                                log=lambda *a: None)
    assert score.provenance["water_only"]["scored_classes"] == ["Flooded area"]
    assert "f1_tolerant" in score.provenance["water_only"]["metrics"]
    assert score.provenance["debris_only"]["scored_classes"] == ["Not Applicable"]
    assert not any(k.startswith("debris") or k.startswith("water_only")
                   for k in score.metrics), "only the union reaches a CSV metric column"


def test_the_union_reference_is_at_least_as_large_as_water_alone(synthetic):
    score = rs.score_with_scene(synthetic["event"], synthetic["scene"], DetectParams(),
                                log=lambda *a: None)
    assert (score.provenance["reference"]["reference_ha"]
            >= score.provenance["water_only"]["reference_ha"])


def test_a_control_false_flood_fraction_uses_the_same_union(synthetic):
    """So the 1% ceiling constrains the debris channel too."""
    control = {**synthetic["event"], "name": "syn_control", "kind": "control",
               "independent_reference": None}
    control.pop("reference")
    score = rs.score_with_scene(control, synthetic["scene"], DetectParams(),
                                log=lambda *a: None)
    union = float(score.metrics["false_flood_fraction"])
    water = float(score.provenance["false_flood_fraction_water_only"])
    assert union >= water, "the gated control metric must not be the narrower one"


# --- rows and the column contract --------------------------------------------

def _contract_columns() -> list[str]:
    """The flood.csv columns the gates file declares, parsed from its comment."""
    text = (ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8")
    body = text.split("flood.csv:", 1)[1].split("damage.csv:", 1)[0]
    names = [w.strip(" ,()|") for w in body.replace("\n#", " ").split()]
    return [n for n in names if n.isidentifier()]


def test_row_columns_match_the_gates_file_contract():
    declared = set(_contract_columns()) | {"run_id", "timestamp", "git_commit",
                                           "params_hash", "event"}
    declared.discard("kind")      # written as flood | flood_dependent | control
    declared.discard("split")
    actual = set(sb.COLUMNS["flood.csv"])
    assert declared - actual == set(), f"contract columns missing from the CSV: {declared - actual}"


def test_written_row_has_every_metric_and_the_derived_kind(synthetic, tmp_path):
    score = rs.score_with_scene(synthetic["event"], synthetic["scene"], DetectParams(),
                                log=lambda *a: None)
    rows = rs.rows_for([score], {score.event: synthetic["event"]}, "20260101T000000Z", "loeo")
    sb.append_rows("flood.csv", rows, results_dir=tmp_path)
    with (tmp_path / "flood.csv").open(newline="", encoding="utf-8") as f:
        got = list(csv.DictReader(f))
    assert len(got) == 1
    row = got[0]
    assert list(row) == sb.COLUMNS["flood.csv"], "column order drifted from the contract"
    assert row["kind"] == "flood" and row["split"] == "loeo"
    assert row["event"] == "synthetic_valley_2024"
    assert row["params_hash"] and row["run_id"] == "20260101T000000Z"
    for key in ("precision", "recall", "f1", "iou", "precision_tolerant",
                "recall_tolerant", "f1_tolerant"):
        assert row[key] != "", f"{key} empty in the written row"
    assert row["false_flood_fraction"] == ""


def test_dependent_reference_event_is_written_as_flood_dependent(synthetic, tmp_path):
    event = {**synthetic["event"], "independent_reference": False}
    score = rs.score_with_scene(event, synthetic["scene"], DetectParams(), log=lambda *a: None)
    rows = rs.rows_for([score], {score.event: event}, "R", "loeo")
    assert rows[0]["kind"] == "flood_dependent"


# --- completeness: a partial set must not become a gate ----------------------

def _score(name, kind, phash="H", f1=0.95, failed=False):
    metrics = ({} if failed else
               ({"false_flood_fraction": 0.001} if kind == sb.KIND_CONTROL
                else {"f1_tolerant": f1, "precision_tolerant": f1, "recall_tolerant": f1,
                      "precision": f1, "recall": f1, "f1": f1, "iou": f1}))
    return rs.EventScore(event=name, kind=kind, params_hash=phash, metrics=metrics,
                         error="boom" if failed else None)


def _event(name, kind="flood", independent=True):
    return {"name": name, "kind": kind,
            "independent_reference": None if kind == "control" else independent}


def test_no_rows_are_written_when_a_planned_event_failed(tmp_path):
    planned = [_event("a"), _event("b"), _event("c")]
    scores = [_score("a", "flood"), _score("b", "flood"), _score("c", "flood", failed=True)]
    written = rs.write_rows(scores, planned, {e["name"]: e for e in planned},
                            "R", "loeo", results_dir=tmp_path, log=lambda *a: None)
    assert written == {}, "a partial flood set was written"
    assert not (tmp_path / "flood.csv").exists()


def test_rows_are_written_when_the_kind_is_complete(tmp_path):
    planned = [_event("a"), _event("b"), _event("x", "control")]
    scores = [_score("a", "flood"), _score("b", "flood"), _score("x", sb.KIND_CONTROL)]
    written = rs.write_rows(scores, planned, {e["name"]: e for e in planned},
                            "R", "full", results_dir=tmp_path, log=lambda *a: None)
    assert written == {sb.KIND_FLOOD: 2, sb.KIND_CONTROL: 1}


def test_one_complete_kind_still_writes_when_another_kind_is_incomplete(tmp_path):
    """Controls complete, floods not: write the controls, withhold the floods."""
    planned = [_event("a"), _event("b"), _event("x", "control")]
    scores = [_score("a", "flood"), _score("b", "flood", failed=True),
              _score("x", sb.KIND_CONTROL)]
    written = rs.write_rows(scores, planned, {e["name"]: e for e in planned},
                            "R", "full", results_dir=tmp_path, log=lambda *a: None)
    assert written == {sb.KIND_CONTROL: 1}


def test_completeness_names_the_missing_events():
    planned = [_event("a"), _event("b")]
    status = rs.completeness([_score("a", "flood"), _score("b", "flood", failed=True)], planned)
    assert status[sb.KIND_FLOOD]["missing"] == ["b"]
    assert status[sb.KIND_FLOOD]["complete"] is False


# --- caching and refusals -----------------------------------------------------

def test_score_cache_round_trips(tmp_path):
    score = _score("a", "flood", phash="abc123")
    rs.save_score(score, runs_root=tmp_path)
    back = rs.load_score("a", "abc123", runs_root=tmp_path)
    assert back is not None and back.metrics == score.metrics and back.ok
    assert rs.load_score("a", "different", runs_root=tmp_path) is None


def test_missing_raster_is_refused_with_the_fetch_command(monkeypatch, tmp_path):
    """No cache, no download: the error must say how to get the data."""
    monkeypatch.setattr(ingest, "connect", lambda *a, **k: pytest.fail("must not log in"))
    monkeypatch.setattr(ingest, "download_s1", lambda *a, **k: pytest.fail("must not download"))
    monkeypatch.setattr(catalog, "search_s1_grd", _fake_scenes)
    event = {"name": "needs_data", "kind": "flood", "independent_reference": True,
             "event_date": str(EVENT_DAY), "bbox": [85.30, 27.90, 85.33, 27.93],
             "reference": "nowhere.shp", "reference_classes": {"water": ["__all__"]}}
    score = rs.score_event(event, DetectParams(), replace(RunParams(), n_pre=3),
                           runs_root=tmp_path, reuse=False, log=lambda *a: None)
    assert not score.ok
    assert "eval.fetch_dev_data" in score.error and "missing" in score.error


def test_an_oversized_event_is_refused_not_clipped(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog, "search_s1_grd", _fake_scenes)
    event = {"name": "huge", "kind": "flood", "independent_reference": True,
             "event_date": str(EVENT_DAY), "bbox": [85.0, 27.0, 86.5, 28.0],
             "reference": "nowhere.shp", "reference_classes": {"water": ["__all__"]}}
    score = rs.score_event(event, DetectParams(), replace(RunParams(), n_pre=3),
                           runs_root=tmp_path, reuse=False, max_pixels=1_000_000,
                           log=lambda *a: None)
    assert not score.ok
    assert "Mpx exceeds" in score.error and "Rule 3.2" in score.error


def _fake_scenes(*_args, **_kwargs):
    out = []
    days = ([EVENT_DAY - timedelta(days=12 * k) for k in range(4, 0, -1)]
            + [EVENT_DAY + timedelta(days=1)])
    for d in days:
        start = datetime(d.year, d.month, d.day, 12, 15, tzinfo=timezone.utc)
        out.append(catalog.Scene(name=f"S1A_{d:%Y%m%d}_fake", start=start,
                                 stop=start + timedelta(seconds=25), platform="S1A",
                                 relative_orbit=85, direction="ASCENDING",
                                 footprint=box(80, 20, 90, 30)))
    return out


def test_prepare_time_parameters_cannot_be_varied_on_a_prepared_scene(synthetic):
    """lee_window is baked in; changing it must raise, not silently mislead."""
    with pytest.raises(pipeline.PipelineError) as err:
        pipeline.classify(synthetic["scene"], replace(DetectParams(), lee_window=7))
    assert "prepared scene" in str(err.value)


# --- the contract read from the gates file ------------------------------------

def test_contract_comes_from_the_gates_file():
    con = rs.contract()
    assert con["boundary_tolerance_px"] == GATES["contract"]["boundary_tolerance_px"]
    assert con["resolution_m"] == GATES["contract"]["resolution_m"]
