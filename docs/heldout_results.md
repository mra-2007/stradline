# Held-out test: Trishuli corridor / EMSR927

Run once, through `/heldout`, 2026-10-14, after `.heldout_unlocked` was created by the human.
Reference data: **European Union, Copernicus Emergency Management Service data** (activation
EMSR927, Grading products, downloaded by the human to `C:/heldout/EMSR927`, outside the repo).

- Commit scored: `eb8b97c7fb5a5e87dedb7b6873080c021ac90616`
- Working tree: clean before and during the run
- Case-study bbox: `[85.10, 27.85, 85.45, 28.30]`, event date `2026-08-26` (`eval/gates.yaml` → `heldout`)
- Parameters hash (default `DetectParams`/`RunParams`/`OpticalParams`/`ExtentParams`, as the live
  system would run): `0550873e7ea6`
- Run directory: `runs/heldout_eb8b97c/`
- Scenes used: Sentinel-1 track 85, ascending. Pre-event: 2026-07-23, 2026-08-04, 2026-08-16.
  Post-event: 2026-08-28 (2-day latency from the event date — the closest available acquisition).
  Copernicus DEM GLO-30, resampled to 20 m.

## `/gate` at this commit (not the held-out score — the dev-event gates, reported for context)

```
flood_mapping      mean(precision_tolerant)       0.5188    >= 0.9  FAIL     6/3
flood_mapping      mean(recall_tolerant)          0.5366    >= 0.9  FAIL     6/3
flood_mapping      mean(f1_tolerant)              0.5266    >= 0.9  FAIL     6/3
flood_mapping      min(f1_tolerant)               0.0798    >= 0.8  FAIL     6/3
flood_mapping      mean(f1)                       0.4258    report  REPORT   6/3
flood_mapping      mean(iou)                      0.3163    report  REPORT   6/3
no_flood_controls  max(false_flood_fraction)      0.0048   <= 0.01  PASS     4/3
damage_overlay     mean(road_segment_f1)          0.3995    >= 0.9  FAIL     6/3
damage_overlay     mean(building_f1)              0.1808    >= 0.9  FAIL     6/3
cutoff_analysis    mean(settlement_f1)            0.3683    >= 0.9  FAIL     6/3
cutoff_analysis    min(settlement_f1)             0.0000    >= 0.8  FAIL     6/3
copilot            max(invented_number)           0.0000    <= 0.0  PASS     453/150
copilot            mean(correct)                  1.0000   >= 0.95  PASS     453/150
live_drills        mean(success)                       -    >= 0.9  NO DATA  0/10
reproducibility    min(identical_output)               -    >= 1.0  NO DATA  0/3
```

## What EMSR927 actually contains, honestly

The six AOI folders supplied are all **Grading** products. None of them ship a flood/water-extent
delineation layer. The only hazard footprint in `observedEventA` is tagged `event_type =
"6-Mass Movement"`, `obj_desc = "Landslide"` — i.e. a debris/mass-movement footprint, not standing
water. **This held-out test cannot score our flood-water detection against an EMS water layer,
because EMS did not map one for this activation.** What it *can* score is: our flood/debris extent
against EMS's debris/mass-movement polygons, and our damage/cut-off conclusions against EMS's own
building- and road-damage grading, inside the small areas EMS actually covers.

Four AOIs carry a `GRA_PRODUCT` (initial Grading) layer: AOI01 (Syapru Besi), AOI02 (Timure), AOI03
(Bidur), AOI05 (Phosretar) — 24 mass-movement polygons total. AOI03 and AOI05 also carry a later
`GRA_MONIT01` monitoring pass; those were **not** folded into the reference, to avoid mixing two
different observation dates into one count. Only the four `PRODUCT` layers, and their own
`areaOfInterestA` footprints, were used.

## 1. Flood/debris extent vs EMS debris polygons

Scored only inside the union of the four EMS AOI footprints intersected with our analysis domain
(1.57% of the case-study bbox — everywhere else, EMS gives no answer, so it is excluded rather than
scored as "no flood").

| | strict (0 px) | tolerant (1 px, ~20 m) |
|---|---|---|
| precision | 0.8837 | 0.8968 |
| recall | 0.0681 | 0.1485 |
| F1 | 0.1265 | 0.2548 |
| IoU | 0.0675 | — |

Identical with and without debris in our own extent definition (our detector did not classify any
pixels here as the separate debris class, so the two flags coincide).

- `false_flood_fraction` inside the domain: 0.0231
- tp 1413, fp 186, fn 19321 (strict)
- Full metrics: `runs/heldout_eb8b97c/validation_with_debris/metrics.json`,
  `validation_without_debris/metrics.json`; error maps alongside (green=agree, red=false alarm,
  blue=missed).

**Reading**: precision is high — where we do flag flood/debris, we usually land inside an EMS
polygon. Recall is very low: we miss most of the EMS debris footprint. The most likely cause is a
mismatch in what is being measured, not a mapping failure in the usual sense — our detector is
built to find standing water from SAR backscatter drop, and EMS's polygons here are dry landslide/
debris-flow scars, which do not present the same radar signature as flooded ground. This is an
honest limitation of comparing a water detector to a mass-movement reference, not evidence that the
flood extent itself (which EMS never delineated) is wrong.

## 2. Damage overlay vs EMS's own building/road damage grading

Same AOI-restricted domain as above. "Ours" = `damaged_buildings.geojson` / `cut_roads.geojson`
from this run, which flag OSM features touched by *our* flood/debris extent. "Reference" = the same
overlay method (`strandline.damage.damaged_buildings` / `cut_road_segments`) applied to the EMS
debris-polygon extent instead, over the identical pre-event OSM snapshot and road segmentation — so
the two sides differ only in which extent is doing the flagging (same methodology as
`eval/damage_eval.py`, restricted to where EMS has coverage). No tolerant variant exists for this
metric — it is an exact `@osmId` / `segment_id` match.

| | ours flagged | EMS flagged | tp | fp | fn | precision | recall | F1 |
|---|---|---|---|---|---|---|---|---|
| buildings | 468 | 2372 | 319 | 149 | 2053 | 0.6816 | 0.1345 | 0.2246 |
| road segments | 97 | 481 | 90 | 7 | 391 | 0.9278 | 0.1871 | 0.3114 |

EMS-only (missed) buildings: 2053. Our-only (extra) buildings: 149.
EMS-only (missed) road segments: 391. Our-only (extra) road segments: 7.

Full detail: `runs/heldout_eb8b97c/damage_vs_ems.json`.

**Reading**: same story as the extent score — where our extent does reach a building or road, it
is usually inside the EMS-damaged set (precision reasonably high, especially for roads), but we
miss the large majority of what EMS flagged, consistent with our extent under-covering the debris
footprint for the reasons above.

## 3. Cut-off settlement analysis vs EMS reference extent

**Not scored: the case run did not finish the cut-off stage before the 21:55 deadline set for this
test.** The flood map, OSM/damage overlay, and damage comparison above all completed and are final;
the network/cut-off stage (which runs a Monte Carlo sensitivity pass) was still in progress when the
deadline arrived. Per the held-out protocol this is reported as a gap, not estimated or guessed at:
there is no settlement_f1 number for the held-out case.

## 4. Live drills, reproducibility

NO DATA, as at `/gate` above — out of scope for this run.

## Honest summary

- EMSR927's Grading products for this activation contain **no flood-water reference layer at all**,
  only mass-movement/debris polygons — so the held-out test could not check our core water-extent
  claim against an independent water map, only against a debris-flow footprint that measures a
  different phenomenon.
- Against that debris reference, both the raw extent score and the damage-overlay consequence of it
  show the same pattern: high precision, low recall. The system is not inventing flood where EMS
  saw none; it is under-detecting the debris-flow footprint specifically, which is expected given
  the detector targets water backscatter, not landslide/debris-flow texture.
- Cut-off analysis could not be scored in the time available; this is recorded as missing, not as a
  pass or a fail.
- This matches the committed dev-event gate state at the same commit: flood_mapping and
  damage_overlay both FAIL there too, so nothing here is a surprise relative to already-reported
  numbers — the held-out run adds the specific EMSR927 evidence and the finding that this
  activation gives no water layer to check against.
