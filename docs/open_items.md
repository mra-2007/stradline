# Open items: measured, deferred, and why

Things this project has **measured** and deliberately **not** acted on yet. Each entry says what
the evidence is, what the fix would be, roughly what it costs, and what has to happen before it
could be adopted. Nothing here is a plan to improve a score by tuning: every item is a change to a
*test*, which under Rule 3.4 needs its own declared decision and its own leave-one-event-out pass
before any row is published from it.

Sources are named per item. Accuracy numbers come from `eval/results/flood.csv`; the pixel-level
attributions come from `docs/accuracy_diagnosis.md` and its provenance file
`docs/accuracy_diagnosis.json` (leave-one-event-out run `20261012T165259Z`, measured 2026-10-13).
Where a number below is an **oracle** bound it is labelled as one: an oracle bound uses the
reference to decide which pixels to add or remove, so no detector can reach it. It bounds what is
worth chasing and is not an achievable score.

## Flood mapping is frozen

The flood gate reads **FAIL** (mean boundary-tolerant F1 0.5266 against 0.90; worst event 0.0798
against the 0.80 floor) and the no-flood control gate reads **PASS** (worst 0.0048 against the 1%
ceiling). Neither Fix A nor Fix B below has been implemented, so neither has changed any of that.

## Fix A - per-polarisation, noise-aware change test (DEFERRED)

**The measurement.** `rough_water` requires BOTH polarisations to move by at least
`DetectParams.min_abs_change_db` (3 dB), in the same direction, before a pixel counts as changed at
all. Pooled over the four dev events whose measured mean slope is at least 13 deg, that conjunction
accounts for **35.0% of all 6,413 missed reference pixels** (2,247 px). Of those, **1,267 px** are
pixels where one polarisation moved 3 dB or more and the other did not - VV falling short on 641 and
VH on 626, so this is not a "VH is noisy" story but one threshold being wrong for two channels.
Sign disagreement costs **2 px** and is therefore not the problem.

The larger bucket is *not* this one and is *not* fixable: 37.0% of steep-event misses have no
seed-strength z-score in either polarisation, and of those only 115 px carry any coherent change
large enough to grow, against 2,256 px with no qualifying change at all. No threshold recovers a
pixel whose backscatter did not move. That is a sensor-and-scale limit and is reported as one.

**Oracle ceiling: mean tolerant F1 0.6718**, up from the measured 0.5266, if this source alone were
removed perfectly on every gate flood event. Still far below the 0.90 gate, which is the main reason
it is deferred rather than treated as the answer.

**The fix.** Compare each polarisation's change against *that polarisation's own* uncertainty,
derived from the published Sentinel-1 IW GRDH noise-equivalent sigma-zero (about -22 dB) and from
the pre-event stack's own ENL-corrected variance (`DetectParams.enl`, reduced by the Lee filter);
require the change only in channels above their noise floor; keep the polarisation requirement as a
sign-consistency test where both channels are usable. Every quantity comes from the sensor
specification and the scene's own pre-event statistics, so it is one rule for every area and date,
not a value fitted to a gorge.

**Estimated effort: 6-10 h** - an estimate, not a measurement - covering the declared decision, the
detection change, a leave-one-event-out pass and the control check. **No new downloads:** it reads
the Sentinel-1 stacks already cached under `runs/eval/<event>/cache`.

**Before it could be adopted:** a declared decision recorded in `docs/tuning_log.md` *before* any
score exists; a leave-one-event-out pass; and both control criteria re-checked, including
`config.CONTROL_ANALYSED_AREA_CEILING`.

## Fix B - reach-relative HAND and flow-path continuity (DEFERRED)

**The measurement.** The drainage-connection test admits a whole connected component if any **one**
of its pixels touches a channel within `DetectParams.drainage_buffer_px` (3 px). That component-level
approximation costs in both directions: it lets field-sized patches into the controls, and it
rejects genuine flood that reaches the channel only outside the buffer, which is **13.3% of
steep-event misses** (855 px).

**The fix.** Both constraints come from terrain layers the pipeline already computes
(`flow_dir`, `accumulation`, HAND), and both apply everywhere. (a) Replace the single global HAND
ceiling with a *reach-relative* one: a candidate pixel must lie below the water surface implied by
the channel reach it actually drains to, rather than below a fixed 20 m above the nearest drainage
anywhere in the scene. (b) Replace component-level connection with per-pixel flow-path continuity
along the D8 directions. Water fills a valley from the channel upward and stands at a common surface
within a reach; wet soil and tilled ground do not.

**Estimated effort: 10-16 h** - an estimate, not a measurement. **No new downloads:** `flow_dir` and
`accumulation` are already in each event's cached `terrain_20m.npz`.

**Before it could be adopted:** as Fix A. It is expected to move the controls and the steep events in
opposite directions, so it must be judged on both at once, not on floods alone.

## Control false alarms: where they actually are (MEASURED)

Pooled over the four gate-plan controls, **52.1% of all 10,692 false-flood pixels are on flat
valley-floor ground away from the channel**, against 25.7% in the channel corridor and 19.5% on a
permanent-water shore. 6,184 of the pooled pixels brightened and 4,508 darkened. The position labels
are terrain-position proxies from the DEM (distance to permanent water, distance to the channel
network, HAND, slope), not land-cover classes - Sentinel-1 and a DEM cannot tell an irrigated field
from wet soil, and nothing here claims they can.

**The reservoir-shore hypothesis for the worst control is tested and FALSE.**
`ecuador_poza_honda_control_2024` carries 7,780 of those pixels (1.64% of its analysed ground against
the declared 1% ceiling, and 0.0048 of its full scoring domain, which is what the gate reads and
which PASSES). Only 237 px lie within 1 px of permanent water and 475 within 1-3 px; 2,868 px sit
10-30 px away and 2,528 px beyond 30 px - that is, most of them are 200 m or more from any water the
detector found. They are radar-only (optical refused that date for cloud), brightening-dominated, and
on low-HAND gentle-slope ground in a dry August. A coherent 3 dB brightening of flat farmed ground is
what soil-moisture change, irrigation and tillage look like to C-band, and `rough_water` calls it
water because inside the valley-floor zone it tests the magnitude of a change and not its sign.

This **closes the untested reservoir hypothesis** carried as open item B in `docs/tuning_log.md`
(2026-10-12). Fix B above is the change aimed at it.

## The damage gate FAILS: measured, with the root cause (2026-10-13)

**Read this first: none of the numbers below is a measurement of real building or road damage.**
Nothing was inspected. Both sides of every comparison are inferences from a flood polygon over the
same pre-event OpenStreetMap snapshot, so what is measured is whether OUR flood extent leads to the
same damage conclusions as an expert flood extent. A high score here would not license the claim
"we find damaged buildings with F1 0.9x", and the scores below are failing anyway.

`eval/results/damage.csv`, run `20261013T123135Z`, all six gate flood events scored. The per-event
evidence behind every count quoted in this section — flagged sets, OSM feature totals, the OSM
snapshot date and the parameters — is written to
`runs/eval/<event>/scores/damage_<params_hash>.json` by `eval/damage_eval.py`:

| gate | measured | target | status |
|---|---|---|---|
| `mean(building_f1)` | **0.1808** | ≥ 0.90 | FAIL |
| `mean(road_segment_f1)` | **0.3995** | ≥ 0.90 | FAIL |

Per event, against each fold's flood score from `eval/results/flood.csv` run `20261012T165259Z`:

| event | flood F1 tol | building F1 | road F1 | buildings in OSM | ours / ref flagged (buildings) |
|---|---|---|---|---|---|
| bhutan_punakha_2024 | 0.1356 | 0.0000 | 0.0000 | 45,554 | 7 / 1, no overlap |
| bosnia_konjic_2024 | 0.0798 | 0.0392 | 0.1613 | 17,718 | 9 / 42 |
| ecuador_poza_honda_2025 | 0.5553 | 0.3348 | 0.5268 | 76,814 | 63 / 158 |
| norway_ringebu_2023 | 0.9151 | 0.6424 | 0.7117 | 46,654 | 182 / 285 |
| norway_selsverket_2023 | 0.5845 | 0.0684 | 0.2915 | 22,815 | 16 / 101 |
| pakistan_larkana_2022 | 0.8891 | 0.0000 | 0.7054 | **65** | 0 / 9 |

**Root cause 1: the overlay inherits the flood extent, and the flood gate FAILS.** Road F1 tracks
flood tolerant F1 with a Spearman rank correlation of **0.886** across the six events — the overlay
is doing its job and faithfully transmitting the flood map's error. This is not a second, separate
failure to fix: *no overlay change can beat the extent it is given*, and the oracle ceiling on the
flood extent itself (Fix A above) is mean tolerant F1 0.6718, well under 0.90.

**Root cause 2: set-F1 on sparse targets AMPLIFIES a localisation error.** Building F1 (0.1808) is
far below flood F1 (0.5266) because a building is flagged only if the extent actually touches it:
water displaced by 100 m still earns partial pixel credit but flags a completely different set of
buildings. Bhutan is the clean demonstration — ours flags 7, the reference flags 1, and the
intersection is **zero**, so F1 is 0.0 even though both sets are tiny and nearby. Roads are less
affected because a long segment is easier to touch, which is why road F1 stays close to flood F1.

**Root cause 3, and independent of the flood map: OSM building completeness.** Larkana has **65**
buildings mapped in the pre-event OSM snapshot across its bbox plus the 15 km buffer, against
17,718–76,814 for every other event. Its flood map is one of the best in the set (tolerant F1
0.8891) and its road F1 is 0.7054, but building F1 is 0.0000 because there are almost no buildings
to find. This limit is in the data, not in our code, and it matters directly for the case study: a
Himalayan valley may be mapped as sparsely as rural Sindh was in 2022.

**Options, for the human to choose.** None is implemented.

1. **Fix the flood map first and re-score damage unchanged** (recommended). Root cause 1 dominates
   and roots 2 and 3 cannot be separated from it while the extent is wrong. Cost: Fix A + Fix B
   above (16–26 h estimated), then one re-run of `eval.damage_eval`, which needs no new OSM
   download — the snapshots are cached per bbox and snapshot date.
2. **Report OSM completeness beside every damage number, and keep the gate as it is.** Each row
   would carry the mapped-feature count so a reader can see when a 0.0 means "we were wrong" and
   when it means "there was nothing mapped". This is honest reporting and costs ~2 h, but it does
   **not** move the gate, and dropping an event because OSM is thin there would be Rule 3.2
   (excluding events) — so the completeness figure is published, never used to exclude.
3. **Ask the human to change what the gate measures** — e.g. a length-weighted road metric and an
   area-weighted building metric that give partial credit for a partly-correct extent, instead of
   set membership. This may be a better operational question, but `eval/gates.yaml` is human-owned
   and changing the metric after seeing the score is Rule 3.2. It can only be a human's declared
   decision, made before any re-score.

## The cut-off gate FAILS: measured, with the root cause (2026-10-13)

**Read this first: none of the numbers below is a measurement of whether anybody was really cut
off.** No road was driven and no village was visited. Both sides of every comparison are inferences
from a flood polygon over the same pre-event OpenStreetMap road network, so what is measured is
whether OUR flood extent leads to the same isolation conclusions as an expert flood extent.

`eval/results/cutoff.csv`, run **`20261013T165631Z`**, all six gate flood events, under the one
declared definition (`config.CUTOFF_GATE_*`: no vehicle route to the nearest health facility,
bridges intact). Per-event evidence in `runs/eval/<event>/scores/cutoff_<params_hash>.json`.

| gate | measured | target | status |
|---|---|---|---|
| `mean(settlement_f1)` | **0.3683** | >= 0.90 | FAIL |
| `min(settlement_f1)` | **0.0000** | >= 0.80 | FAIL |

| event | flood F1 tol | settlement F1 | cut off ours / ref | settlements | clinics in OSM |
|---|---|---|---|---|---|
| bhutan_punakha_2024 | 0.1356 | 0.0000 | 5 / 7, **no overlap** | 856 | 82 |
| bosnia_konjic_2024 | 0.0798 | 0.0000 | 8 / 3, **no overlap** | 424 | 7 |
| ecuador_poza_honda_2025 | 0.5553 | 0.0000 | **0** / 14 | 829 | 69 |
| norway_ringebu_2023 | 0.9151 | 0.9000 | 9 / 11 | 1102 | 8 |
| norway_selsverket_2023 | 0.5845 | 0.3621 | 22 / 94 | 623 | 1 |
| pakistan_larkana_2022 | 0.8891 | 0.9474 | 9 / 10 | 47 | 11 |

### A correction that moved the number UP, and why that is not self-serving

Run `20261013T152246Z` (superseded, and kept: `eval/results/` is append-only) published
`mean(settlement_f1) = 0.3134` using a metric that compared settlement **names**. The 2026-10-13
audit found that names are not unique and that `network.Settlements` even manufactures duplicates
by falling back to `"unnamed <place>"` for every untagged node — 15 place nodes share that one name
in the Larkana area, 33 names collapse at Poza Honda. The metric now compares **positions**, which
are exact because both sides share one `Settlements` object.

The auditor expected the corrected mean to **fall**. It rose, from 0.3134 to **0.3683**, and the
mechanism is worth stating plainly because the direction looks convenient: the name collapse shrank
**both** sides' sets, not only ours. At Larkana all nine of our cut-off settlements are named
`"unnamed hamlet"`, so the old name set held **one** entry against the reference's two → F1 0.6667;
by position it is 9 against 10 with nine true positives → **0.9474**. Ringebu moved 0.8421 → 0.9000
and Selsverket 0.3717 → 0.3621. The correction was forced by an argument that does not depend on its
direction — a metric that cannot tell two settlements apart is not measuring settlements — and the
gate FAILS either way.

### Root causes

**1. It inherits the flood extent, and the flood gate FAILS.** Settlement F1 tracks flood tolerant
F1 with a Spearman rank correlation of **0.8804** (n = 6, p = 0.021, `scipy.stats.spearmanr`).
Ringebu and Larkana, the two best flood maps, give the two best cut-off scores (0.9000, 0.9474); the
two gorge events, whose flood maps score 0.08 and 0.14, give exactly 0.0000. The analysis is
transmitting the flood map's error faithfully, and no routing change beats the extent it is given.
*(An earlier revision of this document said 0.93 and "tighter than the damage overlay's 0.886".
Both were wrong: 0.93 came from a hand-rolled rank correlation with non-standard tie handling, and
at 0.8804 the cut-off coupling is marginally LOOSER than the damage overlay's 0.8857.)*

**2. Set-F1 over a small cut-off set is high-variance, in both directions.** Only 0-22 settlements
are cut off out of 47-1102. At Punakha we name 5 and the reference names 7 with **zero** overlap — a
comparable count, a completely disjoint set, F1 0.0 — because a displaced extent severs *different*
roads and so isolates *different* villages. The same smallness is why Larkana's nine agreeing
settlements produce 0.9474: a handful decides the score either way.

**3. Severing a road is stricter than overlapping it.** Ecuador has a mid-range flood map (tolerant
F1 0.5553) and cuts off **nobody** against the reference's 14. To cut a road the extent must span it
*continuously*; a fragmented extent that overlaps the right pixels in the right general place can
still leave a dry path through every crossing. Pixel F1 does not measure that.

**A fourth thing, not a cause but a limit on what the question means:** OSM health-facility density
runs from **1** facility (Selsverket) to **82** (Punakha), so "route to the nearest clinic" is a
different question in each area. At Selsverket most settlements have no route to the single clinic
even with no flood at all and are correctly reported `no route before the flood` rather than cut
off; the per-event `status_counts` in each evidence file carry the exact split. Settlements the
graph could attach nowhere are a smaller effect but not negligible: 43/856, 20/424, 91/829, 1/1102,
8/623 and **10/47** — **21% at worst**, at Larkana, the event with the thinnest OSM.

### Options, for the human to choose. None is implemented.

1. **Fix the flood map first and re-score unchanged** (recommended). Root cause 1 dominates at
   Spearman 0.88. Cost: Fix A + Fix B above (16-26 h estimated) then one re-run, which needs no new
   OSM download — the snapshots are cached per bbox and snapshot date.
2. **Ask the human to change what the cut-off gate measures.** Set-F1 over sets of fewer than ten is
   high-variance in both directions, and root cause 3 says pixel accuracy is the wrong predictor of
   it. A count-based or population-weighted metric, or one scored over *settlement-destination
   pairs*, would be more stable. `eval/gates.yaml` is human-owned and changing a metric after seeing the
   score is Rule 3.2, so this can only be a human's declared decision made before any re-score.
3. **Make the extent connectivity-aware before routing** — close gaps narrower than a declared width
   across a road, so a fragmented extent severs what it physically would. This addresses root cause 3
   directly and would help the damage overlay too, but it is a detection change: declared decision,
   leave-one-event-out pass and control check first, exactly as for Fix A and Fix B.

## Audit leftovers, 2026-10-13 (`/audit` on `c6497c8` and `f2f2d3e`, verdict FAIL)

Nine of the eleven findings were fixed in the follow-up commit. These are what remains.

- **A. `CLAUDE.md` was modified inside `c6497c8` without being mentioned in its commit message**
  (auditor finding 9, LOW). `git show c6497c8 -- CLAUDE.md` adds 23 lines, a "Working protocol"
  section. `CLAUDE.md` is human-owned and hook-protected, so the text was the human's and was swept
  in by a broad `git add -A` on my side. **Cannot be fixed now without rewriting published history,
  which is worse than the defect.** The auditor's point stands and is the reason it is recorded
  here: *"I could not verify from inside the repo who authored that text — which is exactly the
  problem: the audit trail no longer distinguishes a rule the human wrote from one an agent
  wrote."* For the record: I did not write it, and I cannot edit that file (the hook blocks it).
  **To avoid repeating it:** stage explicitly rather than with `git add -A` when `CLAUDE.md`,
  `eval/gates.yaml`, `tests/test_rules.py` or `.claude/` show as modified, or name them in the
  message.

- **B. The published damage rows carry `git_commit = c6497c8-dirty`.** The producing tree was not a
  commit, so the exact code that made those six rows is not addressable by a hash. `eval/results/`
  is append-only (Rule 3.5), so the rows stay as they are; a future re-score from a clean tree will
  record a clean commit. The numbers themselves reproduce exactly — the auditor re-ran the scorer
  independently and got all six to the digit, and so did the evidence-file regeneration.

- **C. Three claims the auditor could not verify from inside the repo**, recorded so they are not
  mistaken for checked facts: (1) the measurement that ohsome answers 403 on `/elements/geometry`
  and 200 on `/elementsFullHistory/geometry` *from this network* — it is a live-network observation,
  reproducible only by making the calls, and the cached files' clipped `@validFrom`/`@validTo` are
  consistent with the full-history path having been used; (2) the authorship of the `CLAUDE.md` text
  in item A; (3) whether anything under `runs/case_*` contains a comparative output — the guard hook
  blocks listing that path, so neither the auditor nor I inspected it.

- **D. The damage gate's subset exposure is only half-closed.** `eval/damage_eval.write_rows` now
  refuses to append rows for anything narrower than the full gate plan (auditor finding 8), which
  closes it from this side. But `damage_overlay` in `eval/gates.yaml` still has `min_rows: 3` and no
  `where:` clause, so the gate itself would still average a three-row run_id if one ever reached the
  file by another route. Raising `min_rows` to 6 or adding a `where:` clause is the **human's** call
  — `eval/gates.yaml` is human-owned.

## Audit leftovers, 2026-10-13 (second round, `/audit` on `5b20a6e`, verdict FAIL)

Ten of the twelve findings were fixed in the follow-up commit. These remain.

- **E. `eval/results/cutoff.csv` records `git_commit = a6e6443-dirty` and `5b20a6e-dirty`**
  (auditor finding 9, LOW). Neither tree that produced a published cut-off row is addressable by a
  commit. This is the same defect as leftover B above, now in a second results file. `eval/results/`
  is append-only so the rows stay; the record is the point. **To avoid repeating it:** commit before
  scoring, so the row names a clean tree.

- **F. Two values with accuracy consequences are now in config, but one was removed rather than
  moved** (auditor finding 7, partially fixed). `CUTOFF_TARGETS` moved to `config.py`. The edge
  sampling step did not: `_sample_pixels` no longer samples at all — it rasterises the supercover
  with `all_touched=True`, so there is no density knob left to declare. That is the better outcome
  (nothing to get wrong) but it is a removal, not a relocation, and it is recorded here so the audit
  trail does not read as if a tunable were simply dropped.

- **G. The two `settlement_f1` runs in `cutoff.csv` use different metric definitions.**
  `20261013T152246Z` compared names, `20261013T165631Z` compares positions. `check_gates` reads only
  the latest `run_id`, so the gate is computed from the corrected rows — but anyone reading the file
  directly sees two incompatible runs with no column saying so. The CSV schema is declared in the
  human-owned gates file and has no "metric version" column; adding one is the human's call.
  Until then, the superseded run is documented here and in the section above.

- **H. Three things the auditor could not check**, recorded so they are not mistaken for verified:
  (1) whether the `NetworkParams` values (1.5x detour, 500 m link, 150 m / 5 buildings / 500 m merge)
  were ever adjusted after seeing a score — `cutoff.csv` contains no exploratory run and `config.py`
  declares them from settlement geography, but an unpublished run would leave no trace, and the
  auditor could only say it found no evidence of in-sample tuning; (2) the ohsome endpoint
  measurement from the previous round; (3) `runs/case_smoke/`, which it deliberately did not open
  because that is the held-out area — so finding 3's bias could not be quantified on real data.

## Closed

- **Shipped `n_pre` did not match the scored `n_pre`** (closed 2026-10-13, this commit). A default
  run used 5 pre-event images while every scored row used 3, so a default invocation produced a
  configuration that had never been measured and whose `params_hash` matched no published row.
  `strandline/config.py::N_PRE` is now the single declaration and `DEV_N_PRE` an alias of it;
  `tests/test_shipped_defaults.py` asserts a default run reproduces the evaluated hash. No scored
  value changed - no published hash is produced from a default `RunParams()`, because every hash
  path in `eval/tune.py` already passed `n_pre=DEV_N_PRE` explicitly. The *cost* of having settled
  on 3 rather than 5 is unmeasured: no dev-event score exists at 5, and getting one means a fresh
  download of the dev set plus a leave-one-event-out pass. `docs/accuracy_diagnosis.md` still
  reports the mismatch, correctly, as what was true when it was measured; it is a measurement record
  and is not retro-edited.

- **The copilot's measured score is the OFFLINE path only** (open, 2026-10-14). All 453 rows of
  `eval/results/copilot.csv` run `20261014T022950Z` were produced after the Gemini free tier
  returned HTTP 429: the adapter backed off and fell back, so every row's `source` is
  `guardrail`/`keyword`/`offline`. The LLM planner was exercised live on a handful of questions
  before the quota ran out; the LLM writer has unit-test coverage with a fake model only. Run
  `venv\Scripts\python.exe -m eval.copilot_eval --llm` when quota allows and compare the
  `by wording source` split; the gate's claim today covers the fallback path, which is also the
  path a judge gets with no network.
- **The copilot's Nepali number-word check is narrower than the English one** (open, 2026-10-14).
  `copilot/verifier.py` flags a quantity spelled in words, but `एक` ("one") and `छ` ("six") are also
  an ordinary pronoun and the verb "is", so they are deliberately not on the Nepali list: flagging
  them would reject correct prose. A Nepali answer that spelled one of those two words as a number
  would not be caught. Digits in both scripts are covered, and the bundled templates write every
  number as a digit, so this is reachable only through a model-written template.
- **The copilot's question set is ours** (open, 2026-10-14). 1.0000 over 453 questions is a score
  against questions written in `eval/copilot_eval.py` and `eval/copilot_questions.csv`, not against
  questions a judge will ask. The expected answers are independent of the copilot
  (`eval/copilot_expected.py` does not import it), but the *coverage* is not independent: a question
  shape nobody thought of is not measured. The honest claim is "every question we thought to ask",
  not "every question".
- **A settlement named with fewer than four characters needs its capital letter** (open,
  2026-10-14). `copilot/planner.py::MIN_FOLDED_NAME`: short OSM place names ("To", "As") collide
  with ordinary words, so they are matched case-sensitively. A question that writes such a name in
  lower case is answered as a general question instead, which is a miss rather than a wrong answer.
- **The pipeline produces no flooded river length** (open, 2026-10-14). The dashboard's first
  sentence therefore reports flood extent in hectares and the high/medium confidence split, not the
  length of river reached, which Track B asks for in words. Adding it means a length along the
  drainage network in `strandline/`, not arithmetic in `web/`.
- **`routes.geojson` holds attachment links, not path geometries** (open, 2026-10-14).
  `strandline/network.py` writes the link from each settlement to the road node it routes from, so
  the dashboard cannot draw a before-and-after route. It draws the link with the pipeline's own
  note and prints the before and after lengths from the settlement's properties instead.
- **The dashboard's basemap needs the network** (open, 2026-10-14). Terrain, hillshade and every
  data layer come from the local API, but the OpenStreetMap raster tiles do not. With no network
  the map still works and reads correctly; it just has no road grain under the terrain.
- **`pakistan_larkana_2022` likely overlaps a Kuro Siwo training event** (open, 2026-10-15).
  The Kuro Siwo catalogue (github.com/Orion-AI-Lab/KuroSiwo) lists actid 1111009 as
  `act_region: Pakistan, aoi_name: Larkana, ref_date: 20220910T080000` - one of the ten training
  actids given for `models/strandline_kurosiwo_unet.pt`. Our dev event is the same place and the
  same 2022 monsoon flood (event_date 2022-08-26, post S1 2022-08-30). No bbox is published in the
  catalogue to confirm exact AOI overlap, but the place/country/year match is strong enough that
  this event should not be trusted to measure the Kuro Siwo model's (`mode=model`/`fusion`)
  out-of-sample generalisation; the other 9 dev events checked clean (no EMSR number or place match
  against actids 277, 279, 321, 324, 411, 417, 421, 427, 1111010). Before running
  `eval/tune.py`'s model pass, exclude `pakistan_larkana_2022` from the training/held-out set for
  the `model`/`fusion` modes, or hold it out permanently from that comparison.
