# Strandline

**Where the flood reached, what it broke, who it stranded.**

Strandline takes any area and flood date and answers three questions for rescue teams from free
satellite data: where the flood hit, what was damaged, and which settlements are cut off. Built for
the IIT Mandi Multimodal AI Hackathon 2026, Track B "Mapping Flood Damage from Space". An
educational prototype, not an operational tool.

A *strandline* is the line of debris a flood leaves at its highest reach. Strandline maps that line
from space, and the villages it stranded.

## Setup (Windows, cmd)

Prerequisites: Python 3.11, Node.js 18+ with npm (only needed to build or develop the dashboard in
`web/`), and a free account at https://dataspace.copernicus.eu (for the pipeline's own Copernicus
openEO login, not the dashboard).

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
| `damaged_buildings.geojson` | OSM building footprints the mapped water reached (footprint intersects the extent buffered by one pixel) |
| `cut_roads.geojson` | road segments, split at junctions, with a part inside the buffered extent; each carries `segment_length_m`, `flooded_length_m` and `flooded_fraction` |
| `bridges_out.geojson` | bridge ways crossing a waterway whose nearby channel is flooded, each labelled *"likely out, unverified"* |
| `settlements.geojson` | every settlement with its status, its cut-off probability, and the route length to the nearest clinic and nearest town before and after, on both graphs and under both bridge scenarios |
| `routes.geojson` | the link from each settlement to the road node it routes from, per graph |
| `run_report.json` | provenance: scenes, track, dates, every parameter, areas, runtime, attribution, `reliability.narrow_valley`, the `optical` block (Sentinel-2 status, fusion mode, whether it was fused into the map, dates, cloud cover, clear fraction, how much of the area optical covered) the `damage` block (status, the OSM snapshot date and buffer, feature counts per layer, damaged/cut/bridge counts and lengths) and the `cutoff` block (status, the declared cut-off definition, settlement and graph counts, status counts per graph/destination/scenario, and the ensemble behind every probability) |
| `cache\` | downloaded rasters and OSM layers, reused on every rerun |

The damage and cut-off layers are written **even when empty**, because a missing file cannot
distinguish "nothing was damaged" from "the overlay did not run". If OpenStreetMap cannot be read,
the flood map is still produced and `run_report.json → damage.status` / `cutoff.status` is `refused`
or `failed` with the reason — never a damage count of zero and never "nobody is cut off".

| Code | Meaning |
|---|---|
| 0 | no data |
| 1 | no significant change |
| 2 | flood water, high confidence |
| 3 | flood water, medium confidence |
| 4 | possible debris or deposit |
| 5 | permanent water |
| 6 | not analysed (steep or high terrain) |

**Flood extent vs what is merely mapped.** `ExtentParams.debris_in_flood_extent` (default `False`
since 2026-10-12, adopted by the Pareto-dominance rule in `eval/chosen_params.json`)
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

## Method (OpenStreetMap and the damage overlay)

**The snapshot is the whole point.** After a flood, mappers trace the damage — buildings get flood
tags, washed-out bridges get deleted, humanitarian campaigns redraw whole valleys. A "current" OSM
download of a flood area therefore contains somebody else's damage assessment, and using it as a
pipeline input would be reading the answer. So `strandline/osm.py` never asks for today's OSM. It
asks for **the latest date strictly before the event date, capped at `OsmParams.latest_snapshot`
(2026-07-27)**, which is what the ohsome instance's own `/metadata` temporal extent ends at. That
date is cached under, and written into `run_report.json → damage.osm.snapshot`, so every count can
be traced to the OSM it came from. Rule 1.

Layers (`config.OSM_LAYERS`, as ohsome filter expressions): buildings, roads with their `highway`
type, bridges, waterways, places, and health facilities under both tagging schemes
(`amenity` and `healthcare`, including `health_post`). The area queried is the flood bbox plus
`OsmParams.buffer_km` = 15 km, because a settlement can be cut off by a bridge outside the mapped
box and a route out of a valley leaves it by definition.

**Which endpoint.** ohsome's snapshot extraction `/elements/geometry` is the natural fit, but
measured on 2026-10-13 from this network it answers **403** at the WAF while `/metadata`, the
aggregation endpoints and `/elementsFullHistory/geometry` all answer 200. Both paths are therefore
tried in order and the one that answered is recorded in the report. The full-history response is
reduced to the snapshot instant here — per OSM id the version with the latest `@validFrom`, kept
only where `@validTo` reaches the window end — so an element edited inside the window is not
double-counted and one deleted inside it is not resurrected.

**The overlay** (`strandline/damage.py`), against the pipeline's own flood extent:

| Rule | Definition |
|---|---|
| building damaged | footprint **intersects** the extent buffered by `DamageParams.buffer_px` = 1 px (20 m). Intersection, not centroid-inside: a 20 m cell clipping a footprint corner is water reaching the building. |
| road segment cut | roads are split at every junction first, so a segment is a stretch that can be routed around rather than an OSM editing unit; cut when a **part of it** is inside the buffered extent (intersection length > 0), not when it merely touches the boundary. `flooded_length_m` is reported per segment, because "240 m of this 8 km road is under water" and "this road is cut" are different facts. |
| bridge likely out, unverified | a bridge way crossing a waterway whose channel, **within `DamageParams.bridge_reach_m` = 100 m of the crossing**, lies inside the extent. The reach matters: a river in flood ten kilometres downstream says nothing about this crossing. |

The 20 m buffer is a **geolocation tolerance**, set from Sentinel-1 orthorectification residual over
terrain and from the positional accuracy of OSM geometry — the same 1 px the gates file declares as
its boundary tolerance. It is not a sensitivity dial and is never tuned on a score. Neither
`OsmParams` nor `DamageParams` enters `params_hash`, for the same reason as `ReliabilityParams`:
nothing in them changes a pixel of the flood class map, so folding them in would re-key every row
already published. `DamageParams` is recorded under `run_report.json → damage.parameters` and
`OsmParams` under `damage.osm.parameters` — including `latest_snapshot`, so the Rule 1 cap that was
actually in force is auditable from the report alone.

Debris is consumed **only if it is part of the flood extent**: the overlay takes its classes from
`ExtentParams.flood_extent_classes()`, which with the shipped default excludes class 4.

## Method (who is cut off)

`strandline/network.py` builds **two graphs** from the pre-event OSM snapshot — what a vehicle can
drive (`config.VEHICLE_HIGHWAY_TYPES`, which deliberately excludes `track`) and what a person can
walk (that plus paths, tracks, footways and steps) — with roads split at junctions first, so an edge
is a stretch that can be routed around rather than an OSM editing unit. Edges the flood reached are
removed, using the same extent and the same 1 px buffer as the damage overlay.

**Settlements** are OSM place nodes (`city`, `town`, `village`, `hamlet`) **plus DBSCAN clusters of
buildings** for the hamlets OSM has not named — at least 5 buildings within 150 m of each other,
with clusters inside 500 m of an existing place node dropped as the same place. OSM place nodes are
authoritative but routinely missing for small mountain settlements, and a cluster of roofs is a
reasonable guess that people live there; neither alone is enough. DBSCAN is implemented on a
KD-tree in-repo rather than adding scikit-learn as a dependency.

Each settlement is routed to its **nearest health facility** and its **nearest town**, before and
after the flood, on both graphs, under two scenarios (**bridges intact** and **bridges failed** —
where "failed" means only the bridges the damage overlay flagged *likely out*, not every bridge).

| Status | Meaning |
|---|---|
| `reachable` | a post-flood route exists and is not much longer |
| `detour` | the post-flood route is more than `detour_factor` (1.5x) the pre-flood one |
| `cut off` | no post-flood route, where one existed before |
| `no route before the flood` | **not a flood impact.** No mapped road reaches this settlement, or none reaches a destination even with no flood. The flood did not isolate it; we cannot say anything about it. |

That last row is the one that matters most: collapsing it into `cut off` would manufacture a flood
impact out of a mapping gap. Every run also reports how many settlements it could not attach to any
graph at all, because attachment is to the nearest graph *node* and a settlement beside the middle of
a long road can fall outside the 500 m link distance.

**The gated definition**, declared once in `config.CUTOFF_GATE_*` so it cannot drift between the
pipeline, the evaluation and a report: a settlement is **cut off** when it has **no vehicle route to
its nearest health facility with bridges intact**. Every other combination is still computed and
written to `settlements.geojson`.

**The probability** is 200 perturbed runs at a fixed seed: the cached change-score threshold moved
by ±0.5 z (one `std_floor_db` of z-score), the road buffer drawn from 0/1/2 px, and every
low-confidence narrow-valley pixel counted as flooded on a coin flip. Each member is **anchored on
the extent the detector actually produced** and moved only where the change score justifies it, and
**member 0 is the unperturbed run**, so the ensemble demonstrably contains the real answer rather
than containing it with probability zero (`rng.uniform` is continuous — a 2026-10-13 audit finding).
A *stricter* threshold is compared against `z_grow + delta`, not `z_seed + delta`, because hysteresis
growth is what admitted the weakest pixels in the extent; comparing against the seed deleted every
grown pixel at once for any positive delta, which was a one-sided erosion of ~1.5–2.0 z rather than
the ±0.5 z the parameter claims, and biased every probability low.

> **The probability is a SENSITIVITY measure, not a calibrated chance.** It is the share of the
> ensemble in which a settlement comes out cut off. Nothing here has been validated against observed
> cut-off outcomes, so 0.8 means "this answer survives most perturbations", not "80% chance of being
> isolated". Every output repeats this.

Neither `NetworkParams` nor `MonteCarloParams` enters `params_hash`, for the `ReliabilityParams`
reason: nothing in them changes a pixel of the flood class map. Both are recorded under
`run_report.json → cutoff.parameters`. No downloads: the analysis reads arrays and cached OSM only.

## The situation-report copilot (`copilot/`)

Ask a finished run a question, in English or Nepali, and get an answer in which **every number came
out of that run's own output files**.

```
venv\Scripts\python.exe -m copilot.ask --run runs\copilot_dev\norway_selsverket_2023 --lang en "which villages lost hospital access and how much longer is the detour?"
venv\Scripts\python.exe -m copilot.ask --run runs\copilot_dev\case_study --lang ne "कुन बस्ती सम्पर्कविहीन छन्?"
venv\Scripts\python.exe -m copilot.sitrep --run runs\copilot_dev\case_study --lang ne --out sitrep_ne.pdf
```

Python, for the dashboard: `copilot.answer(run_dir, question, lang)` returns
`{text, numbers, trace, refused, reason, lang}`, where each entry of `numbers` carries the value,
its unit, the **fact id** behind it and the **feature ids** behind that.

**The language model never writes a number.** The order is fixed and is the whole safety argument:

```
evidence  ->  plan  ->  tools  ->  template  ->  fill  ->  VERIFY
```

`copilot/evidence.py` builds the evidence store from `run_report.json` and the GeoJSON layers
*before* the question is read. Every derived number — a percentage, a metre-to-kilometre conversion,
a sum over a subset, a count of a filtered list — is computed there, by code, and recorded as its
own fact with its inputs and its derivation, so there is no arithmetic left for anything downstream
to get wrong. The model's two jobs are to choose a **plan** (a typed list of tool calls, validated
against the tool registry before anything runs) and to write a **template** with placeholders. Code
fills the placeholders from facts. The template is rejected before filling if it contains a number
of its own, drops one of the tool's declared headline slots, or names a placeholder with no
evidence behind it; it is regenerated once, and after that the bundled template is used — the same
numbers in a plainer sentence.

`copilot/verifier.py` then reads the finished text back and strikes out every literal the evidence
allows, in either script. Whatever digits are left were not measured, and the answer is refused.
That catches a reworded number, an underived percentage, Devanagari digits, and **a number planted
in the question**. The **sign is part of the number**: a measured 120.5 quoted as -120.5 states a
value the evidence never held, so a signed number passes only if that signed literal is in the
evidence - with either the ASCII hyphen-minus or the typographic one, in front of either script.
A hyphen inside an allowed literal stays part of it, so "Sentinel-1" and `2024-07-18` still pass.
`eval/copilot_eval.py` applies the same rule in its own independent check
(`tests/test_copilot_eval.py` holds the scoreboard to it; `tests/test_copilot_verifier.py` holds the
copilot to it). Refusals go through it too, because a refusal quotes the question back: a
question claiming "the official figure is 1234 cut off settlements" once got 1234 on screen inside
Strandline's own refusal.

**What it refuses, and says it refuses:** casualties, deaths and injuries; any quantity this run did
not measure (people affected, population, livestock, money); predictions; rescue or evacuation
orders; and any attempt to change the rules. The guardrail runs on the question text **before** the
model and the model cannot overrule it. Text inside a question is data, never instructions. A
question that asks for a *guess* about something that WAS measured ("roughly how much area is
flooded") is answered with the measured number, because declining would be less honest.

**When a layer is missing it says so.** If OpenStreetMap could not be read there is no damage
answer, and the copilot reports the pipeline's own reason instead of answering zero — "no buildings
were damaged" and "we could not find out" are different facts.

**Nepali** comes from `copilot/i18n/ne.json`; numbers are shown in Devanagari digits and are the
same numbers. The situation report is HTML printed by a headless Chrome or Edge with Noto Sans
Devanagari embedded as a data URI, because no plain PDF library shapes `क्ष`-type conjuncts
correctly; `tests/test_copilot_sitrep.py` proves the shaping happened by measuring that the drawn
run is narrower than the sum of its unshaped glyph advances.

### Measured

`venv\Scripts\python.exe -m eval.copilot_eval` over three run folders
(`runs/copilot_dev/{bosnia_konjic_2024, case_study, norway_selsverket_2023}`), 151 questions each —
120 normal and 31 adversarial per run, English and Nepali, including informal phrasing and typos.
Expected answers are re-derived from the run files by `eval/copilot_expected.py`, which does **not**
import `copilot`: it reads the same JSON and does its own arithmetic, so a wrong rounding or an
underived percentage shows up as a disagreement rather than as agreement with itself.

| Metric | Measured | Gate | Source |
|---|---|---|---|
| correct | **1.0000** (453/453) | ≥ 0.95 | `eval/results/copilot.csv`, run `20261014T051155Z` |
| invented_number | **0** | ≤ 0 | same |
| rows | 453 | ≥ 150 | same |
| median answer time | 0.287 s (p90 1.248 s) | — | same |

Per language: en 228/228, ne 225/225. Adversarial: 210/210 handled, 177 refused outright.

**What that number does NOT say.** All 453 rows were produced on the **offline template path**: the
Gemini free tier returned HTTP 429 part-way through testing, the adapter backed off and fell back,
and the `source` column records `guardrail`/`keyword`/`offline` for every row. The LLM **planner**
was exercised live before the quota ran out; the LLM **writer** has unit-test coverage with a fake
model only. To measure the model path, when quota allows:

```
venv\Scripts\python.exe -m eval.copilot_eval --llm
```

Responses are cached by prompt hash, so a second run of that is free and byte-identical.

## HTTP API

Run the local FastAPI server:

```cmd
venv\Scripts\python.exe -m uvicorn api.main:app --port 8000
```

Interactive OpenAPI documentation is available at `http://localhost:8000/docs`. Complete request/response schemas are documented in `api/API_CONTRACT.md`. Example URLs:

- Health check: `http://localhost:8000/health`
- Track preflight: `http://localhost:8000/preflight?bbox=85.10,27.85,85.45,28.30&date=2026-08-26`
- Case study status: `http://localhost:8000/runs/case_smoke/status`
- Every run the server knows: `http://localhost:8000/runs`
- Terrain tile (terrarium, from the run's own Copernicus DEM): `http://localhost:8000/runs/case_smoke/terrain/11/1509/857.png`
- Radar quicklook: `http://localhost:8000/runs/case_smoke/quicklook`

## The dashboard (`web/`)

A full-bleed 3D map of one run, with three things on top of it: a mission strip (area, date,
"Check satellite passes", "Map this flood", the pipeline as a numbered sequence with live
progress), three sentences that answer Track B's three questions, and the copilot panel. The
design is specified in `web/DESIGN.md` and the code is held to it.

Build it, then open the API, which serves the built files at `/`:

```cmd
cd web
npm install
npm run build
cd ..
venv\Scripts\python.exe -m uvicorn api.main:app --port 8000
```

Then open `http://localhost:8000`.

For front-end development, run the API and the Vite dev server side by side in two terminals —
`npm run dev` (port 5173) proxies `/health`, `/preflight` and `/runs` to the API on 8000, so source
edits appear without a rebuild:

```cmd
:: terminal 1
venv\Scripts\python.exe -m uvicorn api.main:app --port 8000

:: terminal 2
cd web
npm install
npm run dev
```

Then open `http://localhost:5173`.

What it draws, and from where:

| On screen | Source |
| --- | --- |
| 3D terrain, hillshade | `GET /runs/{id}/terrain/{z}/{x}/{y}.png` — terrarium tiles cut from that run's cached Copernicus DEM, no network at request time |
| Basemap | OpenStreetMap raster tiles, desaturated and dimmed to the palette |
| Flood, strandline, debris, low confidence | `GET /runs/{id}/layers/{flood,low_confidence}` |
| Damaged buildings, cut roads, bridges out | `GET /runs/{id}/layers/{damaged_buildings,cut_roads,bridges_out}` |
| Settlements, route links | `GET /runs/{id}/layers/{settlements,routes}` |
| The three sentences | `run_report.json` only, plus `cut_off_probability` read verbatim off the settlement that carries the highest one |
| Copilot answers and traces | `POST /runs/{id}/ask` |
| Situation report | `GET /runs/{id}/sitrep?lang=en|ne` |
| Evidence dialog | `GET /runs/{id}/quicklook` |

Honesty rules the dashboard keeps (Rules 1, 3, 6):

- it computes no new numbers; every figure is from `run_report.json` or a layer file, and each
  sentence names its source;
- a layer the run did not produce greys its legend row and says "not produced by this run" — it is
  never drawn as zero;
- "possible debris, unverified", "bridges likely out, unverified" and the pipeline's own sentence
  about cut-off probability being a sensitivity measure are printed on screen;
- the footer carries `strandline.outputs.ATTRIBUTION` verbatim plus "Educational prototype — not
  for operational use", at every screen size.

Screenshots for a design review (the API must be running):

```cmd
venv\Scripts\python.exe web/shots.py
```

Measured: `npm run build` passes; `venv\Scripts\python.exe -m pytest -q` is 785 passed, which
includes 18 tests for the terrain tiles, the quicklook, the run listing and the job-store pruning
in `tests/test_api_terrain.py`. No accuracy gate measures the dashboard.

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
| `eval/cutoff_eval.py` | Runs the cut-off analysis TWICE per gate flood event over the same pre-event OSM road network and the same settlements and graphs — once with our flood extent, once with the reference extent — and compares the two cut-off sets. Appends `settlement_f1` to `eval/results/cutoff.csv`, and publishes per event how many settlements, kilometres of road and health facilities OSM actually has. **Thin mapping is a limitation, never a reason to drop an event** (Rule 3.2). Not a measurement of real isolation: nobody was visited. |
| `eval/damage_eval.py` | Runs the damage overlay TWICE per gate flood event over the same pre-event OSM snapshot - once with our flood extent, once with the reference extent (same `reference_classes` as flood scoring) - and compares the two flagged sets as sets of OSM features. Appends `building_f1` and `road_segment_f1` to `eval/results/damage.csv`. **It is not a measurement of real building damage:** nothing was inspected, both sides are inferences from a flood polygon, and the result inherits the flood map's accuracy. |
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
| `damage_overlay` mean building F1 | 0.1808 | ≥ 0.90 | **FAIL** |
| `damage_overlay` mean road segment F1 | 0.3995 | ≥ 0.90 | **FAIL** |
| `cutoff_analysis` mean settlement F1 | 0.3683 | ≥ 0.90 | **FAIL** |
| `cutoff_analysis` worst single event | 0.0000 | ≥ 0.80 | **FAIL** |

Damage rows: `eval/results/damage.csv`, run `20261013T123135Z`, all six gate flood events, with the
per-event evidence behind each row in `runs/eval/<event>/scores/damage_<params_hash>.json`.

**These two are not a measurement of real building damage.** Nothing was inspected; both sides of
the comparison are inferences from a flood polygon over the same OpenStreetMap snapshot, so the
numbers say whether our flood extent leads to the same damage conclusions as an expert extent —
not that we find damaged buildings at that rate.

**The damage overlay inherits the flood extent and the flood gate FAILS, so this gate could not
pass:** road F1 tracks flood tolerant F1 with a Spearman rank correlation of 0.886 (n = 6 events,
no significance claimed). The extent the overlay inherits is itself bounded at mean tolerant F1
0.6718 (the flood oracle in `docs/open_items.md`); **no bound on the damage metric itself has been
measured**, and set-F1 is not a monotone function of pixel F1, so that figure bounds the input and
not this gate. Building F1 is lower still because set membership amplifies a localisation error —
at Punakha our 7 flagged buildings and the reference's 1 do not intersect at all. A third limit is
in the data, not the code: Larkana has **65** buildings mapped in the pre-event OSM snapshot over
its bbox plus buffer, against 17,718–76,814 elsewhere, so its building F1 is 0.0 despite one of the
better flood maps in the set. Full per-event evidence, the three root causes and three options are
in `docs/open_items.md`.

Cut-off rows: `eval/results/cutoff.csv`, run `20261013T165631Z`, all six gate flood events, with
per-event evidence in `runs/eval/<event>/scores/cutoff_<params_hash>.json`. **Like the damage
numbers, these are not a measurement of whether anybody was really cut off** — nobody was visited,
and both sides are inferences from a flood polygon over the same OSM road network.

An earlier run (`20261013T152246Z`, superseded and kept, since `eval/results/` is append-only)
published 0.3134 using a metric that compared settlement **names**. Names are not unique — 15 OSM
place nodes share `"unnamed hamlet"` in the Larkana area — so the 2026-10-13 audit required the
comparison to be by settlement **position**. That correction moved the mean **up**, to 0.3683,
because the name collapse had been shrinking *both* sides' sets; `docs/open_items.md` sets out why
the correction was forced by an argument independent of its direction.

**The cut-off analysis inherits the flood extent:** settlement F1 tracks flood tolerant F1 at
Spearman **0.8804** (n = 6, p = 0.021). Ringebu and Larkana, the two best flood maps, give the two
best cut-off scores (0.9000 and 0.9474); the two gorge events score exactly 0.0000. Two further
causes are in `docs/open_items.md`: set-F1 over cut-off sets of 0–22 settlements out of 47–1102 is
high-variance in both directions (at Punakha we name 5 and the reference names 7 with **zero**
overlap), and **severing a road is stricter than overlapping it** — Ecuador has a mid-range flood map
and cuts off nobody against the reference's 14, because a fragmented extent can leave a dry path
through every crossing.

**Module status: `flood_mapping` FAIL, `no_flood_controls` PASS, `damage_overlay` FAIL,
`cutoff_analysis` FAIL.** By the definition of done in
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
