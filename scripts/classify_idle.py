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

    # Check exclude patterns — simple permission prompts handled by claude-yes
    has_exclude = any(re.search(pat, text, re.MULTILINE) for pat in EXCLUDE_PATTERNS)

    decision_hits = []
    complete_hits = []

    for pat in DECISION_PATTERNS:
        for line in lines:
            if re.search(pat, line):
                decision_hits.append(line[:120])

    for pat in COMPLETE_PATTERNS:
        for line in lines:
            if re.search(pat, line):
                complete_hits.append(line[:120])

    has_decision = len(decision_hits) > 0
    has_complete = len(complete_hits) > 0

    # Only simple permission prompts (no real decision/complete) → unknown
    if not has_decision and not has_complete and has_exclude:
        return "idle_unknown", lines[-3:]

    if has_decision and has_complete:
        return "ambiguous", decision_hits + complete_hits
    elif has_decision:
        return "decision_needed", decision_hits[:3]
    elif has_complete:
        return "task_complete", complete_hits[:3]
    else:
        return "idle_unknown", lines[-3:]


def _is_anthropic_format(base_url, fmt=None):
    """判断 API 格式。优先使用显式 fmt 参数（'anthropic' 或 'openai'），否则从 base URL 推断。"""
    if fmt:
        return fmt.lower().strip() == "anthropic"
    return "anthropic" in base_url.lower()


def _extract_json_from_text(text):
    """从 LLM 回复中提取 JSON 对象。"""
    if not text:
        return None
    decoder = json.JSONDecoder()
    for i in range(len(text)):
        if text[i] == '{':
            try:
                result, _ = decoder.raw_decode(text, i)
                return result
            except json.JSONDecodeError:
                continue
    return None


def _call_llm(base_url, api_key, model, prompt, fmt=None):
    """调用单个 LLM 端点。自动识别 Anthropic/OpenAI 格式。返回 (category, summary) 或 None。"""
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

    # 提取回复文本
    if is_anthropic:
        # Anthropic 格式：content 是数组，可能有 thinking 和 text 多个 item
        text = ""
        for item in data.get("content", []):
            if item.get("type") == "text":
                text = item.get("text", "")
                break
    else:
        # OpenAI 格式：choices[0].message.content
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")

    parsed = _extract_json_from_text(text)
    if parsed:
        return parsed.get("category"), parsed.get("summary", "")
    return None


def classify_with_llm(lines):
    """Use LLM API (primary + fallback) for ambiguous cases.
    Env vars:
      WATCHDOG_LLM_API_KEY / WATCHDOG_LLM_BASE_URL / WATCHDOG_LLM_MODEL  (primary)
      WATCHDOG_LLM_API_KEY_2 / WATCHDOG_LLM_BASE_URL_2 / WATCHDOG_LLM_MODEL_2  (fallback)

    自动识别 Anthropic / OpenAI 兼容格式（根据 base URL 判断）。
    """
    api_key = os.environ.get("WATCHDOG_LLM_API_KEY", "")
    if not api_key:
        return None

    base_url = os.environ.get("WATCHDOG_LLM_BASE_URL", "https://api.anthropic.com")
    model = os.environ.get("WATCHDOG_LLM_MODEL", "claude-haiku-4-5-20251001")
    fmt = os.environ.get("WATCHDOG_LLM_FORMAT", "")

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
        result = _call_llm(base_url, api_key, model, prompt, fmt=fmt)
        if result:
            return result
    except Exception:
        pass

    # Fallback endpoint
    api_key_2 = os.environ.get("WATCHDOG_LLM_API_KEY_2", "")
    if api_key_2:
        base_url_2 = os.environ.get("WATCHDOG_LLM_BASE_URL_2", "https://api.anthropic.com")
        model_2 = os.environ.get("WATCHDOG_LLM_MODEL_2", "claude-haiku-4-5-20251001")
        fmt_2 = os.environ.get("WATCHDOG_LLM_FORMAT_2", "")
        try:
            result = _call_llm(base_url_2, api_key_2, model_2, prompt, fmt=fmt_2)
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
