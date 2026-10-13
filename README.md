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
| `flood_source.tif` | uint8 per-pixel sensor provenance: 0 not flood-affected, 1 Sentinel-1 only, 2 Sentinel-2 only, 3 both sensors agree |
| `quicklook.png` | before, after, change and classification panels |
| `run_report.json` | provenance: scenes, track, dates, every parameter, areas, runtime, attribution, `reliability.narrow_valley` and the `optical` block (Sentinel-2 status, fusion mode, whether it was fused into the map, dates, cloud cover, clear fraction, how much of the area optical covered) |
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

**Flood extent vs what is merely mapped.** `ExtentParams.debris_in_flood_extent` (default `True`)
decides whether class 4 counts as flood *extent*. Debris is written to `flood_classes.tif` either
way — the flag changes no pixel of the class map — but with `False` it is reported as
*"possible debris, unverified"*, excluded from `flood_extent_ha`, excluded from the scored
quantity, and **must not be consumed by the damage overlay or the cut-off analysis**. Every run
writes a `flood_extent` block to `run_report.json` saying which classes are inside it, so a
downstream module never has to guess. The flag has its own dataclass rather than sitting on
`DetectParams`, which is what keeps every published `params_hash` reproducible: the section enters
the hash only when it differs from the default.

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

## Method (Sentinel-2 optical evidence)

Track B requires Sentinel-2 where the sky is clear, so a live run gathers it by default
(`--no-optical` turns the stage off). It is **evidence, not an assumption**: nothing here guesses
through cloud, and where optical cannot see, the map is the radar map.

**Gathered and fused by default, since it was measured.** `OpticalParams.fusion` was `off` until
2026-10-12 — Sentinel-2 gathered and reported on every run but changing no pixel — precisely so
that no unmeasured change reached the scored quantity. The leave-one-event-out optical pass has
now run and adopted `union` under the rule declared before it (see Status). `--optical-fusion off`
still produces the radar-only map exactly, and is the control the comparison rests on
(`tests/test_detect_optical_fusion.py`, `tests/test_pipeline_optical.py`).

1. **Scene choice from metadata.** Every L2A product carries a `cloudCover` attribute, so the
   clearest pre-event and post-event *date* is chosen from the catalogue, before any download —
   a clouded area costs one catalogue query and nothing else. Choice is per UTC date, not per
   scene, because openEO mosaics a date's tiles; a date's cloudiness is the area-weighted mean
   over the scenes that overlap the AOI, and a date the catalogue does not report is counted as
   fully cloudy. The post-event window is **20 days** (`--optical-post-days`), much longer than
   the radar window, because mud and debris scars persist — at the cost, reported in every run,
   that standing water may have drained by then.
2. **Per-pixel masking.** The scene classification layer (SCL) removes cloud, cloud shadow,
   terrain shadow, cirrus, snow and defective pixels. It is a *class* band, so it is downloaded
   and resampled nearest-neighbour in its own cube — never bilinearly. The share of the AOI clear
   on each date and on the pair is reported; below `min_clear_fraction` (20%) optical is refused
   with its reason.
3. **Reflectance, with the offset decided from the pixels.** Scale and offset are taken from the
   file when it declares them. Otherwise the values are digital numbers, and whether
   `BOA_ADD_OFFSET` still needs applying is **measured, not assumed**: the processing baseline in
   the product name says the offset *exists*, but not whether the backend has already applied it,
   and this one has. So the rule is *apply the specification offset unless doing so would make
   more than 5% of valid pixels negative* — reflectance is not a negative quantity, true
   over-correction is a few percent at worst (measured: ≤ 2.0%), and a doubled offset is tens of
   percent (measured: 9–63% on 19 of the 20 dev-set files). It is **not a universal
   discriminator**: the twentieth is 87% cloud, has no dark pixels to place the zero point, and
   reads 1.70% either way — below the 1.96% worst case of a correctly converted file, so the two
   overlap. A scene that bright is under cloud by definition and is refused on
   `min_clear_fraction` before any index is computed; that, not the margin, is what makes the
   undecidable case harmless, and `tests/test_optical_real_files.py` asserts the disjunction
   (every real file is **either** decidable **or** too cloudy to use). The rule is symmetric, so
   a genuine archive product still gets its offset and no code changes if the backend's
   convention ever does. Both candidate figures, the
   raw minimum and which test decided are written to `run_report.json`
   (`optical.evidence.reflectance_conversion.<date>.offset_decision`), and an implausible result
   refuses the stage rather than poisoning an index. This matters out of all proportion to its
   size: MNDWI and NDVI are normalised ratios, so a multiplicative scale cancels but an offset
   does not. Getting it wrong is exactly the bug found on the first real download — see
   `docs/tuning_log.md`, 2026-10-12.
4. **Two change signatures, never a single-date classification.**
   - *water*: MNDWI = (B03 − B11)/(B03 + B11) (Xu 2006) must read as water **and** have risen
     since the pre-event date. The rise test is what rejects permanent water, wet rock and dark
     shadow, which are high or ambiguous on both dates.
   - *deposit*: NDVI must drop **and** B11 reflectance must rise — vegetation or soil buried
     under sediment loses near-infrared vigour and gains short-wave infrared brightness.
5. **Fusion (`--optical-fusion`), before the shared tests, not after them.** Three modes:
   `off` (gathered and reported, changes no pixel), `union` (the shipped default: optical water
   *and* deposit candidates are added), and `water_only` (added 2026-10-12: the MNDWI water
   evidence is fused and the NDVI-drop deposit channel contributes nothing — scored, and measured
   to tie `union` exactly once debris is out of the flood extent, but not adopted, see Status).
   Optical
   candidates are added to the radar masks *inside* `detect.detect_flood`, so the same HAND, slope,
   minimum-mapping-unit and drainage-connection tests apply to them. There is no second definition
   of "valley floor" and optical cannot place water where radar would not have been allowed to.
   Where both sensors call a pixel water it becomes **high confidence**; `flood_source.tif` records
   which sensor saw each pixel. Every optical parameter, fusion included, lives in `OpticalParams`
   and never in `DetectParams` — that is what keeps a radar-only `params_hash` byte-identical to
   the ones already published, and it is pinned by
   `tests/test_params_hash_reproducible.py`.
6. **Never fatal.** No clear scene, too much cloud, an unreadable file, a dead backend — every
   optical failure degrades the run to radar-only and is recorded in `run_report.json` under
   `optical.status` (`used`, `disabled`, `no_clear_pair`, `refused`, `failed`) with its reason
   (`tests/test_pipeline_optical.py`).

**`union` is SCORED, on floods and on controls.** The numbers in Status below are the fused
configuration (final `params_hash 63a6d284bc4e`; two leave-one-out folds carry their own hash, see
below), and because fusion is now the default they describe the map a default run produces. Controls were re-scored too, as they had to be: fusion can only
add pixels and so can only raise the false-flood rate, and the worst control did rise, from
0.0000 to 0.0022, well inside the 1% ceiling. A run that merely *consulted* Sentinel-2 still
records its own `params_hash` (the optical section enters the hash whenever the stage is enabled):
the hash says what the run asked for, `optical.status` says what it got — `used` on 6 of the 10
dev events, `refused` on 4 where the clear pair fell below 20%. See `docs/tuning_log.md`,
2026-10-12, for the pass, the adoption rule and the caveat on where the gain comes from.

## Scoring the dev set (checking only, never an input)

Three steps. Nothing here is ever a pipeline input: references are read only by `validation/` and
`eval/`, and `strandline/` cannot import either (tested).

```
rem 1. fetch the Sentinel-1 and DEM rasters the scoreboard needs (openEO, resumable, 3 in parallel)
venv\Scripts\python.exe -m eval.fetch_dev_data --estimate
venv\Scripts\python.exe -m eval.fetch_dev_data --plan gates --workers 3

rem 1b. and the Sentinel-2 pair per event, needed to score optical fusion
rem     (454 MB more in 40 files, MEASURED; floods AND controls, since fusion only adds pixels)
venv\Scripts\python.exe -m eval.fetch_dev_data --estimate --optical
venv\Scripts\python.exe -m eval.fetch_dev_data --plan gates --optical --workers 3

rem 2. run and score every event at the current config.py parameters
venv\Scripts\python.exe -m eval.run_scoreboard --no-write

rem 3. choose parameters by leave-one-event-out and score each left-out event
venv\Scripts\python.exe -m eval.tune

rem 3b. and the second pass over the optical grid: fusion off vs union, same SAR
rem      point per fold, then the declared adoption rule. Rows only if it says KEEP.
venv\Scripts\python.exe -m eval.tune --plan gates --optical

venv\Scripts\python.exe -m eval.check_gates
```

| Script | What it does |
|---|---|
| `eval/fetch_dev_data.py` | Downloads only allowed inputs (Sentinel-1, Copernicus DEM, and with `--optical` the Sentinel-2 pair) into `runs/eval/<event>/cache`, resumable, `--estimate` for a size plan with no network. An event with no clear Sentinel-2 pair downloads none and is scored radar-only. |
| `eval/run_scoreboard.py` | Runs the pipeline per event from the cache and scores floods against their reference using each event's `reference_classes`; controls on `false_flood_fraction`. Appends rows to `eval/results/flood.csv`. |
| `eval/tune.py` | Leave-one-event-out over the grid in `config.py::TUNE_GRIDS`: maximise mean tolerant F1 on the OTHER events subject to every control ≤ 1% false flood, then score the left-out event (`split=loeo`). With `--optical`, a **second** leave-one-event-out pass over `OPTICAL_TUNE_GRIDS`, crossing each fold's own SAR point with every optical point, then the adoption rule in `config.OPTICAL_ADOPTION` — declared before the pass, and rows are appended only if it says KEEP. Before the verdict is trusted it checks that fusion `off` reproduces the radar-only numbers exactly (30/30 on the current dev set). Writes `eval/chosen_params.json` and a diff against `config.py` — it never edits `config.py`. |
| `validation/compare.py` | The metric definitions, used by both the scoreboard and the CLI below, so a manual check and a gate row cannot disagree. |
| `eval/diagnose_errors.py` | Takes the error map apart, per event: every missed reference pixel attributed to one cause (not analysed / no change signal / a named rule), every false alarm attributed to a terrain position and the rule that admitted it, boundary offset and a ±5 px shift scan, acquisition facts, and an oracle upper bound per error source. Writes `docs/accuracy_diagnosis.md` plus `docs/accuracy_diagnosis.json`, its provenance. Changes no code and no parameter, appends no results row, and re-renders from the JSON with `--from-json`. |

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
20 m boundary tolerance).** Primary quantity is **flood extent = our flood classes only** — debris
was taken out of it on 2026-10-12 — scored against each reference's flooded **+ debris/mass-movement**
classes. The reference side deliberately does **not** follow that change, which makes this the
harder reading: a reference debris polygon is now an unreachable miss. Every number below is a row in
`eval/results/flood.csv` (`run_id=20261012T165259Z`, `split=loeo` for floods, `split=full` for
controls). Leave-one-event-out means the rows do **not** share one `params_hash`: four events were
scored at `0550873e7ea6`, Poza Honda at `d46da9ccd649` and Larkana at `f08e2edf2c4c`, those two
being the folds whose SAR `z_seed` differed — the per-fold hashes are in
`eval/chosen_params.json`; method and caveats: `eval/chosen_params.json`,
`docs/tuning_log.md`. Measured at `n_pre=3` (`DEV_N_PRE`, the dev download-cost setting), a thinner
reference stack than the shipped default of 5.

> **These numbers are Sentinel-1 + Sentinel-2 fused, with debris OUTSIDE the flood extent, and
> that is what a default run maps** (`params_hash 0550873e7ea6`). Adopted on 2026-10-12 by the
> Pareto-dominance rule: in every one of the six leave-one-event-out folds it is at least as good
> as the previous configuration on mean training tolerant F1 **and** on both false-flood fractions
> of all four controls, strictly better on 24 of those quantities, and worse on none. The rule was
> declared and committed before it was implemented, but **after** the stricter criterion found no
> feasible point — that is unlike every other rule in this project and `docs/tuning_log.md` says
> so plainly.
>
> **Debris is no longer part of the flood extent.** The debris layer is still written and must be
> labelled *"possible debris, unverified"*; it is excluded from `flood_extent_ha`, from the scored
> quantity, and **must not be consumed by the damage overlay or the cut-off analysis**. Why:
> measured precision **0.0** against the only expert mass-movement reference in the dev set, and
> 96% of all its dev-set pixels on a dry no-flood control.
>
> **The optical gain remains concentrated in the wrong place.** Most of it is one event — Larkana,
> a flat floodplain with a 100% clear pair and the second-best radar score. In the steep gorges
> where the system actually fails, optical adds little and on four of the ten events cloud refused
> it altogether. Fusing Sentinel-2 helps most where radar already worked; it does not fix the
> measured failure.

### Final per-event scores (`venv\Scripts\python.exe -m eval.check_gates`)

Strict = zero tolerance; tolerant = 20 m (1 px) boundary tolerance. Each row is the event held out
of parameter selection.

| Event | Terrain | P | R | **F1** | IoU | P tol | R tol | **F1 tol** |
|---|---|---|---|---|---|---|---|---|
`S2` is the share of the area Sentinel-2 could see on **both** dates; `—` means cloud refused
optical and the row is radar-only.

| Event | Terrain | S2 | P | R | **F1** | IoU | P tol | R tol | **F1 tol** |
|---|---|---|---|---|---|---|---|---|---|
| bosnia_konjic_2024 | gorge, 21.8° mean slope | 88% | 0.039 | 0.038 | **0.038** | 0.019 | 0.067 | 0.098 | **0.080** |
| bhutan_punakha_2024 | gorge, 24.4° | 54% | 0.081 | 0.091 | **0.085** | 0.045 | 0.126 | 0.147 | **0.136** |
| ecuador_poza_honda_2025 | hill valley, 13.1° | — | 0.432 | 0.390 | **0.410** | 0.258 | 0.579 | 0.534 | **0.555** |
| norway_selsverket_2023 | valley, 16.3° | 33% | 0.480 | 0.468 | **0.474** | 0.311 | 0.556 | 0.616 | **0.585** |
| pakistan_larkana_2022 | irrigated plain, 0.3° | 100% | 0.790 | 0.843 | **0.815** | 0.688 | 0.862 | 0.918 | **0.889** |
| norway_ringebu_2023 | broad valley, 9.9° | — | 0.769 | 0.699 | **0.732** | 0.577 | 0.923 | 0.907 | **0.915** |
| **mean (6 events)** | | | **0.432** | **0.421** | **0.426** | **0.316** | **0.519** | **0.537** | **0.527** |

No-flood controls, false flood fraction: Punakha 0.0001, Konjic 0.0011, Ringebu 0.0014,
Poza Honda 0.0048. Coverage: 6/6 gate-eligible floods, 4 of 8 declared controls.

> **The shipped configuration FAILS the project's own stricter control criterion.** Say this
> first, because it is the most important caveat on the flood map. The gated metric divides by the
> whole scoring domain, which deliberately includes terrain-excluded ground (a reference flood
> there would be a miss we must be charged for, so narrowing it would be Rule 3.2). On a control
> there is no reference to miss, so on a Himalayan AOI that is 95% terrain-excluded the same false
> pixels are divided by twenty. Over the ground the detector could actually classify, the four
> controls read **0.28%, 1.00%, 0.68% and 1.64%** against a declared 1% ceiling — so **one of four
> still fails it**, Poza Honda, and Konjic sits exactly on the line. Before the 2026-10-12
> adoption three of four failed; taking debris out of the flood extent cut Punakha from 4.37% to
> 0.28%. Recorded as `false_flood_fraction_analysed_area` in each control's score file:
> reported, never gated, because the gates file is human-owned and keeps its declared metric.
>
> On 2026-10-12 that criterion was declared as an additional, stricter feasibility constraint and a
> leave-one-event-out pass was run over a 14-point grid (fusion `off`/`union`/`water_only` crossed
> with debris in or out of the flood extent). **Nothing in the grid was feasible, so nothing was
> adopted** and no row was written. The binding control is `ecuador_poza_honda_control_2024` at
> 1.64%, identical at all 14 points: optical was refused there (12.7% clear) and its debris count
> is zero, so neither axis can touch it — its 7,780 false pixels are Sentinel-1 flood *water*. The
> refusal, every candidate's rejection reason (30 radar points under `final`, 42 second-pass
> configurations under `optical_pass`) and three options with costs are in
> `eval/chosen_params_strict_control.json` and `docs/tuning_log.md`, 2026-10-12. Reproduce with
> `python -m eval.tune --plan gates --optical --chosen-out eval/chosen_params_strict_control.json`.
>
> That refusal was then resolved by a rule change the human declared and committed: the
> **Pareto-dominance rule**, which adopted debris-OUT (mean tolerant F1 0.5231 → 0.5266, Punakha's
> analysed-area control rate 0.0437 → 0.0028). It did **not** make the configuration feasible and
> was never capable of it: Poza Honda's 1.64% is identical at all 42 configurations, because its
> false pixels are Sentinel-1 flood water, optical is refused there and its debris count is zero.
> **The stricter criterion remains failed on that control and that remains an open issue.**

### Gate status

| Gate | Measured | Target | Status |
|---|---|---|---|
| `flood_mapping` mean precision, tolerant | 0.5188 | ≥ 0.90 | **FAIL** |
| `flood_mapping` mean recall, tolerant | 0.5366 | ≥ 0.90 | **FAIL** |
| `flood_mapping` mean F1, tolerant | 0.5266 | ≥ 0.90 | **FAIL** |
| `flood_mapping` worst single event, F1 tolerant | 0.0798 | ≥ 0.80 | **FAIL** |
| `flood_mapping` mean F1, strict | 0.4258 | report | — |
| `flood_mapping` mean IoU, strict | 0.3163 | report | — |
| `no_flood_controls` max false flood | 0.0048 | ≤ 0.01 | **PASS** |

**Module status: `flood_mapping` FAIL, `no_flood_controls` PASS.** By the definition of done in
CLAUDE.md, flood mapping is **not done**. It is finalised, not passing: the method, the parameters
and the evidence are fixed and committed, and the gate it misses is reported as missed.

**Flood mapping is FROZEN** as of 2026-10-12. No further detection change. Everything built on top
of it — the damage overlay, the cut-off analysis, the copilot, the dashboard and the sitrep — reads
this map as it stands and must carry three things with every number it quotes: the measured
accuracy above, the narrow-valley caveat, and the **failed stricter control criterion** on Poza
Honda (1.64% over analysed ground).

`debris_in_flood_extent` is now `False`, so the flood extent is **flood water only**. The debris
layer is still written and must be labelled *"possible debris, unverified"*; the damage overlay and
the cut-off analysis **must not consume it** — its measured precision against the only expert
debris reference in the dev set is 0.0.

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
| bosnia_konjic_2024 | 0.079 | 42.6 | 0.134% | 42.3 | 21.8° |
| bhutan_punakha_2024 | 0.116 | 31.3 | 0.023% | 46.3 | 24.4° |
| ecuador_poza_honda_2025 | 0.555 | 197.6 | 0.306% | 178.6 | 13.1° |
| norway_selsverket_2023 | 0.585 | 125.1 | 1.015% | 121.8 | 16.3° |
| pakistan_larkana_2022 | 0.889 | 50187.7 | 50.795% | 53535.1 | 0.3° |
| norway_ringebu_2023 | 0.915 | 863.4 | 2.015% | 785.2 | 9.9° |

Sources, column by column: F1 from `eval/results/flood.csv`; `reference_ha`,
`reference_fraction_of_domain` and `predicted_flood_affected_ha` from each event's score file
under `runs/eval/<event>/scores/` at that fold's chosen hash (`63a6d284bc4e` for four events,
`297a77922b9f` for Poza Honda and `e5e341834c3d` for Larkana, which are the folds whose SAR
`z_seed` differed — each listed in `eval/chosen_params.json` under `optical_pass.fusion_union`);
mean slope from the DEM-derived `terrain:` note declared per event in `eval/events.yaml`.

**The quantity of flooding is approximately right; on the two steepest events it is in the wrong
place.** At Konjic the detector predicts 42.3 ha against a 42.6 ha reference — within a hectare —
and overlaps almost none of it. That is a **localisation failure at 20 m in a gorge, not a threshold that needs
retuning** — no parameter in the declared grid fixes it, and `hand_max_water_m` was offered 35 m by
the grid and the data declined it. A 20 m resolution cell in a floor only two or three pixels wide
straddles water, bank and wall, so its class reflects whichever scatterer dominated a mixed pixel.
That is what `reliability_narrow_valley.tif` now marks, and what
`run_report.json → reliability.narrow_valley` quantifies per run.

**Where that error actually sits** is taken apart per event in `docs/accuracy_diagnosis.md`
(`venv\Scripts\python.exe -m eval.diagnose_errors`), which attributes every missed reference pixel
to one cause and every false alarm to a terrain position. Two hypotheses are settled there and both
are negative: the gorge error is **not** a geometric misregistration (the ±5 px shift scan finds its
best shift within two pixels of zero, in inconsistent directions, for a small gain) and **not** an
edge-of-polygon disagreement (only a few percent of steep-event errors lie within 1 px of a correct
detection). The reservoir-shore explanation for the worst control is settled there too, also
negative. That document names the largest fixable source and the physics-based fix for it; nothing
has been implemented, and flood mapping stays frozen.

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
- **Debris is the weakest channel in the pipeline, and its measured precision is 0.0.** Brightening can also come from vegetation or structural change, hence "possible debris". The channel was tightened on 2026-10-11 after the old one covered 3.8% of an AOI at precision 0.0062. The class-count audit of 2026-10-12 then measured what survives: **296.8 ha across the whole dev set, 96% of it on a dry no-flood control** (7,111 of 7,420 pixels, from the optical deposit channel on a winter→spring pair, i.e. phenology and snowmelt). On `bosnia_konjic_2024`, the only dev event with an expert mass-movement reference (23.28 ha), the shipped configuration predicts 18 debris pixels of which **0 are correct** — precision 0.0, recall 0.0. The radar debris channel alone produces 107 pixels on one of ten events. Debris is inside the scored quantity (`FLOOD_AFFECTED_CLASSES`), so treat any debris hectare figure as unvalidated and say so; the flood-water channel carries the map.
- **Rough water — addressed, not solved.** The original `darkening` method assumed open water is dark; in a steep gorge a turbulent, sediment-laden flood is a rough scatterer and brightens instead, and measured on EMSR770 **0.0% of the expert flood extent passed the darkness gate**. The default is now `rough_water`, which accepts a coherent change of either sign on the valley floor and takes darkness only as the high-confidence test. That changed the numbers decisively (Punakha 0.025 → 0.118, Poza Honda 0.125 → 0.555 tolerant F1, and the binding Norwegian control from 4.9% to 0.14% false flood) and it is still far from the gate: the sign of the change was not the only problem.
- **Optical evidence is measured now, and it helps where help was least needed.** Fusing
  Sentinel-2 gains **+0.0098** mean tolerant F1 held out, but **+0.0548 of the +0.0587 summed
  gain is a single event** — Larkana, a flat floodplain with a 100% clear pair. In the gorges that
  cause the gate failure it is −0.0023, −0.0033, and exactly nothing where cloud refused it. It is
  adopted because the declared rule says a measured gain that costs no event and no control is
  kept, not because it fixes anything.
- **Optical is cloud-limited by nature, and that limit bites hardest where it would help most.**
  **Four of the ten dev events had no usable pair at all**, and three of those four missed the 20%
  floor by under a point (19.5%, 19.9%, 17.1%). In every one it is the *intersection* of two clear
  dates that fails — one date is clear, the other is not — which is a structural cost of using
  optical as a change detector, not bad luck. `min_clear_fraction` was set from the specification
  before any of this was measured and has deliberately **not** been lowered to admit them. Two
  further costs are structural. (1) Terrain shadow is excluded as unusable, and it is commonest in
  exactly the narrow gorges where radar is weakest, so optical coverage is lowest where it would
  help most — `optical.evidence.clear_fraction_pair` says how much of each area it actually saw.
  (2) The post-event window is 20 days, so standing water may have drained before the clear scene;
  optical *water* is therefore a lower bound at long latency and deposits are the stronger optical
  signal. On Bosnia the whole optical gain came from the deposit channel (538 water candidates
  against 19,013 deposit candidates).
- **Bridges.** Too narrow to confirm collapse at this resolution.

## Attribution

- Contains modified Copernicus Sentinel data 2026.
- Produced using Copernicus WorldDEM-30 © DLR e.V. 2010–2014 and © Airbus Defence and Space GmbH 2014–2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.
- © OpenStreetMap contributors.
- Reference maps used for checking: European Union, Copernicus Emergency Management Service data.
- Training data, if used: Kuro Siwo (Bountos et al., 2024); Sen1Floods11 (Bonafilia et al., CVPR Workshops 2020).
