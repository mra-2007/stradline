"""The scoreboard must keep non-independent events out of every gate.

events.yaml keeps `kind: flood` for every flood event; the CSV `kind` column is
derived. A flood event whose reference came from our own input sensor must be
written as flood_dependent so the gates file's `where: {kind: flood}` filter
drops it, with no edit to the gates file.
"""

import csv

import pytest
import yaml

from pathlib import Path

from eval import check_gates, scoreboard as sb

ROOT = Path(__file__).resolve().parents[1]
EVENTS = yaml.safe_load((ROOT / "eval" / "events.yaml").read_text(encoding="utf-8"))["events"]
GATES = yaml.safe_load((ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8"))

INDEP = {"name": "x", "kind": "flood", "independent_reference": True}
DEP = {"name": "y", "kind": "flood", "independent_reference": False}
CTRL = {"name": "z", "kind": "control", "independent_reference": None}


# --- the mapping itself -------------------------------------------------------

def test_independent_flood_is_written_as_flood():
    assert sb.row_kind(INDEP) == "flood"
    assert sb.gate_eligible(INDEP)


def test_dependent_flood_is_written_as_flood_dependent():
    assert sb.row_kind(DEP) == "flood_dependent"
    assert not sb.gate_eligible(DEP)


def test_control_is_written_as_control():
    assert sb.row_kind(CTRL) == "control"
    assert not sb.gate_eligible(CTRL)


@pytest.mark.parametrize("value", [False, None, "true", "yes", 1, 0, "", "True"])
def test_only_a_real_boolean_true_reaches_a_gate(value):
    """A truthy-looking string or a missing answer must not buy gate eligibility."""
    assert sb.row_kind({"name": "e", "kind": "flood", "independent_reference": value}) \
        == "flood_dependent"


def test_missing_independence_field_defaults_to_excluded():
    assert sb.row_kind({"name": "e", "kind": "flood"}) == "flood_dependent"


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        sb.row_kind({"name": "e", "kind": "flood_dependent"})


def test_row_kind_is_derived_not_accepted_from_the_caller():
    """Passing kind in `metrics` must not override the derived value."""
    row = sb.flood_row(DEP, {"f1": 0.99, "kind": "flood"}, run_id="R", params_hash="H")
    assert row["kind"] == "flood_dependent"


# --- the gate filter actually drops them --------------------------------------

def test_gate_filter_excludes_dependent_rows(tmp_path):
    run_id = "20260101T000000Z"
    rows = [
        sb.flood_row(INDEP, {"f1_tolerant": 0.95}, run_id=run_id, params_hash="H", commit="c"),
        sb.flood_row(DEP, {"f1_tolerant": 0.10}, run_id=run_id, params_hash="H", commit="c"),
    ]
    path = sb.append_rows("flood.csv", rows, results_dir=tmp_path)
    where = GATES["modules"]["flood_mapping"]["where"]
    _, selected = check_gates.select_latest(check_gates.read_rows(path), where)
    names = {r["event"] for r in selected}
    assert names == {"x"}, "the dependent event leaked into the gate selection"


def test_appending_never_rewrites_existing_rows(tmp_path):
    a = sb.flood_row(INDEP, {"f1": 0.5}, run_id="20260101T000000Z", params_hash="H", commit="c")
    b = sb.flood_row(INDEP, {"f1": 0.6}, run_id="20260102T000000Z", params_hash="H", commit="c")
    sb.append_rows("flood.csv", [a], results_dir=tmp_path)
    sb.append_rows("flood.csv", [b], results_dir=tmp_path)
    with (tmp_path / "flood.csv").open(newline="", encoding="utf-8") as f:
        got = list(csv.DictReader(f))
    assert [r["run_id"] for r in got] == ["20260101T000000Z", "20260102T000000Z"]
    assert [r["f1"] for r in got] == ["0.5", "0.6"]


def test_unexpected_column_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        sb.append_rows("flood.csv", [{"run_id": "R", "event": "x", "made_up": 1}],
                       results_dir=tmp_path)


# --- against the real dev event file ------------------------------------------

def test_every_real_event_maps_to_a_valid_csv_kind():
    for ev in EVENTS:
        assert sb.row_kind(ev) in {"flood", "flood_dependent", "control"}


def test_events_yaml_still_uses_only_flood_and_control():
    """The derived value must not leak back into the human-owned vocabulary."""
    for ev in EVENTS:
        assert ev["kind"] in {"flood", "control"}, (
            f"{ev['name']}: events.yaml must keep kind flood/control; "
            "flood_dependent exists only in the results CSV")


def test_non_independent_real_events_are_excluded_from_gates():
    excluded = {ev["name"] for ev in EVENTS if not sb.gate_eligible(ev) and ev["kind"] == "flood"}
    for ev in EVENTS:
        if ev["kind"] == "flood" and ev["independent_reference"] is False:
            assert ev["name"] in excluded


def test_enough_independent_events_to_satisfy_min_rows():
    groups = sb.summarise()
    need = GATES["modules"]["flood_mapping"]["min_rows"]
    assert len(groups["flood"]) >= need, (
        f"only {len(groups['flood'])} gate-eligible flood events, min_rows is {need}")
    assert len(groups["control"]) >= GATES["modules"]["no_flood_controls"]["min_rows"]
