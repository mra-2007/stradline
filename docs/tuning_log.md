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
