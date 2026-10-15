# Model results: Kuro Siwo U-Net vs. physics (2026-10-15)

One leave-one-event-out pass of `eval/tune.py --model-pass` (Rule 3.4: parameters/mode are never
chosen by looking at the held-out event), crossing `MODEL_TUNE_GRIDS` (`physics` 1 point, `model`
3 points over `t_gorge`, `fusion` 9 points over `t_gorge` x `fusion_weight`) with the already-
published SAR+optical point. Source: `eval/results/flood.csv`, run_id `20261015T041122Z`,
git_commit `f5389cc`. Checkpoint `models/strandline_kurosiwo_unet.pt`
sha256 `3c93497645eeaaf114fb530e6647cd2149d45acc038207fc010f2704163d2547` (gitignored, not in this
repo).

**Conclusion: the model did not beat physics on these gorge floods. `ModelParams.mode` stays
`physics`** (the shipped default, unchanged). Every one of the 5 clean folds' leave-one-out
selection landed on `mode=model` over `mode=fusion` (fusion never won a fold under the control
ceiling), and even that lost to the physics baseline on mean tolerant F1 (0.4064 vs 0.4541) and on
4 of 5 individual events.

## Per-event, out-of-sample (boundary-tolerant F1, strict F1 alongside per Rule 2)

| Event | Physics F1 (strict / tolerant) | Model F1 (strict / tolerant) |
|---|---|---|
| bhutan_punakha_2024 | 0.0853 / 0.1356 | 0.1121 / 0.1292 |
| bosnia_konjic_2024 | 0.0380 / 0.0798 | 0.0170 / 0.0403 |
| ecuador_poza_honda_2025 | 0.4102 / 0.5553 | 0.3719 / 0.4794 |
| norway_ringebu_2023 | 0.7320 / 0.9151 | 0.7234 / 0.8689 |
| norway_selsverket_2023 | 0.4739 / 0.5845 | 0.4026 / 0.5143 |
| **clean mean** | **0.3479 / 0.4541** | **0.3254 / 0.4064** |

Physics row: latest published radar+optical LOEO pass (run_id `20261012T165259Z`), same 5 events,
same metric. The model's only win anywhere in this table is strict F1 on `bhutan_punakha_2024`
(0.1121 vs 0.0853); physics wins every other strict and tolerant comparison, including both
clean means.

**Fusion:** searched (9 points/fold) but never selected by any fold's leave-one-out under the
control ceiling, so there is no honest out-of-sample fusion number to publish here — reporting an
in-sample fusion score would be exactly the Rule 3.4 violation this pass exists to avoid.

## Contaminated (scored, excluded from every mean and from `/gate`)

| Event | Physics F1 (strict / tolerant) | Model F1 (strict / tolerant) |
|---|---|---|
| pakistan_larkana_2022 | 0.8154 / 0.8891 | 0.7868 / 0.8787 |

`pakistan_larkana_2022` overlaps Kuro Siwo training actid `1111009` (`strandline.config.KS_TRAINED_EVENTS`).
Scored at the final model-pass point (never trained on by the tuner, never held out either), split
`loeo_contaminated` in `eval/results/flood.csv` — a gate input would be in-sample for the model and
is never used as one.

## No-flood controls (false-flood fraction, ceiling 1%)

| Control | False-flood fraction |
|---|---|
| bhutan_punakha_control_2024 | 0.0001 |
| bosnia_konjic_control_2024 | 0.0000 |
| ecuador_poza_honda_control_2024 | 0.0023 |
| norway_ringebu_control_2023 | 0.0004 |

Worst control **0.0023**, well inside the 0.01 ceiling. All four pass regardless of mode.

## Training data citation (Rule 1)

Kuro Siwo — Bountos, M.N. et al. (2024), *Kuro Siwo: 33 billion m² under the flood*, NeurIPS
Datasets and Benchmarks Track, CC BY 4.0 (`strandline.config.KUROSIWO_CITATION`).

## Honesty notes (Rule 6)

- These are the only model-pass numbers that exist; nothing here is rounded up or hidden.
- `physics` is not a fallback chosen for convenience — it is the measured winner.
- The 5 clean events are a small, Northern-Hemisphere/temperate-and-tropical-flash-flood sample;
  "did not beat physics" is a statement about this sample, not a universal claim about the model.
