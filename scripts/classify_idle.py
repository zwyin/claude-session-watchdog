"""Classify idle session state from tmux pane output.

Usage: classify_idle.py <session> [--llm|--llm-only|--keyword-only]

Default mode (--llm-only): all cases go to LLM, keyword as fallback on timeout.
  --llm:          keyword first, LLM only for ambiguous/unknown
  --keyword-only: keyword only, no LLM

Reads the last 50 effective lines (noise filtered) of the tmux pane.

Output (JSON):
  {"category": "decision_needed|task_complete|ambiguous|idle_unknown",
   "confidence": 0.0-1.0 or "high"/"low",
   "trigger": "key reasoning/evidence phrase or empty",
   "summary": "one-line context summary",
   "last_lines": "last 5 lines of pane content",
   "effective_content": "last 50 effective lines"}
"""

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from llm_utils import call_llm, get_llm_endpoints

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
]

# 排除：claude-yes wrapper 自动处理的简单权限确认
EXCLUDE_PATTERNS = [
    r'^accept edits on',
    r'^Allow',
    r'^\s*(Yes|No)\s*$',
]

# 噪音行：状态栏、分隔线、空行、提示符
_NOISE_PATTERNS = [
    re.compile(r'^[─═━\-]{3,}$'),            # 纯分隔线
    re.compile(r'^[┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬│║─═]+ *$'),     # box drawing
    re.compile(r'^\s*$'),                  # 空行
    re.compile(r'^❯\s*$'),                # 纯提示符
    re.compile(r'^(模型|输入|会话|目录):'),  # 状态栏
    re.compile(r'^⏵⏵'),                   # 状态指示
    re.compile(r'^\.{3,}$'),              # 省略号行
    re.compile(r'^─{10,}'),               # 长分隔线（可能有尾部内容）
]


def _strip_noise(lines):
    """Remove noise lines (separators, status bars, empty lines) from captured output."""
    clean = []
    for line in lines:
        if any(pat.search(line) for pat in _NOISE_PATTERNS):
            continue
        clean.append(line)
    return clean


def capture_last_lines(session, count=50):
    """Read tmux pane, strip noise, return last N meaningful lines."""
    raw_count = count * 6
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{raw_count}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        lines = []
        for line in result.stdout.split("\n"):
            line = line.replace("\xc2\xa0", " ").replace("\r", "").strip()
            if line:
                lines.append(line)
        return _strip_noise(lines)[-count:]
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


def classify_with_llm(lines):
    """Use LLM API (primary + fallback) for classification.
    Returns (category, summary, confidence, trigger) on success,
    or ("llm_timeout", ..., None, "") on failure, or None if no API key.
    """
    endpoints = get_llm_endpoints()
    if not endpoints:
        return None

    context = "\n".join(lines[-50:])
    prompt = f"""Below is the tail of a Claude Code session. The session has just become idle (paused at the ❯ prompt).

IMPORTANT: Focus ONLY on Claude's LAST message — the most recent output block before the idle prompt. Earlier interactions are completed and irrelevant.

Claude Code UI symbols you must understand:
- ✳ Running task… (Xm Xs · ↓ X.Xk tokens) = CURRENTLY EXECUTING, not idle/complete
- ✻ Worked for Xm Xs = phase completed, session transitioning
- ⏺ message = Claude's status update or action
- ⎿ output = tool/sub-agent output
- ✔ item = completed sub-task
- ◼ item = PENDING sub-task (if any ◼ remains, task is NOT complete)

Based on Claude's LAST message only, classify why the session is idle:
1. "decision_needed" — Claude's last message asks a question, proposes options, or needs human decision/approval
2. "task_complete" — Claude's last message reports work finished, all tasks done, waiting for review. Only if NO pending ◼ items remain and the last message explicitly says everything is done.
3. "idle_unknown" — Cannot determine from the last message (e.g. mid-execution with ✳, unclear state, mix of ✔ and ◼)

Reply in JSON only: {{"category": "...", "confidence": 0.0-1.0, "reasoning": "the most meaningful key phrase from Claude's LAST message that supports your classification (NOT a timing indicator like 'Baked for' or 'Worked for')", "summary": "one-line Chinese summary of Claude's LAST message"}}

Session output (last 50 effective lines, noise filtered):
{context}"""

    valid_categories = {"decision_needed", "task_complete", "idle_unknown"}
    for base_url, api_key, model, fmt in endpoints:
        try:
            parsed = call_llm(base_url, api_key, model, prompt, fmt=fmt)
            if parsed:
                cat = parsed.get("category", "")
                if cat not in valid_categories:
                    continue
                return (
                    cat,
                    parsed.get("summary", ""),
                    parsed.get("confidence"),
                    parsed.get("reasoning", ""),
                )
        except Exception:
            continue

    return "llm_timeout", "LLM 调用失败（主备均不可用）", None, ""


def main():
    if len(sys.argv) < 2:
        sys.exit(0)

    session = sys.argv[1]
    # Default to --llm-only if no classification flag specified
    has_classify_flag = any(a in sys.argv for a in ("--llm", "--llm-only", "--keyword-only"))
    if not has_classify_flag:
        sys.argv.append("--llm-only")

    lines = capture_last_lines(session)
    if not lines:
        sys.exit(0)

    # Step 1: keyword classification
    category, context_lines = classify_by_keywords(lines)

    summary = "; ".join(context_lines[:2]) if context_lines else ""
    confidence = 0.7
    reasoning = "; ".join(context_lines[:2]) if context_lines else ""

    # 关键字匹配 idle_unknown 时降低置信度
    if category == "idle_unknown":
        confidence = 0.3
        reasoning = "无关键字匹配"

    # Step 2: LLM classification
    # --llm: keyword pre-filter + LLM for ambiguous/unknown (legacy)
    # --llm-only: all cases go to LLM, skip keyword (default)
    # no flag: keyword only, no LLM
    use_llm = "--llm" in sys.argv or "--llm-only" in sys.argv
    llm_only = "--llm-only" in sys.argv

    if use_llm and (llm_only or category in ("ambiguous", "idle_unknown")):
        llm_result = classify_with_llm(lines)
        if llm_result and llm_result[0] and llm_result[0] != "llm_timeout":
            category = llm_result[0]
            summary = llm_result[1] or summary
            if len(llm_result) > 2 and llm_result[2] is not None:
                confidence = llm_result[2]
            if len(llm_result) > 3:
                reasoning = llm_result[3]
        elif llm_only:
            # LLM-only 模式超时时，用关键字结果兜底
            confidence = 0.3
            reasoning = "LLM 超时，关键字兜底"

    output = {
        "category": category,
        "confidence": confidence,
        "reasoning": reasoning,
        "summary": summary[:200],
        "last_lines": "\n".join(lines[-15:]),
        "effective_content": "\n".join(lines[-50:]),
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
