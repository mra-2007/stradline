"""Scoring the cut-off analysis: the CSV contract, refusals and completeness.

SYNTHETIC: every set below is written out here, so nothing in this file is
real-world cut-off accuracy (Rule 3.8). The real numbers come from
`eval/cutoff_eval.py` into `eval/results/cutoff.csv`.
"""

from __future__ import annotations

import csv
import json

import pytest
import yaml

from eval import cutoff_eval
from strandline.config import (
    CUTOFF_GATE_GRAPH,
    CUTOFF_GATE_SCENARIO,
    CUTOFF_GATE_TARGET,
    STATUS_CUT_OFF,
    STATUS_REACHABLE,
)

ROOT = cutoff_eval.ROOT


def _score(event, f1=0.95, settlements=20, road_km=120.0):
    return cutoff_eval.CutoffScore(
        event=event, params_hash="abc123abc123", settlement_f1=f1,
        detail={"cut_off_ours": ["a"], "cut_off_reference": ["a"],
                "osm_completeness": {"settlements": settlements, "road_km": road_km,
                                     "health_facilities": 3}})


# --- the gated definition is one declared thing -------------------------------

def test_the_gated_definition_matches_the_one_in_config():
    assert (CUTOFF_GATE_GRAPH, CUTOFF_GATE_TARGET, CUTOFF_GATE_SCENARIO) == (
        "vehicle", "health", "bridges_intact")


def test_the_cut_off_set_uses_only_the_cut_off_status():
    class R:
        class settlements:
            names = ["a", "b", "c"]

    out = cutoff_eval.cut_off_set(R, [STATUS_CUT_OFF, STATUS_REACHABLE, STATUS_CUT_OFF])
    assert out == {"a", "c"}


# --- rows and the CSV contract ------------------------------------------------

def test_a_partial_set_of_events_writes_nothing(tmp_path):
    planned = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    scores = [_score("a"), _score("b"),
              cutoff_eval.CutoffScore(event="c", params_hash="h", error="boom")]
    assert cutoff_eval.write_rows(scores, planned, "20260101T000000Z", tmp_path,
                                  log=lambda *a: None, gate_plan=[]) == 0
    assert not (tmp_path / "cutoff.csv").exists()


def test_a_metric_of_none_blocks_the_row_and_so_the_gate():
    s = cutoff_eval.CutoffScore(event="e", params_hash="h", settlement_f1=None)
    assert not s.ok
    assert cutoff_eval.rows_for([s], "20260101T000000Z") == []


def test_a_complete_run_writes_the_declared_columns(tmp_path):
    names = ("a", "b", "c")
    written = cutoff_eval.write_rows([_score(n) for n in names],
                                     [{"name": n} for n in names],
                                     "20260101T000000Z", tmp_path,
                                     log=lambda *a: None, gate_plan=[])
    assert written == 3
    rows = list(csv.DictReader((tmp_path / "cutoff.csv").read_text(encoding="utf-8")
                               .splitlines()))
    gates = yaml.safe_load((ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8"))
    metrics = {g["metric"] for g in gates["modules"]["cutoff_analysis"]["gates"]}
    assert metrics == {"settlement_f1"}
    assert metrics <= set(rows[0])
    for required in gates["contract"]["required_columns"]:
        assert required in rows[0]


def test_a_run_narrowed_with_events_writes_nothing(tmp_path):
    """Rule 3.2: cutoff_analysis has min_rows 3 and no where: clause, so a
    hand-picked three-event run would become the latest run_id."""
    plan = cutoff_eval.gate_plan_events()
    subset = plan[:3]
    assert cutoff_eval.write_rows([_score(n) for n in subset],
                                  [{"name": n} for n in subset],
                                  "20260101T000000Z", tmp_path,
                                  log=lambda *a: None) == 0


def test_the_full_gate_plan_writes_its_rows(tmp_path):
    plan = cutoff_eval.gate_plan_events()
    assert cutoff_eval.write_rows([_score(n) for n in plan],
                                  [{"name": n} for n in plan],
                                  "20260101T000000Z", tmp_path,
                                  log=lambda *a: None) == len(plan)


def test_the_real_gate_checker_reads_what_this_module_writes(tmp_path):
    from eval import check_gates

    results = tmp_path / "eval" / "results"
    results.mkdir(parents=True)
    plan = cutoff_eval.gate_plan_events()
    cutoff_eval.write_rows([_score(n, 0.95) for n in plan],
                           [{"name": n} for n in plan], "20260101T000000Z",
                           results, log=lambda *a: None)
    cfg = yaml.safe_load((ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8"))
    report = check_gates.check(cfg, tmp_path, only="cutoff_analysis")
    assert check_gates.module_status(report)["cutoff_analysis"] == "PASS", report
    low = tmp_path / "low"
    (low / "eval" / "results").mkdir(parents=True)
    cutoff_eval.write_rows([_score(n, 0.4) for n in plan],
                           [{"name": n} for n in plan], "20260101T000000Z",
                           low / "eval" / "results", log=lambda *a: None)
    assert check_gates.module_status(
        check_gates.check(cfg, low, only="cutoff_analysis"))["cutoff_analysis"] == "FAIL"


# --- completeness is published, never used to exclude -------------------------

def test_osm_completeness_travels_with_every_score():
    s = _score("a", settlements=3, road_km=8.0)
    c = s.detail["osm_completeness"]
    assert c["settlements"] == 3 and c["road_km"] == 8.0


def test_a_thinly_mapped_event_is_still_written(tmp_path):
    """Rule 3.2: thin mapping is a limitation, never a reason to drop an event."""
    plan = cutoff_eval.gate_plan_events()
    scores = [_score(n) for n in plan]
    scores[0] = _score(plan[0], f1=0.0, settlements=1, road_km=2.0)
    assert cutoff_eval.write_rows(scores, [{"name": n} for n in plan],
                                  "20260101T000000Z", tmp_path,
                                  log=lambda *a: None) == len(plan)


def test_the_evidence_file_is_written_beside_the_others(tmp_path):
    from eval import damage_eval
    from eval import run_scoreboard as rs

    flood = rs.score_path("e", "h", tmp_path)
    assert cutoff_eval.detail_path("e", "h", tmp_path).parent == flood.parent
    assert cutoff_eval.detail_path("e", "h", tmp_path).name.startswith("cutoff_")
    assert (cutoff_eval.detail_path("e", "h", tmp_path)
            != damage_eval.detail_path("e", "h", tmp_path))


def test_the_evidence_file_records_the_completeness_and_both_sets(tmp_path):
    path = cutoff_eval.save_detail(_score("an_event", settlements=7), tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["settlement_f1"] == 0.95
    assert body["osm_completeness"]["settlements"] == 7
    assert body["cut_off_ours"] == ["a"]


def test_a_failed_event_writes_no_evidence(tmp_path):
    failed = cutoff_eval.CutoffScore(event="e", params_hash="h", error="boom")
    assert cutoff_eval.save_detail(failed, tmp_path) is None
