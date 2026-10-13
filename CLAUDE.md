# Strandline: rules for Claude Code

This file is human-owned. You cannot edit it, eval/gates.yaml, tests/test_rules.py or .claude/
(hooks block it). If a rule seems wrong, tell the human why; do not work around it.

## What we are building

**Strandline** shows rescuers where a flood reached, what it broke and who it stranded, from free
satellite data. IIT Mandi Multimodal AI Hackathon 2026, Track B "Mapping Flood Damage from Space".

- Input: any area (bbox) and flood date. Output: flood and debris map, damaged buildings, roads
  and bridges, cut-off settlements, a grounded copilot, a dashboard and a one-page situation report
  in English and Nepali.
- Judging is LIVE, on areas and dates the judges choose. The system must run end-to-end from raw
  satellite data with no manual steps, and must refuse clearly rather than guess.
- Case study: the 26 Aug 2026 glacier-collapse flood down the Bhote Koshi–Trishuli corridor,
  compared with Copernicus EMS activation EMSR927 (sealed until the final day, see Rule 4).
- Score weights: flood and damage mapping 30%, AI component (situation-report copilot) 25%,
  cut-off settlements 20%, report, Q&A and honesty about limitations 15%, dashboard and sitrep 10%.

## Environment

- Windows. Your shell is Git Bash or PowerShell: always use forward slashes.
- Python: always `venv/Scripts/python.exe`, never bare `python` (macOS/Linux: `venv/bin/python`).
- Tests: `venv/Scripts/python.exe -m pytest -q`
- Gates: `venv/Scripts/python.exe -m eval.check_gates` (or `/gate`)
- Pipeline: `venv/Scripts/python.exe -m strandline.run --bbox W S E N --event-date YYYY-MM-DD --out runs/<name>`
- Copernicus login (openEO) is done once by the human in their own terminal; the token is cached.

## Repo map

- `strandline/`: the pipeline (catalog, ingest, detect, terrain, reliability, optical, outputs, run, osm, damage, network; later flowpath)
- `validation/`: scoring against reference maps. CHECKING ONLY.
- `eval/`: dev events, scoreboard, leave-one-event-out tuning, gates
- `copilot/`, `api/`, `web/`: being added
- `reference/satquery-ai-main/`: our earlier project, read-only (see reuse map below)
- `tests/`: `tests/test_rules.py` is human-owned and encodes these rules
- `docs/open_items.md`: known gaps, one line each; `docs/tuning_log.md`: decisions (append only)

## Rule 1: data (breaking this disqualifies the team)

- Pipeline inputs ONLY: Sentinel-1, Sentinel-2, Copernicus DEM, OpenStreetMap snapshot dated before
  the event (ohsome API; latest available snapshot 2026-07-27), Kuro Siwo, Sen1Floods11.
- Copernicus EMS, UNOSAT, any published damage map and OSM edits after the event may be used only
  inside `validation/` and `eval/` to score results. Never as pipeline input, never cached where the
  pipeline can read it, never used to set a value for the area being mapped.
- `strandline/` never imports `validation/` or `eval/` (tested).
- Every report, dashboard and sitrep carries `strandline.outputs.ATTRIBUTION` plus citations of any
  training dataset used (Bountos et al. 2024 for Kuro Siwo; Bonafilia et al. 2020 for Sen1Floods11).
  Comparison outputs credit "European Union, Copernicus Emergency Management Service data".
- Respect: no images of victims anywhere. Call the system an educational prototype.

## Rule 2: the 90% accuracy gates

- `eval/gates.yaml` defines every gate. Summary: flood precision, recall and F1 (20 m boundary
  tolerance) ≥ 0.90 averaged over dev events, worst event ≥ 0.80; no-flood controls ≤ 1% false
  flood; damage road and building F1 ≥ 0.90; cut-off settlement F1 ≥ 0.90; copilot 0 invented
  numbers and ≥ 95% correct on ≥ 150 questions; live drills ≥ 90% success; 100% reproducible.
- A gate counts as PASS only when its measured value meets the target. NO DATA means not measured.
- A module is complete when it is built, tested, measured by its gate and committed. A failing gate
  is reported honestly (measured value, target, main cause with evidence); do not loop on it.
  Improvement attempts happen only when a prompt asks for them.
- Strict (zero-tolerance) scores are always reported next to the tolerant ones.
- Never fake, hide or round up a number.

## Rule 3: no metric gaming (any one of these is an automatic FAIL)

1. Using the Trishuli case study or EMSR927 for anything before the human unlocks it.
2. Changing what is measured: shrinking the scoring area, excluding hard pixels, areas or events,
   changing tolerance or metric definitions, scoring only easy events.
3. Event-specific code: any branch or value keyed to a bbox, date, place or event name.
4. In-sample tuning: parameter values are chosen by `eval/tune.py` with leave-one-event-out, or set
   from physics or literature without looking at scores. Never hand-tuned to one event.
5. Deleting, overwriting or cherry-picking rows in `eval/results/*.csv` (append-only).
6. Weakening tests: deleting, skipping, xfail, loosening asserts, broad try/except.
7. A tunable value outside `strandline/config.py`.
8. Reporting any number that is not in `eval/results/` or a `run_report.json`; presenting a synthetic
   test result as real-world accuracy.
9. Overall pixel accuracy as a headline metric (most pixels are dry; it means nothing).

## Rule 4: the held-out test

The Trishuli case study is scored ONCE, on the final day, through `/heldout`, after the human
creates `.heldout_unlocked`. Until then its reference data stays outside the repo and hooks block any
path, command or web request mentioning EMSR927. Running the pipeline on the case-study area to
check that it works is allowed; comparing it with any reference is not. Dev events must not
overlap `heldout.exclusion_bbox` in `eval/gates.yaml` (tested).

## Rule 5: engineering

- Big change: plan first. Then small steps, a test for every new function and every bug fixed.
- Deterministic: fixed seeds; the same inputs give byte-identical outputs.
- Every run writes provenance to `run_report.json`: scenes, dates, parameters, parameters hash, runtime.
- Cache every download by bbox + date + stage. Test on small areas first; respect free-tier quotas.
- Live robustness: every failure gives a clear message (what failed, why, what to try). A refusal
  is better than a silent wrong map.
- Commit only files you changed for the task (no blind `git add -A`), message
  `<module>: <what changed> | gates: <status>`.

## Rule 6: honesty

Never claim accuracy that was not measured. Label synthetic tests as synthetic. When unsure, say so.
When blocked, stop and ask. The 15% honesty criterion is judged in Q&A: every number must be explainable.

## Process rules (human-owned)

- Never stop processes with taskkill /IM; stop only by PID.
- Never start, stop or restart eval.fetch_dev_data; the human runs it, and --estimate is its only
  no-network mode.

## Working protocol (applies to every prompt)

Scope
- Flood detection is frozen: do not edit strandline/detect.py, terrain.py, reliability.py, optical.py,
  the detection or optical parameters in config.py, or eval/chosen_params.json unless the prompt says
  "flood-mapping change".
- Build only what the prompt asks. No refactors, renames or extras outside the task.

Start
- Begin with a numbered plan of at most 6 steps with rough minutes each, then work without waiting
  for approval unless a rule blocks you. Print "Step k/n done" after each step.
- Read only the files the task needs; Grep before Read. Never read docs/tuning_log.md or
  eval/results/*.csv in full; grep or tail them.

Long jobs
- Never run a command expected to take more than 10 minutes (downloads, drills, re-scoring every
  event, Monte Carlo over all events). Print the exact Windows cmd command for the human
  (venv\Scripts\python.exe ...) and stop; continue when the human says it finished.
- No polling loops, no sleep-and-check loops. No background agents unless the prompt asks for /audit.

Testing
- While iterating, run only the new or affected tests; run the full suite once before committing.

Finish (every module)
1. /gate once; report it as printed. If a gate fails, give value, target and main cause once.
2. Do not run /audit unless the prompt explicitly asks for it. When asked: one audit, apply its
   findings once, re-run the tests, no second round. Known gaps go to docs/open_items.md, one line each.
3. Update only this module's README section: how to run it and the measured numbers with their source file.
4. Commit.
5. Final message, at most 10 lines: what was built, gate numbers, open items, commit hash, and the
   next command for the human if any.

## Formats the tests and gates depend on

`eval/events.yaml`:

```yaml
events:
  - name: snake_case_name          # never a held-out name
    kind: flood                     # flood | control (control = same kind of valley, no flood)
    event_date: "YYYY-MM-DD"
    bbox: [W, S, E, N]
    reference: data/reference/<name>/<file>   # flood events only; scoring only
    reference_classes: {water: [...], debris: [...]}   # how reference layers map to our classes
    source: "activation id or dataset, with URL"
    notes: "terrain, flood type"
```

Results CSVs: see the comment block at the end of `eval/gates.yaml`. Every row has `run_id`
(UTC `YYYYMMDDTHHMMSSZ`), `timestamp`, `git_commit`, `params_hash` and `event`.

## SatQuery reuse map (`reference/satquery-ai-main/`, read-only)

- Port when asked: `agent/planner.py`, `agent/dsl.py`, `agent/executor.py`, `agent/tasks.py` (structure),
  `evidence/schema.py`, `tools/verifier.py`, `raster_io/readers.py`, and from `web/src/components/`
  TraceDrawer, AnswerPanel and VerificationPanel.
- Never port: `perception/` and the ONNX model (62.7% on real data, no flood class),
  `evidence/change.py`, `evidence/fusion.py`, `tools/cross_modal.py`, `tools/caption.py`,
  `tools/grounding.py`, demo patches.

## Copilot rules

- The language model never writes a number. It writes templates with placeholders such as
  `{flooded_area_ha}`; code fills them from the evidence store.
- A verifier checks every number in the final text, including Devanagari digits, against the
  evidence. Any unmatched number means the answer is rejected and regenerated or refused.
- English and Nepali. Works offline with the keyword fallback. Every answer carries its trace.
- The API key lives in `.env` (never committed).
