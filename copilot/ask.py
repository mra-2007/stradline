"""Ask the copilot one question about one run folder.

    venv/Scripts/python.exe -m copilot.ask --run runs/<name> --lang en|ne "question"

Prints the answer, then the evidence behind every number in it, then the
execution trace - because an answer a judge cannot check is worth nothing
(Rule 6). `--json` prints the same thing as the API returns it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from strandline.config import COPILOT_LANGUAGES, CopilotParams
from copilot.executor import answer
from copilot.templates import load_i18n


def _utf8_stdout() -> None:
    """Windows consoles still default to cp1252, which cannot print Devanagari
    at all. Reconfiguring is the difference between a Nepali answer and a
    UnicodeEncodeError in front of a judge."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):       # pragma: no cover - not a tty
            pass


def render(result: dict, i18n: dict, show_trace: bool = True) -> str:
    lines = [result["text"], ""]
    if result["numbers"]:
        for number in result["numbers"]:
            features = ", ".join(str(f) for f in number["feature_ids"][:6])
            if len(number["feature_ids"]) > 6:
                features += ", ..."
            lines.append(f"  {number['formatted']} {number['unit'] or ''}".rstrip()
                         + f"  <- {number['fact_id']}  [{number['source_file']}]"
                         + (f"  features: {features}" if features else ""))
        lines.append("")
    if result["refused"]:
        lines.append(f"REFUSED: {result['reason']}")
        lines.append("")
    if show_trace:
        for entry in result["trace"]:
            step = entry.get("step")
            if step in ("question", "evidence", "plan", "write", "verify", "timing",
                        "refuse"):
                lines.append(f"  [{step}] " + json.dumps(
                    {k: v for k, v in entry.items() if k != "step"},
                    ensure_ascii=False)[:400])
            else:
                lines.append(f"  [{step}] {entry.get('tool')}"
                             f"({json.dumps(entry.get('parameters', {}))})"
                             f" -> {len(entry.get('fact_ids', []))} fact(s),"
                             f" {len(entry.get('feature_ids', []))} feature(s)")
    return "\n".join(lines)


def main(argv=None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("question", help="the question, in English or Nepali")
    ap.add_argument("--run", required=True, type=Path, help="a Strandline run folder")
    ap.add_argument("--lang", default="en", choices=list(COPILOT_LANGUAGES))
    ap.add_argument("--json", action="store_true", help="print the API response verbatim")
    ap.add_argument("--no-trace", dest="trace", action="store_false", default=True)
    ap.add_argument("--no-llm", dest="llm", action="store_false", default=True,
                    help="skip the language model and use the offline templates")
    ap.add_argument("--model", default=None, help="override the configured model name")
    args = ap.parse_args(argv)

    params = CopilotParams()
    if args.model:
        params = replace(params, model=args.model)
    result = answer(args.run, args.question, args.lang, params, use_llm=args.llm)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result, load_i18n(args.lang), show_trace=args.trace))
    return 1 if result["refused"] and result["reason"] == "no_run" else 0


if __name__ == "__main__":
    sys.exit(main())
