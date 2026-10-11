# Strandline

**Where the flood reached, what it broke, who it stranded.**

Strandline takes any area and flood date and answers three questions for rescue teams from free
satellite data: where the flood hit, what was damaged, and which settlements are cut off. Built for
the IIT Mandi Multimodal AI Hackathon 2026, Track B "Mapping Flood Damage from Space". An
educational prototype, not an operational tool.

A *strandline* is the line of debris a flood leaves at its highest reach. Strandline maps that line
from space, and the villages it stranded.

## Setup (Windows, cmd)

```
py -3.11 -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m pytest -q
```

Register a free account at https://dataspace.copernicus.eu before the first full run.

macOS/Linux teammates: use `venv/bin/python`, and replace `venv/Scripts/python.exe` with
`venv/bin/python` in the three hook commands in `.claude/settings.json`.

## How accuracy is enforced

| Mechanism | What it does |
|---|---|
| `eval/gates.yaml` | The 90% gates for every module. Human-owned. |
| `python -m eval.check_gates` | Checks every gate against the latest results. Exit 0 only if all pass. |
| `tests/test_rules.py` | Fails if any gate is set below 90%, if the pipeline imports validation code, if the case study leaks into dev events or pipeline code, or if attributions are missing. |
| `.claude/hooks/` | Claude Code cannot edit the rules, cannot finish a turn while tests fail, and cannot touch the sealed EMSR927 reference. Every session starts with a gate-status brief. |
| `accuracy-auditor` agent | Independent read-only review for leakage, metric gaming and unsupported claims (`/audit`). |
| `/heldout` | Final-day-only Trishuli test, after the human creates `.heldout_unlocked`. |

| Gate | Target |
|---|---|
| Flood map, mean over dev events (20 m boundary tolerance) | precision, recall and F1 ≥ 0.90 |
| Flood map, worst single event | F1 ≥ 0.80 |
| No-flood control areas | false flood ≤ 1% |
| Damage overlay | road and building F1 ≥ 0.90 |
| Cut-off settlements | F1 ≥ 0.90 |
| Copilot (≥ 150 questions) | 0 invented numbers, ≥ 95% correct |
| Live drills (≥ 10 random areas/dates) | ≥ 90% success |
| Reproducibility | 100% identical outputs |

Flood scores are leave-one-event-out: parameters are chosen on the other events and scored on the
left-out one. Strict zero-tolerance scores are always reported alongside.

## Day-1 go/no-go test

**1. Pre-flight (no login needed).** Shows which Sentinel-1 tracks imaged the area and which one is picked:

```
venv\Scripts\python.exe -m strandline.run --bbox 85.10 27.85 85.45 28.30 --event-date 2026-08-26 --out runs\case_preflight --dry-run
```

**2. Full run** (in your own terminal: the first run opens a browser login, then the token is cached):

```
venv\Scripts\python.exe -m strandline.run --bbox 85.10 27.85 85.45 28.30 --event-date 2026-08-26 --out runs\case_smoke
```

**3. Geolocation check.** Drag `runs\case_smoke\flood_polygons.geojson` into https://geojson.io. Permanent
water must sit on the real river within 1–2 pixels. This is a smoke test only: do not compare the
case study with any reference map until the final day.

**4. Decision.** Go if the corridor lights up in `quicklook.png` and the polygons sit on the river.
No-go if downloads keep failing or no usable same-track pair exists.

## Outputs (in `--out`)

| File | Content |
|---|---|
| `flood_classes.tif` | uint8 class map (codes below) |
| `zscore_combined.tif` | change z-score: negative = darker, positive = brighter |
| `hand_m.tif` | height above nearest drainage (m) |
| `flood_polygons.geojson` | flood and debris polygons (EPSG:4326) with area in ha |
| `reliability_narrow_valley.tif` | uint8: 0 not valley floor, 1 valley floor resolved, 2 low confidence (floor narrower than `min_valley_width_px`) |
| `reliability_narrow_valley.geojson` | the code-2 zone as polygons, each carrying the caveat sentence |
| `quicklook.png` | before, after, change and classification panels |
| `run_report.json` | provenance: scenes, track, dates, every parameter, areas, runtime, attribution, and `reliability.narrow_valley` |
| `cache\` | downloaded rasters, reused on every rerun |

| Code | Meaning |
|---|---|
| 0 | no data |
| 1 | no significant change |
| 2 | flood water, high confidence |
| 3 | flood water, medium confidence |
| 4 | possible debris or deposit |
| 5 | permanent water |
| 6 | not analysed (steep or high terrain) |

## Method (flood mapping)

1. **Track selection.** Pick the relative orbit with the earliest post-event image and at least 3 pre-event images. If none qualifies, refuse rather than guess.
2. **Speckle filtering.** Lee filter on linear backscatter, then dB.
3. **Change scoring.** Per-pixel z-score of the post-event image against the pre-event stack, VV and VH (multi-temporal approach, DeVries et al., 2020).
4. **Water and debris rules**, by mode (`detect.mode`, chosen by `eval/tune.py`, never per area).
   - `rough_water` (**the default**): a coherent change of *either sign* low on the valley floor is water — at least `min_abs_change_db` (3 dB) in both polarisations, both moving the same way, and |z| past `z_seed`. A turbulent, sediment-laden gorge flood is a *rough* scatterer at C-band and can brighten rather than darken, so the sign of the change is not a reliable signature there. Water and debris are separated by terrain position: on the valley floor a strong change is water, above it strong brightening is debris. Darkness becomes the high-confidence test only.
   - `darkening`: water is dark and darkens — strong darkening plus post-event VV below an Otsu threshold on the valley floor; debris brightens in both polarisations with a rough post-event surface. The correct physics on a flat floodplain, and infeasible on the dev controls (see Status).
5. **Hysteresis growing.** Strong-change seeds start a region; weaker change joins only if connected.
6. **Terrain masks.** HAND and slope from the Copernicus DEM; permanent water excluded.
7. **Cleanup.** Minimum mapping unit; every region must connect to the drainage network.
8. **Reliability layer (no effect on detection).** From the DEM alone: valley-floor pixels whose
   local floor thickness is below `min_valley_width_px` (default 3 px = 60 m at the shipped 20 m
   grid) are marked *"low confidence: valley narrower than radar resolution"*. A resolution cell
   there straddles water, bank and wall, so its class is a statement about which scatterer
   dominated a mixed pixel. Thickness is measured by morphological opening of the valley-floor
   mask with a disc of radius `min_valley_width_px / 2`. `run_report.json` records the zone's
   area and **what share of the flood-affected area falls inside it**; the dashboard and the
   copilot attach that sentence to any number drawn from it. The layer never reads the class map
   except to count that share, and never changes a detected pixel (`tests/test_reliability.py`).

The detection runs in `rough_water` mode (see Status below). All parameters live in
`strandline/config.py`; `ReliabilityParams` is deliberately outside `params_hash`, because it
cannot change an output pixel and must not give a new hash to an unchanged detection.

## Scoring the dev set (checking only, never an input)

Three steps. Nothing here is ever a pipeline input: references are read only by `validation/` and
`eval/`, and `strandline/` cannot import either (tested).

```
rem 1. fetch the Sentinel-1 and DEM rasters the scoreboard needs (openEO, resumable, 3 in parallel)
venv\Scripts\python.exe -m eval.fetch_dev_data --estimate
venv\Scripts\python.exe -m eval.fetch_dev_data --plan gates --workers 3

rem 2. run and score every event at the current config.py parameters
venv\Scripts\python.exe -m eval.run_scoreboard --no-write

rem 3. choose parameters by leave-one-event-out and score each left-out event
venv\Scripts\python.exe -m eval.tune
venv\Scripts\python.exe -m eval.check_gates
```

| Script | What it does |
|---|---|
| `eval/fetch_dev_data.py` | Downloads only allowed inputs (Sentinel-1, Copernicus DEM) into `runs/eval/<event>/cache`, resumable, `--estimate` for a size plan with no network. |
| `eval/run_scoreboard.py` | Runs the pipeline per event from the cache and scores floods against their reference using each event's `reference_classes`; controls on `false_flood_fraction`. Appends rows to `eval/results/flood.csv`. |
| `eval/tune.py` | Leave-one-event-out over the grid in `config.py::TUNE_GRID`: maximise mean tolerant F1 on the OTHER events subject to every control ≤ 1% false flood, then score the left-out event (`split=loeo`). Writes `eval/chosen_params.json` and a diff against `config.py` — it never edits `config.py`. |
| `validation/compare.py` | The metric definitions, used by both the scoreboard and the CLI below, so a manual check and a gate row cannot disagree. |

Things these scripts refuse to do, by design:

- **Download during scoring.** A missing raster is an error naming the fetch command.
- **Write a partial set of rows.** If any planned flood event fails, no flood row is written at all,
  so a gate is never an average over whichever events happened to work.
- **Shrink the scoring area.** The domain is every analysed pixel of the event's published bbox,
  terrain-excluded ground included; the domain fraction is reported next to every score. An event
  too large for memory is refused, never clipped.
- **Apply its own answer.** The tuner prints a diff; a human edits `config.py`.

Coverage is printed as *scored / planned / declared* after every run, so a dev set that is only
partly downloaded can never be mistaken for the whole one.

### One event, by hand

```
venv\Scripts\python.exe -m validation.compare --pred runs\<dev_event>lood_classes.tif --reference data
eference\<dev_event>\<observed_event_file> --classes "Flooded area" --aoi data
eference\<dev_event>\<aoi_file> --tolerance-px 1 --out runs\<dev_event>alidation
```

Reports strict and boundary-tolerant precision, recall, F1 and IoU, plus an error map.

## Status

**Verified (synthetic tests only):** detection logic on a synthetic Himalayan valley with gamma
speckle (flood F1 ≥ 0.99 over 10 seeds); rejection of mountain bait, isolated specks and the
permanent river; near-zero false flood on no-event scenes; track selection; HAND; metrics; a full
offline end-to-end run with mocked downloads; the gate checker. Synthetic results prove the logic,
not real-world accuracy.

**Measured on real data (6 independent mountain/valley flood references, leave-one-event-out,
20 m boundary tolerance).** Primary quantity is flood-affected area = our flood classes ∪ debris,
against each reference's flooded + debris/mass-movement classes. Rows: `eval/results/flood.csv`
(`split=loeo`); method and caveats: `eval/chosen_params.json`, `docs/tuning_log.md`.

| Gate | Measured | Target | Status |
|---|---|---|---|
| mean precision / recall / F1, tolerant | 0.518 / 0.511 / **0.513** | ≥ 0.90 | **FAIL** |
| worst single event, F1 tolerant | **0.069** | ≥ 0.80 | **FAIL** |
| mean F1 / IoU, strict | 0.407 / 0.294 | report | — |
| no-flood controls, max false flood | **0.0048** | ≤ 0.01 | **PASS** |

Per-event held-out tolerant F1: Konjic 0.069, Punakha 0.118, Poza Honda 0.555, Selsverket 0.588,
Larkana 0.834, Ringebu 0.915. Coverage: 6/6 gate-eligible floods, 4 of 8 declared controls.

The detector now runs in `rough_water` mode, chosen over the original `darkening` mode by
leave-one-event-out: it wins on 5 of 6 held-out events, and `darkening` is in fact **infeasible** —
all 18 of its grid points breach the control ceiling, worst case 4.9% false flood on a dry
Norwegian valley. The terrain widening offered by the grid was **not** chosen, so the gain comes
from the rough-water channel and the debris tightening.

**Why the flood gate fails, measured.** Held-out F1 tracks the reference's positive fraction (rank
correlation 0.886), and the predicted *area* is close to correct on every event (e.g. 33.5 ha
against a 42.6 ha reference at Konjic, 785 vs 863 ha at Ringebu). The quantity of flooding is
approximately right; on the two steepest events with the smallest references it is in the **wrong
place**. That is a localisation failure at 20 m in a gorge, not a threshold that needs retuning —
no parameter in the declared grid fixes it. Options and their costs are in `docs/tuning_log.md`.

**Honest summary for the report:** this maps flood-affected area well where the flood covers ≥ 2%
of the area and the ground is moderate (0.83–0.92 tolerant F1), and poorly where the flood covers
< 0.2% of a steep gorge (0.07–0.12). It does not meet the 90% gate. Positive fraction and slope are
confounded in this dev set, so those two explanations cannot yet be separated.

**Not yet verified:** damage overlay, cut-off settlements, copilot, live drills and reproducibility
all read NO DATA — not started.

## Known limitations (for the report)

- **Revisit time.** Sentinel-1 images an area only every few days: it maps aftermath, it cannot warn.
- **Narrow gorges.** At 20 m, edges are uncertain; boundary-tolerant and strict scores are both reported.
- **Radar geometry.** Layover and shadow on steep slopes; the SAR mask band stays off until its encoding is verified.
- **Debris ambiguity.** Brightening can also come from vegetation or structural change, hence "possible debris". Measured on EMSR770: the current debris channel covers 3.8% of that AOI at precision 0.0062 against the expert mass-movement class — far too permissive, see `docs/tuning_log.md`.
- **Rough water.** The detector assumes open water is dark. In a steep gorge a turbulent, sediment-laden flood is a rough scatterer and brightens instead; measured on EMSR770, 0.0% of the expert flood extent passes the darkness gate. This is a method limitation, not a threshold to retune.
- **Bridges.** Too narrow to confirm collapse at this resolution.

## Attribution

- Contains modified Copernicus Sentinel data 2026.
- Produced using Copernicus WorldDEM-30 © DLR e.V. 2010–2014 and © Airbus Defence and Space GmbH 2014–2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.
- © OpenStreetMap contributors.
- Reference maps used for checking: European Union, Copernicus Emergency Management Service data.
- Training data, if used: Kuro Siwo (Bountos et al., 2024); Sen1Floods11 (Bonafilia et al., CVPR Workshops 2020).
