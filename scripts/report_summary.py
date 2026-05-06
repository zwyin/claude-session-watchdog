"""Generate period summary report from session-events.jsonl.

Usage: report_summary.py <time_start> <time_end>

time_start/time_end: ISO format like "2026-05-07T08:00:00" (local time)

Reads JSONL events, filters by time range, outputs JSON:
{
  "total_events": N,
  "stuck": N, "auto_interrupt": N, "recovered": N,
  "idle_decision": N, "idle_task_complete": N, "idle_unknown": N,
  "avg_duration": N,
  "session_details": [
    {"session": "name", "stuck": N, "interrupt": N, "recovered": N, "idle": N}
  ],
  "session_count": N
}
"""

import json
import sys
from datetime import datetime
from collections import defaultdict


def load_events(events_file, time_start, time_end):
    """Load events from JSONL file, filter by [time_start, time_end)."""
    events = []
    try:
        with open(events_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = event.get("timestamp", "")
                if not ts_str:
                    continue
                # Parse UTC timestamp, compare in local context
                # JSONL stores UTC (e.g., "2026-05-06T14:23:00Z")
                # time_start/time_end are local time
                # Convert event timestamp to local for comparison
                try:
                    if ts_str.endswith("Z"):
                        ts_utc = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        ts_local = ts_utc.astimezone()
                    else:
                        ts_local = datetime.fromisoformat(ts_str)
                except (ValueError, TypeError):
                    continue
                if time_start <= ts_local < time_end:
                    events.append(event)
    except FileNotFoundError:
        pass
    return events


def generate_report(events, session_count=0):
    """Generate summary + per-session breakdown from events list."""
    type_counts = defaultdict(int)
    session_stats = defaultdict(lambda: defaultdict(int))
    durations = []

    for e in events:
        evt = e.get("event", "")
        sess = e.get("session", "unknown")
        type_counts[evt] += 1
        session_stats[sess][evt] += 1
        dur = e.get("duration_minutes")
        if dur and evt == "stuck":
            try:
                durations.append(int(dur))
            except (ValueError, TypeError):
                pass

    avg_duration = 0
    if durations:
        avg_duration = sum(durations) // len(durations)

    # Per-session detail lines
    details = []
    for sess in sorted(session_stats.keys()):
        s = session_stats[sess]
        details.append({
            "session": sess,
            "stuck": s.get("stuck", 0),
            "interrupt": s.get("auto_interrupt", 0),
            "recovered": s.get("recovered", 0),
            "idle": sum(v for k, v in s.items() if k.startswith("idle_")),
        })

    return {
        "total_events": len(events),
        "stuck": type_counts.get("stuck", 0),
        "auto_interrupt": type_counts.get("auto_interrupt", 0),
        "recovered": type_counts.get("recovered", 0),
        "idle_decision": type_counts.get("idle_idle_decision", 0) + type_counts.get("idle_decision", 0),
        "idle_task_complete": type_counts.get("idle_task_complete", 0),
        "idle_unknown": type_counts.get("idle_idle_unknown", 0) + type_counts.get("idle_unknown", 0),
        "avg_duration": avg_duration,
        "session_details": details,
        "session_count": session_count,
    }


def format_details_text(details):
    """Format session details as a compact text block for notifications."""
    lines = []
    for d in details:
        parts = [f"**{d['session']}**"]
        if d["stuck"]:
            parts.append(f"挂起 {d['stuck']}")
        if d["interrupt"]:
            parts.append(f"恢复 {d['interrupt']}")
        if d["recovered"]:
            parts.append(f"已恢复 {d['recovered']}")
        if d["idle"]:
            parts.append(f"空闲 {d['idle']}")
        if len(parts) == 1:
            parts.append("正常")
        lines.append(" · ".join(parts))
    return "\n".join(lines)


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: report_summary.py <time_start> <time_end>"}))
        sys.exit(1)

    time_start_str = sys.argv[1]
    time_end_str = sys.argv[2]
    events_file = sys.argv[3] if len(sys.argv) > 3 else (
        __import__("os").path.expanduser("~/.claude/session-events.jsonl")
    )

    try:
        time_start = datetime.fromisoformat(time_start_str)
        time_end = datetime.fromisoformat(time_end_str)
    except ValueError:
        print(json.dumps({"error": f"invalid time format: {time_start_str} or {time_end_str}"}))
        sys.exit(1)

    events = load_events(events_file, time_start, time_end)
    report = generate_report(events)

    # Add formatted details text
    report["details_text"] = format_details_text(report["session_details"])

    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
