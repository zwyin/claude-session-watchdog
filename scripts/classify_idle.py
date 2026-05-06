"""Classify idle session state from tmux pane output.

Usage: classify_idle.py <session> [--llm]

Reads the last 30 lines of the tmux pane, runs keyword matching to classify:
  - decision_needed: Claude is waiting for human input/decision
  - task_complete: Claude finished work, waiting for review
  - ambiguous: both patterns matched, needs human judgment
  - idle_unknown: no clear classification

With --llm flag and WATCHDOG_LLM_API_KEY env var set, ambiguous cases are
sent to an LLM for semantic analysis (with timeout/fallback).

Output (JSON):
  {"category": "decision_needed|task_complete|ambiguous|idle_unknown",
   "summary": "one-line context summary",
   "last_lines": "last 5 lines of pane content"}
"""

import json
import re
import subprocess
import sys
import os

# ── 关键字模式 ──────────────────────────────────────────────────────────────
# 决策类：Claude 在等用户做非 trivial 的判断（排除 accept edits 等 claude-yes 处理的）
DECISION_PATTERNS = [
    r'你觉得|你认为|你看',
    r'选.*方案|选择.*方式|你倾向',
    r'是否需要.*继续|是否需要.*调整|要不要.*改',
    r'确认一下|确认是否',
    r'我建议.*你觉得',
    r'需要你决定|需要你判断|需要你来',
    r'或者你有其他想法',
    r'should I .* or',
    r'which .*(?:approach|method|option).*prefer',
    r'what do you think',
    r'would you like me to',
    r'do you want me to',
]

# 完成类：Claude 工作结束，等人验收
COMPLETE_PATTERNS = [
    r'已经完成|已实现|已修改|已部署',
    r'功能.*完成|任务.*完成|全部.*完成',
    r'代码已提交|PR 已创建|PR created',
    r'请查看|请测试|请验证|请检查',
    r'I\'ve (?:completed|finished|done)',
    r'[Aa]ll changes.*(?:applied|made)',
    r'ready for review',
    r'⏵⏵',
]

# 排除：claude-yes wrapper 自动处理的简单权限确认
EXCLUDE_PATTERNS = [
    r'^accept edits on',
    r'^Allow',
    r'^\s*(Yes|No)\s*$',
]


def capture_last_lines(session, count=30):
    """Read the last N lines from a tmux session pane."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{count + 20}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        # Strip NBSP, filter empty lines
        lines = []
        for line in result.stdout.split("\n"):
            line = line.replace("\xc2\xa0", " ").strip()
            if line:
                lines.append(line)
        return lines[-count:]
    except (subprocess.TimeoutExpired, OSError):
        return []


def classify_by_keywords(lines):
    """Run keyword matching on lines, return (category, matched_context)."""
    text = "\n".join(lines)

    # Check exclude patterns first — skip simple permission prompts
    for pat in EXCLUDE_PATTERNS:
        if re.search(pat, text, re.MULTILINE):
            # If ONLY exclude patterns match, don't classify as decision
            pass

    decision_hits = []
    complete_hits = []

    for pat in DECISION_PATTERNS:
        m = re.search(pat, text)
        if m:
            # Get surrounding context (the line containing the match)
            for line in lines:
                if re.search(pat, line):
                    decision_hits.append(line[:120])

    for pat in COMPLETE_PATTERNS:
        m = re.search(pat, text)
        if m:
            for line in lines:
                if re.search(pat, line):
                    complete_hits.append(line[:120])

    has_decision = len(decision_hits) > 0
    has_complete = len(complete_hits) > 0

    if has_decision and has_complete:
        return "ambiguous", decision_hits + complete_hits
    elif has_decision:
        return "decision_needed", decision_hits[:3]
    elif has_complete:
        return "task_complete", complete_hits[:3]
    else:
        return "idle_unknown", lines[-3:]


def _call_llm(base_url, api_key, model, prompt):
    """Call a single LLM endpoint. Returns parsed (category, summary) or None."""
    import urllib.request

    url = f"{base_url}/v1/messages"
    payload = json.dumps({
        "model": model,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    req = urllib.request.Request(url, data=payload, headers=headers)
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read().decode())

    text = data.get("content", [{}])[0].get("text", "")
    m = re.search(r'\{[^}]+\}', text)
    if m:
        result = json.loads(m.group())
        return result.get("category"), result.get("summary", "")
    return None


def classify_with_llm(lines):
    """Use LLM API (primary + fallback) for ambiguous cases.
    Env vars:
      WATCHDOG_LLM_API_KEY / WATCHDOG_LLM_BASE_URL / WATCHDOG_LLM_MODEL  (primary)
      WATCHDOG_LLM_API_KEY_2 / WATCHDOG_LLM_BASE_URL_2 / WATCHDOG_LLM_MODEL_2  (fallback)
    """
    api_key = os.environ.get("WATCHDOG_LLM_API_KEY", "")
    if not api_key:
        return None

    base_url = os.environ.get("WATCHDOG_LLM_BASE_URL", "https://api.anthropic.com")
    model = os.environ.get("WATCHDOG_LLM_MODEL", "claude-haiku-4-5-20251001")

    context = "\n".join(lines[-20:])
    prompt = f"""Analyze this Claude Code session output (last 20 lines).
The session is at an idle prompt. Classify the state:

1. "decision_needed" — Claude asked the user a non-trivial question or needs human judgment
2. "task_complete" — Claude finished work and is waiting for user review/feedback
3. "idle_unknown" — Cannot determine, just idle

Reply in JSON only: {{"category": "...", "summary": "one-line Chinese summary"}}

Session output:
{context}"""

    # Primary endpoint
    try:
        result = _call_llm(base_url, api_key, model, prompt)
        if result:
            return result
    except Exception:
        pass

    # Fallback endpoint
    api_key_2 = os.environ.get("WATCHDOG_LLM_API_KEY_2", "")
    if api_key_2:
        base_url_2 = os.environ.get("WATCHDOG_LLM_BASE_URL_2", "https://api.anthropic.com")
        model_2 = os.environ.get("WATCHDOG_LLM_MODEL_2", "claude-haiku-4-5-20251001")
        try:
            result = _call_llm(base_url_2, api_key_2, model_2, prompt)
            if result:
                return result
        except Exception:
            pass

    return "llm_timeout", "LLM 调用失败（主备均不可用）"


def main():
    if len(sys.argv) < 2:
        sys.exit(0)

    session = sys.argv[1]
    use_llm = "--llm" in sys.argv

    lines = capture_last_lines(session)
    if not lines:
        sys.exit(0)

    # Step 1: keyword classification
    category, context_lines = classify_by_keywords(lines)

    summary = "; ".join(context_lines[:2]) if context_lines else ""

    # Step 2: LLM for ambiguous cases (if enabled and needed)
    if category == "ambiguous" and use_llm:
        llm_result = classify_with_llm(lines)
        if llm_result and llm_result[0]:
            category = llm_result[0]
            summary = llm_result[1] or summary

    output = {
        "category": category,
        "summary": summary[:200],
        "last_lines": "\n".join(lines[-5:]),
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
