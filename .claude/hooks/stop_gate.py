"""Stop hook: Claude cannot finish a turn while the test suite is failing.

Runs the full pytest suite. On failure it blocks the stop once (exit 2) and
sends the failures back to Claude to fix. If Claude is already continuing
because of this hook (stop_hook_active), it lets the turn end but shows the
human a warning, so failing tests are never hidden.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
TIMEOUT_S = 540


def tail(text: str, n: int = 40) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--maxfail=10"],
            cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"systemMessage": f"Strandline: test suite took over {TIMEOUT_S}s and was not checked."}))
        sys.exit(0)
    except OSError as err:
        print(json.dumps({"systemMessage": f"Strandline: could not run tests ({err})."}))
        sys.exit(0)

    if proc.returncode in (0, 5):  # 5 = no tests collected
        sys.exit(0)

    output = tail(proc.stdout + "\n" + proc.stderr)
    summary = output.splitlines()[-1] if output else "tests failing"
    if data.get("stop_hook_active"):
        print(json.dumps({"systemMessage": f"Strandline: tests are STILL failing ({summary}). "
                                           "Do not trust 'done' until this is fixed."}))
        sys.exit(0)

    sys.stderr.write(
        "STRANDLINE STOP GATE: the test suite is failing, so this work is not done.\n\n"
        f"{output}\n\n"
        "Fix the cause (never delete, skip, xfail or weaken a test), rerun the suite, then finish. "
        "If the failure is outside your control, say so plainly to the human instead of hiding it.\n"
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
