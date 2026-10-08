---
name: gate
description: Check every Strandline accuracy gate (the 90% rules in eval/gates.yaml) and explain what is passing, failing or missing. Use before calling any module done.
argument-hint: "[module name, optional]"
---

Run the gate checker and report honestly. Do not change any code or data in this command.

1. Run `venv/Scripts/python.exe -m eval.check_gates`. If the human named a module ("$ARGUMENTS" is not empty), add `--module $ARGUMENTS`.
2. Show the table exactly as printed.
3. For every FAIL: give the measured value, the target, the run_id, and the most likely cause, using the per-event rows in eval/results/ and any error maps under runs/.
4. For every NO DATA: say which evaluation script must be built or run to produce it, and which columns it must write (see the comment block at the end of eval/gates.yaml).
5. If a HELD-OUT LEAK is printed, stop everything and report it first.
6. Finish with a one-line verdict per module: DONE or NOT DONE.

Never edit eval/gates.yaml (it is human-owned). If you believe a gate is wrong, explain why to the human.
