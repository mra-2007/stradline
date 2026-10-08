---
name: audit
description: Run the independent accuracy-auditor on recent changes to catch rule violations, held-out leakage, metric gaming and unsupported claims before a module is called done.
argument-hint: "[commit, range or module, optional]"
---

Use the accuracy-auditor subagent to audit $ARGUMENTS (default: uncommitted changes plus the last commit).

When it returns:
1. Show its VERDICT and every finding exactly as written. Do not soften or summarise away a FAIL.
2. For each finding, propose the smallest fix.
3. Ask the human before applying fixes that change detection logic, parameters or evaluation code. Fixes to documentation wording or missing tests can be applied directly, followed by the test suite.
