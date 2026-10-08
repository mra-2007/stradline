---
name: heldout
description: FINAL DAY ONLY. Run the sealed Trishuli held-out test once, with frozen parameters, and compare against the EMSR927 reference maps.
argument-hint: "<folder with the EMSR927 reference files>"
disable-model-invocation: true
---

This is the held-out test. It runs once. The result is reported as it comes out.

Reference folder given by the human: $ARGUMENTS

1. Check that `.heldout_unlocked` exists in the repo root. If not, STOP: only the human can unlock this.
2. Check that the git working tree is clean. Record the commit hash and the parameters_hash. If the tree is dirty, STOP and ask the human to commit first.
3. Run `venv/Scripts/python.exe -m eval.check_gates` and record the status of every gate. Do not block on it.
4. Run the full pipeline on the case study (bbox and date from the `heldout` section of eval/gates.yaml) into `runs/heldout_<commit>`.
5. List the reference folder and identify the observed flood extent, debris or flash-flood layers and the area of interest. Run `validation.compare` against them (strict and boundary-tolerant, with and without debris), and the damage and cut-off comparisons if those modules exist, including the cut-off analysis on the reference extent to separate mapping error from network error.
6. Write `docs/heldout_results.md`: commit, parameters hash, scenes used, every metric, error maps, and an honest reading of where and why the system was wrong. Credit "European Union, Copernicus Emergency Management Service data".
7. Do not change any parameter, threshold or detection code after seeing these results. If something crashes, fix only the crash, rerun, and record the fix in the results file.
