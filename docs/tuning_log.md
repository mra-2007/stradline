# Tuning log

Every change to a value in `strandline/config.py`, and every observation that
might later justify one, is recorded here.

Rules this log exists to keep honest (project Rule 3):

- Parameter values come from `eval/tune.py` with leave-one-event-out, or from
  physics or literature chosen without looking at scores. Never hand-tuned to
  one event.
- No entry here may be keyed to a bbox, date, place or event name.
- An **observation** records what was seen and changes nothing. A **change**
  records a new value and must cite the dev-event rows that justify it.

---

## 2026-10-11 — Observation: open-water darkness assumption fails in steep terrain

**Status:** observation only. No parameter changed. No reference data used.

**Where it came from:** a smoke run of the pipeline on a steep Himalayan
corridor (`runs/case_smoke`), done to check that the ingest path works
end-to-end. Allowed under Rule 4: the pipeline was run, nothing was compared
against any reference map. No held-out reference was opened, downloaded or
looked for.

### What was measured

From the cached Sentinel-1 stack and the Copernicus DEM only:

| Quantity | Value |
|---|---|
| Otsu threshold on the valley-floor zone | **-8.02 dB** |
| Reported water threshold after clamping | -15.00 dB (`water_db_max`) |
| Post-event VV inside the valley-floor zone | p1 -17.6, p5 -13.5, median -9.0 dB |
| Valley-floor pixels darker than -15 dB | 2.9% |
| Post-event VV on the DEM-derived channel | median **-8.2 dB** |
| Pre-event mean VV on the same channel | median **-8.3 dB** |
| Strongly-darkened valley-floor pixels (zc <= -3) | 8 785 |
| ...of those, passing the <= -15 dB darkness gate | **9.5%** |
| Analysed fraction of the scene | 4.5% (median slope 30.5 deg, median HAND 519 m) |

### What it means

The valley-floor backscatter histogram is **unimodal land**. There is no water
mode for Otsu to find, so Otsu returns a land-like value and the clamp to
`water_db_max` becomes the operative threshold. The adaptive step contributes
nothing in this terrain.

The physical cause is that a steep, turbulent mountain river is not specular:
its backscatter (-8.2 dB) is indistinguishable from the surrounding land.
The `detect.py` method assumes open water is dark, which holds on the floodplain
dev events and does not hold here. The darkness gate therefore discards about
90% of the valley-floor change signal, and the "permanent water" class resolves
into 190 fragments of median size 2 px rather than a river course.

This is a method limitation, not a bug, and **no threshold value fixes it** --
the two distributions overlap.

### What was changed

Only reporting. `otsu_water_threshold` now returns the raw Otsu value, which
bound clamped, and the pixel count it used; all three reach `run_report.json`
(commit `b3fb7d3`). Previously the report showed `"water_threshold_db": -15.0`
alone, which reads as a measured value when it was a parameter ceiling.
Classification output is byte-identical: 0 differing pixels.

### What was ruled out

Checked and found correct, so none of these explain the above:

- **Downloads.** Six same-track acquisitions, 2 bands (VV, VH), linear, on one
  identical grid. Ranges normal (VV median ~ -8.4 dB, VH ~ -14.9 dB).
- **DEM.** Full coverage, 454-5848 m, plausible for the terrain.
- **Geolocation.** Real VV correlated against SAR brightness simulated from the
  DEM: peak at **dy 0, dx +1 px** whole-scene, and dx = 0 or +1 in every
  elevation band from 400 m to 6000 m (two independent implementations).
  Elevation dependence **0.4 px per 1000 m**, against **61.7 px per 1000 m**
  predicted if the product were ellipsoid-geocoded without terrain correction.
  The product is terrain-corrected; there is no shift to remove.

### Open questions for the scoreboard, not decided here

Recorded so the options are not re-derived later. None implemented.

1. **Drop the darkness gate for the medium-confidence class**, keeping
   darkening + HAND + slope + drainage connection, and keep darkness only for
   high confidence. Must be scored against the no-flood controls, since the
   gate is what holds the false-flood rate down.
2. **`gamma0-terrain` instead of `sigma0-ellipsoid`.** Real VV correlates with
   DEM-simulated terrain brightness at **r = 0.75**, so backscatter here is
   dominated by geometry rather than surface. Radiometric terrain flattening
   should make the valley-floor histogram more separable. The coefficient is
   now a supported config choice with the default **unchanged**; it moves only
   if the dev-event scoreboard proves it better.
3. **Lean on the debris/brightening channel**, which is better matched to a
   debris-laden mountain flood than open-water darkening.

Caveat on (1)-(3): `bhutan_thimphu_punakha_2024` is currently the only dev
event with comparable terrain, so it alone would carry the generalisation
argument. A second steep-terrain dev event would strengthen any decision taken
on this evidence.

---

## 2026-10-11 — Change: backscatter coefficient is a supported choice (default unchanged)

**Status:** plumbing only. **No parameter value changed.**

`RunParams.backscatter_coefficient` now accepts either `sigma0-ellipsoid`
(unchanged default) or `gamma0-terrain`, validated on construction and
selectable with `--backscatter-coefficient`. Anything else is refused with a
clear message before any network call.

The coefficient is part of every Sentinel-1 cache filename
(`s1_<coefficient>_<track>_<day>_<res>m.tif`). The two coefficients are
different measurements of the same scene, so a pre/post stack that mixed them
would show change that is an artefact of processing rather than of the flood.
Separate names make that mixture impossible rather than merely unlikely.

Existing `runs/case_smoke` cache files were renamed into the new scheme. Their
`run_report.json` records `sigma0-ellipsoid`, so the new names describe the
bytes already on disk; nothing was re-downloaded and no pixel changed.

The default moves only on leave-one-event-out scoreboard evidence, recorded
here as a new entry.

---

## 2026-10-11 — Observation: on an independent mountain reference, flood water BRIGHTENED

**Status:** observation only. No parameter changed. No value chosen from a score.

**Where it came from:** the first real scoreboard run of `eval/run_scoreboard.py`,
on `bosnia_konjic_2024` (Neretva gorge, Copernicus EMS EMSR770 AOI01, reference
SPOT-6/7 optical at +3.4 d, INDEPENDENT of our Sentinel-1 input). Scored from
the cache with default `config.py` parameters, `n_pre=3`, `--no-write`, so no
row reached `eval/results/`. Numbers below are from
`runs/eval/bosnia_konjic_2024/scores/3674bd8ba80a.json`. One event is not a
gate and these are not gate numbers.

### What was measured

Reference `water` class = `event_type` "5-Flood", 3 polygons, 19.3 ha inside an
31 823 ha domain (0.06% positives). Domain fraction 1.00 - nothing was excluded
from scoring.

| Quantity | Value |
|---|---|
| Flood precision / recall / F1 (strict and tolerant) | **0.00 / 0.00 / 0.00** |
| Predicted flood area | 1.52 ha against 19.3 ha of reference |
| Combined z-score under the reference flood | median **+3.30**, p10 +0.60 |
| ...of those pixels, passing the darkening seed (z <= -3.0) | **3.3%** |
| Post-event VV under the reference flood | median **-8.0 dB**, p10 -9.2 dB |
| ...passing the darkness gate (<= -15 dB `water_db_max`) | **0.0%** |
| Reference flood pixels inside the water terrain zone | **94.2%** |
| Reference flood classified as `possible debris / deposit` | **79.0%** |
| Reference flood classified as `no significant change` | 20.7% |
| Debris class vs the reference mass-movement class | precision **0.0062**, recall 0.3213 |
| Debris class extent | 1 197 ha, i.e. 3.8% of the whole AOI |

### What it means

**The terrain mask is not the problem.** 94.2% of the reference flood sits
inside the HAND <= 20 m, slope <= 15 deg water zone, so the flood was reachable.
Widening the zone to HAND <= 35 m / slope <= 25 deg would reach 99.8% and cost
little, but it would not change the result: the pixels are already analysed.

**The darkening assumption is the problem, and it fails in the same direction as
the case-study observation above.** The reference flood *brightened* (median
combined z **+3.30**) and its post-event VV is **-8.0 dB**, nowhere near the
-15 dB open-water gate: 0.0% of reference pixels pass it. No value of
`water_db_max` in the declared grid (-18, -15, -12 dB) and no value of `z_seed`
can recover this flood, because the sign of the change is wrong, not its
magnitude. A turbulent, sediment-laden flood in a gorge is a rough scatterer,
and CEMS still maps it as flood trace.

**The brightening channel is not a free fix.** It covers 79% of the reference
flood, but it also covers 3.8% of the entire AOI and scores precision 0.0062
against the reference mass-movement class it is supposed to find. Its overlap
with the flood is mostly coincidence of extent. Scoring flood as
water OR debris would give recall ~0.79 at precision ~0.013 - F1 about 0.03.

So on this event both channels fail: one cannot fire, the other fires almost
everywhere. This is a method limitation, consistent with the 2026-10-11
case-study observation, now confirmed against an INDEPENDENT expert reference.

### What was NOT done, and why

No parameter was changed and no axis was added to `TUNE_GRID` on the strength of
this number. Rule 3.4: values come from leave-one-event-out or from physics, and
a grid widened because one event scored badly is in-sample tuning wearing a
search's clothes. The decision below is the human's.

### Options, with their costs

1. **Add a rough-water channel to `detect.py`**: inside the water terrain zone,
   accept a strong change of EITHER sign when the drainage-connection and HAND
   tests pass, and keep darkness only for the high-confidence class. Justified
   by surface-roughness physics rather than by a score, but it is a method
   change and the no-flood controls must prove it does not raise the false-flood
   rate - that is exactly what the controls in the plan are for.
2. **Tighten the debris channel first.** 3.8% of an AOI is not a debris map;
   precision 0.0062 against the mass-movement reference says the brightening
   seed is far too permissive. Worth fixing on its own merits whatever happens
   to the flood class.
3. **Widen the water terrain zone** to HAND <= 35 m / slope <= 25 deg. Cheap,
   physically defensible (flood water on a gorge floor is not confined to 20 m
   above the channel), reaches 99.8% of this reference, and changes nothing
   about the sign problem. Do it as part of (1), not instead of it.

Recommendation: (2) then (1), with the terrain-zone widening of (3) folded in,
and all three scored by `eval/tune.py` over the whole dev set rather than this
one event.

---

## 2026-10-11 — Change: elevation model set explicitly; gamma0-terrain removed

**Status:** two processing changes, both forced by measured backend behaviour.
No detection parameter was tuned.

**Where it came from:** the first full dev-set download, which finished 38 of
90 files and failed 52. Two distinct causes, both diagnosed from openEO itself.

### Cause 1: SRTM has no data above 60 deg N

Every Norwegian job (Selsverket and Ringebu, both ~61.5 deg N) failed. The batch
job logs show the failure inside
`openeogeotrellis/collections/s1backscatter_orfeo.py::_get_dem_dir_context`,
with `elevation_model=None` in the `sar_backscatter` arguments and a missing
SRTMGL1 tile N61E009. The backend default resolves to SRTM, which stops at
60 deg N.

Note this bit **sigma0-ellipsoid** jobs, not only terrain-flattened ones:
orthorectification needs a DEM whatever the radiometric reference area is.

**Changed:** `ingest.download_s1` now passes `elevation_model` to
`sar_backscatter` explicitly, and `RunParams.elevation_model` declares it
(`COPERNICUS_30`, the only supported value, global, and the same DEM the terrain
stage already uses - so geocoding and HAND now rest on one elevation source
instead of two).

**Consequence for the cache, deliberately:** the elevation model is now part of
every Sentinel-1 cache filename and of `params_hash`. It changes where pixels
land, so a stack mixing SRTM- and COPERNICUS_30-geocoded dates would show change
that came from the processing. The 28 Sentinel-1 files already downloaded under
the old names are therefore **stale and unreadable by name** and must be
fetched again; the 10 DEM files are unaffected (`download_dem` never called
`sar_backscatter`). Cached SCORES are invalidated too, because `params_hash`
changed - which is the behaviour we want.

### Cause 2: this backend cannot do gamma0-terrain at all

Every gamma0-terrain job failed, and none of them appears in the account's job
list, because they were refused before a job was created. The backend's own
process description settles it:

    conn.describe_process("sar_backscatter")
    coefficient: {"enum": ["sigma0-ellipsoid"], "type": "string"}

and a request is refused client-side from that schema:

    OpenEoClientException: Invalid `sar_backscatter` coefficient
    'gamma0-terrain'. Should be one of ['sigma0-ellipsoid']

**Changed:** `BACKSCATTER_COEFFICIENTS` is now `("sigma0-ellipsoid",)`.
`TUNE_GRID` keeps the `run.backscatter_coefficient` axis, which now has one
value, so the grid drops from 18 points to 9; putting the name back in
`BACKSCATTER_COEFFICIENTS` re-enables the search with no other edit if the
backend ever advertises it.

**What this costs us, stated plainly.** The open question from the
2026-10-11 observation above - whether radiometric terrain flattening separates
the valley-floor histogram that sigma0 leaves unimodal - **cannot be answered on
this backend**. It is not that gamma0-terrain scored worse; it is unavailable,
so there is no measurement either way. Terrain flattening remains a real
candidate explanation for the steep-terrain failure mode and would need a
different processing route (for example local RTC from the GRD with the
Copernicus DEM) to test. That is a build, not a parameter, and it is not
attempted here.

### What this does to the Konjic observation above

The EMSR770 numbers in the previous entry were measured on SRTM-geocoded
Sentinel-1 files, since Konjic at 43.7 deg N is inside SRTM coverage and those
downloads succeeded. They must be **re-measured** once the stack is refetched
under COPERNICUS_30. The finding is radiometric (the flood brightened: combined
z median +3.30, 0.0% of reference pixels below -15 dB) and a geocoding change of
a pixel or two is unlikely to reverse it, but "unlikely" is not "measured", and
the earlier numbers should not be quoted after the refetch.

---

## 2026-10-11 — Change (DECLARED BEFORE SCORING): rough-water detection mode

**Status:** method change, approved by the human after the two observations
above. This entry is written **before any score of the new mode exists** and
before the dev-set download finished. Everything decided here - the grid, the
parameter values, the scoring definition - is fixed by this entry. Nothing in
it comes from a Konjic number; where a value could have been read off the
Konjic reachability table, it is instead put in the grid for
leave-one-event-out to choose. Order of work, deliberately: declare, implement,
test on synthetic data, download, then score.

### What is being changed, and why

Two measured facts motivate it, both recorded above: in a steep gorge the flood
water **brightened** (combined z +3.30, 0.0% below the -15 dB darkness gate), so
the darkening channel cannot fire; and the existing debris channel fires on
**3.8% of a whole AOI** at precision 0.0062, so it is no use as a substitute.

Physical reading: specular dark water is a floodplain phenomenon. A confined,
turbulent, sediment- and debris-laden flood in a gorge is a **rough** surface at
C-band, and can scatter more than the dry valley floor it replaced. The sign of
the backscatter change is therefore not a reliable signature of flooding in this
terrain; the **magnitude** of a coherent change low on the valley floor is.

### The new mode

`DetectParams.mode` selects between:

- `darkening` - **today's algorithm, unchanged.** It stays the default and stays
  available, so the comparison is against the real baseline rather than against
  a rewritten one.
- `rough_water` - three changes together:
  1. **Rough-water channel.** On the valley floor, a *significant change of
     either sign* is flood water. HAND, slope, minimum mapping unit and the
     drainage-connection test all still apply, so the change must be low, flat
     and connected to the channel network to count.
  2. **Tightened debris.** Every change, of either channel, must now also exceed
     an absolute dB threshold and move **both** polarisations in the **same
     direction**. The old debris grow step asked only for z >= 1.5, which with
     `std_floor_db` 0.5 is a 0.75 dB change - at the radiometric noise level of
     Sentinel-1 GRD, which is why it fired almost everywhere.
  3. **Terrain widening** folded in, as a grid axis rather than an assertion
     (see below).
  Debris and water are separated by **terrain position**, not by radiometry:
  inside the valley-floor zone a strong change is water; above it, within the
  debris zone, strong *brightening* is debris. Water lies low and near the
  channel; debris fans sit higher and steeper. Darkness is kept only as the
  **high-confidence** criterion for water, where it still means specular water.

### The grid, declared now (30 points)

Per mode, so no point varies a parameter its mode ignores:

| Axis | Values | Where the values come from |
|---|---|---|
| `run.backscatter_coefficient` | sigma0-ellipsoid | the only one this backend offers |
| `detect.z_seed` | 2.5, 3.0, 3.5 | 2.5-3.5 sigma, the range in the multi-temporal change literature this detector follows (DeVries et al. 2020) |
| `detect.hand_max_water_m` | 20.0, 35.0 | **both modes.** 20 m is the current value; HAND-based floodplain delineation uses 5-30 m (Rennó et al. 2008; Nobre et al. 2011), and a confined gorge flood adds flow depth plus superelevation on bends, so 35 m is the physical upper end. Put in the grid rather than set, because the Konjic reachability table would otherwise be choosing it. |
| `detect.water_db_max` | -18, -15, -12 dB | **darkening mode only.** Unchanged from the previous grid. |
| `detect.min_abs_change_db` | 2.0, 3.0 dB | **rough_water mode only.** Sentinel-1 GRD radiometric stability is of order 0.5 dB, so a physically meaningful surface change must clear a few dB; 3 dB (a factor of 2 in power) is the long-standing convention in SAR change detection. |

`detect.slope_max_water_deg` is **not** widened and **not** in the grid: a water
surface is flat, and a 20 m DEM slope above 15 deg on a valley floor is far more
likely a wall pixel than a flooded one. Keeping it fixed also keeps the two
modes comparable on it.

Grid sizes: `darkening` 1x3x2x3 = **18**, `rough_water` 1x3x2x2 = **12**.
`hand_max_water_m` is in both grids on purpose, so the scoreboard can say
whether any gain came from the rough-water channel or merely from the terrain
widening.

**Stated caveat on the comparison:** 18 candidates against 12 is not a like-for-
like contest. A mode with more candidates has more chances to fit the training
events, so a narrow win on the training mean would mean little. Only the
held-out (leave-one-event-out) numbers decide, and the margin is reported
per event.

### The scoring definition, decided now

Track B asks for flooded **and debris-covered** areas, so the primary quantity
becomes **flood-affected area = water ∪ rough-water ∪ debris** - in class terms
`CLASS_FLOOD_HIGH ∪ CLASS_FLOOD_MEDIUM ∪ CLASS_DEBRIS`. It is scored against
each reference's **flooded + debris/mass-movement** classes, taken from that
event's own `reference_classes` mapping in events.yaml. These are the numbers
that go in the gate columns of `eval/results/flood.csv`. Controls'
`false_flood_fraction` is computed on the same union, so the 1% ceiling now
constrains the debris channel too.

Water-only precision, recall, F1 and IoU (our flood classes against the
reference `water` class alone) continue to be computed and are recorded in each
event's score file, next to the union numbers.

**This change makes the task harder on five of the six independent events, not
easier.** Only Konjic declares a reference debris class, so for the other five
the reference union is identical to the reference water class while our
prediction gains the debris class - i.e. more opportunity for false positives
against the same truth. That asymmetry is the honest direction and is the reason
it is safe to decide this before seeing a score.

### How the decision will be taken

`eval/tune.py` runs leave-one-event-out over both modes' grids on all six
independent flood events, with the four controls as a hard feasibility
constraint. The new mode is adopted **only** if it beats `darkening` on the
held-out events and every control stays at or under 1% false flood. If it wins
on the training mean but not on held-out events, it is rejected and that is
recorded here. If no mode is feasible, nothing is adopted and the gate stays
failing with the evidence attached.

---

## 2026-10-11 — Observation: first real A/B of the two modes, 3 of 10 events

**Status:** observation. NOT a decision, NOT a gate number, and nothing was
tuned on it. The grid and the scoring definition were declared and **committed
(d114107) before these numbers existed**, which is what makes it safe to look
at them: they cannot feed back into the grid.

**What it is:** the only three events whose COPERNICUS_30 stacks have finished
downloading, scored at the **config.py default parameters** in both modes -
not the grid, not leave-one-event-out. Primary metric as declared above:
flood-affected area (our flood classes ∪ debris) against the reference's
flooded + debris/mass-movement classes. From the score files under
`runs/eval/<event>/scores/`.

| Event | Mode | P | R | F1 | P tol | R tol | **F1 tol** | pred ha | ref ha |
|---|---|---|---|---|---|---|---|---|---|
| bosnia_konjic_2024 | darkening | 0.019 | 0.527 | 0.036 | 0.035 | 0.759 | **0.067** | 1202.0 | 42.6 |
| bosnia_konjic_2024 | rough_water | 0.036 | 0.028 | 0.032 | 0.065 | 0.075 | **0.069** | 33.5 | 42.6 |
| bhutan_punakha_2024 | darkening | 0.008 | 0.195 | 0.015 | 0.013 | 0.323 | **0.025** | 769.8 | 31.3 |
| bhutan_punakha_2024 | rough_water | 0.071 | 0.081 | 0.076 | 0.111 | 0.126 | **0.118** | 35.3 | 31.3 |
| ecuador_poza_honda_2025 | darkening | 0.055 | 0.325 | 0.094 | 0.073 | 0.455 | **0.125** | 1163.6 | 197.6 |
| ecuador_poza_honda_2025 | rough_water | 0.439 | 0.390 | 0.413 | 0.588 | 0.534 | **0.559** | 175.8 | 197.6 |

Water-only (our flood classes vs the reference `water` class alone), for the
same runs: Konjic 0.00 / 0.023 (darkening / rough_water F1), Punakha 0.134 /
0.081, Poza Honda 0.377 / 0.413.

### What can and cannot be read from this

**Can:** the debris tightening works as intended. Predicted flood-affected area
falls from 1202 to 34 ha at Konjic, 770 to 35 ha at Punakha and 1164 to 176 ha
at Poza Honda, against references of 43, 31 and 198 ha. The old channel's
"recall" of 0.53 at Konjic was an artefact of covering 1202 ha of a 31 823 ha
AOI at precision 0.019; it was never detection.

**Can:** `rough_water` is ahead on tolerant F1 on all three, decisively on two
(4.6x at Punakha, 4.5x at Poza Honda) and within noise at Konjic.

**Cannot:** that these are the numbers the gate will see. Default parameters,
three events, no leave-one-event-out, and the decision rule declared above
requires all six independent events plus the four controls.

### The honest headline, before anyone asks

**Every one of these is far below the 0.90 gate.** The best figure on the board
is 0.56 tolerant F1 on the flattest of the three events. At Konjic the new mode
predicts 33.5 ha against a 42.6 ha reference yet overlaps almost none of it, so
the rough-water channel is not finding that flood either - it has stopped
over-predicting without starting to be right. Whether the grid can close that
is exactly what the tuner is for, and if it cannot, the gate fails and the
report says so with these rows attached. No parameter will be hand-moved toward
these numbers.

---

## 2026-10-11 — DECISION: rough_water adopted. Flood gate FAILS, honestly.

**Status:** the leave-one-event-out run declared above has been executed on all
6 independent flood events and 4 controls, 301 scores over 30 grid points.
Run id in `eval/chosen_params.json`; rows in `eval/results/flood.csv`
(`split=loeo` for floods, `split=full` for controls).

### Mode decision, by the rule fixed before scoring

**`rough_water` is adopted.** It met both conditions:

1. **It wins on held-out events.** Tolerant F1, leave-one-event-out, against
   `darkening` at the config.py defaults: Punakha 0.118 vs 0.025, Konjic 0.069
   vs 0.067, Poza Honda 0.555 vs 0.125, Ringebu 0.915 vs 0.780, Selsverket
   0.588 vs 0.295. It loses on one, Larkana 0.834 vs 0.843 - the flat irrigated
   plain, the one event where specular dark water is the correct physics.
2. **Every control is under the ceiling**, max 0.0048.

**`darkening` could not be used at all: 0 of its 18 grid points were feasible.**
Every one was rejected by a control, and the binding one is
`norway_ringebu_control_2023` at **0.0490 false flood** - 4.9% of a dry
Norwegian valley called flood-affected, against a 1% ceiling, and its best
configuration still gives 0.045. `rough_water` brings that control to 0.0014.
So the old mode fails the no-flood gate outright, which the earlier
single-event look had not revealed.

Chosen parameters (`mode=rough_water`, `z_seed=3.0`, `hand_max_water_m=20.0`,
`min_abs_change_db=3.0`, `sigma0-ellipsoid`); 2 points tied, grid order decided.
`hand_max_water_m` **stayed at 20.0**: the terrain widening to 35 m, searched in
both modes, was not chosen. So the gain came from the rough-water channel and
the debris tightening, not from widening the zone. Diff to apply by hand:
`detect.mode darkening -> rough_water` (and `run.n_pre 5 -> 3`, which is the
dev-scoring cost setting, not a tuning result - do not apply that one).

### The gate: measured against target

| Gate | Measured | Target | Status |
|---|---|---|---|
| mean precision (tolerant) | **0.5177** | >= 0.90 | FAIL |
| mean recall (tolerant) | **0.5105** | >= 0.90 | FAIL |
| mean F1 (tolerant) | **0.5134** | >= 0.90 | FAIL |
| worst-event F1 (tolerant) | **0.0694** | >= 0.80 | FAIL |
| mean F1 (strict) | 0.4071 | report | - |
| mean IoU (strict) | 0.2937 | report | - |
| max control false flood | **0.0048** | <= 0.01 | **PASS** |

Per-event held-out tolerant F1: Konjic 0.069, Punakha 0.118, Poza Honda 0.555,
Selsverket 0.588, Larkana 0.834, Ringebu 0.915.

### Root cause, with evidence

F1 tracks **how much of the AOI the reference actually calls flood**, not the
terrain directly. Rank correlation between held-out tolerant F1 and the
reference's positive fraction is **0.886**:

| Event | F1 tol | ref ha | positives | predicted ha | mean slope |
|---|---|---|---|---|---|
| bosnia_konjic_2024 | 0.069 | 42.6 | 0.134% | 33.5 | 21.8 deg |
| bhutan_punakha_2024 | 0.118 | 31.3 | 0.023% | 35.3 | 24.4 deg |
| ecuador_poza_honda_2025 | 0.555 | 197.6 | 0.306% | 178.6 | 13.1 deg |
| norway_selsverket_2023 | 0.588 | 125.1 | 1.015% | 120.3 | 16.3 deg |
| pakistan_larkana_2022 | 0.834 | 50187.7 | 50.795% | 38836.6 | 0.3 deg |
| norway_ringebu_2023 | 0.915 | 863.4 | 2.015% | 785.2 | 9.9 deg |

The decisive observation: **the predicted AREA is now close to right on every
event** (33.5 vs 42.6 ha, 35.3 vs 31.3, 178.6 vs 197.6, 120.3 vs 125.1, 785 vs
863). The quantity of flooding is approximately correct; on the two steep
events it is in the **wrong place**. That is a localisation failure at 20 m in a
gorge, not a thresholding failure - and it is why no parameter in this grid
fixes it. Positive fraction and slope are confounded in this dev set (the
steepest events also have the smallest references), so this evidence cannot
separate "steep terrain" from "tiny target"; both point at the same two events.

### Options, costs stated

1. **Accept and report honestly.** 0.51 mean tolerant F1 over six independent
   mountain references, 0.92 on the best, with a measured false-flood rate of
   0.5%. The 90% gate is not met and the report says so. Cost: no gate passes.
   This is the current state and is what the README now says.
2. **Attack localisation, not thresholds.** The predicted area is right and
   misplaced, so the candidates are geometric: terrain-corrected geocoding of
   the GRD (the backend's sigma0-ellipsoid is not RTC and gamma0-terrain is
   unavailable - see 2026-10-11 above), finer resolution than 20 m in narrow
   valleys, or a flow-path constraint that pushes detections onto the channel
   the DEM says water must follow. Real work, and the only option aimed at the
   measured cause.
3. **Re-scope the claim to where it is measured.** The method reaches 0.83-0.92
   tolerant F1 where the flood covers >= 2% of the AOI and mean slope is
   <= 10 deg, and 0.07-0.12 where the flood covers < 0.2% at >= 20 deg. Stating
   that split is more useful to a rescuer than one averaged number. Does not
   change the gate, and must not be presented as passing it.
4. **Widen the dev set** so positive fraction and slope stop being confounded:
   the five flood_dependent events are already downloadable and would add
   references at intermediate positive fractions. Scores on them cannot move a
   gate (Sentinel-1-derived references), but they can tell us which of the two
   explanations is doing the work.

**Not done, and not to be done:** no threshold was moved toward these numbers
after seeing them, no event was dropped, and the scoring domain was not
narrowed. `hand_max_water_m` was offered 35 m by the grid and the data declined
it.

---

## 2026-10-11 — FINALISED: parameters applied, layer added, SAR flood mapping closed as FAIL

**Status:** the decision above is now in the code. Nothing was tuned in this
step: no score was computed, no parameter was searched, no event was rescored.
This entry records what was applied, what was added, and the final numbers as
measured.

### What was applied to `strandline/config.py`

From `eval/chosen_params.json` (`run_id=20261011T110254Z`,
`params_hash=d4f9b1791c82`):

| Key | Was | Now | Applied? |
|---|---|---|---|
| `detect.mode` | `darkening` | `rough_water` | **yes** |
| `detect.z_seed` | 3.0 | 3.0 | already the value |
| `detect.hand_max_water_m` | 20.0 | 20.0 | already the value (35 m declined by the data) |
| `detect.min_abs_change_db` | 3.0 | 3.0 | already the value |
| `run.backscatter_coefficient` | `sigma0-ellipsoid` | `sigma0-ellipsoid` | already the value |
| `run.n_pre` | 5 | 5 | **no — deliberately not applied** |

`run.n_pre` stays at 5. The tuner ran at 3 because that is `DEV_N_PRE`, the
dev-set download-cost setting (`config.py`), not a tuning result. Applying it
would have shipped a thinner reference stack to live runs to match an artefact
of how cheaply the dev set was scored. The consequence, stated once more: every
measured number in this log and in the README describes a 3-image reference
stack, which gives a noisier per-pixel std than the shipped 5, so `std_floor_db`
does more of the work there than it will live. The direction of that bias is not
known and is not claimed.

So exactly **one** parameter moved, and it is the one the mode comparison was
about.

Two test files moved with the default, and neither assertion was weakened:

- `tests/test_detect_modes.py` pinned the default to `darkening` with the
  rationale "so a comparison is against the real baseline". That comparison is
  finished, so the test now pins the default to the mode the tuner chose.
- `tests/test_detect.py` is the `darkening`-mode synthetic regression suite (dark
  water graded as water, the brightening fan graded as debris) and now names
  that mode explicitly instead of inheriting the default. Under `rough_water`
  the same synthetic scene is still **fully** detected — recall 1.00, fn = 0 —
  but terrain position rather than radiometry assigns the class, so the 771 px
  debris fan on the valley floor comes out as water. `test_detect_modes.py`
  covers that over both modes and asserts the fan survives inside the union.

### What was added: the narrow-valley reliability layer

New: `strandline/reliability.py`, `config.ReliabilityParams`, the `RELIABILITY_*`
codes, `reliability_narrow_valley.tif`, `reliability_narrow_valley.geojson` and
`run_report.json → reliability.narrow_valley`.

It exists because of the root cause measured above and **changes no detection** —
the class map is byte-identical with and without it, and
`tests/test_reliability.py` asserts the class array is not even mutated. It is
computed from the Copernicus DEM alone: no backscatter, no reference map.

Definition: valley floor by the detector's **own** HAND and slope limits (not a
second definition, or it would qualify the wrong pixels), then local floor
thickness by morphological opening with a disc of radius
`min_valley_width_px / 2`, implemented on two Euclidean distance transforms so
the radius may be fractional. Floor pixels the disc cannot reach are the narrow
ones.

`min_valley_width_px = 3.0` is set from imaging geometry, not from any score: at
the shipped 20 m grid it is 60 m, and a cell in a floor that narrow mixes water,
bank and wall, so its class is a statement about which scatterer dominated. It
is configurable and lives in `config.py` (Rule 3.7).

`ReliabilityParams` is deliberately **outside** `params_dict` and therefore
outside `params_hash`. It cannot change an output pixel, so including it would
give a new hash to an unchanged detection and make every row already in
`eval/results/` look like a different configuration. It is written to
`run_report.json` under its own key instead, and a test pins that.

The reported share is of **flood-affected** area (`FLOOD_AFFECTED_CLASSES`), the
primary scored quantity, so the caveat qualifies the same number the gates and
the sitrep report. With nothing detected the share is `None`, not `0.0`: a 0.0
would read as "none of it is uncertain".

The exact sentence lives in `config.NARROW_VALLEY_CAVEAT` so the dashboard, the
copilot and the Nepali sitrep cannot drift apart:
**"low confidence: valley narrower than radar resolution"**.

### The final table, strict and tolerant

Every number is a row in `eval/results/flood.csv`
(`run_id=20261011T110254Z`, `params_hash=d4f9b1791c82`, `split=loeo`), scored at
`DEV_N_PRE=3`. Strict = zero tolerance, tolerant = 1 px (20 m).

| Event | P | R | **F1** | IoU | P tol | R tol | **F1 tol** |
|---|---|---|---|---|---|---|---|
| bosnia_konjic_2024 | 0.0358 | 0.0282 | **0.0315** | 0.0160 | 0.0645 | 0.0752 | **0.0694** |
| bhutan_punakha_2024 | 0.0714 | 0.0805 | **0.0757** | 0.0393 | 0.1111 | 0.1264 | **0.1183** |
| ecuador_poza_honda_2025 | 0.4321 | 0.3904 | **0.4102** | 0.2580 | 0.5789 | 0.5335 | **0.5553** |
| norway_selsverket_2023 | 0.4860 | 0.4674 | **0.4765** | 0.3128 | 0.5618 | 0.6164 | **0.5878** |
| pakistan_larkana_2022 | 0.8215 | 0.6357 | **0.7168** | 0.5586 | 0.8662 | 0.8047 | **0.8343** |
| norway_ringebu_2023 | 0.7685 | 0.6988 | **0.7320** | 0.5772 | 0.9234 | 0.9070 | **0.9151** |
| **mean** | **0.4359** | **0.3835** | **0.4071** | **0.2937** | **0.5177** | **0.5105** | **0.5134** |

Controls (`split=full`), false flood fraction: `bhutan_punakha_control_2024`
0.0000, `bosnia_konjic_control_2024` 0.0000, `norway_ringebu_control_2023`
0.0014, `ecuador_poza_honda_control_2024` 0.0048.

The strict column is the honest one for a judge who does not grant a pixel of
slack: **mean strict F1 0.4071, mean strict IoU 0.2937.** The tolerant column is
what the gate is defined on.

### Gate status, verbatim from `python -m eval.check_gates`

| Module | Gate | Value | Target | Status |
|---|---|---|---|---|
| flood_mapping | mean(precision_tolerant) | 0.5177 | >= 0.9 | **FAIL** |
| flood_mapping | mean(recall_tolerant) | 0.5105 | >= 0.9 | **FAIL** |
| flood_mapping | mean(f1_tolerant) | 0.5134 | >= 0.9 | **FAIL** |
| flood_mapping | min(f1_tolerant) | 0.0694 | >= 0.8 | **FAIL** |
| flood_mapping | mean(f1) | 0.4071 | report | REPORT |
| flood_mapping | mean(iou) | 0.2937 | report | REPORT |
| no_flood_controls | max(false_flood_fraction) | 0.0048 | <= 0.01 | **PASS** |

6 flood rows against a minimum of 3; 4 control rows against a minimum of 3.
Every other module reads NO DATA.

**`flood_mapping` is FAIL, so by the definition of done in the project rules SAR
flood mapping is NOT DONE.** It is finalised, not passed: the method, the
parameters and the evidence are fixed, and the gate it misses is reported as
missed. Rule 2 is satisfied by stopping here with the measurement, the target,
the root cause and the options — not by another tuning round.

### The limitation, as it will be said in the report and in Q&A

Gorge localisation at 20 m. Held-out tolerant F1 is 0.069 at Konjic (21.8° mean
slope, reference 0.134% of the AOI) and 0.118 at Punakha (24.4°, 0.023%), against
0.834 at Larkana (0.3°, 50.8%) and 0.915 at Ringebu (9.9°, 2.0%). Rank
correlation between held-out tolerant F1 and the reference's positive fraction is
0.886. **Predicted area is close to correct on every event** — 33.5 vs 42.6 ha,
35.3 vs 31.3, 178.6 vs 197.6, 120.3 vs 125.1, 785.2 vs 863.4 — so the quantity of
flooding is approximately right and on the two steepest events it is in the wrong
*place*. A 20 m cell in a floor two or three pixels wide mixes water, bank and
wall; that is now marked per run by the reliability layer rather than left for a
reader to infer.

Two honesty points that must stay attached to that paragraph:

1. **Positive fraction and slope are confounded** in this dev set — the steepest
   events also have the smallest references — so this evidence cannot separate
   "steep terrain" from "tiny target". Option 4 above (widening the dev set) is
   what would separate them. Neither explanation is claimed as established.
2. The remedy is **geometric, not a retune** (option 2 above): no parameter in
   the declared grid fixes it, and the grid offered `hand_max_water_m = 35 m`
   and the data declined it. Nothing was hand-moved toward these numbers after
   seeing them, no event was dropped, and the scoring domain was not narrowed.

Source files for every number above: `eval/results/flood.csv`,
`eval/chosen_params.json`, `runs/eval/<event>/scores/d4f9b1791c82.json`
(`reference_ha`, `reference_fraction_of_domain`,
`predicted_flood_affected_ha`), and the DEM-derived `terrain:` notes in
`eval/events.yaml` for mean slope.

---

## 2026-10-11 — Change (DECLARED BEFORE SCORING): Sentinel-2 optical evidence

**Status:** code and parameters added; **no optical number measured, and none
claimed.** The dev-set Sentinel-2 data does not exist yet. Everything below is
written down *before* a single optical score, which is the only order in which
Rule 3.4 permits a grid to be declared.

### Why

Track B requires Sentinel-2 where the sky is clear, and the radar-only detector
closed as **FAIL** for a reason that is geometric, not a matter of tuning: on the
two steepest events the predicted *area* is close to right and the predicted
*place* is wrong, in valley floors two or three 20 m pixels wide (see the entry
of 2026-10-11, "FINALISED"). A second sensor with an independent imaging
geometry is the one addition with a plausible route at that failure; another pass
over the radar grid is not. Fresh mud and boulder deposits are also far more
visible in short-wave infrared than at C-band, and deposits are half of what
Track B asks for.

### What was added

- `strandline/optical.py` — scene choice from catalogue cloud-cover metadata (so
  a clouded area is never downloaded), SCL masking, digital-number-to-reflectance
  conversion with a refusal if the result is not plausible reflectance, and two
  change signatures:
  - **water**: MNDWI (Xu 2006) must read as water *and* have risen since the
    pre-event date. The rise requirement is what makes it a change detector:
    permanent water, wet rock and terrain shadow are high or ambiguous on both
    dates and so cannot qualify.
  - **deposit**: NDVI must drop *and* B11 reflectance must rise.
- Fusion inside `detect.detect_flood`, not after it, so optical candidates pass
  **the same** HAND, slope, minimum-mapping-unit and drainage-connection tests as
  radar candidates. There is deliberately no second definition of "valley floor",
  and optical cannot put water anywhere radar would not have been allowed to.
- Agreement between the two sensors becomes a second route to high confidence,
  alongside the radar darkness test.
- `flood_source.tif`: per-pixel provenance (radar only / optical only / both), its
  own layer, never mixed into the class map.
- Every run reports an `optical` block: status, the chosen dates and their cloud
  cover, the clear fraction of the AOI on each date and on the pair, the
  reflectance convention actually applied, and the source-layer hectares.

### Parameters, and where each value comes from

Not one of these was chosen by looking at a score; there is no score to look at.

| Parameter | Value | Source |
|---|---|---|
| `scl_valid_classes` | 4, 5, 6, 7 | L2A product spec. Cloud (8, 9), cirrus (10), snow (11), both shadow classes (2, 3) and the defective classes (0, 1) are out. **7 (unclassified) is kept** because L2A routinely assigns it to turbid water and freshly flooded ground — exactly the target. **2 (cast shadow) is dropped** even though terrain shadow is common in the gorges we care about and excluding it costs us coverage there: a shadowed pixel is dark in every band and its indices are not trustworthy. |
| `mndwi_water` | 0.0 | Xu (2006): MNDWI > 0 separates water from built-up land and soil. **In the grid** (below). |
| `mndwi_rise_min` | 0.20 | Four times the ~0.05 index scatter that atmospheric and BRDF differences between two clear dates produce. Not searched. |
| `ndvi_drop_min` | 0.15 | A loss well outside a few weeks of seasonal phenology. **In the grid.** |
| `swir_rise_min` | 0.03 reflectance | About three times the radiometric uncertainty of an L2A SWIR band. Not searched. |
| `max_post_latency_days` | 20 | Deposits and mud scars persist for weeks, and a valley under cloud on day 1 is often clear a fortnight later. Longer than the radar window (12 d) on purpose. The cost is real and is reported, never hidden: standing water may have drained by then, so optical **water** evidence at a long latency is a lower bound and deposits are the stronger optical signal. |
| `search_days_before` | 60 | Long enough to find one clear pre-event date in a monsoon climate; short enough that land cover has not changed for other reasons. |
| `max_scene_cloud_cover` | 70% | A **download-cost** gate on tile-level metadata, deliberately loose: tile cloud cover says little about a much smaller AOI. The real decision is `min_clear_fraction`, measured from SCL after download. |
| `min_clear_fraction` | 0.20 | Below one fifth of the area clear on both dates, optical evidence describes too little of the map to be worth fusing, and is refused with its reason. |
| `reflectance_scale`, `boa_add_offset` | 10000, -1000 | ESA baseline 04.00 (products after 2022-01-25) carries `BOA_ADD_OFFSET = -1000`; earlier baselines carry none, and the baseline is read from the product name. These are the **fallbacks**, used only when the file declares no scale or offset. |

### The grid, declared now (5 points)

One grid per fusion mode, exactly as the radar grid is one per detection mode, so
no point varies a parameter its mode ignores:

| Fusion mode | Axes | Points |
|---|---|---|
| `off` | none — with fusion off an index threshold cannot change a pixel | **1** |
| `union` | `optical.mndwi_water` (0.0, 0.2) x `optical.ndvi_drop_min` (0.10, 0.20) | **4** |

The mode is `OpticalParams.fusion`, **not** a `DetectParams` field — see the correction below.

- `0.2` for `mndwi_water` asks whether a stricter threshold is needed once the
  rise test has already removed permanent water; flood water in a Himalayan gorge
  is heavily sediment-laden and its MNDWI is lower than that of clear water, so
  the literature value may well be the better one. The data decides.
- `0.10 / 0.20` bracket the 0.15 set from phenology.
- **`off` is in the grid because the comparison has to be able to say no.** If
  optical evidence does not help on the dev set, `off` wins and that is the
  result reported.

**Searched as a second leave-one-event-out pass**, crossed with the SAR point
already chosen for that fold, not folded into the radar grid. That is a cost
decision — a joint search would be 12 x 5 = 60 detections per event per fold
instead of 5 — taken now, in advance, and not after seeing a score. Each fold's
SAR point is still chosen without the held-out event, so the result stays out of
sample. The grid is machine-validated (`eval/tune.py::validate_optical_grids`,
`tests/test_optical_tuning_plan.py`) so it cannot drift from the fields it claims
to set. The scoring pass itself is **not yet written**: it cannot be exercised
until the download exists, and shipping an untestable search is worse than
shipping the declaration.

### What this means for the numbers already reported

This is the honesty point that matters most in this entry.

- The measured flood numbers — mean tolerant F1 **0.5134**, mean strict F1
  **0.4071**, `params_hash d4f9b1791c82`, `eval/results/flood.csv` — are
  **radar-only**. They remain exactly as they were; nothing was re-run,
  re-written or removed.
- **`OpticalParams.fusion` ships as `off`.** Sentinel-2 is gathered, masked,
  turned into evidence, written and reported on every default run — but it
  changes no output pixel, so the class map a default run produces is
  byte-identical to the radar-only detector those numbers measured
  (`tests/test_detect_optical_fusion.py::test_the_shipped_default_changes_no_pixel`,
  `tests/test_pipeline_optical.py::test_the_shipped_default_reports_optical_but_changes_no_pixel`).
  The measured numbers therefore still describe what the shipped pipeline maps.
- **`fusion = union` is UNSCORED and opt-in** (`--optical-fusion union`). No
  accuracy number may be quoted for it until `eval.fetch_dev_data --optical` has
  run and the dev set has been re-scored — **floods and controls both**, because
  fusion can only ADD pixels and so can only raise the false-flood rate, which
  means the 1% control ceiling does not cover it either. Shipping an unmeasured
  change to the scored quantity is not a trade this project makes; the earlier
  draft of this entry defaulted to `union` on the grounds that the problem
  statement asks for Sentinel-2, and that was the wrong call — the problem
  statement is satisfied by gathering and reporting it, which `off` does.
- `eval.check_gates` reads exactly as it did before this change (flood_mapping
  FAIL 0.513, no_flood_controls PASS 0.0048), because no row was added. That is
  the correct state: the gate still describes the radar-only detector, and says
  so.
- The dev harness defaults to **no optical** (`prepare(op=None)`), so every
  cached score and every recorded `params_hash` stays reproducible. Optical
  scoring is opt-in, with `--optical`.

### Correction made before this entry was committed: the params_hash break

The first draft of this change put `optical_fusion` on **`DetectParams`**. Because
`params_dict` hashes `asdict(detect)` unconditionally, that silently re-hashed
**every configuration ever recorded** — `d4f9b1791c82 -> 4d050525b109` — while
provably changing no output pixel. The damage would have been: no invocation of
the code could reproduce the hashes that are the primary key of every row in
`eval/results/flood.csv`, every cached score under
`runs/eval/<event>/scores/<hash>.json` (which `eval/run_scoreboard.py` rejects on
a hash mismatch), and every number published in this log and the README,
including the `scores/d4f9b1791c82.json` source files cited above. Three
statements in the code and docs asserted the opposite.

Caught by `/audit` before the commit. The fix: **every optical parameter lives in
`OpticalParams`**, whose section `params_dict` includes only when optical is
enabled, so a radar-only configuration hashes byte-identically to the way it did
before Sentinel-2 existed here. `tests/test_params_hash_reproducible.py` now pins
this: the recorded `chosen_parameters` must re-hash to its own recorded
`params_hash`, every `params_hash` in `eval/results/flood.csv` must be
reproducible from a point of the declared radar grid, and no optical field may
appear in `DetectParams` or `RunParams`.

The lesson worth keeping, stated precisely: **a field of the radar dataclasses
that cannot change an output pixel must not be able to change `params_hash`** —
the same rule already written down for `ReliabilityParams`, which this change
briefly broke for optical.

The scope of that sentence matters, because the shipped default looks at first
glance like a counter-example. With `enabled=True, fusion="off"` the hash is
`a424ec7b58a0` while the class map is byte-identical to `d4f9b1791c82`. That is
intended and is a different thing: the *presence* of the whole `optical` section
records that the run consulted a second sensor and would have used it had the
grid said so, which is provenance a reader needs and which no radar field can
express. What must never move is the hash of a configuration that did not
consult optical at all — and that is exactly what the test pins. The rule is
about fields inside `DetectParams` and `RunParams`; the optical section is a
section, and it is present or absent as a whole.

### One open item, stated rather than assumed

Whether this openEO backend returns L2A as raw digital numbers, or has already
applied the baseline-04 offset, is **not established**. It matters: MNDWI and
NDVI are normalised ratios, so a multiplicative scale cancels but an offset does
not. The code therefore (a) prefers scale and offset declared by the file
itself, (b) otherwise derives the offset from the processing baseline in the
product name, (c) refuses the whole optical stage if the result is not plausible
reflectance, and (d) records the method, scale, offset, median before and after,
and the fraction of negative reflectance in `run_report.json` — a large negative
fraction being the signature of an offset applied twice. The first real scene
settles it by inspection of that block. Until then this is an open item, not a
verified convention.

### Estimated cost of the dev-set download

`eval.fetch_dev_data --estimate --optical` over the 10 gate-plan events
(16.64 Mpx): **~857 MB of Sentinel-2 in 40 files**, on top of the 726 MB of
Sentinel-1 and DEM already cached. The Sentinel-1 and DEM bytes per pixel are
measured from `runs/case_smoke`; the Sentinel-2 figures are **derived from them
and not yet confirmed against a real file**, and the constants
(`BYTES_PER_PIXEL_S2_BAND`, `BYTES_PER_PIXEL_S2_SCL`) should be recalibrated
once the first files land.

---

## 2026-10-12 — BUG, found on the first real Sentinel-2 download: the BOA offset was applied twice

The open item recorded at the end of the previous entry — *"whether this openEO
backend returns L2A as raw digital numbers, or has already applied the
baseline-04 offset, is not established"* — is now settled, and the answer is the
one that was wrong in the code.

**The Copernicus Data Space openEO backend returns L2A reflectance with
`BOA_ADD_OFFSET` ALREADY APPLIED.** `optical.to_reflectance` applied it again.

### The evidence, from all 20 downloaded files

Two independent proofs, both from the pixels:

1. **Negative reflectance.** Over pixels the scene-classification layer calls
   usable, subtracting the offset a second time drives **60-97% of the green
   (B03) and red (B04) bands below zero**, and 9.1-62.8% of each file pooled
   over all five bands. Reflectance is a non-negative physical quantity.
   Without the second subtraction the same figure is **0.0-2.0%**, which is the
   small over-correction tail atmospheric correction is known to produce.
2. **Digital numbers below the offset.** The files contain valid values of 4,
   18, 24 and down to -997. A raw baseline-04 product stores
   `reflectance x 10000 + 1000` as an unsigned integer with 0 reserved for
   NO_DATA, so **no valid value can be below 1**, let alone negative. Values
   near -1000 are exactly what a stored zero looks like after the backend has
   subtracted the offset.

Both point the same way, and the two hypotheses are two orders of magnitude
apart on test 1. Representative numbers, `median` being the median converted
reflectance of the whole file:

| file | raw min DN | raw median DN | negative, offset applied once | negative, applied twice | median (correct) |
|---|---|---|---|---|---|
| `bhutan_punakha_2024` 2024-08-01 | -353 | 1537 | 0.003% | 40.00% | 0.154 |
| `bosnia_konjic_2024` 2024-08-14 | -993 | 852 | 0.122% | 54.86% | 0.085 |
| `bhutan_punakha_control_2024` 2024-01-14 | -743 | 668 | 0.037% | 62.84% | 0.067 |
| `norway_ringebu_control_2023` 2023-04-22 | -997 | 1308 | 1.964% | 45.76% | 0.131 |
| `pakistan_larkana_2022` 2022-09-05 | -892 | 779 | 0.297% | 60.12% | 0.078 |

### Why nothing caught it

Three guards existed and all three passed:

- `plausible_reflectance_range` is `(-0.2, 1.6)`, and the error is a shift of
  exactly **-0.1**. A green band at 0.05 becomes -0.05, which is still inside
  the plausible range, so `implausible_fraction` was **0.000** on every file and
  the refusal never fired. The range was set to catch a wrong *scale* — a factor
  of 10000 — and it does; it cannot catch a wrong *offset* of a tenth.
- `negative_fraction` **was** computed and recorded, specifically as the
  signature of this bug, and it read 0.40-0.63. Nothing acted on it. Recording a
  number is not the same as testing it, and that is the lesson worth keeping
  from this entry.
- Every synthetic test passed, because the test fixtures
  (`tests/test_pipeline_optical.py`) write digital numbers in the *archive*
  convention, `reflectance * 10_000 + 1_000`, which is the convention this
  backend does not use. A synthetic test cannot discover a fact about a backend.

Impact: it corrupted **both** indices. MNDWI and NDVI are normalised ratios, so
a multiplicative scale cancels but an offset does not: shifting B03 and B11 down
by 0.1 changes `(B03-B11)/(B03+B11)` by an amount that depends on the brightness
of the pixel, which is a non-linear distortion of exactly the quantity the water
test thresholds. No optical number produced before today is meaningful. None was
ever published or gated — fusion was `off` and no optical row was ever written —
so nothing in `eval/results/` or the README has to be withdrawn.

### The fix: decide it from the pixels, per file, in both directions

`optical.dn_offset` now decides whether the specification offset still needs
applying, rather than assuming either way:

> apply the specification offset **unless** doing so would make more than
> `OpticalParams.max_negative_fraction` (5%) of valid pixels negative.

Set from physics and the product specification, not from any dev-event score:
reflectance is not negative, over-correction is a few percent at worst
(measured worst case here 1.96%), and a doubled offset is tens of percent. The
5% ceiling is the same value and the same argument as the existing
`max_implausible_fraction`, and sits roughly 2.5x above the worst correct
observation and 2x below the lowest incorrect one.

The rule is **symmetric**, which is what makes it safe to leave in the path
rather than hard-coding this backend's habit:

- a genuine archive product, offset not yet applied, has a small true negative
  fraction and **keeps** its offset (asserted in `tests/test_optical.py`);
- if this backend ever stops pre-applying the offset, the same line of code
  starts applying it again, with no edit here;
- the processing baseline still decides the case it can decide — before
  baseline 04.00 there is no offset to apply or to double-apply.

Everything the decision rested on is written to `run_report.json` under
`optical.evidence.reflectance_conversion.<date>.offset_decision`: both candidate
negative fractions, the ceiling, the raw minimum and median, and which test
decided. Rule 6 — a reader checks the conclusion instead of trusting it.

### The one file the test cannot decide, stated rather than hidden

`ecuador_poza_honda_control_2024` 2024-08-31 is **87% cloud**; its median
reflectance is 0.56 and its 1st percentile 0.087, so it contains no pixel dark
enough to place the zero point, and **both** conventions yield plausible,
non-negative reflectance (0.00% against 1.70%). There is no pixel test that can
decide it, and the rule picks the specification convention, which on this
backend is the wrong one.

Its 1.70% also **overlaps** the 1.96% worst case of a correctly converted file
(`norway_ringebu_control_2023` 2023-04-22), so the two populations are not
cleanly separated and the "two orders of magnitude" figure above describes 19
files, not 20. No ceiling can split them. The comments in `config.py` and
`optical.py` and the README now say so, because attributing the safety of this
rule to its margin would be attributing it to the wrong mechanism.

It costs nothing, for a reason that is itself part of the guarantee: a date that
bright is a date under cloud, so its clear fraction (12.7%) is below
`min_clear_fraction` (20%) and `build_evidence` refuses the pair before any
index is computed. The wrong conversion cannot reach a map.
`tests/test_optical_real_files.py` asserts the disjunction — a file is **either**
distinguishable by its dark tail **or** too cloudy to be used — so a future file
that is neither fails the suite instead of being converted on a coin toss.

### Tests added

- `tests/test_optical.py` — four cases on the conversion: an already-corrected
  product does not get the offset twice (the regression test for this bug), a
  genuine archive product still does, the decision records every number it
  rested on, and an older baseline decides on the baseline alone.
- `tests/test_optical_real_files.py` — **new file, and the only test that reads
  downloaded Sentinel-2 data.** Every real file must convert to plausible,
  non-negative reflectance, and must be decidable or cloud-refused. It skips
  when the cache is absent, so a fresh clone is unaffected. It exists because
  the bug was undiscoverable on synthetic input, by construction.

### Sentinel-2 download size: estimate replaced by measurement

The previous entry predicted **857 MB** of Sentinel-2 from figures *derived*
from Sentinel-1 and flagged as not confirmed against a real file. The real
download is **454 MB in 40 files** — the estimate was 89% high.

The derivation was wrong in an instructive way: it scaled from Sentinel-1, whose
two float bands of speckle are near-incompressible, whereas optical reflectance
over land is smooth and the LZW-compressed GeoTIFF recovers most of a 4-byte
float. Measured, pooled over the 40 files:

| constant | was (derived) | now (measured) | per-file range |
|---|---|---|---|
| `BYTES_PER_PIXEL_S2_BAND` | 4.9 | **2.7** | 2.00-2.94 |
| `BYTES_PER_PIXEL_S2_SCL` | 1.3 | **0.1** | 0.03-0.17 |

`eval.fetch_dev_data --estimate --optical` now predicts 452 MB against 454 MB
actual. The spread is terrain and cloud: densest are the two cloudy Ecuador
dates, lightest the small uniform Norwegian valley. The pooled figure is used
rather than the mean, so the estimate is right in total even where it is wrong
per event, and `ESTIMATE_CAVEAT` now says which numbers are measured and from
what.

---

## 2026-10-12 — MEASURED: optical fusion scored, adopted, and honestly a small win in the wrong place

The second leave-one-event-out pass has run (`eval.tune --plan gates --optical`,
run_id `20261012T114247Z`, 15 configurations x 10 events). Fusion `union` is
**adopted** by the rule declared in `config.OPTICAL_ADOPTION` before the pass,
and `strandline/config.py` now ships it.

### Method, and what is out of sample about it

Each fold's optical point is chosen on the other five flood events and crossed
with **the SAR point the radar pass chose without that event**, so both halves
of the configuration are out of sample for the event they are scored on. `off`
and `union` are each selected within their own candidates (1 point and 4
points), so each gets its own best shot on the same training events and the
same SAR point: the only difference between the two columns is Sentinel-2.
Controls are compared at the final SAR point and remain **in sample** with
respect to the feasibility constraint, exactly as in the radar pass.

One read of each optical pair serves all 5 points (`optical.ReadPair`); only
`build_evidence` and `classify` run per point. `optical.READ_FIELDS` is the
machine-checked contract that makes that safe — an axis that would change the
read is rejected by `validate_optical_grids`.

### The control that had to pass first: `off` must BE radar-only

Fusion `off` gathers Sentinel-2, converts it, builds the evidence and then adds
no pixel, so its metrics must be identical to the radar-only configuration at
the same SAR point. Measured on the real dev set: **30/30 event-point pairs
identical** (`optical_pass.fusion_off_equals_radar_only`). Without that the `off`
column would not be the configuration the published numbers describe and the
comparison would mean nothing. It is checked, not assumed.

### What Sentinel-2 could actually see

| event | optical | clear pair | pre -> post | post latency |
|---|---|---|---|---|
| `bhutan_punakha_2024` | used | 53.7% | 2024-08-01 -> 2024-08-16 | 6 d |
| `bosnia_konjic_2024` | used | 88.1% | 2024-08-14 -> 2024-10-23 | 19 d |
| `ecuador_poza_honda_2025` | **refused** | 17.1% (33.2 pre, 42.9 post) | — | — |
| `norway_ringebu_2023` | **refused** | 19.5% (80.0 pre, 29.9 post) | — | — |
| `norway_selsverket_2023` | used | 32.6% | 2023-06-24 -> 2023-08-18 | 10 d |
| `pakistan_larkana_2022` | used | 100.0% | 2022-07-12 -> 2022-09-05 | 10 d |
| `bhutan_punakha_control_2024` | used | 62.8% | 2024-01-14 -> 2024-03-19 | 9 d |
| `bosnia_konjic_control_2024` | used | 99.8% | 2024-06-22 -> 2024-07-27 | 7 d |
| `ecuador_poza_honda_control_2024` | **refused** | 12.7% (100.0 pre, 12.7 post) | — | — |
| `norway_ringebu_control_2023` | **refused** | 19.9% (29.1 pre, 80.0 post) | — | — |

**Four of ten events have no usable pair**, and three of those four miss the 20%
floor by less than a point (19.5%, 19.9%, 17.1%). That is uncomfortably close to
the threshold and it is worth being explicit that `min_clear_fraction` was set
from the specification before any of this was measured and has NOT been lowered
to let them in — lowering it now, having seen that it would admit three more
events, would be Rule 3.2. The pattern itself is the real finding: in every one
of the four the *pre* or the *post* date is clear and the other is not, so it is
the **intersection** that fails. A change detector needs both dates, and that is
a structural cost of the method, not bad luck.

The refused events are kept in the pool and scored at every point, radar-only.
Their two columns are therefore identical **by construction**, which is why the
per-event table prints the clear fraction next to the delta: a reader must be
able to tell that apart from a genuine dead heat.

### The result, per event (held out, same SAR point both ways)

| event | clear pair | F1 tol `off` | F1 tol `union` | delta | strict F1 `off` | strict F1 `union` |
|---|---|---|---|---|---|---|
| `bhutan_punakha_2024` | 53.7% | 0.1183 | 0.1160 | **−0.0023** | 0.0757 | 0.0732 |
| `bosnia_konjic_2024` | 88.1% | 0.0694 | 0.0789 | **+0.0095** | 0.0315 | 0.0377 |
| `ecuador_poza_honda_2025` | — | 0.5553 | 0.5553 | 0.0000 | 0.4102 | 0.4102 |
| `norway_ringebu_2023` | — | 0.9151 | 0.9151 | 0.0000 | 0.7320 | 0.7320 |
| `norway_selsverket_2023` | 32.6% | 0.5878 | 0.5845 | **−0.0033** | 0.4765 | 0.4739 |
| `pakistan_larkana_2022` | 100.0% | 0.8343 | 0.8891 | **+0.0548** | 0.7168 | 0.8154 |
| **mean** | | **0.5134** | **0.5232** | **+0.0098** | 0.4071 | 0.4237 |

Strict (zero-tolerance) F1 moves the same way and slightly further, 0.4071 ->
0.4237, which is reported next to the tolerant numbers as Rule 2 requires.

Controls (false-flood fraction, ceiling 1%):

| control | `off` | `union` | delta |
|---|---|---|---|
| `bhutan_punakha_control_2024` | 0.0000 | 0.0022 | +0.0022 |
| `bosnia_konjic_control_2024` | 0.0000 | 0.0011 | +0.0011 |
| `ecuador_poza_honda_control_2024` | 0.0048 | 0.0048 | 0.0000 |
| `norway_ringebu_control_2023` | 0.0014 | 0.0014 | 0.0000 |

### Provenance of the rule, stated because the git history does not prove it

`OPTICAL_ADOPTION` and the measured result land in the **same commit**, so the
repository history cannot by itself show the rule was fixed before the numbers
were known. What can be checked is better than a commit order anyway: the four
criteria are **the human's, given in the task prompt before the pass was
written**, verbatim —

> "Keep fusion, and make it the default, only if mean tolerant F1 improves, no
> event and no control gets worse by more than 0.01, and every control stays at
> or under 1% false flood."

`config.OPTICAL_ADOPTION` encodes exactly those four and nothing else, and
`eval/tune.py::adoption_verdict` applies them mechanically, reporting the
measured value beside each. Nothing was added, relaxed or reinterpreted after
the result was seen — and had it been, the margin it would have had to move is
on record here: the mean gain is +0.0098 against a required > 0, and the worst
event regression −0.0033 against an allowed 0.01.

`/audit` checked this and recorded precisely what is and is not established,
which is worth copying here rather than paraphrasing:

- **The candidate grid is clean on git alone.** `OPTICAL_TUNE_GRIDS` was
  committed at `9c31e7c`, before any optical score existed, and ran
  unchanged — `mndwi_water: (0.0, 0.2)`, `ndvi_drop_min: (0.10, 0.20)`. The
  values that could be tuned were fixed in public beforehand.
- **The rule itself is evidenced by the artefact, not by a commit.**
  `eval/chosen_params.json` records `optical_pass.adoption.rule` written at the
  run timestamp, and HEAD was committed 24 minutes earlier — so the rule was in
  `config.py` before the verdict was computed, but inside an unwitnessed
  window. The auditor states it cannot rule out that the thresholds were set
  after seeing preliminary numbers, and neither can this entry.
- **`min_mean_gain = 0.0` is the weakest possible bar.** True, and it is the
  human's wording ("only if mean tolerant F1 improves"). It is the other three
  criteria that do the work.

**Process fix for the next pass, adopted now:** declare the rule in its own
commit *before* running — `config: declare <RULE> before the <X> pass | gates:
unchanged` — so the ordering is on the record and not on anyone's word.

### The adoption rule, as applied

| criterion | measured | required | |
|---|---|---|---|
| mean tolerant F1 improves | 0.5134 -> 0.5232, gain **+0.0098** | > 0 | PASS |
| no flood event worse by more than 0.01 | worst **−0.0033** (`norway_selsverket_2023`) | <= 0.01 | PASS |
| no control worse by more than 0.01 | worst **+0.0022** (`bhutan_punakha_control_2024`) | <= 0.01 | PASS |
| every control at or under the gate ceiling | worst **0.0048** (unchanged) | <= 0.01 | PASS |

**Verdict: KEEP.** All four criteria pass, so `OpticalParams.fusion` becomes
`union`. The adopted point is `mndwi_water=0.0` (Xu 2006, unchanged) and
`ndvi_drop_min=0.20`, which won on **all six folds** — the stricter of the two
searched values. That is the direction to expect: the deposit signature is the
looser of the two optical channels, it fires hardest on the dry controls (15% of
the Bhutan control AOI as candidate pixels before the terrain tests), and 0.10
cost the controls more than it bought the floods. `optical.ndvi_drop_min` is
therefore now 0.20 in `config.py`, chosen by the tuner and not by hand.

### The caveat that matters more than the verdict

**The gain is concentrated, and it is in the wrong place.** Of the +0.0587 of
summed per-event gain, **+0.0548 is one event** — Larkana 2022, a flat
floodplain with a 100% clear pair, 1.41 million optical water candidate pixels,
and the second-best radar score in the set. Bosnia adds +0.0095, and there the
gain came from the *deposit* channel (538 water candidates against 19,013
deposit candidates), not from water.

Where the system actually fails — the steep Himalayan and Norwegian gorges that
keep `flood_mapping` at FAIL — optical contributes **zero or slightly negative**:
−0.0023 at Punakha, −0.0033 at Selsverket, and exactly nothing at the two events
where cloud refused it. This is the same limitation recorded on 2026-10-11 from
the other side: terrain shadow is commonest in exactly the narrow valleys where
radar is weakest, so SCL removes optical coverage precisely where it would help
most, and the surviving coverage is the valley *rim* rather than the floor.

So the honest statement, which is what will be said in Q&A: **fusing Sentinel-2
is a real but small improvement that helps most where the radar map was already
good, and does not address the measured failure.** It is adopted because the
declared rule says a measured improvement that costs no event and no control
should be kept — not because it rescues anything.

### Gate status after the pass, verbatim

```
flood_mapping      mean(precision_tolerant)       0.5137    >= 0.9  FAIL     6/3
flood_mapping      mean(recall_tolerant)          0.5366    >= 0.9  FAIL     6/3
flood_mapping      mean(f1_tolerant)              0.5231    >= 0.9  FAIL     6/3
flood_mapping      min(f1_tolerant)               0.0789    >= 0.8  FAIL     6/3
flood_mapping      mean(f1)                       0.4237    report  REPORT   6/3
flood_mapping      mean(iou)                      0.3152    report  REPORT   6/3
no_flood_controls  max(false_flood_fraction)      0.0048   <= 0.01  PASS     4/3
```

`flood_mapping` still **FAILS**: 0.523 against a 0.90 target, worst event 0.0789
against a 0.80 floor. The optical pass moved the mean by one percentage point.
`no_flood_controls` still **PASSES** at 0.0048, and the worst control is
unchanged because optical was refused on it.

Per-event table at the adopted configuration (`params_hash 63a6d284bc4e`, rows
in `eval/results/flood.csv` under run_id `20261012T114247Z`):

| event | clear | P | R | F1 | IoU | P tol | R tol | F1 tol |
|---|---|---|---|---|---|---|---|---|
| `bhutan_punakha_2024` | 0.537 | 0.0614 | 0.0907 | 0.0732 | 0.0380 | 0.0959 | 0.1469 | 0.1160 |
| `bosnia_konjic_2024` | 0.881 | 0.0378 | 0.0376 | 0.0377 | 0.0192 | 0.0662 | 0.0977 | 0.0789 |
| `ecuador_poza_honda_2025` | — | 0.4321 | 0.3904 | 0.4102 | 0.2580 | 0.5789 | 0.5335 | 0.5553 |
| `norway_ringebu_2023` | — | 0.7685 | 0.6988 | 0.7320 | 0.5772 | 0.9234 | 0.9070 | 0.9151 |
| `norway_selsverket_2023` | 0.326 | 0.4803 | 0.4677 | 0.4739 | 0.3105 | 0.5558 | 0.6164 | 0.5845 |
| `pakistan_larkana_2022` | 1.000 | 0.7899 | 0.8426 | 0.8154 | 0.6883 | 0.8617 | 0.9183 | 0.8891 |
| **mean** | | 0.4283 | 0.4213 | **0.4237** | **0.3152** | **0.5136** | **0.5366** | **0.5232** |

Measured with `n_pre=3` (`DEV_N_PRE`, a cost setting) rather than the shipped
default of 5, as every dev number in this log is. The tuner's diff also lists
`run.n_pre 5 -> 3` for that reason; it is **not** a tuning result and was not
applied.

### FLOOD MAPPING IS NOW FROZEN

No further detection change. The next work is the damage overlay, the cut-off
analysis and the copilot, all of which read this map as it stands and must
carry its measured accuracy and the narrow-valley caveat with every number
they quote.

### What changed in the code

- `strandline/config.py` — `OpticalParams.fusion` `off` -> `union`;
  `ndvi_drop_min` 0.15 -> 0.20; new `max_negative_fraction`; new
  `OPTICAL_ADOPTION` (declared before the pass).
- `strandline/optical.py` — `dn_offset` (the conversion fix), `ReadPair`,
  `read_pair`, `evidence_from_pair`, `READ_FIELDS`.
- `strandline/run.py` — `PreparedScene.optical_pair`, so a caller rebuilding
  evidence re-reads exactly the pair that was scored.
- `eval/tune.py` — the optical pass: `materialise_full`, `full_point_hash`,
  `build_optical_matrix`, `optical_fold_selections`, `adoption_verdict`,
  `fusion_off_matches_radar_only`, `--optical`.
- `eval/run_scoreboard.py` — `op` threaded through `prepare_event`,
  `score_with_scene` and `score_event`; `optical_provenance` written into every
  score file, so no score can be silent about Sentinel-2.
- `eval/fetch_dev_data.py` — Sentinel-2 bytes per pixel measured, not derived.
- Tests: `tests/test_eval_tune_optical.py` and
  `tests/test_optical_real_files.py` new; the shipped-default pin in
  `tests/test_detect_optical_fusion.py` moved from "`off`" to "whatever
  `eval/chosen_params.json` records as adopted", which is strictly stronger;
  `tests/test_params_hash_reproducible.py` now accepts a hash reachable from the
  radar grid **crossed with** the optical grid, and separately asserts that a
  published optical row implies a recorded verdict of KEEP.

---

## 2026-10-12 — Class-count audit before the freeze: debris is reachable, and almost all of it is wrong

A pre-freeze sanity check on the dev events only: the pixel count of every
output class, for every event and control in the latest scoreboard run
(`run_id 20261012T114247Z`). Counts are `areas_ha / 0.04` from each published
score file under `runs/eval/<event>/scores/<hash>.json`; a class absent from
`areas_ha` has zero pixels.

| event | kind | nodata | no change | flood high | flood med | **debris** | perm water | terrain excl | total |
|---|---|---|---|---|---|---|---|---|---|
| `bhutan_punakha_control_2024` | control | 0 | 163,910 | 4 | 484 | **7,111** | 2,366 | 3,262,316 | 3,436,191 |
| `bosnia_konjic_control_2024` | control | 13 | 56,951 | 3 | 874 | **16** | 29,661 | 708,068 | 795,586 |
| `ecuador_poza_honda_control_2024` | control | 3 | 465,650 | 2,927 | 4,853 | **0** | 2,202 | 1,140,375 | 1,616,010 |
| `norway_ringebu_control_2023` | control | 66 | 194,527 | 194 | 1,353 | **0** | 31,479 | 843,381 | 1,071,000 |
| `bhutan_punakha_2024` | flood | 1 | 168,172 | 460 | 422 | **275** | 4,546 | 3,262,315 | 3,436,191 |
| `bosnia_konjic_2024` | flood | 19 | 62,390 | 7 | 1,032 | **18** | 24,069 | 708,051 | 795,586 |
| `ecuador_poza_honda_2025` | flood | 2 | 468,291 | 1,535 | 2,929 | **0** | 2,876 | 1,140,377 | 1,616,010 |
| `norway_ringebu_2023` | flood | 21 | 174,329 | 16,332 | 3,297 | **0** | 33,640 | 843,381 | 1,071,000 |
| `norway_selsverket_2023` | flood | 0 | 46,190 | 1,499 | 1,547 | **0** | 3,435 | 255,529 | 308,200 |
| `pakistan_larkana_2022` | flood | 38 | 911,054 | 915,198 | 423,179 | **0** | 220,696 | 0 | 2,470,165 |
| **total** | | 163 | 2,711,464 | 938,159 | 439,970 | **7,420** | 354,970 | 12,163,793 | 16,615,939 |

### Is debris zero everywhere? No — and the question it was asked to settle has a worse answer

Debris is non-zero on **4 of 10** (7,420 px = 296.8 ha) and zero on 6, so the
conditional does not arise and there is no dead-code question to answer. The
channel is reachable, and the suite already proves it mechanically:
`tests/test_detect.py::test_debris_detected` requires recall >= 0.9 on the
synthetic debris fan under the shipped parameters, and
`tests/test_detect_modes.py::test_the_debris_fan_is_still_found_somewhere_in_the_union`
guards the same thing against further tightening.

**Working as intended, then.** But splitting the 7,420 pixels by the sensor that
produced them is the finding, and it is not comfortable. The same SAR point
scored three ways:

| event | radar-only | fusion `off` | fusion `union` | optical |
|---|---|---|---|---|
| `bhutan_punakha_2024` | 107 | 107 | 275 | used |
| `bosnia_konjic_2024` | 0 | 0 | 18 | used |
| `norway_selsverket_2023` | 0 | 0 | 0 | used |
| `pakistan_larkana_2022` | 0 | 0 | 0 | used |
| `ecuador_poza_honda_2025` | 0 | 0 | 0 | refused |
| `norway_ringebu_2023` | 0 | 0 | 0 | refused |
| `bhutan_punakha_control_2024` | 0 | 0 | **7,111** | used |
| `bosnia_konjic_control_2024` | 0 | 0 | 16 | used |
| `ecuador_poza_honda_control_2024` | 0 | 0 | 0 | refused |
| `norway_ringebu_control_2023` | 0 | 0 | 0 | refused |
| **total** | **107** | **107** | **7,420** | |

Three things follow.

**1. The SAR debris channel is very nearly vacuous.** 107 pixels (4.28 ha) on
one of ten events, out of 16.6 million analysed. It is not dead — the terrain
annulus it needs (`zone_debris & ~zone_water`, i.e. HAND 20-35 m, or HAND <= 20 m
with slope 15-30 deg) is reachable within 3 px of a drainage line, and Punakha
proves it — but it is one deliberate tightening away from never firing. That
tightening was the right call and is recorded above (2026-10-11: the old channel
covered 3.8% of an AOI at precision 0.0062); the point here is that what
survives it is 107 pixels, and no gate would notice if it became zero.

**2. 96% of all dev-set debris is on a no-flood CONTROL.** 7,111 of 7,420
pixels are `bhutan_punakha_control_2024`, produced entirely by the optical
deposit channel on a 2024-01-14 -> 2024-03-19 pair. Winter to spring, nine
weeks apart: NDVI falls and SWIR rises across a Himalayan AOI because of
phenology and snowmelt, which is precisely what `ndvi_drop_min` exists to
exclude. The tuner chose the stricter of its two candidate values (0.20 over
0.10) on all six folds, and 7,111 false pixels survive it.

**3. On the only event with an expert debris reference, the debris class has
never been right about a single pixel.** `bosnia_konjic_2024` is the one dev
event whose `reference_classes` declares a debris class ("6-Mass Movement",
23.28 ha, 582 reference pixels). From `provenance.debris_only` in its score
files:

| configuration | predicted | tp | fp | fn | precision | recall |
|---|---|---|---|---|---|---|
| radar-only `d4f9b1791c82` | 0 | 0 | 0 | 582 | — (undefined) | 0.0 |
| fusion `off` `4f9c8aa0f92f` | 0 | 0 | 0 | 582 | — (undefined) | 0.0 |
| fusion `union` `63a6d284bc4e` | 18 | **0** | **18** | 582 | **0.0** | **0.0** |

Fusion took the debris class from "predicts nothing" to "predicts 18 pixels,
all of them wrong". The debris class is inside the **scored quantity**
(`FLOOD_AFFECTED_CLASSES`), so this is not a cosmetic layer.

### A related measurement problem the audit exposed, reported not fixed

The control ceiling is diluted by terrain-excluded ground, and on steep AOIs
the dilution is large. `validation.compare.scoring_domain` deliberately
includes terrain-excluded pixels, and its docstring justifies that for a FLOOD
event: a reference flood there is a miss we must be charged for, and narrowing
the domain is the "shrinking the scoring area" Rule 3.2 forbids. On a CONTROL
the same choice cuts the other way — there is no reference to miss, so the
excluded mass only pads the denominator:

| control | affected px | gated ffr (whole domain) | analysed px | **ffr over analysed ground** |
|---|---|---|---|---|
| `bhutan_punakha_control_2024` | 7,599 | 0.0022 | 173,875 | **4.37%** |
| `bosnia_konjic_control_2024` | 893 | 0.0011 | 87,505 | **1.02%** |
| `ecuador_poza_honda_control_2024` | 7,780 | 0.0048 | 475,632 | 1.64% |
| `norway_ringebu_control_2023` | 1,547 | 0.0014 | 227,553 | 0.68% |

With fusion `off` the first two read **0.05%** and **0.03%** over analysed
ground (89 and 26 px). So fusion raised the Bhutan control's false-flood rate
over classifiable ground by a factor of **85** (89 -> 7,599 px), while the gated
metric moved 0.0000 -> 0.0022 and passed the 1% ceiling with room to spare,
because 95% of that AOI is terrain-excluded and sits in the denominator.

**Both numbers are true and they answer different questions.** Nothing here is
a reason to change the gate: `eval/gates.yaml` is human-owned, the metric and
the domain were declared in advance, and redefining either after seeing a
result is exactly Rule 3.2. What was wrong was that only the flattering number
was recorded. So `eval/run_scoreboard.py` now writes, for every control and
**reported only, never gated**: `analysed_pixels`,
`terrain_excluded_pixels`, `terrain_excluded_fraction_of_domain` and
`false_flood_fraction_analysed_area`. The ten published score files were
re-scored to carry it and **every gated metric came out byte-identical**, which
also re-confirms the optical path is reproducible.

### Verdict: no bug, no parameter changed, and one judgement call left to the human

- The debris logic matches its declared design. Reachable, tested, extremely
  conservative on radar.
- The control metric is as declared and documented. Not touched.
- No parameter was changed. The only code change is the reported-only control
  diagnostic above, plus its tests
  (`tests/test_eval_scoreboard.py::test_a_control_also_records_its_false_flood_
  rate_over_analysed_ground`, and a companion asserting a flood event does not
  get it, because a flood event's recall needs the wide domain).

**What a human should decide, with this evidence in front of them:** whether the
debris class belongs inside the scored quantity at all while its only
measured precision against an expert debris reference is 0.0, and whether
optical *deposit* fusion should have been adopted on a rule whose control
criterion is diluted twentyfold on the steepest AOI in the set. The adoption
followed the declared rule exactly and the rule was the right shape; the
evidence now says the deposit channel is the weakest thing in the pipeline. I
am not reopening it unilaterally — flood mapping is frozen and this is a
parameter-and-metric question, not a bug.

The honest one-line summary for Q&A: **the flood-water channel carries the
map; the debris channel contributes 296.8 ha across the dev set, 96% of it on
a dry control, and 0.0 precision where ground truth exists.**

---

## 2026-10-12 — DECLARED BEFORE SCORING: a stricter control criterion, and two new options

**Nothing in this entry has been scored.** It is written, and committed, before
the pass it governs is run — the process fix promised in the previous entry,
where `/audit` correctly noted that git could not corroborate a "declared
before" claim because the rule and the result landed in one commit. This time
the declaration is its own commit and the ordering is on the record.

Two decisions, both prompted by the class-count audit above.

### Decision 1: every control must now pass BOTH false-flood ceilings

The audit showed the gated control metric is diluted by terrain-excluded
ground: `bhutan_punakha_control_2024` reads 0.0022 over the whole scoring
domain and **4.37%** over the ground the detector could actually classify,
because 95% of that AOI is terrain-excluded and sits in the denominator. A 1%
ceiling that a 4.37% false-flood rate passes is not doing the job it was
written for.

So from now on, for this and **every later adoption decision**, a parameter set
is feasible only if every control satisfies both:

| constraint | source | value |
|---|---|---|
| `false_flood_fraction` (whole scoring domain) | `eval/gates.yaml`, human-owned | <= 0.01 |
| `false_flood_fraction_analysed_area` (domain minus terrain-excluded) | this project, declared here | <= 0.01 |

**`eval/gates.yaml` is NOT touched.** The published gate keeps its declared
metric and its declared domain, exactly as before; this is an ADDITIONAL
feasibility constraint inside the tuner, and it can only ever make adoption
harder. Tightening a constraint is not the "changing what is measured" Rule 3.2
forbids — that rule is about loosening, shrinking and excluding, and all three
remain forbidden. The reference definition, the scoring domain and the boundary
tolerance are unchanged.

Consequence accepted in advance: this may make the currently shipped
configuration INFEASIBLE. Fusion `union` puts Bhutan's control at 4.37% over
analysed ground, so on today's evidence `union` cannot survive the new
criterion. That is the point of adding it.

A cached control score written before this field existed cannot demonstrate the
constraint, so it is not reused: the tuner re-scores any control whose cached
score lacks `false_flood_fraction_analysed_area` rather than assuming it would
have passed.

### Decision 2: two options added to the declared grid

Both are added to the SECOND (optical) pass, which is crossed with the SAR
point each fold chose without its held-out event — the same structure as
before, so both halves of every configuration stay out of sample.

**(a) `extent.debris_in_flood_extent`, values `(True, False)`.**

`True` is today's behaviour: the debris class is part of the flood extent and
of the primary scored quantity. `False` keeps debris as an **output layer**,
written and reported as *"possible debris, unverified"*, but takes it out of
the flood extent — so it is not scored, not counted in flood-affected hectares,
and **must not be consumed by the damage overlay or the cut-off analysis**,
which are the next modules to be built.

Why it is on the grid rather than simply switched off: the audit measured the
debris class at precision **0.0** against the only expert mass-movement
reference in the dev set (18 predicted, 0 correct, 582 missed), with 96% of all
its dev-set pixels on a dry no-flood control. That is strong evidence against
it, but "strong evidence against" is not the same as a measured decision, and
this project does not ship either one on a judgement call when it can score it.

The reference definition does **not** change: references are still scored as
flooded + debris/mass-movement, exactly as declared on 2026-10-11. So `False`
makes the task HARDER in principle — the 582 reference debris pixels at Konjic
become unreachable misses — and it is allowed to lose on that basis.

Where the flag lives, and why not on `DetectParams`: it changes no pixel of the
class map (debris is still written as `CLASS_DEBRIS`), but it does change the
scored quantity, so it must change `params_hash`. Putting it on `DetectParams`
would re-hash every configuration already published — the exact bug recorded in
this log on 2026-10-11. It therefore gets its own `ExtentParams`, whose section
`params_dict` includes **only when it differs from the default**, so every
published hash stays reproducible and the new option gets a hash of its own.

**(b) `optical.fusion`, values `(off, union, water_only)`.**

`water_only` is new and the code **does** support it with a small change: the
MNDWI water evidence is fused exactly as in `union`, and the NDVI-drop /
SWIR-rise deposit channel contributes nothing. It is worth scoring because the
audit localised the damage precisely — the optical deposit channel produced
7,111 false pixels on one dry control, while the optical *water* channel is
where Larkana's +0.0548 came from (1.41 million water candidates on a 100%
clear pair). `water_only` is the configuration that keeps the one and drops the
other, and no one has measured it.

One sub-grid per fusion mode, so no point ever varies a parameter its mode
ignores — the convention already in force:

```
off         {}                                             1 point
union       mndwi_water (0.0, 0.2) x ndvi_drop_min (0.10, 0.20)   4 points
water_only  mndwi_water (0.0, 0.2)                         2 points
```

`ndvi_drop_min` is deliberately absent from `water_only`: with no deposit
channel it cannot change a pixel. Crossed with `debris_in_flood_extent`, the
second pass is **7 x 2 = 14 points**, and crossed with the three distinct SAR
points the folds chose, **42 configurations**.

### How the winner is chosen, and the rule this supersedes

The objective and the tie-break are unchanged: maximise mean boundary-tolerant
F1 over the training events, subject to every control passing **both** ceilings,
ties broken by grid order (declaration order in `config.py`).

`OPTICAL_ADOPTION`'s requirement that the mean must IMPROVE is **explicitly
suspended for this decision**, and that is a deliberate declaration, not a
convenience: the stricter control criterion may rule out the configuration now
shipped, and when the reason for a change is that the incumbent is infeasible,
"it does not improve the mean" must not be allowed to block it. **Whatever wins
under the two ceilings is adopted, even if the mean is worse than today's
0.5232.** A configuration that scores lower but keeps a dry valley dry is the
better system, and this project would rather report a lower number it believes.

After this pass, flood mapping is frozen for good.

---

## 2026-10-12 — RESULT: nothing in the declared grid is feasible. STOP, and report.

The pass declared above has been run once, as declared
(`eval.tune --plan gates --optical`, 14 second-pass points, the stricter
control criterion in force). **The outcome is a refusal: no parameter set in
the declared grid satisfies both control ceilings, so nothing is adopted.**

- `strandline/config.py` is **unchanged**. No parameter was adopted.
- **No row was written** to `eval/results/flood.csv` (still 20 rows + header).
  The tuner refused to write a partial set, as it must.
- `eval/check_gates.py` reads exactly as before: `flood_mapping` FAIL 0.5231,
  `no_flood_controls` PASS 0.0048. Those numbers still describe the
  configuration adopted on 2026-10-12 and published under run_id
  `20261012T114247Z`. (`0.5231` here and `0.5232` in the tables below are the
  same unrounded **0.523150**, rounded by two different printers; neither is a
  second measurement.)
- The refusal is recorded in its own artefact,
  **`eval/chosen_params_strict_control.json`**, deliberately NOT overwriting
  `eval/chosen_params.json`: that file is the record of the configuration the
  published rows actually describe, and a pass that adopted nothing must not
  erase it.
- That artefact records `"git_commit": "47feb0c-dirty"`, and the suffix is the
  point. The pass ran while the code enforcing the new ceiling was still
  uncommitted, on top of the declaration-only commit `47feb0c`, so the artefact
  must not claim to be reproducible from `47feb0c` alone - it is not.
  `eval/scoreboard.py::git_commit` now uses `git describe --always --dirty`
  precisely so this cannot be hidden; an earlier version recorded a clean hash
  for a dirty tree, which `/audit` caught. The code is committed in the commit
  that carries this entry, and every one of the 420 scores is deterministic and
  cached, so the run reproduces from that tree.

### The binding constraint, and why no option on the grid can reach it

| control | affected px | gated ffr | analysed px | **ffr over analysed ground** | feasible? |
|---|---|---|---|---|---|
| `ecuador_poza_honda_control_2024` | 7,780 | 0.0048 | 475,632 | **0.0164** | **NO** |
| `norway_ringebu_control_2023` | 1,547 | 0.0014 | 227,553 | 0.0068 | yes |
| `bhutan_punakha_control_2024` | 89 – 9,707 | 0.0000 – 0.0028 | 173,875 | 0.0005 – 0.0558 | depends |
| `bosnia_konjic_control_2024` | 26 – 1,189 | 0.0000 – 0.0015 | 87,505 | 0.0003 – 0.0136 | depends |

`ecuador_poza_honda_control_2024` reads **0.0164 at every one of the 42
configurations — all 14 points crossed with all three SAR points —
identically**: one distinct `(gated, analysed)` pair, `(0.0048, 0.0164)`, over
42 of 42 scored configurations, same 7,780 pixels every time. That is not a
coincidence and it is the whole result:

- **optical was REFUSED on that event** (clear pair 12.7%, below the 20%
  minimum), so all three fusion modes — `off`, `union`, `water_only` — produce
  the same map there. The fusion axis cannot move it.
- **its debris count is zero**, so `debris_in_flood_extent` removes nothing.
  The extent axis cannot move it either.

Its 7,780 false pixels are 2,927 high-confidence plus 4,853 medium-confidence
**flood water**, from the radar channel alone. And note that this control is only
**70.6%** terrain-excluded, not 95% like Bhutan's — so the dilution is milder
here and the gated figure (0.48%) is still less than a third of the analysed one
(1.64%). The binding constraint is not an artefact of an extreme denominator.

So the stricter criterion is failed by the Sentinel-1 water detector itself, at
every radar grid point, and neither option declared above was ever capable of
rescuing it. **No event or option was excluded to reach this conclusion.** The
30 radar candidates are in `eval/chosen_params_strict_control.json` under
`final` and `loeo`; the 42 second-pass configurations are under `optical_pass`
with every candidate's rejection reason, and each has a score file under
`runs/eval/<event>/scores/`.

A hypothesis for those 7,780 pixels, stated as a hypothesis and NOT measured:
`events.yaml` selected this control because it has a **permanent reservoir
inside the bbox** (2,202 permanent-water pixels), and a reservoir whose level
rose between the pre-event stack and the post-event image produces exactly this
signature — new water, adjacent to permanent water, on flat ground, connected to
drainage. Confirming it needs work that would reopen detection, and detection is
frozen.

The exact breakdown of the 30 radar candidates, from
`eval/chosen_params_strict_control.json`, because "nothing was feasible" should
be auditable rather than asserted:

| rejected by | count | which |
|---|---|---|
| the **gated** ceiling (`false_flood_fraction` > 0.01) | **24** | all 18 `darkening` points, plus the 6 `rough_water` points with `min_abs_change_db` **2.0 dB** — `norway_ringebu_control_2023` at 1.13%–5.16% |
| the **new analysed-area** ceiling | **6** | the 6 `rough_water` points with `min_abs_change_db` **3.0 dB** — `ecuador_poza_honda_control_2024` at 1.64% |
| feasible | **0** | — |

The discriminating axis is `min_abs_change_db`, and that is worth stating
precisely because the counts alone do not reveal it: `hand_max_water_m` also
splits the twelve `rough_water` points six and six, but it splits them 3 gated /
3 analysed on EACH of its two values, so it explains nothing. `min_abs_change_db`
splits them cleanly — every 2.0 dB point is rejected on the gated ceiling, every
3.0 dB point on the new one. Physically that is what should be expected: 2.0 dB
is near the sensor's radiometric stability, so it admits enough marginal change
to flood the Norwegian control outright, while 3.0 dB is tight enough to pass
the gated ceiling and is then caught by the stricter one at Ecuador.

So most of the grid was already infeasible on the OLD ceiling: the `darkening`
mode has been known infeasible since 2026-10-11, and the loose 2.0 dB change
threshold floods the Norwegian control. The new criterion is what removed the
last 6 — the six points that include the shipped configuration (`d4f9b1791c82`
is one of them).

An earlier version of this table attributed the split to `hand_max_water_m` and
concluded that "widening the terrain zone to 35 m floods the Norwegian control".
That was wrong, and `/audit` caught it from the artefact. The counts were right
and the conclusion drawn from them was not, which is the third time in this
entry that a number has been checked and the sentence around it has not.

### What the two new options measured, reported because it was asked for

All of this is reproducible with one command, which is the tuner itself — an
earlier draft used a throwaway script that was not in the repo, and a number no
command can reproduce is not a result:

```
venv\Scripts\python.exe -m eval.tune --plan gates --optical \
    --chosen-out eval/chosen_params_strict_control.json
```

These are **diagnostic numbers, not an adoption**; every one is a
`runs/eval/<event>/scores/<hash>.json`, and each fold is scored at its own
recorded SAR point so both halves stay out of sample.

Per-event strict and boundary-tolerant F1:

| event | incumbent `union` 0.20 debris-**IN** | `union` 0.10 debris-**OUT** | `water_only` debris-**OUT** | `off` debris-IN (radar-only) |
|---|---|---|---|---|
| | strict / tolerant | strict / tolerant | strict / tolerant | strict / tolerant |
| `bhutan_punakha_2024` | 0.0732 / 0.1160 | 0.0853 / 0.1356 | 0.0853 / 0.1356 | 0.0757 / 0.1183 |
| `bosnia_konjic_2024` | 0.0377 / 0.0789 | 0.0380 / 0.0798 | 0.0380 / 0.0798 | 0.0315 / 0.0694 |
| `ecuador_poza_honda_2025` | 0.4102 / 0.5553 | 0.4102 / 0.5553 | 0.4102 / 0.5553 | 0.4102 / 0.5553 |
| `norway_ringebu_2023` | 0.7320 / 0.9151 | 0.7320 / 0.9151 | 0.7320 / 0.9151 | 0.7320 / 0.9151 |
| `norway_selsverket_2023` | 0.4739 / 0.5845 | 0.4739 / 0.5845 | 0.4739 / 0.5845 | 0.4765 / 0.5878 |
| `pakistan_larkana_2022` | 0.8154 / 0.8891 | 0.8154 / 0.8891 | 0.8154 / 0.8891 | 0.7168 / 0.8343 |
| **mean** | **0.4237 / 0.5232** | **0.4258 / 0.5266** | **0.4258 / 0.5266** | **0.4071 / 0.5134** |

Both control fractions, at the configurations that matter:

| control | incumbent gated / analysed | `union` 0.10 debris-OUT | `water_only` debris-OUT | `off` |
|---|---|---|---|---|
| `bhutan_punakha_control_2024` | 0.0022 / **0.0437** | 0.0001 / 0.0028 | 0.0001 / 0.0028 | 0.0000 / 0.0005 |
| `bosnia_konjic_control_2024` | 0.0011 / **0.0102** | 0.0011 / 0.0100 | 0.0011 / 0.0100 | 0.0000 / 0.0003 |
| `ecuador_poza_honda_control_2024` | 0.0048 / **0.0164** | 0.0048 / **0.0164** | 0.0048 / **0.0164** | 0.0048 / **0.0164** |
| `norway_ringebu_control_2023` | 0.0014 / 0.0068 | 0.0014 / 0.0068 | 0.0014 / 0.0068 | 0.0014 / 0.0068 |

Three findings worth keeping, none of them an adoption:

1. **Taking debris out of the flood extent improves BOTH the objective and the
   controls.** Mean tolerant F1 0.5232 -> 0.5266, and Bhutan's analysed-area
   control rate 0.0437 -> 0.0028, a factor of 16. It makes the task harder in
   principle (reference debris becomes an unreachable miss) and it still wins.
   That is the strongest evidence yet that the debris class does not belong in
   the scored quantity.
2. **`water_only` ties the best `union` point exactly, at 0.5266.** Not
   approximately — identically, on every event. With debris out of the extent
   the deposit channel cannot reach the scored quantity at all, so the two
   configurations are the same measurement; `water_only` simply says so in the
   parameters instead of relying on the extent flag to neutralise it.
3. **The declared per-fold selection was computed, both ways.** With both
   ceilings enforced — the declared rule — **every one of the six folds has NO
   FEASIBLE POINT**, which is the refusal above, now established by the method
   rather than inferred from it. With the control constraint lifted, which is a
   COUNTERFACTUAL and is labelled as one, **all six folds independently select
   `union` / `mndwi_water` 0.0 / `ndvi_drop_min` 0.10 /
   `debris_in_flood_extent` False**, unanimously, on their own training events.
   Each fold has 3–4 points tied at the top, resolved by grid order; at the
   Punakha fold the tied set is

   | tied point | hash |
   |---|---|
   | `union` mndwi 0.0 ndvi 0.10 debris-OUT | `0550873e7ea6` |
   | `union` mndwi 0.0 ndvi 0.20 debris-OUT | `4669714cb7ee` |
   | `water_only` mndwi 0.0 debris-**IN** | `b73a7a728df4` |
   | `water_only` mndwi 0.0 debris-OUT | `a9a8fba3ae98` |

   every one of which either removes debris from the extent or removes the
   optical deposit channel, or both. So the direction is robust to the tie-break
   even though the single winner is not.

### A correction: how that number was first obtained, and why it was wrong

The first draft of this entry asserted the same winner, but had reached it by
**comparing the held-out scores** of the 14 points — the diagonal, each event at
its own fold — and reading the best mean off that. `/audit` caught it. Choosing
between candidates by their held-out scores is selection on held-out data,
which is precisely what Rule 3.4 and the whole leave-one-event-out structure
exist to prevent; the declared rule selects on each fold's TRAINING events and
only then looks at the held-out one. The required scores did not even exist at
the time: the debris-OUT and `water_only` points had been scored only on the
event each fold held out, so the declared computation could not have been run.

The fix was not to soften the sentence but to do the computation. `eval/tune.py`
now runs the second pass even when the radar pass is infeasible, against the
FROZEN SAR point each fold chose in the published run (`frozen_sar_points`,
reported in the output and recorded as `optical_pass.sar_point_source`), so all
42 configurations x 10 events = **420 scores** exist and the per-fold selection
above is the declared one. The conclusion survived; the method that produced it
did not, and the version that stands is the second one.

Worth stating plainly because it is the second time the same shape of mistake
has appeared in this work: a number that is easy to compute is not the number
the rule asks for, and the only defence is to run the rule.

### Why nothing was adopted anyway

The declared rule is feasible-or-nothing: a point is adopted only if every
control passes both ceilings. None does. Adopting the best INFEASIBLE point
because it happens to dominate the incumbent would be a rule invented after
seeing the result, which is precisely what Rule 3.4 forbids and what the
previous entry's process fix was written to prevent. So the measurement stands
and the decision does not get made here.

**The shipped configuration therefore still fails the criterion declared
above**, on three of four controls (Ecuador 1.64%, Bhutan 4.37%, Bosnia 1.02%
over analysed ground). That is now stated in the README rather than left
implicit. It is an uncomfortable state and it is the true one.

### Options, with costs

1. **Accept the refusal and ship the incumbent.** No code change. Cost: the
   project ships a configuration it has declared, and measured, to fail its own
   stricter control criterion on three of four controls. Everything is
   reported, nothing is hidden. This is the state as committed.
2. **Declare a dominance rule and re-run.** "When no point is feasible, adopt
   the point that is no worse than the incumbent on every control and better on
   the objective." That would adopt `union` 0.10 debris-OUT (or equivalently
   `water_only` debris-OUT): mean tolerant F1 +0.0034, Bhutan's analysed control
   rate down 16-fold, Ecuador unchanged. Cost: it is a new rule, so it must be
   declared and committed by the human BEFORE the re-run, exactly as the
   previous rule was. I have not invented it into effect.
3. **Reopen detection for the Ecuador failure.** Add a declared axis aimed at
   the measured cause — a reservoir-level test, or excluding a buffer around
   permanent water from the flood channel. Cost: it contradicts the freeze,
   needs its own declaration, a full re-score, and it risks recall on genuine
   floods that really do abut permanent water, which is most river floods.

My recommendation, for what it is worth: **option 2.** The evidence that debris
should leave the flood extent now comes from four independent directions:
precision 0.0 against the only expert debris reference; 96% of all dev-set
debris on a dry control; a configuration that is better on both the objective
and the controls with debris removed; and the declared per-fold objective
selecting a debris-OUT point unanimously across all six folds, with every point
in every fold's tied set removing either the debris class or the optical deposit
channel. Option 2 is the smallest declared change that acts on that.

But it IS a rule change, and rule changes here belong to the human and to a
commit that precedes the run. Note also what option 2 would and would not fix:
it would not make the configuration feasible. Ecuador stays at 1.64% whatever is
adopted, so option 2 buys a better map and a 16-fold safer Bhutan control while
leaving the project still in breach of its own stricter criterion on one
control. Only option 3 could change that, and option 3 reopens detection.

### Two things `/audit` flagged that are recorded rather than fixed

- **`water_only` keeping the SAR debris channel is tested only in `darkening`
  mode** (`tests/test_detect_optical_fusion.py`). The reason is measured and is
  the finding itself: under the shipped `rough_water` the SAR debris channel
  produces nothing on the synthetic fixture, so there is no debris to keep and
  the test would be vacuous. It guards against that explicitly
  (`assert (bare.classes == CLASS_DEBRIS).sum() > 0`) and `darkening` is a
  declared config mode, not an event-specific branch. The property is therefore
  untested in the shipped mode, and closing that needs a fixture whose SAR
  debris fires under `rough_water` - which the dev set suggests is hard to build
  honestly, since 107 pixels on one of ten real events is all it produces.
- **The audited tree was edited mid-audit.** `/audit` ran against a moving
  `docs/tuning_log.md` and said so. Its PASS lines are statements about the tree
  at that moment. The lesson, adopted: commit before requesting an audit, so the
  audit has a frozen tree - the same discipline as declaring a rule before
  running the pass it governs.

### Flood mapping is frozen

No further detection change. The next modules read this map as it stands, and
`debris_in_flood_extent` is `True` in the shipped configuration, so the damage
overlay and the cut-off analysis will consume a flood extent that INCLUDES a
debris class of measured precision 0.0. That constraint must be carried with
every number they produce, and it is the single most important thing for a
reader of those modules to know.

---

## 2026-10-12 — DECLARED (after the refusal, and labelled as such): the Pareto-dominance rule

**This rule was written AFTER the stricter criterion found no feasible point.**
That is stated first because it is the thing a reader must weigh. Every other
rule in this log was fixed before the numbers it judges existed; this one was
not, and no amount of careful wording changes that. What can be said in its
favour is below, and so can what cannot.

The human declared it, in these words, and this entry is committed **before the
rule is implemented or run**:

> When no grid point passes both control ceilings, adopt the point that in every
> leave-one-event-out fold is at least as good as the incumbent on mean tolerant
> F1 and on every control's false-flood fraction (both the gated and the
> analysed-area one), and strictly better on at least one; ties go to fewer
> false alarms.

### What it says, precisely enough to implement

The rule fires **only** when the ordinary feasible-or-nothing selection has
already failed. If any point passes both ceilings, that selection stands and
this rule is never consulted.

A candidate point `P` is adopted in place of the incumbent `I` only if, **in
every one of the six leave-one-event-out folds**, with both points crossed with
that fold's own SAR point:

| quantity | requirement |
|---|---|
| mean boundary-tolerant F1 over that fold's **training** events | `P >= I` |
| `false_flood_fraction` on **every** control | `P <= I` |
| `false_flood_fraction_analysed_area` on **every** control | `P <= I` |

and `P` is strictly better than `I` on at least one of those quantities in at
least one fold. No quantity may be traded against another: a candidate that
gains F1 anywhere while worsening any control anywhere is rejected, in that
fold, and therefore altogether.

The F1 comparison is on each fold's **training** events, not its held-out one.
That is deliberate and it is the correction forced by `/audit` earlier today:
comparing held-out scores to choose between candidates is selection on held-out
data. The held-out numbers are reported afterwards, as the result.

Ties, in order: **fewer false alarms first** — the lowest worst-case
analysed-area false-flood fraction across the controls, then the lowest
worst-case gated fraction, then the smallest total flood-affected pixel count on
the controls. Only if candidates are identical on all three does grid order
decide, as everywhere else in this project.

### Why this is weaker evidence than the rules above it, stated plainly

- **It was written after the result.** A rule chosen with the candidate table in
  view can be shaped, consciously or not, to admit a particular candidate. The
  honest mitigation is not a claim of good faith but the rule's SHAPE: Pareto
  dominance has no free parameters. There is no threshold to tune, no weighting
  between F1 and false alarms, and no event that can be traded away. It either
  dominates everywhere or it is rejected.
- **It compares against the incumbent, not against an absolute standard.** So it
  can only ever move the project to a configuration that is better on every
  measured axis. It cannot license a trade-off, and it cannot license a
  regression.
- **It does not make anything feasible.** This must not be mistaken for passing
  the stricter criterion. Whatever is adopted, `ecuador_poza_honda_control_2024`
  stays at **1.64%** false flood over analysed ground against a declared 1%
  ceiling, because that control's false pixels are Sentinel-1 flood water and no
  point on the grid changes them. **The stricter criterion remains FAILED and
  that remains an open issue**, to be reported in the README, in the report and
  in Q&A for as long as it is true.

### How it is applied

Through `eval/tune.py`, not by hand: `dominance_candidates` and
`select_by_dominance`, run as part of `eval.tune --plan gates --optical`, which
first confirms that the feasible-or-nothing selection found nothing and only
then consults this rule. The incumbent is read from the shipped `config.py`
defaults rather than written down here, so the comparison is always against what
the project actually ships; the code asserts the incumbent is a point of the
declared grid. The per-fold comparison table for every candidate is written to
`eval/chosen_params.json` under `optical_pass.dominance`, so the verdict can be
checked rather than taken.

---

## 2026-10-12 — ADOPTED by the dominance rule: debris leaves the flood extent

The rule declared above was implemented in `eval/tune.py` and run. It found
**four** candidates that dominate the incumbent, all four tied on false alarms,
and grid order chose among them:

**ADOPTED: `optical.fusion = union`, `optical.mndwi_water = 0.0`,
`optical.ndvi_drop_min = 0.10`, `extent.debris_in_flood_extent = False`** —
`params_hash 0550873e7ea6`, `eval/chosen_params.json`, run_id
`20261012T165259Z`.

Strictly better than the incumbent on **24** of the compared quantities, worse
on **none**, in every one of the six folds. The four dominating candidates were
`union` 0.0/0.10 debris-OUT, `union` 0.0/0.20 debris-OUT, `water_only` 0.0
debris-IN and `water_only` 0.0 debris-OUT — every one of which removes either
the debris class from the extent or the optical deposit channel, or both, which
is the same direction from four independent starting points.

`config.py` now ships it: `ndvi_drop_min` 0.20 -> 0.10 and
`debris_in_flood_extent` True -> False. The two belong together. 0.20 was chosen
earlier the same day *while debris was still part of the extent*, when the
deposit channel's false positives counted against the score; with debris out of
the extent they cannot reach the scored quantity at all, so the threshold no
longer trades controls against floods and the looser value is simply better on
the floods. Splitting the two changes would reinstate the trade.

### The published numbers, after adoption

Leave-one-event-out, 20 m boundary tolerance, `n_pre=3`. Each row is the event
held out of parameter selection; `eval/results/flood.csv`, run_id
`20261012T165259Z`.

| event | P | R | **F1** | IoU | P tol | R tol | **F1 tol** | hash |
|---|---|---|---|---|---|---|---|---|
| `bhutan_punakha_2024` | 0.0805 | 0.0907 | **0.0853** | 0.0445 | 0.1259 | 0.1469 | **0.1356** | `0550873e7ea6` |
| `bosnia_konjic_2024` | 0.0385 | 0.0376 | **0.0380** | 0.0194 | 0.0674 | 0.0977 | **0.0798** | `0550873e7ea6` |
| `ecuador_poza_honda_2025` | 0.4321 | 0.3904 | **0.4102** | 0.2580 | 0.5789 | 0.5335 | **0.5553** | `d46da9ccd649` |
| `norway_ringebu_2023` | 0.7685 | 0.6988 | **0.7320** | 0.5772 | 0.9234 | 0.9070 | **0.9151** | `0550873e7ea6` |
| `norway_selsverket_2023` | 0.4803 | 0.4677 | **0.4739** | 0.3105 | 0.5558 | 0.6164 | **0.5845** | `0550873e7ea6` |
| `pakistan_larkana_2022` | 0.7899 | 0.8426 | **0.8154** | 0.6883 | 0.8617 | 0.9183 | **0.8891** | `f08e2edf2c4c` |
| **mean** | 0.4316 | 0.4213 | **0.4258** | 0.3163 | 0.5189 | 0.5366 | **0.5266** | |

Gate movement, incumbent -> adopted: mean tolerant F1 **0.5231 -> 0.5266**,
mean tolerant precision 0.5137 -> 0.5188, mean tolerant recall 0.5366 ->
0.5366 (unchanged), strict F1 0.4237 -> 0.4258, strict IoU 0.3152 -> 0.3163,
worst event 0.0789 -> 0.0798. Every one moved the right way or stood still.

Controls, both fractions, at the adopted configuration:

| control | gated | analysed-area | under 1%? |
|---|---|---|---|
| `bhutan_punakha_control_2024` | 0.0001 | 0.0028 | yes |
| `bosnia_konjic_control_2024` | 0.0011 | 0.0100 | yes (at the line) |
| `ecuador_poza_honda_control_2024` | 0.0048 | **0.0164** | **NO** |
| `norway_ringebu_control_2023` | 0.0014 | 0.0068 | yes |

Bhutan's analysed-area rate fell from **0.0437 to 0.0028**, a factor of 16, and
its gated rate from 0.0022 to 0.0001. That is the single largest effect of this
change and it is on a dry valley that should never have been called flooded.

### THE OPEN ISSUE, unchanged and to be reported for as long as it is true

**The stricter control criterion declared earlier today is still FAILED.**
`ecuador_poza_honda_control_2024` reads **1.64% false flood over analysed
ground** against a declared 1% ceiling. The dominance rule was never capable of
fixing it and did not: that control's 7,780 false pixels are Sentinel-1 flood
*water*, optical is refused there (12.7% clear) and its debris count is zero, so
all 42 configurations on the grid read exactly 0.0164. Adoption is an
improvement, not a resolution.

The gated control gate (`no_flood_controls`) PASSES at 0.0048 because its
denominator includes terrain-excluded ground; the project's own stricter reading
of the same pixels does not. Both numbers are published, and the README says the
shipped configuration fails the stricter one. Closing it needs the reservoir
hypothesis tested and probably a detection change, which is option 3 and
contradicts the freeze.

### What `/audit` caught in this round, and what it cost

The re-audit returned FAIL on two findings, both fixed here:

1. **The tree was not frozen.** I kept implementing while the audit ran — the
   third occurrence — so its PASS covered only `ce46ed6` and the entire
   dominance work was unaudited. Worse, it found that my dominance branch
   reconstructed per-fold Selections with `select(..., analysed_ceiling=1.0)`:
   **the escape hatch whose removal from production I had just declared and
   tested, used on the adoption path**. It is gone; the rows are now read
   straight from the matrix, because by that point the rule has already chosen
   and there is nothing left to select. `tests/test_eval_tune_optical.py` now
   parses `eval/tune.py` with `ast` and fails if any call lifts or disables that
   ceiling — the first version of that test used a regex and matched the
   docstring sentence explaining the hatch, which is its own small lesson about
   checking the check.
2. **The refusal breakdown attributed the 24/6 split to the wrong parameter.**
   Corrected above: the discriminating axis is `min_abs_change_db`, not
   `hand_max_water_m`, which splits 3/3 on each value and explains nothing.

Two further defects surfaced while fixing those, neither reported by the audit:

- **`eval.run_scoreboard` was not scoring the shipped configuration.** It built
  `DetectParams()` and `dev_run_params()` and passed no `OpticalParams` or
  `ExtentParams`, so after the default moved it scored a radar-only,
  debris-OUT configuration that no rule had adopted — and wrote ten rows for it
  (`6bd9957fb9b5`, run_id `20261012T165407Z`, superseded). It now passes the
  shipped `OpticalParams()` and `ExtentParams()` by default, prints both, and
  reports `params 0550873e7ea6`; `--no-optical` remains available. The stray
  rows stay, because the CSV is append-only, and are traceable to the declared
  radar grid crossed with the declared extent grid.
- **The hash-omission rule keyed off the LIVE default.** `params_dict` omitted
  the `extent` section when it equalled `ExtentParams()`, which works right up
  to the day the default moves — and then a configuration scoring a *different*
  quantity silently hashes like the rows already published, which is the exact
  failure the arrangement exists to prevent. It now compares against a fixed
  constant, `EXTENT_HASH_BASELINE = ExtentParams(debris_in_flood_extent=True)`,
  so the published hashes depend on what was measured rather than on what
  happens to be shipped. All six previously published hashes still reproduce;
  `tests/test_params_hash_reproducible.py` pins the constant and the reason.

The nine tests that broke when the default moved all broke for the right
reason, and all were corrected to assert the new truth rather than loosened:
`--no-optical` is radar-only in sensor terms but no longer the pre-extent
configuration and must not borrow its hash; the primary scored quantity is now
flood water only and the recorded sentence says so; the shipped-default pin now
accepts either endorsement route.

### Flood mapping is frozen, for good

No further detection change. What the next modules inherit, and must carry with
every number: the accuracy above, the narrow-valley caveat, the failed stricter
control criterion on Ecuador — and that `debris_in_flood_extent` is now
**False**, so the flood extent is flood water only. The debris layer is still
written and must be labelled *"possible debris, unverified"*; the damage overlay
and the cut-off analysis **must not consume it**.

---

## 2026-10-13 — Provenance of the adopted rows, and a record the tuner nearly erased

`/audit` objected, correctly, that the adopted gate rows were written by a dirty
tree: their `git_commit` column reads `2c8d8f4-dirty`, so no commit reproduced
them. Resolving that turned up two more things worth writing down.

### The rows do reproduce from the committed tree

The code is committed as `65d301e`. Every published pair was re-scored from
scratch against it and compared metric by metric:

**What is recorded, and therefore what may be claimed:** all 10 published rows
match their score files under `runs/eval/<event>/scores/<hash>.json` on every
metric, 0 mismatches — independently re-checked by `/audit`. The per-fold hashes
are `0550873e7ea6` for four events, `d46da9ccd649` for Poza Honda,
`f08e2edf2c4c` for Larkana, and `0550873e7ea6` for all four controls, and each
of those reproduces from the declared grids with today's code.

**What was ALSO run but is NOT recorded, and so is not claimed as a result:** a
re-score of those ten pairs from the committed tree, which came out identical on
every metric. `/audit` was right to reject it under Rule 3.8 — the number was in
a terminal, not in a file. It is not written to `eval/results/repro.csv` here
either, deliberately: that file feeds the `reproducibility` gate, the check I
ran compares *scored metrics* rather than output rasters byte for byte, and
flipping a 90% gate from NO DATA to PASS on the narrower of two readings is
exactly the kind of move this project does not make. It is listed as an open
item below and belongs to the reproducibility module's own step.

So the `-dirty` suffix records the state of the tree at the moment of writing,
which is what it is for. The suffix stays: rewriting history to make a label
tidy would be worse than the label.

### Every run_id in `eval/results/flood.csv`, accounted for

The file is append-only and now holds 60 rows under six run_ids. `/audit` found
two of them undisclosed, including the one the control gate actually reads, so
here is all six:

| run_id | rows | what it is | read by a gate? |
|---|---|---|---|
| `20261011T110254Z` | 10 | the radar-only adoption (`rough_water`) | superseded |
| `20261012T114247Z` | 10 | the optical-fusion adoption, debris still IN | superseded |
| `20261012T165149Z` | 10 | the dominance adoption — **first** of two identical writes, made while wiring the row-writing branch | superseded |
| `20261012T165259Z` | 10 | the dominance adoption, identical to the above. **The flood gate reads this one** (`split=loeo`) | **yes** |
| `20261012T165407Z` | 10 | a scoreboard run at a configuration nobody adopted (`6bd9957fb9b5`): the scoreboard did not yet consult optical. Superseded | no |
| `20261012T170414Z` | 10 | the scoreboard re-run at the SHIPPED configuration (`0550873e7ea6`, `split=full`). **The control gate reads this one**, since `no_flood_controls` filters on `kind` only and takes the latest run_id | **yes** |

The two identical dominance writes `165149Z` and `165259Z` are a free
reproducibility check and were left in place rather than removed, because the
file is append-only. Nothing was deleted or altered: every historical version of
`flood.csv` is an exact line-prefix of the current one, which `/audit` verified
across all seven commits.

### Grid order was load-bearing in the tie, and that is worth saying

`/audit` checked whether the declared tie-break could have been reverse-
engineered to pick the adopted point, and the answer is no — but not because the
tie-break decided. All four dominating candidates are **identical on all three**
of its components: worst analysed-area false flood 0.0164, worst gated 0.0048,
total flood-affected 427.68 ha on the controls. So no permutation or reweighting
of the tie-break could change the outcome, and **grid order alone chose**.

That matters in one direction: with the extent axis as the inner loop — fixed in
`ce46ed6`, before the rule was declared — index 3 wins. As an outer loop,
`water_only` debris-**IN** would have won instead, a materially different
configuration with a lower held-out mean (0.5252 against 0.5266). Grid order was
therefore load-bearing and was fixed in the same commit that produced the
scores, not before them. Nothing was lost by it: the adopted point is tied-best
on held-out F1 among the four, and every one of the four removes debris from the
extent or the deposit channel or both.

### Why the rows could NOT simply be re-emitted with a clean hash

The obvious fix - re-run the pass on the clean tree and let it append fresh rows
- does not work, for a reason that is itself a property worth recording. The
dominance rule compares candidates against **the shipped configuration**, and
the shipped configuration is now the adopted one. Nothing can be strictly better
than itself, so the rule correctly adopts nothing, writes no rows, and reports
`nothing (incumbent stands)`.

That is the right answer and a good sign: **the adopted point is a fixed point
of the rule.** Re-running cannot drift it, and a future candidate must beat the
shipped configuration on every quantity in every fold to displace it.

### The bug that nearly cost the record

That re-run **overwrote `eval/chosen_params.json`** with its own
adopted-nothing report - erasing the record of the adoption that the published
rows are keyed to. Caught by inspecting the diff, and restored from `65d301e`.

Nothing in the data was lost, but the failure mode is sharp: a well-intentioned
re-run, on a clean tree, for a good reason, silently replaces the only record
saying which configuration the published gate describes. The earlier refusal
pass avoided it only because I happened to pass `--chosen-out` to a different
file, which is luck, not design.

So the tuner now refuses it. `overwrite_refusal` blocks a write to the
chosen-parameters record when **this run adopted nothing and the existing record
documents an adoption**, naming the run_id it is protecting and pointing at
`--chosen-out` or the explicit `--force-chosen-out`. Two tests pin it, including
the exact scenario above. Verified by re-running the pass afterwards: the record
survived and no row was appended.

A pattern across the last two days, stated because it keeps recurring in
different clothes: **the dangerous operations here are not the scoring ones, they
are the recording ones.** A wrong number gets caught by a gate or an audit. A
lost or overwritten record leaves the numbers intact and makes them unexplainable,
which under Rule 3.8 is the same as not having them. The protections that have
had to be added - append-only results, a separate artefact for a refusal, the
`-dirty` marker, the extent hash baseline, and now this - are all of that one
kind.

---

## 2026-10-13 — OPEN ITEMS carried forward (flood mapping is frozen; these are not detection changes)

`/audit` returned FAIL on two findings and raised five more. All seven are
addressed in the commit carrying this entry except where listed below, which are
recorded here rather than fixed, by an explicit budget decision for this step:
apply the findings once, commit, and carry the remainder as open items instead
of opening another audit round.

**Fixed in this commit**

1. **README misstated the primary scored quantity** (FAIL). It still said
   "flood classes ∪ debris" nineteen lines above the paragraph explaining that
   debris had been taken out. Corrected to "flood extent = our flood classes
   only", with the reference side explicitly not following the change.
2. **An unsupported "10 of 10 byte-identical" claim** (FAIL). Scoped to what a
   file records; see the entry above and open item A.
3. **A hash/quantity collision left open at the keying site.** `build_matrix`
   keyed the radar matrix on `params_hash(dp, rp)` (the baseline extent) while
   `score_with_scene` had started resolving `ep=None` to the SHIPPED default and
   writing under that, so a cache miss after 2026-10-12 would have stored a
   debris-OUT score under `d4f9b1791c82` — the hash of the published debris-IN
   rows. The radar pass now passes `EXTENT_HASH_BASELINE` explicitly at both the
   key and the write. No published number was affected: no such score was ever
   written (`/audit` checked the mtimes).
4. **Two undisclosed run_ids**, including the one the control gate reads. Table
   above.
5. **The ceiling guard was narrower than its claim.** `analysed_ceiling` is now
   keyword-only, and the `ast` test rejects any constant looser than the
   declared ceiling and any non-constant, not just `1.0 / 1 / None`.
6. **Two docstrings still asserted the invariant the default move broke** —
   that a DEFAULT `ExtentParams` hashes like `None`. It does not, and must not.
7. **A measured number hardcoded into production output.** The "stays at 1.64%"
   sentence is now computed from the matrix by `binding_control_note`, so it
   corrects itself or disappears.

**Open items**

- **A. The reproducibility gate is still NO DATA, and the re-score check is not
  recorded.** The check I ran compares scored *metrics*; the gate's
  `identical_output` is better read as output rasters byte for byte. Writing the
  metric-level result to `eval/results/repro.csv` would flip a 90% gate from NO
  DATA to PASS on the narrower reading, which is not a trade this project makes.
  **To do, in the reproducibility module's own step:** run a byte-level check of
  `flood_classes.tif`, `flood_source.tif` and `run_report.json` across two runs
  of the same inputs, and record both readings.
- **B. The stricter control criterion still FAILS on
  `ecuador_poza_honda_control_2024`**: 1.64% false flood over analysed ground
  against a declared 1% ceiling, identical at all 42 configurations. Its 7,780
  false pixels are Sentinel-1 flood *water*; the reservoir hypothesis is
  untested. **This is the most important open item in flood mapping** and
  closing it needs a detection change, which the freeze forbids without a new
  declared decision.
- **C. Feasibility is tested on 4-decimal-rounded fractions.**
  `bosnia_konjic_control_2024` reads 0.0100 against a 0.01 ceiling and passes
  "at the line"; a true 0.01004 would round down and pass. No effect today,
  because Ecuador makes every point infeasible regardless. Fix by comparing
  unrounded values when the ceiling is next consulted.
- **D. `water_only` keeping the SAR debris channel is tested only in
  `darkening` mode**, because the shipped `rough_water` produces no SAR debris
  on the synthetic fixture — itself a measured finding (107 px across the whole
  dev set). The property is untested in the shipped mode.
- **E. Grid order is load-bearing in the dominance tie** (section above). A
  future declared grid should fix axis order *before* scores exist, as
  `TUNE_GRIDS` and `OPTICAL_TUNE_GRIDS` did and as the extent axis did not quite.

None of A–E changes a published number, a parameter, or a gate. Flood mapping
stays frozen.
