"""SessionStart hook: prints a short status brief that Claude Code adds to its
context, so every session starts knowing the gate status, git state and
whether the held-out test is still sealed."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])


def run(args: list[str]) -> str:
    try:
        out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=60)
        return (out.stdout or out.stderr).strip()
    except Exception as err:  # never break session start
        return f"unavailable ({type(err).__name__})"


def main() -> None:
    gates = run([sys.executable, str(ROOT / "eval" / "check_gates.py"), "--brief"])
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    last = run(["git", "log", "-1", "--format=%h %s"])
    dirty = run(["git", "status", "--porcelain"])
    n_dirty = len([ln for ln in dirty.splitlines() if ln.strip()]) if "unavailable" not in dirty else "?"
    sealed = "SEALED (do not look for the EMSR927 reference)" if not (ROOT / ".heldout_unlocked").exists() \
        else "UNLOCKED by the human: only /heldout may use it"
    print("STRANDLINE SESSION BRIEF")
    print(f"- {gates}")
    print(f"- git: branch {branch}, last commit '{last}', {n_dirty} uncommitted file(s)")
    print(f"- held-out Trishuli test: {sealed}")
    print("- Rules: CLAUDE.md. Done = tests pass + module gates PASS (/gate) + auditor PASS (/audit).")


if __name__ == "__main__":
    main()
