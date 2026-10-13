"""Scoring the damage overlay: set agreement, the CSV contract, and refusals.

SYNTHETIC: the flagged sets and the hand-made reference mask below are written
out here, so nothing in this file is real-world damage accuracy (Rule 3.8).

What is established:
  - `set_metrics` is a set-F1 with the edge cases decided deliberately, in
    particular that "neither extent flagged anything" scores None rather than
    a free 1.0 that could lift a gate average;
  - the reference extent is turned into geometry by exactly the same code path
    as ours, so a difference in the flagged sets can only come from the extent;
  - a partial set of events writes NO rows (Rule 3.2);
  - the rows match the column contract declared at the end of eval/gates.yaml,
    and the gate reads them.
"""

from __future__ import annotations

import csv

import geopandas as gpd
import numpy as np
import pytest
import yaml
from rasterio.transform import from_origin
from shapely.geometry import box

from eval import damage_eval
from strandline import damage
from strandline.config import CLASS_FLOOD_MEDIUM, CLASS_NO_CHANGE, DamageParams, ExtentParams

ROOT = damage_eval.ROOT
RES = 20.0
N = 50
CRS = "EPSG:32633"


class Grid:
    def __init__(self):
        self.res = RES
        self.crs = CRS
        self.width = self.height = N
        self.transform = from_origin(0.0, N * RES, RES, RES)


GRID = Grid()


# --- set agreement ------------------------------------------------------------

def test_perfect_agreement_scores_one():
    m = damage_eval.set_metrics({"a", "b", "c"}, {"a", "b", "c"})
    assert m["f1"] == 1.0 and m["tp"] == 3 and m["fp"] == 0 and m["fn"] == 0


def test_total_disagreement_scores_zero():
    m = damage_eval.set_metrics({"a"}, {"b"})
    assert m["f1"] == 0.0 and m["tp"] == 0


def test_a_half_overlap_scores_the_textbook_value():
    m = damage_eval.set_metrics({"a", "b"}, {"b", "c"})
    assert m == pytest.approx({"tp": 1, "fp": 1, "fn": 1, "precision": 0.5,
                               "recall": 0.5, "f1": 0.5}, abs=1e-9)


def test_over_flagging_costs_precision_and_under_flagging_costs_recall():
    over = damage_eval.set_metrics({"a", "b", "c", "d"}, {"a"})
    assert over["precision"] == 0.25 and over["recall"] == 1.0
    under = damage_eval.set_metrics({"a"}, {"a", "b", "c", "d"})
    assert under["precision"] == 1.0 and under["recall"] == 0.25


def test_neither_side_flagging_anything_is_not_a_free_perfect_score():
    """Agreement, yes - but a 1.0 here would let a featureless event lift a gate."""
    m = damage_eval.set_metrics(set(), set())
    assert m["f1"] is None
    assert "nothing to score" in m["note"]


def test_a_metric_of_none_makes_the_score_unusable_and_so_blocks_the_gate():
    s = damage_eval.DamageScore(event="e", params_hash="h",
                                building_f1=None, road_segment_f1=0.9)
    assert not s.ok
    assert damage_eval.rows_for([s], "20260101T000000Z") == []


def test_feature_ids_fall_back_rather_than_collapsing_unidentified_rows():
    gdf = gpd.GeoDataFrame({"@osmId": [None, None, "way/3"]},
                           geometry=[box(0, 0, 1, 1)] * 3, crs=CRS)
    ids = damage_eval.feature_ids(gdf, "@osmId")
    assert len(ids) == 3, "two unidentified rows must not collapse into one"
    assert "way/3" in ids


def test_feature_ids_of_an_empty_or_column_less_layer_is_empty():
    empty = gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=CRS)
    assert damage_eval.feature_ids(empty, "@osmId") == set()
    no_col = gpd.GeoDataFrame({"other": [1]}, geometry=[box(0, 0, 1, 1)], crs=CRS)
    assert damage_eval.feature_ids(no_col, "@osmId") == set()


# --- the reference extent gets identical treatment ----------------------------

def test_the_reference_extent_is_built_by_the_same_code_as_ours(monkeypatch):
    """Identical buffer and polygonisation, so only the extent differs.

    A reference mask covering the same cells as a flood class map must produce
    the same geometry - if it did not, part of any F1 below would be an
    artefact of how the two sides were rasterised.
    """
    mask = np.zeros((N, N), dtype=bool)
    mask[:, 20:30] = True
    monkeypatch.setattr(damage_eval.compare, "reference_mask",
                        lambda path, grid, values: (mask, None))
    monkeypatch.setattr(damage_eval.Path, "exists", lambda self: True)

    event = {"name": "synthetic", "reference": "data/reference/x/y.shp",
             "reference_classes": {"water": ["__all__"], "debris": []}}
    ref_geom = damage_eval.reference_extent_geometry(event, GRID, DamageParams())

    ours = np.full((N, N), CLASS_NO_CHANGE, dtype=np.uint8)
    ours[:, 20:30] = CLASS_FLOOD_MEDIUM
    our_geom = damage.extent_geometry(ours, GRID, ExtentParams(), DamageParams().buffer_px)
    assert ref_geom.equals(our_geom)


def test_a_missing_reference_is_refused_not_scored_as_empty(monkeypatch):
    event = {"name": "synthetic", "reference": "data/reference/nope/missing.shp",
             "reference_classes": {"water": ["__all__"]}}
    with pytest.raises(Exception) as err:
        damage_eval.reference_extent_geometry(event, GRID, DamageParams())
    assert "missing" in str(err.value).lower()


# --- rows and the CSV contract ------------------------------------------------

def _score(event, b=0.9, r=0.8):
    return damage_eval.DamageScore(event=event, params_hash="abc123abc123",
                                   building_f1=b, road_segment_f1=r,
                                   detail={"counts": {}})


def test_a_partial_set_of_events_writes_nothing(tmp_path):
    """Rule 3.2: a gate must not be an average over whatever worked."""
    planned = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    scores = [_score("a"), _score("b"),
              damage_eval.DamageScore(event="c", params_hash="h", error="boom")]
    written = damage_eval.write_rows(scores, planned, "20260101T000000Z", tmp_path,
                                     log=lambda *a: None)
    assert written == 0
    assert not (tmp_path / "damage.csv").exists()


def test_a_complete_set_writes_one_row_per_event_in_the_declared_columns(tmp_path):
    planned = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    scores = [_score("a"), _score("b"), _score("c")]
    written = damage_eval.write_rows(scores, planned, "20260101T000000Z", tmp_path,
                                     log=lambda *a: None)
    assert written == 3
    rows = list(csv.DictReader((tmp_path / "damage.csv").read_text(encoding="utf-8")
                               .splitlines()))
    assert [r["event"] for r in rows] == ["a", "b", "c"]
    declared = yaml.safe_load(
        (ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8"))
    gate_metrics = {g["metric"] for g in declared["modules"]["damage_overlay"]["gates"]}
    assert gate_metrics == {"road_segment_f1", "building_f1"}, (
        "the gates file asks for different metrics than this test expects")
    assert gate_metrics <= set(rows[0]), (
        f"the gate reads {sorted(gate_metrics)}, the row has {sorted(rows[0])}")
    for required in declared["contract"]["required_columns"]:
        assert required in rows[0]
    assert {"run_id", "timestamp", "git_commit", "params_hash", "event"} <= set(rows[0])


def test_the_results_file_is_append_only(tmp_path):
    planned = [{"name": "a"}]
    damage_eval.write_rows([_score("a")], planned, "20260101T000000Z", tmp_path,
                           log=lambda *a: None)
    damage_eval.write_rows([_score("a", 0.5, 0.5)], planned, "20260102T000000Z", tmp_path,
                           log=lambda *a: None)
    rows = list(csv.DictReader((tmp_path / "damage.csv").read_text(encoding="utf-8")
                               .splitlines()))
    assert len(rows) == 2, "an earlier row must never be overwritten (Rule 3.5)"
    assert [r["run_id"] for r in rows] == ["20260101T000000Z", "20260102T000000Z"]


def _damage_gate(tmp_path, scores, names):
    """Run the REAL gate checker over a damage.csv written by this module."""
    from eval import check_gates

    results_dir = tmp_path / "eval" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    damage_eval.write_rows(scores, [{"name": n} for n in names], "20260101T000000Z",
                           results_dir, log=lambda *a: None)
    cfg = yaml.safe_load((ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8"))
    report = check_gates.check(cfg, tmp_path, only="damage_overlay")
    return report, check_gates.module_status(report)["damage_overlay"]


def test_rows_written_by_this_module_move_the_gate_off_no_data(tmp_path):
    """End to end against the real checker and the real gates file.

    The point is not that 0.95 passes - it is that the file this module writes
    is the file the gate reads, with the columns and the aggregation the gates
    file declares. A format mistake here would otherwise show up only as a
    permanent NO DATA that looked like missing work.
    """
    names = ("a", "b", "c")
    report, status = _damage_gate(tmp_path, [_score(n, 0.95, 0.93) for n in names], names)
    assert status == "PASS", report
    assert {r["metric"] for r in report} == {"road_segment_f1", "building_f1"}
    assert all(r["rows"] == 3 for r in report)
    values = {r["metric"]: r["value"] for r in report}
    assert values == {"building_f1": 0.95, "road_segment_f1": 0.93}


def test_a_score_below_the_declared_threshold_fails_the_gate(tmp_path):
    """The other direction: this module cannot write a row that hides a failure."""
    names = ("a", "b", "c")
    _report, status = _damage_gate(tmp_path, [_score(n, 0.5, 0.4) for n in names], names)
    assert status == "FAIL"


def test_too_few_events_leaves_the_gate_at_no_data(tmp_path):
    """gates.yaml asks for min_rows 3; two complete events are not enough."""
    names = ("a", "b")
    _report, status = _damage_gate(tmp_path, [_score(n) for n in names], names)
    assert status == "NO DATA"
