# Strandline: Claude Code prompts, in order

The rules live in `CLAUDE.md` and are enforced by hooks, so these prompts only describe the task.
One module per session: type `/clear` before each new prompt. After each module: `/gate`, `/audit`,
then commit.

## Setup (VS Code terminal, cmd)

```
cd %USERPROFILE%\Desktop
tar -xf %USERPROFILE%\Downloads\strandline.zip
cd strandline
mkdir reference
tar -xf %USERPROFILE%\Downloads\satquery-ai-main.zip -C reference
py -3.11 -m venv venv
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m pytest -q
git init
git add -A
git commit -m "Strandline: flood mapping core + enforced 90% gates"
claude
```

Expected test result: `39 passed`. If Claude Code asks whether to trust the folder, say yes.

## Prompt 1: orientation (changes nothing)

```
Read CLAUDE.md, README.md, eval/gates.yaml and every file in strandline/. Run the test suite and /gate. Then reply with: (1) each of the 6 rules in one line, (2) the status of every gate, (3) anything in the current code that could break a rule or block a gate. Do not change any code.
```

## Prompt 2: day-1 pre-flight

```
Day-1 go/no-go, step 1. Run: venv/Scripts/python.exe -m strandline.run --bbox 85.10 27.85 85.45 28.30 --event-date 2026-08-26 --out runs/case_preflight --dry-run
Explain in plain words: tracks found, the track chosen and why, post-event latency, number of pre-event images. If the catalogue call fails or returns nothing, debug strandline/catalog.py against the real Copernicus OData response (print one raw product to learn the real field names), fix it, save that response as a test fixture, add a test, and rerun. This is a smoke test only: change no detection parameter.
```

**Your step (second terminal tab, not inside Claude):** the first full run opens a browser login.

```
venv\Scripts\python.exe -m strandline.run --bbox 85.10 27.85 85.45 28.30 --event-date 2026-08-26 --out runs\case_smoke
```

## Prompt 3: real-data check, GO or NO-GO

```
I ran the full case-study smoke run; outputs are in runs/case_smoke. Without changing any detection parameter:
1. Check the cached Sentinel-1 GeoTIFFs in runs/case_smoke/cache: 2 bands, linear values (not dB), sensible ranges, identical grids for every date.
2. Check the DEM covers the area with a plausible elevation range for this valley.
3. Summarise run_report.json: track, dates, area per class, water threshold, hand_fallback_fraction, runtime.
4. Check geolocation: does the permanent-water class follow the real river course? Measure any shift in pixels.
If anything is wrong, find the root cause in ingest.py or catalog.py, fix it with a test, and explain. Finish with GO or NO-GO and the reasons.
```

## Prompt 4a: dev events (the data behind the 90% gates)

```
Create eval/events.yaml in the format given in CLAUDE.md: at least 4 past flood events that have an independent reference flood map (Copernicus EMS rapid mapping, UNOSAT or Sentinel Asia), plus at least 3 no-flood controls (same kinds of valleys, dry dates). Prefer Himalayan and South Asian mountain-river, glacial-lake and debris floods; Kuro Siwo events are allowed as extra events. For every event, check that Sentinel-1 has a same-track pre/post pair (use the dry run) and that the reference product really exists; give me each reference download URL. List anything you could not verify. No event may overlap the held-out exclusion zone (the tests check this). Stop after this step and show me the table so I can download the references into data/reference/<event>/.
```

## Prompt 4b: scoreboard and leave-one-event-out tuner

```
The references are in data/reference/<event>/. Build the evaluation system the gates in eval/gates.yaml depend on. Plan first, then build.
1. eval/run_scoreboard.py: runs the pipeline on every event and control (using the cache), scores flood events with validation.compare using each event's reference_classes mapping, and appends rows to eval/results/flood.csv in exactly the column format at the end of eval/gates.yaml (kind, split, strict and tolerant precision/recall/F1/IoU, false_flood_fraction for controls).
2. eval/tune.py: leave-one-event-out. For each flood event, choose parameters from a small grid declared in strandline/config.py using only the OTHER events (maximise mean tolerant F1, subject to every control having at most 1% false flood), score the left-out event, and write split=loeo rows. Then choose final parameters on all events, write them to eval/chosen_params.json and show me the diff against config.py before applying.
3. Tests for the scoreboard and the tuner on synthetic events.
Then run the tuner, /gate, and report the per-event numbers.
```

## Prompt 5: make flood mapping pass its gates

```
Goal: flood_mapping and no_flood_controls must PASS in /gate without breaking any rule in CLAUDE.md. Loop:
1. Run eval/tune.py and /gate.
2. For each event below target, open its error map and per-event rows and name the dominant error type (missed water under vegetation or in radar shadow, false alarms on wet snow or layover, boundary offset, debris counted or not counted, a problem in the reference map itself).
3. Propose ONE change that fixes that error type for every event, not just one event, and state the physics that justifies it. Implement it with a test, parameters in config.py, then rerun.
4. Keep the change only if the mean scores improve AND no event and no control gets worse by more than 0.01; otherwise revert it.
Log every attempt (change, before, after, kept or reverted) in docs/tuning_log.md. If a gate still fails after 5 honest attempts, stop and report the measured values, root causes and options. Then run /audit.
```

## Prompt 6: radar layover/shadow mask (optional)

```
use_sar_mask is False because the openEO layover/shadow mask encoding is unverified. Write scripts/inspect_sar_mask.py that downloads one acquisition for a small part of a DEV event with mask=True, prints the unique mask values and counts, and saves a PNG of the mask next to a DEM hillshade. Tell me what the values mean. If the encoding is clear, wire it in behind the existing flag with a test, run the scoreboard before and after, and keep it only if no gate gets worse.
```

## Prompt 7: Sentinel-2 optical fusion

```
Add Sentinel-2 evidence in strandline/optical.py. Download cloud-masked Sentinel-2 L2A (SCL band) before and after the event on the same grid via openEO. Where both dates are clear, compute MNDWI for water and NDVI loss for debris. Fuse with radar: both agree means high confidence, one source means medium, a contradiction is flagged. Parameters in config.py, tests on synthetic arrays. Run the scoreboard before and after and show the per-event change. Keep it only if flood gates improve or stay equal and no control gets worse.
```

## Prompt 8: OSM and damage

```
Build strandline/osm.py and strandline/damage.py.
- osm.py: fetch buildings, roads (with highway type), bridges, places and health facilities (hospital, clinic, health_post, doctors) from the ohsome API at a snapshot date strictly before the event date and no later than 2026-07-27. Refuse to run otherwise. Record the snapshot date in run_report.json. Cache by bbox and snapshot date.
- damage.py: buildings, road segments and bridges that intersect flood or debris (buffer in config.py), with counts and lengths. Bridges over flooded channels are labelled "likely out, unverified".
- eval: for every dev event, run damage twice, with our flood map and with the reference extent, and append road_segment_f1 and building_f1 to eval/results/damage.csv in the gates.yaml format.
Tests for the date guard and the overlay. Then /gate and /audit.
```

## Prompt 9: who is cut off

```
Build strandline/network.py.
- Two graphs from pre-event OSM: vehicle and foot (foot includes paths and tracks). Remove flooded or debris-covered edges.
- Settlements: OSM place nodes plus DBSCAN building clusters for unnamed hamlets.
- For each settlement: can it still reach the nearest health facility and the nearest town; old and new route lengths. Run with bridges intact and with bridges failed.
- Monte Carlo, about 200 runs with a fixed seed and ranges in config.py: vary the flood threshold and the road buffer, and give each settlement a cut-off probability.
- Output GeoJSON with status, probability, and old and new routes.
- eval: for every dev event, compare cut-off settlements from our map with those from the reference extent, and append settlement_f1 to eval/results/cutoff.csv.
- Tests on a tiny hand-made graph with known answers.
Then /gate and /audit.
```

## Prompt 10: flood-path bonus

```
Build strandline/flowpath.py. Given any upstream point, trace the D8 flow path down the valley on the DEM (reuse the flow directions from terrain.py) and list the settlements within a buffer of the path in downstream order, with distance along the path. Add a CLI flag --trace-from LON LAT. Tests on a synthetic DEM with a known path.
```

## Prompt 11: the copilot (25% of the score)

```
Port the SatQuery copilot into copilot/. First read reference/satquery-ai-main/agent/ (planner.py, dsl.py, executor.py, tasks.py, registry.py), evidence/schema.py and tools/verifier.py, then port ONLY what the reuse map in CLAUDE.md allows.
- Replace every BigEarthNet tool with flood tools that read our outputs: flooded area, debris area, damaged buildings, cut roads, bridges likely out, cut-off settlements, settlement status, route to hospital, flood path.
- Numbers: templates with placeholders filled by code, plus a verifier that rejects any number (including Devanagari digits) that is not in the evidence.
- Situation report: one page, English and Nepali, map thumbnail, confidence, limitations box, attribution.
- Keep the offline keyword fallback and the execution trace.
- eval/copilot_eval.py: at least 100 normal and 50 adversarial questions ("just estimate the death toll", "roughly how many", fake numbers planted in the question), in English and Nepali, appended to eval/results/copilot.csv as one row per question (correct 0/1, invented_number 0/1).
Then /gate and /audit.
```

## Prompt 12: backend API

```
Build api/ with FastAPI: a pre-flight endpoint listing available Sentinel-1 tracks for a bbox and date; POST /runs (bbox and date, runs as a background job, progress in SQLite); GET /runs/{id}/status (stage-by-stage progress); GET /runs/{id}/layers/{name} (GeoJSON); POST /runs/{id}/ask (copilot); GET /runs/{id}/sitrep?lang=en|ne (PDF). Cache everything by bbox, date and stage. Tests with the pipeline mocked.
```

## Prompt 13: dashboard

```
Build web/ with React, TypeScript and OpenLayers on an OSM basemap. Dark, clean, mission-control style, fast:
- Draw a box and pick a date; a pre-flight panel shows the available tracks before running.
- Live progress per stage.
- Before/after swipe slider for radar and optical.
- Flood layer shaded by confidence; debris; damaged buildings, cut roads, bridges likely out.
- Settlements coloured by cut-off probability; clicking one animates its old route and its detour or "no route".
- Copilot chat where clicking any number highlights the map features it came from (reuse the trace and answer panels from reference/satquery-ai-main/web).
- Sitrep download with an English/Nepali toggle; attribution footer.
Check it at 1366x768 and at phone width. No images of victims.
```

## Prompt 14: live drills and reproducibility

```
Build eval/drill.py: picks a random area (5–30 km box) and date from a list of Himalayan and South Asian valleys and dates that are NOT in the dev events or the held-out zone, runs the full pipeline end-to-end with a time limit, and appends one row to eval/results/drills.csv (success 1 if it finishes with a valid output or a clear, correct refusal; 0 on a crash or timeout; runtime_s; failure_stage). Run 10 drills and fix the root cause of every failure with a test. Also build eval/repro.py: run 3 cases twice from cache, compare output hashes, and append to eval/results/repro.csv. Then /gate.
```

## Final day: the held-out Trishuli test (you unlock it)

1. Download the EMSR927 products into a folder OUTSIDE the project, for example `C:\heldout\EMSR927`.
2. Commit everything, then in the cmd terminal, inside the project: `type nul > .heldout_unlocked`
3. In Claude Code: `/heldout C:/heldout/EMSR927`

## Prompt 15: report and video

```
Write docs/report.md (maximum 6 pages): problem, data and the rules we followed, method, every gate with its measured numbers and source file, the held-out Trishuli results from docs/heldout_results.md, error analysis, limitations (revisit time, narrow gorges, radar geometry, debris ambiguity, bridges, OSM completeness), responsible-use note, attributions and dataset citations. Every number must come from eval/results, a run_report.json or docs/heldout_results.md. Then write a 3-minute demo video script with timings. Then /audit.
```
