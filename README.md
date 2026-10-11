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
against each reference's flooded + debris/mass-movement classes. Every number below is a row in
`eval/results/flood.csv` (`run_id=20261011T110254Z`, `params_hash=d4f9b1791c82`, `split=loeo` for
floods, `split=full` for controls); method and caveats: `eval/chosen_params.json`,
`docs/tuning_log.md`. Measured at `n_pre=3` (`DEV_N_PRE`, the dev download-cost setting), a thinner
reference stack than the shipped default of 5.

### Final per-event scores (`venv\Scripts\python.exe -m eval.check_gates`)

Strict = zero tolerance; tolerant = 20 m (1 px) boundary tolerance. Each row is the event held out
of parameter selection.

| Event | Terrain | P | R | **F1** | IoU | P tol | R tol | **F1 tol** |
|---|---|---|---|---|---|---|---|---|
| bosnia_konjic_2024 | gorge, 21.8° mean slope | 0.036 | 0.028 | **0.032** | 0.016 | 0.065 | 0.075 | **0.069** |
| bhutan_punakha_2024 | gorge, 24.4° | 0.071 | 0.081 | **0.076** | 0.039 | 0.111 | 0.126 | **0.118** |
| ecuador_poza_honda_2025 | hill valley, 13.1° | 0.432 | 0.390 | **0.410** | 0.258 | 0.579 | 0.534 | **0.555** |
| norway_selsverket_2023 | valley, 16.3° | 0.486 | 0.467 | **0.477** | 0.313 | 0.562 | 0.616 | **0.588** |
| pakistan_larkana_2022 | irrigated plain, 0.3° | 0.822 | 0.636 | **0.717** | 0.559 | 0.866 | 0.805 | **0.834** |
| norway_ringebu_2023 | broad valley, 9.9° | 0.769 | 0.699 | **0.732** | 0.577 | 0.923 | 0.907 | **0.915** |
| **mean (6 events)** | | **0.436** | **0.384** | **0.407** | **0.294** | **0.518** | **0.511** | **0.513** |

No-flood controls, false flood fraction: Punakha 0.0000, Konjic 0.0000, Ringebu 0.0014,
Poza Honda 0.0048. Coverage: 6/6 gate-eligible floods, 4 of 8 declared controls.

### Gate status

| Gate | Measured | Target | Status |
|---|---|---|---|
| `flood_mapping` mean precision, tolerant | 0.5177 | ≥ 0.90 | **FAIL** |
| `flood_mapping` mean recall, tolerant | 0.5105 | ≥ 0.90 | **FAIL** |
| `flood_mapping` mean F1, tolerant | 0.5134 | ≥ 0.90 | **FAIL** |
| `flood_mapping` worst single event, F1 tolerant | 0.0694 | ≥ 0.80 | **FAIL** |
| `flood_mapping` mean F1, strict | 0.4071 | report | — |
| `flood_mapping` mean IoU, strict | 0.2937 | report | — |
| `no_flood_controls` max false flood | 0.0048 | ≤ 0.01 | **PASS** |

**Module status: `flood_mapping` FAIL, `no_flood_controls` PASS.** By the definition of done in
CLAUDE.md, SAR flood mapping is **not done**. It is finalised, not passing: the method, the
parameters and the evidence are fixed and committed, and the gate it misses is reported as missed.

The detector runs in `rough_water` mode, chosen over the original `darkening` mode by
leave-one-event-out: it wins on 5 of 6 held-out events, and `darkening` is in fact **infeasible** —
all 18 of its grid points breach the control ceiling, worst case 4.9% false flood on a dry
Norwegian valley. The terrain widening offered by the grid (`hand_max_water_m` 20 → 35 m) was
**not** chosen, so the gain comes from the rough-water channel and the debris tightening.

### The limitation that fails the gate: gorge localisation, measured

Held-out F1 tracks the reference's positive fraction (rank correlation **0.886**), and the predicted
*area* is close to correct on every event:

| Event | F1 tol | reference ha | reference % of AOI | predicted ha | mean slope |
|---|---|---|---|---|---|
| bosnia_konjic_2024 | 0.069 | 42.6 | 0.134% | 33.5 | 21.8° |
| bhutan_punakha_2024 | 0.118 | 31.3 | 0.023% | 35.3 | 24.4° |
| ecuador_poza_honda_2025 | 0.555 | 197.6 | 0.306% | 178.6 | 13.1° |
| norway_selsverket_2023 | 0.588 | 125.1 | 1.015% | 120.3 | 16.3° |
| pakistan_larkana_2022 | 0.834 | 50187.7 | 50.795% | 38836.6 | 0.3° |
| norway_ringebu_2023 | 0.915 | 863.4 | 2.015% | 785.2 | 9.9° |

Sources, column by column: F1 from `eval/results/flood.csv`; `reference_ha`,
`reference_fraction_of_domain` and `predicted_flood_affected_ha` from each event's score file
`runs/eval/<event>/scores/d4f9b1791c82.json`; mean slope from the DEM-derived `terrain:` note
declared per event in `eval/events.yaml`.

**The quantity of flooding is approximately right; on the two steepest events it is in the wrong
place.** At Konjic the detector predicts 33.5 ha against a 42.6 ha reference and overlaps almost
none of it. That is a **localisation failure at 20 m in a gorge, not a threshold that needs
retuning** — no parameter in the declared grid fixes it, and `hand_max_water_m` was offered 35 m by
the grid and the data declined it. A 20 m resolution cell in a floor only two or three pixels wide
straddles water, bank and wall, so its class reflects whichever scatterer dominated a mixed pixel.
That is what `reliability_narrow_valley.tif` now marks, and what
`run_report.json → reliability.narrow_valley` quantifies per run.

Positive fraction and mean slope are **confounded** in this dev set — the steepest events also have
the smallest references — so this evidence cannot separate "steep terrain" from "tiny target". Both
explanations point at the same two events. Options and their costs are in `docs/tuning_log.md`
(2026-10-11); the one aimed at the measured cause is geometric, not a retune.

**Honest summary for the report:** this maps flood-affected area well where the flood covers ≥ 2%
of the area and the ground is moderate (0.83–0.92 tolerant F1), and poorly where the flood covers
< 0.2% of a steep gorge (0.07–0.12). It does not meet the 90% gate, and the split — not the average
— is what a rescuer should be told.

**Not yet verified:** damage overlay, cut-off settlements, copilot, live drills and reproducibility
all read NO DATA — not started.

## Known limitations (for the report)

- **Revisit time.** Sentinel-1 images an area only every few days: it maps aftermath, it cannot warn.
- **Narrow gorges — the measured failure, and the biggest one.** A 20 m resolution cell in a valley
  floor only two or three pixels wide straddles water, bank and wall. Measured, leave-one-event-out:
  tolerant F1 **0.069 at Konjic (21.8° mean slope) and 0.118 at Punakha (24.4°)**, against
  0.834–0.915 on the two flattest events, while the predicted *area* is close to right everywhere
  (33.5 vs 42.6 ha at Konjic, 35.3 vs 31.3 at Punakha). The flood is found in roughly the right
  quantity and the **wrong place**. No parameter in the declared grid fixes it; the fix is
  geometric (see `docs/tuning_log.md`, 2026-10-11, option 2). Every run therefore ships
  `reliability_narrow_valley.tif` / `.geojson` marking that ground *"low confidence: valley narrower
  than radar resolution"*, and `run_report.json → reliability.narrow_valley` gives the share of the
  flood-affected area inside it. Boundary-tolerant and strict scores are both always reported.
- **Positive fraction and slope are confounded** in this dev set: the steepest events also have the
  smallest references (0.023–0.134% of the AOI), so the evidence cannot say whether the detector
  fails on steep ground or on tiny targets. Both readings are stated, neither is claimed.
- **Radar geometry.** Layover and shadow on steep slopes; the SAR mask band stays off until its encoding is verified.
- **Debris ambiguity.** Brightening can also come from vegetation or structural change, hence "possible debris". Measured on EMSR770: the current debris channel covers 3.8% of that AOI at precision 0.0062 against the expert mass-movement class — far too permissive, see `docs/tuning_log.md`.
- **Rough water — addressed, not solved.** The original `darkening` method assumed open water is dark; in a steep gorge a turbulent, sediment-laden flood is a rough scatterer and brightens instead, and measured on EMSR770 **0.0% of the expert flood extent passed the darkness gate**. The default is now `rough_water`, which accepts a coherent change of either sign on the valley floor and takes darkness only as the high-confidence test. That changed the numbers decisively (Punakha 0.025 → 0.118, Poza Honda 0.125 → 0.555 tolerant F1, and the binding Norwegian control from 4.9% to 0.14% false flood) and it is still far from the gate: the sign of the change was not the only problem.
- **Bridges.** Too narrow to confirm collapse at this resolution.

## Attribution

- Contains modified Copernicus Sentinel data 2026.
- Produced using Copernicus WorldDEM-30 © DLR e.V. 2010–2014 and © Airbus Defence and Space GmbH 2014–2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.
- © OpenStreetMap contributors.
- Reference maps used for checking: European Union, Copernicus Emergency Management Service data.
- Training data, if used: Kuro Siwo (Bountos et al., 2024); Sen1Floods11 (Bonafilia et al., CVPR Workshops 2020).
