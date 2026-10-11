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
