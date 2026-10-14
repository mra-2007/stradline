"""Running a plan, and turning what it produced into an answer.

Ported in STRUCTURE from `reference/satquery-ai-main/agent/executor.py`: a
plan goes in, tool outputs and a literal trace come out, with no model in the
loop. The model has already done its two jobs by the time this runs - it chose
the plan, and it will word the result - and neither of them can put a number
anywhere.

The order is fixed and is the whole safety argument:

    evidence  ->  plan  ->  tools  ->  template  ->  fill  ->  VERIFY

The evidence exists before the question is planned; the tools only read it;
the template has no numbers in it; the filler can only insert a fact; and the
verifier reads the finished text back and refuses anything it cannot find in
the evidence.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from strandline.config import CopilotParams
from copilot import llm, planner, templates
from copilot.evidence import (LAYER_FILES, EvidenceError, EvidenceStore,
                               MissingEvidence)
from copilot.tools import IMPLEMENTATIONS, UnknownSettlement
from copilot.verifier import verify

# Units that are quantities rather than labels. Only these are reported in the
# answer's `numbers` list.
QUANTITY_UNITS = ("ha", "km", "m", "percent", "probability", "count", "days", "factor",
                  "degrees")

# Parsed run files, keyed by path and modification time. The FACTS are always
# rebuilt per question - a store accumulates derived facts as tools run, and
# re-using one across questions would let a number derived for an earlier
# question count as evidence for a later one, which is exactly the hole the
# verifier exists to close.
_RAW_CACHE: dict = {}


def _read_json(path: Path):
    key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    if key not in _RAW_CACHE:
        if len(_RAW_CACHE) > 64:
            _RAW_CACHE.clear()
        _RAW_CACHE[key] = json.loads(path.read_text(encoding="utf-8"))
    return _RAW_CACHE[key]


def load_store(run_dir) -> EvidenceStore:
    """A fresh evidence store for one question, over cached file contents."""
    run_dir = Path(run_dir)
    report_path = run_dir / "run_report.json"
    if not report_path.exists():
        # from_run raises EvidenceError with the message that says what to do.
        return EvidenceStore.from_run(run_dir)
    store = EvidenceStore(run_dir=run_dir, report=_read_json(report_path))
    for capability, files in LAYER_FILES.items():
        for name in files:
            path = run_dir / name
            if not path.exists():
                continue
            try:
                store.layers[name] = _read_json(path)
            except json.JSONDecodeError as err:
                store.missing.setdefault(
                    capability, f"{name} is present but unreadable: {err}")
    store._build()
    return store


@dataclass
class Answer:
    text: str
    numbers: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    refused: bool = False
    reason: str | None = None
    lang: str = "en"

    def as_dict(self) -> dict:
        return {"text": self.text, "numbers": list(self.numbers), "trace": list(self.trace),
                "refused": self.refused, "reason": self.reason, "lang": self.lang}


def _refusal(i18n, key: str, lang: str, trace: list, **fmt) -> Answer:
    text = i18n["refusals"][key].format(**fmt) if fmt else i18n["refusals"][key]
    trace.append({"step": "refuse", "refusal": key})
    return Answer(text=text, refused=True, reason=key, trace=trace, lang=lang)


def _missing(i18n, capability: str, reason: str, lang: str, trace: list) -> Answer:
    template = i18n["missing"].get(capability)
    # The pipeline's reason is a sentence fragment without punctuation; give
    # it a full stop so it does not run into the sentence after it.
    reason = str(reason).strip().rstrip(".") + "."
    text = (template.format(reason=reason) if template
            else i18n["refusals"]["out_of_scope"])
    trace.append({"step": "refuse", "refusal": f"missing:{capability}", "reason": reason})
    return Answer(text=text, refused=True, reason=f"missing:{capability}", trace=trace,
                  lang=lang)


def answer(run_dir, question: str, lang: str = "en",
           params: CopilotParams | None = None, *, use_llm: bool = True,
           cache_dir=None, env_file=None, complete=llm.complete) -> dict:
    """Answer one question about one run folder. The copilot's whole surface.

    Returns {text, numbers, trace, refused, reason, lang}, JSON-serialisable,
    which is what the dashboard and the evaluation both consume.
    """
    started = time.perf_counter()
    params = params or CopilotParams()
    i18n = templates.load_i18n(lang)
    trace: list = [{"step": "question", "lang": lang, "run_dir": str(run_dir),
                    "question": str(question)}]

    try:
        store = load_store(run_dir)
    except EvidenceError as err:
        trace.append({"step": "evidence", "error": str(err)})
        return Answer(text=str(err), refused=True, reason="no_run", trace=trace,
                      lang=lang).as_dict()
    trace.append({"step": "evidence", "facts": len(store.facts),
                  "missing": sorted(store.missing),
                  "run_id": (store.get("run.id").value if store.get("run.id") else None),
                  "params_hash": (store.get("run.params_hash").value
                                  if store.get("run.params_hash") else None)})

    planned = planner.plan(question, store, params, use_llm=use_llm, cache_dir=cache_dir,
                           env_file=env_file, complete=complete)
    trace.append({"step": "plan", "source": planned.source, "notes": planned.notes,
                  "plan": planned.plan.as_list() if planned.plan else None,
                  "refusal": planned.refusal})
    if planned.refusal is not None or planned.plan is None:
        key = planned.refusal or "out_of_scope"
        out = _refusal(i18n, key, lang, trace,
                       **({"query": planned.query} if key == "unknown_settlement" else {}))
        out.trace.append({"step": "timing", "seconds": round(time.perf_counter() - started, 4)})
        return out.as_dict()

    results = []
    for step in planned.plan:
        implementation = IMPLEMENTATIONS[step.tool]
        try:
            result = implementation(store, lang, i18n, **step.parameters)
        except MissingEvidence as err:
            out = _missing(i18n, err.capability, err.reason, lang, trace)
            out.trace.append({"step": "timing",
                              "seconds": round(time.perf_counter() - started, 4)})
            return out.as_dict()
        except UnknownSettlement as err:
            out = _refusal(i18n, "unknown_settlement", lang, trace, query=err.query)
            out.trace.append({"step": "timing",
                              "seconds": round(time.perf_counter() - started, 4)})
            return out.as_dict()
        results.append(result)
        trace.append({"step": step.id, "tool": step.tool, "parameters": step.parameters,
                      "template": result.template_key,
                      "fact_ids": [f.id for f in result.facts()],
                      "feature_ids": list(result.feature_ids()),
                      "source_files": list(result.source_files())})

    allowed = store.allowed_strings(lang)
    written = templates.write_answer(question, results, lang, store.text_literals(),
                                     params,
                                     use_llm=use_llm, cache_dir=cache_dir,
                                     env_file=env_file, complete=complete)
    trace.append({"step": "write", "source": written["source"], "notes": written["notes"]})

    checked = verify(written["text"], allowed, lang)
    trace.append({"step": "verify", "ok": checked.ok, **checked.as_dict()})
    if not checked.ok:
        out = _refusal(i18n, "unverified", lang, trace)
        out.trace.append({"step": "timing",
                          "seconds": round(time.perf_counter() - started, 4)})
        return out.as_dict()

    numbers = _numbers_in_answer(results, written["text"], lang)
    trace.append({"step": "timing", "seconds": round(time.perf_counter() - started, 4)})
    return Answer(text=written["text"], numbers=numbers, trace=trace, lang=lang).as_dict()


def _numbers_in_answer(results, text: str, lang: str) -> list:
    """Every quantity the finished text actually states, with its fact id and
    the features behind it. Not every fact the tools touched - the ones a
    reader can see and may want to check."""
    out, seen = [], set()
    for result in results:
        for fact in result.facts():
            if fact.id in seen or fact.unit not in QUANTITY_UNITS:
                continue
            formatted = fact.formatted(lang)
            if formatted == "—" or formatted not in text:
                continue
            seen.add(fact.id)
            out.append({"value": fact.value, "unit": fact.unit, "fact_id": fact.id,
                        "formatted": formatted, "feature_ids": list(fact.feature_ids),
                        "source_file": fact.source_file, "derivation": fact.derivation})
    return out
