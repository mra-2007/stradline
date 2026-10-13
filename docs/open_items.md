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
