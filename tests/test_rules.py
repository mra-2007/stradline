"""PROJECT RULES AS TESTS (human-owned: Claude Code is blocked from editing this file).

These encode the hard rules in CLAUDE.md so they cannot be broken quietly:
  - the pipeline never touches validation/eval code (disqualification guard)
  - no accuracy gate is ever set below the 90% hard floor
  - the Trishuli case study stays out of dev events, pipeline code and the repo
  - required attributions are present
"""

import ast
import io
import tokenize
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "strandline"
GATES = yaml.safe_load((ROOT / "eval" / "gates.yaml").read_text(encoding="utf-8"))
HELD = GATES["heldout"]

HARD_FLOOR = 0.90          # every normal ">=" gate
WORST_CASE_FLOOR = 0.80    # gates explicitly marked floor: true
CEILINGS = {"false_flood_fraction": 0.01, "invented_number": 0.0}
REQUIRED_MODULES = {"flood_mapping", "no_flood_controls", "damage_overlay", "cutoff_analysis",
                    "copilot", "live_drills", "reproducibility"}
MIN_ROWS = {"flood_mapping": 3, "no_flood_controls": 3, "damage_overlay": 3, "cutoff_analysis": 3,
            "copilot": 150, "live_drills": 10, "reproducibility": 3}
SKIP_DIRS = {"venv", ".venv", ".git", "node_modules", "runs", "cache", "__pycache__", ".pytest_cache"}


# --- Data rules ---------------------------------------------------------------

def _imports(py: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_pipeline_never_imports_validation_or_eval():
    """Reference maps may only be used for checking (Track B data rules)."""
    for py in PIPELINE.rglob("*.py"):
        for name in _imports(py):
            assert name.split(".")[0] not in {"validation", "eval"}, f"{py.name} imports {name}"


# --- Accuracy gates -------------------------------------------------------------

def test_every_required_module_has_gates():
    assert REQUIRED_MODULES <= set(GATES["modules"]), "a gated module was removed"


def test_gates_never_below_hard_floor():
    for name, mod in GATES["modules"].items():
        assert mod["gates"], f"{name} has no gates"
        for g in mod["gates"]:
            if g.get("report_only"):
                continue
            if g["op"] == ">=":
                floor = WORST_CASE_FLOOR if g.get("floor") else HARD_FLOOR
                assert float(g["value"]) >= floor, f"{name}.{g['metric']} set below {floor}"
            elif g["op"] == "<=":
                assert g["metric"] in CEILINGS, f"{name}.{g['metric']}: unknown ceiling metric"
                assert float(g["value"]) <= CEILINGS[g["metric"]], f"{name}.{g['metric']} loosened"
            else:
                raise AssertionError(f"{name}.{g['metric']}: unknown operator {g['op']}")


def test_flood_gates_cover_precision_recall_and_f1():
    metrics = {g["metric"] for g in GATES["modules"]["flood_mapping"]["gates"] if not g.get("report_only")}
    assert {"precision_tolerant", "recall_tolerant", "f1_tolerant"} <= metrics


def test_scoring_contract_not_loosened():
    assert GATES["contract"]["boundary_tolerance_px"] <= 1
    assert GATES["modules"]["flood_mapping"]["where"].get("split") == "loeo"
    for name, need in MIN_ROWS.items():
        assert int(GATES["modules"][name].get("min_rows", 0)) >= need, f"{name}: min_rows lowered"


# --- Held-out case study --------------------------------------------------------

def _overlaps(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def test_dev_events_exclude_heldout():
    path = ROOT / "eval" / "events.yaml"
    if not path.exists():
        return
    events = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("events", [])
    for ev in events:
        name = str(ev.get("name", "")).lower()
        assert not any(n in name for n in HELD["forbidden_names"]), f"held-out name in dev event {name}"
        bbox = ev.get("bbox")
        assert bbox and len(bbox) == 4, f"{name}: bbox [W, S, E, N] required"
        assert not _overlaps(bbox, HELD["exclusion_bbox"]), f"{name} overlaps the held-out exclusion zone"
        assert ev.get("kind") in {"flood", "control"}, f"{name}: kind must be flood or control"


def _code_tokens(py: Path):
    """NAME and STRING tokens, skipping comments and docstrings."""
    src = py.read_text(encoding="utf-8")
    doc_lines = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.NAME, tokenize.STRING) and tok.start[0] not in doc_lines:
            yield tok


def test_pipeline_has_no_case_study_special_casing():
    for py in PIPELINE.rglob("*.py"):
        for tok in _code_tokens(py):
            low = tok.string.lower()
            hit = [n for n in HELD["forbidden_names"] if n in low]
            assert not hit, f"{py.name}:{tok.start[0]} mentions held-out name {hit[0]!r} in code"


def test_heldout_reference_not_in_repo_until_unlocked():
    if (ROOT / ".heldout_unlocked").exists():
        return
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        assert "emsr927" not in path.name.lower(), f"held-out reference inside repo: {path.relative_to(ROOT)}"


# --- Attribution ------------------------------------------------------------------

def test_required_attributions_present():
    from strandline.outputs import ATTRIBUTION
    from validation.compare import REFERENCE_CREDIT
    text = " ".join(ATTRIBUTION)
    assert "Contains modified Copernicus Sentinel data 2026." in text
    assert "Copernicus WorldDEM-30" in text
    assert "© OpenStreetMap contributors." in text
    assert REFERENCE_CREDIT == "European Union, Copernicus Emergency Management Service data"
