"""Review watchdog events with LLM to detect false positives/negatives.

Usage: review_events.py <time_start> <time_end> [--events-file PATH]

Reads JSONL events from the given time range, sends each stuck/interrupt/idle
event to an LLM for re-evaluation. Reports confirmed vs false-positive verdicts.

Output (JSON):
{
  "period": {"start": "...", "end": "..."},
  "total_reviewed": N,
  "reviews": [
    {"timestamp": "...", "session": "...", "original_event": "stuck",
     "verdict": "confirmed|false_positive", "reason": "..."}
  ],
  "summary": {
    "stuck_total": N, "stuck_confirmed": N, "stuck_false_positive": N,
    "idle_total": N, "idle_confirmed": N, "idle_reclassified": N,
    "accuracy_rate": 0.xx
  }
}
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from report_summary import load_events
from llm_utils import call_llm, get_llm_endpoints

MAX_REVIEW_EVENTS = 30


def _get_llm_config():
    endpoints = get_llm_endpoints()
    if not endpoints:
        return None
    return {"endpoints": endpoints}


def review_stuck_event(event, config):
    """Ask LLM whether a stuck/interrupt event was truly stuck or a false positive."""
    notes = event.get("notes", "")
    session = event.get("session", "unknown")
    duration = event.get("duration_minutes", "?")

    prompt = f"""You are reviewing a watchdog system's stuck-session detection.

Event details:
- Session: {session}
- Detected state: {event["event"]}
- Duration stuck: {duration} minutes
- Notes: {notes}

Based on this information, was this session truly stuck (unresponsive/frozen/looping),
or was it a false positive (actively working but slow, e.g. long compilation, API call,
file processing)?

Consider:
- A session stuck for 10+ minutes with "hash unchanged" is likely truly stuck
- If notes mention "Ctrl-C + continue" and the session recovered, it was likely stuck
- Short durations (<5 min) are more likely false positives

Reply in JSON only: {{"verdict": "confirmed" or "false_positive", "reason": "one-line Chinese explanation"}}"""

    for base_url, api_key, model, fmt in config["endpoints"]:
        try:
            result = call_llm(base_url, api_key, model, prompt, fmt=fmt)
            if result:
                return result.get("verdict", "review_failed"), result.get("reason", "")
        except Exception:
            continue

    return "review_failed", "LLM 调用失败"


def review_idle_event(event, config):
    """Ask LLM to re-classify an idle event."""
    notes = event.get("notes", "")
    session = event.get("session", "unknown")
    duration = event.get("duration_minutes", "?")
    original = event.get("event", "")

    prompt = f"""You are reviewing a watchdog system's idle-session classification.

Event details:
- Session: {session}
- Original classification: {original}
- Idle duration: {duration} minutes
- Notes: {notes}

Re-classify this idle state:
- "decision_needed" — Claude was waiting for human input/decision
- "task_complete" — Claude finished work, waiting for review
- "idle_unknown" — Cannot determine, genuinely unknown

Reply in JSON only: {{"verdict": "confirmed" or "reclassified:decision_needed" or "reclassified:task_complete" or "reclassified:idle_unknown", "reason": "one-line Chinese explanation"}}"""

    for base_url, api_key, model, fmt in config["endpoints"]:
        try:
            result = call_llm(base_url, api_key, model, prompt, fmt=fmt)
            if result:
                return result.get("verdict", "review_failed"), result.get("reason", "")
        except Exception:
            continue

    return "review_failed", "LLM 调用失败"


def generate_review(events, config):
    """Review events and generate audit report."""
    stuck_events = [e for e in events if e.get("event") in ("stuck", "auto_interrupt")]
    idle_events = [e for e in events if "idle" in e.get("event", "")]

    # Limit to MAX_REVIEW_EVENTS, prefer recent
    review_stuck = stuck_events[-MAX_REVIEW_EVENTS:] if len(stuck_events) > MAX_REVIEW_EVENTS else stuck_events
    remaining = MAX_REVIEW_EVENTS - len(review_stuck)
    review_idle = idle_events[-remaining:] if len(idle_events) > remaining else idle_events

    reviews = []

    for e in review_stuck:
        verdict, reason = review_stuck_event(e, config)
        reviews.append({
            "timestamp": e.get("timestamp", ""),
            "session": e.get("session", ""),
            "original_event": e.get("event", ""),
            "verdict": verdict,
            "reason": reason,
        })

    for e in review_idle:
        verdict, reason = review_idle_event(e, config)
        reviews.append({
            "timestamp": e.get("timestamp", ""),
            "session": e.get("session", ""),
            "original_event": e.get("event", ""),
            "verdict": verdict,
            "reason": reason,
        })

    # Summary
    stuck_reviews = [r for r in reviews if r["original_event"] in ("stuck", "auto_interrupt")]
    idle_reviews = [r for r in reviews if "idle" in r["original_event"]]

    stuck_confirmed = sum(1 for r in stuck_reviews if r["verdict"] == "confirmed")
    stuck_fp = sum(1 for r in stuck_reviews if r["verdict"] == "false_positive")
    stuck_failed = sum(1 for r in stuck_reviews if r["verdict"] == "review_failed")
    idle_confirmed = sum(1 for r in idle_reviews if r["verdict"] == "confirmed")
    idle_reclassified = sum(1 for r in idle_reviews if r["verdict"].startswith("reclassified"))
    idle_failed = sum(1 for r in idle_reviews if r["verdict"] == "review_failed")
    total_verdicted = len(stuck_reviews) + len(idle_reviews)
    confirmed_total = stuck_confirmed + idle_confirmed
    accuracy = confirmed_total / total_verdicted if total_verdicted > 0 else 0

    return {
        "period": {
            "start": events[0].get("timestamp", "") if events else "",
            "end": events[-1].get("timestamp", "") if events else "",
            "total_events": len(events),
        },
        "total_reviewed": len(reviews),
        "reviews": reviews,
        "summary": {
            "stuck_total": len(stuck_reviews),
            "stuck_confirmed": stuck_confirmed,
            "stuck_false_positive": stuck_fp,
            "stuck_review_failed": stuck_failed,
            "idle_total": len(idle_reviews),
            "idle_confirmed": idle_confirmed,
            "idle_reclassified": idle_reclassified,
            "idle_review_failed": idle_failed,
            "accuracy_rate": round(accuracy, 2),
        },
    }


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: review_events.py <time_start> <time_end> [--events-file PATH]"}))
        sys.exit(1)

    time_start_str = sys.argv[1]
    time_end_str = sys.argv[2]
    events_file = os.path.expanduser("~/.claude/session-events.jsonl")
    for i, arg in enumerate(sys.argv[3:], 3):
        if arg == "--events-file" and i + 1 < len(sys.argv):
            events_file = sys.argv[i + 1]

    try:
        time_start = datetime.fromisoformat(time_start_str)
        time_end = datetime.fromisoformat(time_end_str)
    except ValueError:
        print(json.dumps({"error": f"invalid time format"}))
        sys.exit(1)

    events = load_events(events_file, time_start, time_end)
    if not events:
        print(json.dumps({"error": "no events in time range", "period": {"start": time_start_str, "end": time_end_str}}))
        sys.exit(0)

    config = _get_llm_config()
    if not config:
        print(json.dumps({"error": "WATCHDOG_LLM_API_KEY not set"}))
        sys.exit(1)

    report = generate_review(events, config)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
