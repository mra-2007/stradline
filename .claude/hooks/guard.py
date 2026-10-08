"""PreToolUse guard for Strandline (runs before every file edit, shell command,
file search and web fetch).

Blocks (exit 2, reason goes back to Claude):
  1. Changes to human-owned files: CLAUDE.md, eval/gates.yaml, tests/test_rules.py,
     .claude/ (rules, hooks, agents, skills) and the .heldout_unlocked marker.
  2. Any access to the held-out Trishuli reference (paths, commands or web
     requests mentioning EMSR927) until the human creates .heldout_unlocked.
"""

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
PROTECTED = ["claude.md", "eval/gates.yaml", "tests/test_rules.py", ".claude/", ".heldout_unlocked"]
HELDOUT_MARKERS = ["emsr927"]

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}
SHELL_TOOLS = {"Bash", "PowerShell"}
WEB_TOOLS = {"WebFetch", "WebSearch"}

# Shell patterns that can change a file. Harmless redirections are removed first.
HARMLESS_REDIRECTS = re.compile(r"\d?>&\d|\d?>\s*(/dev/null|nul|\$null)\b", re.I)
WRITE_HINTS = re.compile(
    r">|\btee\b|\bsed\b[^|;&]*\s-i|\brm\b|\bmv\b|\bcp\b|\bdel\b|\berase\b|\bmove\b|\bcopy\b|\bren\b|"
    r"\brename\b|\btruncate\b|\bdd\b|\bln\b|\bchmod\b|\bchown\b|\battrib\b|\btouch\b|\bunlink\b|"
    r"\binstall\b|\bpatch\b|\bperl\b|\bpython\S*\s+-c\b|\bnode\s+-e\b|\bruby\s+-e\b|\bawk\b[^|;&]*-i|"
    r"Set-Content|Add-Content|Out-File|Remove-Item|Move-Item|Copy-Item|Rename-Item|New-Item|Clear-Content|"
    r"\bgit\s+(checkout|restore|rm|mv|apply|stash|reset|clean|update-index)\b",
    re.I,
)


def norm(text: str) -> str:
    return str(text).replace("\\", "/").lower()


def rel_path(path: str) -> str:
    p = norm(path)
    root = norm(ROOT).rstrip("/")
    if root and p.startswith(root + "/"):
        p = p[len(root) + 1:]
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def is_protected(path: str) -> str | None:
    p = rel_path(path)
    for prot in PROTECTED:
        if prot.endswith("/"):
            if p.startswith(prot) or f"/{prot}" in f"/{p}":
                return prot
        elif p == prot or p.endswith(f"/{prot}"):
            return prot
    return None


def heldout_locked() -> bool:
    return not (ROOT / ".heldout_unlocked").exists()


def mentions_heldout(text: str) -> bool:
    low = norm(text)
    return any(m in low for m in HELDOUT_MARKERS)


def block(reason: str) -> None:
    sys.stderr.write(f"BLOCKED by Strandline rules: {reason}\n")
    sys.exit(2)


HELDOUT_REASON = ("the Trishuli/EMSR927 reference is the held-out test. It stays sealed until the human "
                  "creates .heldout_unlocked and runs /heldout on the final day. Do not look for it.")


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # never break the session on malformed input
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}

    if tool in EDIT_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        prot = is_protected(path)
        if prot:
            block(f"{path} is human-owned ({prot}). If you think it must change, explain why to the human.")
        if heldout_locked() and mentions_heldout(path):
            block(HELDOUT_REASON)

    elif tool in READ_TOOLS:
        if heldout_locked() and mentions_heldout(json.dumps(ti)):
            block(HELDOUT_REASON)

    elif tool in SHELL_TOOLS:
        cmd = str(ti.get("command", ""))
        low = norm(cmd)
        if ".heldout_unlocked" in low:
            block(".heldout_unlocked is created only by the human.")
        if heldout_locked() and mentions_heldout(cmd):
            block(HELDOUT_REASON)
        touched = [p for p in PROTECTED if p.rstrip("/") in low]
        if touched and WRITE_HINTS.search(HARMLESS_REDIRECTS.sub("", cmd)):
            block(f"this command could modify human-owned files ({', '.join(touched)}). "
                  "Read them with the Read tool instead; ask the human to make changes.")

    elif tool in WEB_TOOLS:
        if heldout_locked() and mentions_heldout(json.dumps(ti)):
            block(HELDOUT_REASON)

    sys.exit(0)


if __name__ == "__main__":
    main()
