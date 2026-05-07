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

# Reuse timezone helper from report_summary
sys.path.insert(0, os.path.dirname(__file__))
from report_summary import load_events

MAX_REVIEW_EVENTS = 30


def _is_anthropic_format(base_url, fmt=None):
    if fmt and fmt.strip():
        return fmt.lower().strip() == "anthropic"
    return "anthropic" in base_url.lower()


def _extract_json_from_text(text):
    if not text:
        return None
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        brace = text.find('{', idx)
        if brace == -1:
            break
        try:
            result, _ = decoder.raw_decode(text, brace)
            return result
        except json.JSONDecodeError:
            idx = brace + 1
    return None


def _call_llm(base_url, api_key, model, prompt, fmt=None):
    import urllib.request

    is_anthropic = _is_anthropic_format(base_url, fmt)

    if is_anthropic:
        url = f"{base_url}/v1/messages"
        payload = json.dumps({
            "model": model,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        url = f"{base_url}/chat/completions"
        payload = json.dumps({
            "model": model,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

    req = urllib.request.Request(url, data=payload, headers=headers)
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read().decode())

    if is_anthropic:
        text = ""
        for item in data.get("content", []):
            if item.get("type") == "text":
                text = item.get("text", "")
                break
    else:
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")

    parsed = _extract_json_from_text(text)
    return parsed


def _get_llm_config():
    api_key = os.environ.get("WATCHDOG_LLM_API_KEY", "")
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "base_url": os.environ.get("WATCHDOG_LLM_BASE_URL", "https://api.anthropic.com"),
        "model": os.environ.get("WATCHDOG_LLM_MODEL", "claude-haiku-4-5-20251001"),
        "fmt": os.environ.get("WATCHDOG_LLM_FORMAT", ""),
        "api_key_2": os.environ.get("WATCHDOG_LLM_API_KEY_2", ""),
        "base_url_2": os.environ.get("WATCHDOG_LLM_BASE_URL_2", "https://api.anthropic.com"),
        "model_2": os.environ.get("WATCHDOG_LLM_MODEL_2", "claude-haiku-4-5-20251001"),
        "fmt_2": os.environ.get("WATCHDOG_LLM_FORMAT_2", ""),
    }


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

    try:
        result = _call_llm(config["base_url"], config["api_key"], config["model"], prompt, fmt=config["fmt"])
        if result:
            return result.get("verdict", "confirmed"), result.get("reason", "")
    except Exception:
        pass

    if config.get("api_key_2"):
        try:
            result = _call_llm(config["base_url_2"], config["api_key_2"], config["model_2"], prompt, fmt=config["fmt_2"])
            if result:
                return result.get("verdict", "confirmed"), result.get("reason", "")
        except Exception:
            pass

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

    try:
        result = _call_llm(config["base_url"], config["api_key"], config["model"], prompt, fmt=config["fmt"])
        if result:
            return result.get("verdict", "confirmed"), result.get("reason", "")
    except Exception:
        pass

    if config.get("api_key_2"):
        try:
            result = _call_llm(config["base_url_2"], config["api_key_2"], config["model_2"], prompt, fmt=config["fmt_2"])
            if result:
                return result.get("verdict", "confirmed"), result.get("reason", "")
        except Exception:
            pass

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
    idle_confirmed = sum(1 for r in idle_reviews if r["verdict"] == "confirmed")
    idle_reclassified = sum(1 for r in idle_reviews if r["verdict"].startswith("reclassified"))
    total_verdicted = stuck_confirmed + stuck_fp + idle_confirmed + idle_reclassified
    accuracy = (stuck_confirmed + idle_confirmed) / total_verdicted if total_verdicted > 0 else 0

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
            "idle_total": len(idle_reviews),
            "idle_confirmed": idle_confirmed,
            "idle_reclassified": idle_reclassified,
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
