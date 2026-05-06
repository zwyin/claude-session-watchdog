"""Get the age (in seconds) since the last JSONL record for a tmux session.

Usage: jsonl_age.py <tmux_session_name>

Maps: tmux session → pane working dir → ~/.claude/projects/<encoded>/*.jsonl
Reads the last line of the most recently modified JSONL file, parses its
timestamp, and prints the age in seconds. Prints nothing on any failure.
"""

import json
import os
import glob
import re
import subprocess
import sys
from datetime import datetime, timezone


def main():
    if len(sys.argv) < 2:
        return

    try:
        session = sys.argv[1]

        # Get working directory from tmux session
        result = subprocess.run(
            ["tmux", "display-message", "-t", session, "-p", "#{pane_current_path}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return
        workdir = result.stdout.strip()
        if not workdir:
            return

        # Encode path to match Claude Code's project directory naming
        home = os.path.expanduser("~")
        rel = workdir.replace(home + "/", "").replace(home, "")
        encoded = re.sub(r"[^a-zA-Z0-9]+", "-", rel).strip("-")
        user = os.environ.get("USER", "")
        project_dir = os.path.join(home, ".claude", "projects",
                                  f"-{user.replace(os.sep, '-')}-{encoded}")

        # Fallback: scan projects dir for matching suffix
        if not os.path.isdir(project_dir):
            proj_base = os.path.join(home, ".claude", "projects")
            if os.path.isdir(proj_base):
                for d in os.listdir(proj_base):
                    if d.endswith("-" + encoded) or d.endswith(encoded):
                        candidate = os.path.join(proj_base, d)
                        if os.path.isdir(candidate):
                            project_dir = candidate
                            break

        if not os.path.isdir(project_dir):
            return

        # Find most recently modified .jsonl in project dir (non-recursive)
        jsonl_files = glob.glob(os.path.join(project_dir, "*.jsonl"))
        if not jsonl_files:
            return

        active = max(jsonl_files, key=os.path.getmtime)

        # Read last non-empty line
        with open(active, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size == 0:
                return
            fh.seek(max(0, size - 2048))
            tail = fh.read().decode("utf-8", errors="replace").strip()

        lines = [l for l in tail.split("\n") if l.strip()]
        if not lines:
            return

        last = json.loads(lines[-1])
        ts_str = last.get("timestamp", "")
        if not ts_str:
            return

        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        print(int(max(age, 0)))

    except (json.JSONDecodeError, KeyError, ValueError, OSError, subprocess.TimeoutExpired):
        pass


if __name__ == "__main__":
    main()
