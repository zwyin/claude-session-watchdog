#!/usr/bin/env bash
# Claude Code tmux session watchdog v2.0.0
# Monitors all tmux sessions running claude-yes/claude, detects stuck sessions,
# logs events, sends notifications, and auto-intervenes.
#
# Detection v2: hash-based + JSONL last record + output token stagnation
# Usage: ./watchdog.sh [start|stop|status|run|daemon|test-notify|daily-summary]

set -euo pipefail

# Load .env if present (for FEISHU_WEBHOOK / FEISHU_SECRET)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/../.env" ]; then
  # shellcheck disable=SC1090
  source "$SCRIPT_DIR/../.env"
fi

# ── Dependency check ────────────────────────────────────────────────────────
if ! command -v tmux &>/dev/null; then
  echo "ERROR: tmux is required but not found in PATH." >&2
  echo "  Install: brew install tmux (macOS) or apt install tmux (Linux)" >&2
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 is required but not found in PATH." >&2
  exit 1
fi

# ── Version ─────────────────────────────────────────────────────────────────
VERSION="2.0.0"

# ── Config ──────────────────────────────────────────────────────────────────
# All persistent state goes under ~/.claude/ to keep things centralized.
EVENTS_FILE="$HOME/.claude/session-events.jsonl"   # JSONL log of all stuck/recovered events
PID_FILE="$HOME/.claude/watchdog.pid"                # daemon PID for stop/status
LOCK_FILE="$HOME/.claude/watchdog.lock"              # prevents duplicate daemon instances
LOG_FILE="$HOME/.claude/watchdog.log"                # runtime log with timestamps
STATE_DIR="$HOME/.claude/watchdog-state"             # per-session state (hash, timestamps)

SAMPLE_INTERVAL=15         # seconds between samples
STUCK_THRESHOLD=600        # seconds unchanged → stuck event + notification (10 min)
INTERVENE_THRESHOLD=900    # seconds unchanged → auto Ctrl-C + continue (15 min)
INTERVENE_COOLDOWN=600     # seconds after intervention before next one (10 min)
DAILY_SUMMARY_HOUR=22      # hour to send daily summary (22:00)
JSONL_STALE_THRESHOLD=600  # seconds since last JSONL record → consider stale (10 min)

# Feishu webhook notification (set via env or .env file)
FEISHU_WEBHOOK="${FEISHU_WEBHOOK:-}"
FEISHU_SECRET="${FEISHU_SECRET:-}"

# ── Logging ─────────────────────────────────────────────────────────────────
# Dual output: append to log file AND print to stdout (so launchd captures it).
log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ── State management ────────────────────────────────────────────────────────
# Each session gets a flat file per key under STATE_DIR (e.g., gps.hash, gps.unchanged_since).
# This avoids needing a database and is easy to debug with cat/rm.
init_state() {
  mkdir -p "$STATE_DIR"
}

get_state() {
  local session="$1" key="$2"
  local f="$STATE_DIR/${session}.${key}"
  if [ -f "$f" ]; then
    cat "$f"
  fi
}

set_state() {
  local session="$1" key="$2" val="$3"
  echo -n "$val" > "$STATE_DIR/${session}.${key}"
}

clear_state() {
  local session="$1"
  rm -f "$STATE_DIR/${session}."*
}

# ── Event logging ───────────────────────────────────────────────────────────
# Appends a single JSON line per event. Fields match 04-monitoring-plan.md spec.
log_event() {
  local event="$1" session="$2" duration="$3" notes="${4:-}"
  local intervention="${5:-none}"
  mkdir -p "$(dirname "$EVENTS_FILE")"
  # Determine recovered status: only "recovered" events have recovered=true
  local recovered_val="false"
  if [ "$event" = "recovered" ]; then
    recovered_val="true"
  fi
  printf '{"timestamp":"%s","event":"%s","session":"%s","project":"%s","duration_minutes":%s,"model":"GLM-5.1","phase":"unknown","intervention":"%s","recovered":%s,"notes":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$event" \
    "$session" \
    "$session" \
    "$duration" \
    "$intervention" \
    "$recovered_val" \
    "$notes" \
    >> "$EVENTS_FILE"
}

# ── Template engine ─────────────────────────────────────────────────────────
# SCRIPT_DIR is already set at the top (line 12) for .env loading; reuse it here.
TEMPLATE_FILE="$SCRIPT_DIR/notify-templates.json"

# Render template and send notification in one step (avoids bash parsing issues)
# Usage: notify_from_template "stuck" "session=gps" "duration=12" ...
notify_from_template() {
  local section="$1"
  shift
  local vars=""
  for arg in "$@"; do
    vars="$vars\n$arg"
  done

  # Write vars to temp file to avoid escaping issues
  local tmpvars
  tmpvars=$(mktemp)
  echo -e "$vars" > "$tmpvars"

  python3 -c "
import json, hmac, hashlib, base64, time, urllib.request, urllib.parse

with open('$TEMPLATE_FILE', 'r') as f:
    templates = json.load(f)
tpl = templates.get('$section', {})
title_tpl = tpl.get('title', '')
color = tpl.get('color', 'blue')
body_tpl = tpl.get('body', '')

variables = {}
with open('$tmpvars', 'r') as f:
    for line in f:
        line = line.strip()
        if '=' in line:
            k, v = line.split('=', 1)
            variables[k.strip()] = v.strip()

for k, v in variables.items():
    title_tpl = title_tpl.replace('{' + k + '}', v)
    body_tpl = body_tpl.replace('{' + k + '}', v)

print(f'RENDERED: color={color} title={title_tpl}')

webhook = '$FEISHU_WEBHOOK'
secret = '$FEISHU_SECRET'
if not webhook or not secret:
    print('NOTIFY_SKIP: no feishu config')
else:
    ts = str(int(time.time()))
    string_to_sign = f'{ts}\n{secret}'
    sign = base64.b64encode(hmac.new(string_to_sign.encode('utf-8'), digestmod=hashlib.sha256).digest()).decode('utf-8')
    url = f'{webhook}?timestamp={ts}&sign={urllib.parse.quote(sign)}'
    payload = json.dumps({
        'msg_type': 'interactive',
        'card': {
            'header': {'title': {'tag': 'plain_text', 'content': title_tpl}, 'template': color},
            'elements': [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': body_tpl}}]
        }
    })
    req = urllib.request.Request(url, data=payload.encode('utf-8'), headers={'Content-Type': 'application/json'})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        if data.get('code') == 0:
            print(f'NOTIFY_OK: {title_tpl}')
        else:
            print(f'NOTIFY_FAIL: {data}')
    except Exception as e:
        print(f'NOTIFY_ERROR: {e}')
" 2>&1 | while IFS= read -r line; do
    log "$line"
  done

  rm -f "$tmpvars"
}

# ── Session context helpers ─────────────────────────────────────────────────
# Extract human-readable status from tmux pane for notification context.
get_session_workdir() {
  local session="$1"
  tmux display-message -t "$session" -p '#{pane_current_path}' 2>/dev/null | sed "s|$HOME|~|" || echo "unknown"
}

# Read the bottom 12 lines and filter for Claude Code status keywords.
get_session_status_line() {
  local session="$1"
  python3 -c "
import subprocess
try:
    result = subprocess.run(['tmux', 'capture-pane', '-t', '$session', '-p', '-S', '-12'],
                            capture_output=True, text=True, timeout=5)
    lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
    status_keywords = ['模型:', '输入:', '会话:', '目录:', '⏵⏵', '────']
    status = [l for l in lines if any(k in l for k in status_keywords)]
    for l in status[-5:]:
        print(l)
except Exception:
    pass
" 2>/dev/null
}

# Read the bottom 80 lines, strip status bar lines and idle prompts.
get_session_last_lines() {
  local session="$1"
  python3 -c "
import subprocess
try:
    result = subprocess.run(['tmux', 'capture-pane', '-t', '$session', '-p', '-S', '-80'],
                            capture_output=True, text=True, timeout=5)
    lines = result.stdout.strip().split('\n')
    status_keywords = ['模型:', '输入:', '会话:', '目录:', '────', '⏵⏵']
    output = [l.rstrip() for l in lines
              if l.strip()
              and not any(k in l for k in status_keywords)
              and l.strip() != '❯']
    for l in output[-8:]:
        print(l)
except Exception:
    pass
" 2>/dev/null
}

notify_stuck() {
  local session="$1" duration="$2"
  local status_line last_lines date_str time_str
  status_line=$(get_session_status_line "$session")
  last_lines=$(get_session_last_lines "$session")
  date_str=$(date '+%Y-%m-%d')
  time_str=$(date '+%H:%M:%S')

  osascript -e "display notification \"$session unresponsive ${duration}min\" with title \"Watchdog: hang\"" 2>/dev/null || true
  notify_from_template "stuck" \
    "session=$session" "duration=$duration" "date=$date_str" "time=$time_str" \
    "status_line=$status_line" "last_output=$last_lines"
}

notify_intervene() {
  local session="$1" duration="$2"
  local status_line last_lines date_str time_str
  status_line=$(get_session_status_line "$session")
  last_lines=$(get_session_last_lines "$session")
  date_str=$(date '+%Y-%m-%d')
  time_str=$(date '+%H:%M:%S')

  osascript -e "display notification \"$session auto-recovered\" with title \"Watchdog: recovery\"" 2>/dev/null || true
  notify_from_template "intervene" \
    "session=$session" "duration=$duration" "date=$date_str" "time=$time_str" \
    "status_line=$status_line" "last_output=$last_lines" \
    "action=Ctrl-C + resume task"
}

notify_recovered() {
  local session="$1" duration="$2"
  local date_str time_str
  date_str=$(date '+%Y-%m-%d')
  time_str=$(date '+%H:%M:%S')

  osascript -e "display notification \"$session resumed\" with title \"Watchdog: resumed\"" 2>/dev/null || true
  notify_from_template "recovered" \
    "session=$session" "duration=$duration" "date=$date_str" "time=$time_str"
}

notify_daemon_start() {
  local count="$1"
  local date_str time_str
  date_str=$(date '+%Y-%m-%d')
  time_str=$(date '+%H:%M:%S')

  notify_from_template "start" \
    "session_count=$count" "date=$date_str" "time=$time_str" \
    "version=$VERSION"
}

# ── Daily summary ───────────────────────────────────────────────────────────
send_daily_summary() {
  local today total total_stuck total_interrupt total_recovered avg_dur
  today=$(date '+%Y-%m-%d')
  total=$(wc -l < "$EVENTS_FILE" 2>/dev/null || echo 0)
  total_stuck=$(grep -c '"event":"stuck"' "$EVENTS_FILE" 2>/dev/null || echo 0)
  total_interrupt=$(grep -c '"event":"auto_interrupt"' "$EVENTS_FILE" 2>/dev/null || echo 0)
  total_recovered=$(grep -c '"event":"recovered"' "$EVENTS_FILE" 2>/dev/null || echo 0)

  local today_events today_stuck today_interrupt today_recovered
  today_events=$(grep "$today" "$EVENTS_FILE" 2>/dev/null | wc -l | tr -d ' ' || echo 0)
  today_stuck=$(grep "$today" "$EVENTS_FILE" 2>/dev/null | grep -c '"event":"stuck"' || echo 0)
  today_interrupt=$(grep "$today" "$EVENTS_FILE" 2>/dev/null | grep -c '"event":"auto_interrupt"' || echo 0)
  today_recovered=$(grep "$today" "$EVENTS_FILE" 2>/dev/null | grep -c '"event":"recovered"' || echo 0)

  local avg_min="0"
  if [ "$today_stuck" -gt 0 ]; then
    avg_min=$(grep "$today" "$EVENTS_FILE" 2>/dev/null | grep '"event":"stuck"' | python3 -c "
import sys, json
durations = [json.loads(l).get('duration_minutes', 0) for l in sys.stdin if l.strip()]
print(f'{sum(durations)/len(durations):.0f}' if durations else '0')
" 2>/dev/null || echo 0)
  fi

  local active_count
  active_count=$(get_claude_sessions | wc -l | tr -d ' ')

  local rendered
  notify_from_template "daily" \
    "date=$today" "today_events=$today_events" \
    "today_stuck=$today_stuck" "today_interrupt=$today_interrupt" \
    "today_recovered=$today_recovered" "avg_duration=$avg_min" \
    "total_events=$total" "total_stuck=$total_stuck" \
    "total_interrupt=$total_interrupt" "total_recovered=$total_recovered" \
    "session_count=$active_count"

  log "DAILY SUMMARY sent: today=${today_events} events, total=${total} events"
}

# ── Detect claude sessions ──────────────────────────────────────────────────
# Find tmux sessions whose child process is claude-yes/agent-yes/claude.
get_claude_sessions() {
  for s in $(tmux list-sessions -F '#{session_name}' 2>/dev/null); do
    local pane_pid
    pane_pid=$(tmux list-panes -t "$s" -F '#{pane_pid}' 2>/dev/null | head -1)
    if ps -o pid,ppid,command 2>/dev/null | grep -E "agent-yes|claude-yes|/claude$" | grep -v grep | awk -v ppid="$pane_pid" '$2 == ppid {found=1; exit} END {exit !found}' 2>/dev/null; then
      echo "$s"
    fi
  done
}

# ── Capture pane hash (strips timer patterns to avoid false negatives) ──────
# Timer spinners (e.g. "2m 15s") and timestamps would cause hash to change
# even when the session is genuinely stuck. We normalize these before hashing.
get_pane_hash() {
  local session="$1"
  tmux capture-pane -t "$session" -p -S -50 2>/dev/null \
    | tail -20 \
    | sed -E 's/[0-9]+m [0-9]+s/TIMER/g; s/[0-9]+m[0-9]+s/TIMER/g; s/[0-9]+:[0-9]+(am|pm)?/TIME/g' \
    | md5 2>/dev/null || echo ""
}

# ── JSONL last record age (seconds) ────────────────────────────────────────
# Returns age in seconds since the last JSONL entry, or empty on failure.
# Maps: tmux session → pane working dir → ~/.claude/projects/<encoded-path>/*.jsonl
# All errors are silently swallowed — this is a best-effort signal.
get_jsonl_age_seconds() {
  local session="$1"
  python3 -c "
import json, os, time, glob, sys

try:
    # Map tmux session → working directory → project dir
    import subprocess
    result = subprocess.run(
        ['tmux', 'display-message', '-t', '$session', '-p', '#{pane_current_path}'],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode != 0:
        sys.exit(0)
    workdir = result.stdout.strip()
    if not workdir:
        sys.exit(0)

    # Encode path: strip HOME prefix, replace all non-alnum with dashes, collapse
    home = os.path.expanduser('~')
    rel = workdir.replace(home + '/', '').replace(home, '')
    import re
    encoded = re.sub(r'[^a-zA-Z0-9]+', '-', rel).strip('-')
    import platform
    user = os.environ.get('USER', '')
    project_dir = os.path.join(home, '.claude', 'projects', f'-{user.replace(os.sep, "-")}-{encoded}')
    # Fallback: try listing projects dir and match by encoded suffix
    if not os.path.isdir(project_dir):
        proj_base = os.path.join(home, '.claude', 'projects')
        if os.path.isdir(proj_base):
            for d in os.listdir(proj_base):
                if d.endswith('-' + encoded) or d.endswith(encoded):
                    candidate = os.path.join(proj_base, d)
                    if os.path.isdir(candidate):
                        project_dir = candidate
                        break

    if not os.path.isdir(project_dir):
        sys.exit(0)

    # Find most recently modified .jsonl (not in subagents/)
    jsonl_files = [
        f for f in glob.glob(os.path.join(project_dir, '*.jsonl'))
        if '/subagents/' not in f
    ]
    if not jsonl_files:
        sys.exit(0)

    active = max(jsonl_files, key=os.path.getmtime)

    # Read last non-empty line
    with open(active, 'rb') as fh:
        fh.seek(0, 2)
        size = fh.tell()
        if size == 0:
            sys.exit(0)
        # Read last 2KB (enough for one JSON line)
        fh.seek(max(0, size - 2048))
        tail = fh.read().decode('utf-8', errors='replace').strip()

    lines = [l for l in tail.split('\n') if l.strip()]
    if not lines:
        sys.exit(0)

    last = json.loads(lines[-1])
    ts_str = last.get('timestamp', '')
    if not ts_str:
        sys.exit(0)

    # Parse ISO timestamp
    from datetime import datetime, timezone
    ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age < 0:
        age = 0
    print(int(age))
except (json.JSONDecodeError, KeyError, ValueError, OSError, subprocess.TimeoutExpired):
    pass
" 2>/dev/null
}

# ── Output token count from status line ────────────────────────────────────
# Extracts the numeric value of the 输出: field (e.g. "228.9k" → "228.9k").
# Used for stagnation detection: if output tokens don't change, the model
# isn't producing new content even if the screen hash is changing (timer).
get_output_tokens() {
  local session="$1"
  python3 -c "
import subprocess, re, sys
try:
    result = subprocess.run(
        ['tmux', 'capture-pane', '-t', '$session', '-p', '-S', '-8'],
        capture_output=True, text=True, timeout=5
    )
    for line in result.stdout.split('\n'):
        m = re.search(r'输出:\s*([0-9.]+[km]?)', line)
        if m:
            print(m.group(1))
            break
except Exception:
    pass
" 2>/dev/null
}

# ── Check if session is idle ───────────────────────────────────────────────
# Idle sessions (at the ❯ prompt) should not trigger stuck detection.
# We check for known idle indicators in the last few lines of the pane.
is_idle_prompt() {
  local session="$1"
  local last_lines
  last_lines=$(tmux capture-pane -t "$session" -p -S -10 2>/dev/null | tail -8)
  if echo "$last_lines" | grep -qE '(^❯|^\s*❯|accept edits on|\[超时\]|⏵⏵|Esc to cancel|waiting for input)'; then
    return 0
  fi
  local last_line
  last_line=$(tmux capture-pane -t "$session" -p 2>/dev/null | tail -1)
  if echo "$last_line" | grep -qE '^\s*目录:'; then
    return 0
  fi
  return 1
}

# ── Intervene: Ctrl-C + continue ───────────────────────────────────────────
# The core auto-recovery logic. Sends Escape then Ctrl-C to break any pending
# API request, waits for the prompt to return, then sends a continuation message.
# This mirrors what a human would do manually — no process killing.
intervene() {
  local session="$1" duration="$2"
  log "INTERVENE: $session stuck ${duration}s, sending Ctrl-C + continue"

  tmux send-keys -t "$session" Escape 2>/dev/null || true
  sleep 0.3
  tmux send-keys -t "$session" C-c 2>/dev/null || true
  sleep 3

  tmux send-keys -t "$session" -l "继续刚才的任务。如果当前方案卡住了，把任务拆小再执行" 2>/dev/null || true
  sleep 0.5
  tmux send-keys -t "$session" Enter 2>/dev/null || true

  log_event "auto_interrupt" "$session" "$((duration / 60))" "auto Ctrl-C + continue after ${duration}s stuck" "auto_watchdog"
  notify_intervene "$session" "$((duration / 60))"

  set_state "$session" "last_intervene" "$(date +%s)"
  set_state "$session" "unchanged_since" ""
}

# ── Main monitoring loop (v2: hash + JSONL + token stagnation) ──────────────
# Three-signal combined detection:
#   Path A: screen hash unchanged → classic stuck detection
#   Path B: hash changing BUT JSONL stale AND tokens stagnant → "deep stuck"
#           (e.g. timer spinner updating the screen while API is hung)
do_check() {
  init_state
  local now
  now=$(date +%s)

  # Daily summary at configured hour
  local current_hour
  current_hour=$(date '+%H')
  local last_summary
  last_summary=$(get_state "_global" "last_summary_date")
  local today
  today=$(date '+%Y-%m-%d')
  if [ "$current_hour" = "$DAILY_SUMMARY_HOUR" ] && [ "$last_summary" != "$today" ]; then
    send_daily_summary
    set_state "_global" "last_summary_date" "$today"
  fi

  local sessions
  sessions=$(get_claude_sessions)

  if [ -z "$sessions" ]; then
    log "No claude sessions found"
    return
  fi

  for session in $sessions; do
    # ── Idle sessions: clear state, handle recovery ──
    if is_idle_prompt "$session"; then
      local was_stuck
      was_stuck=$(get_state "$session" "stuck_notified")
      if [ "$was_stuck" = "1" ]; then
        local stuck_since
        stuck_since=$(get_state "$session" "unchanged_since")
        if [ -n "$stuck_since" ]; then
          local stuck_dur=$((now - stuck_since))
          log "RECOVERED: $session (was stuck ${stuck_dur}s, now at idle prompt)"
          log_event "recovered" "$session" "$((stuck_dur / 60))" "session recovered, now at idle prompt" "none"
          notify_recovered "$session" "$((stuck_dur / 60))"
        fi
      fi
      clear_state "$session"
      continue
    fi

    # ── Signal 1: Hash-based detection (timer-stripped) ──
    local current_hash
    current_hash=$(get_pane_hash "$session")
    if [ -z "$current_hash" ]; then
      continue
    fi

    local prev_hash
    prev_hash=$(get_state "$session" "hash")

    local hash_unchanged="0"
    if [ "$current_hash" = "$prev_hash" ] && [ -n "$prev_hash" ]; then
      hash_unchanged="1"
    fi

    # ── Signal 2: JSONL last record age ──
    local jsonl_age=""
    jsonl_age=$(get_jsonl_age_seconds "$session")
    local jsonl_stale="0"
    if [ -n "$jsonl_age" ] && [ "$jsonl_age" -ge "$JSONL_STALE_THRESHOLD" ] 2>/dev/null; then
      jsonl_stale="1"
    fi

    # ── Signal 3: Output token stagnation ──
    local current_tokens=""
    current_tokens=$(get_output_tokens "$session")
    local prev_tokens
    prev_tokens=$(get_state "$session" "output_tokens")
    local tokens_stagnant="0"
    if [ -n "$current_tokens" ] && [ -n "$prev_tokens" ] && [ "$current_tokens" = "$prev_tokens" ]; then
      tokens_stagnant="1"
    fi
    if [ -n "$current_tokens" ]; then
      set_state "$session" "output_tokens" "$current_tokens"
    fi

    # ── Combined stuck detection ──
    # Path A: hash unchanged (classic detection)
    # Path B: hash changing BUT JSONL stale AND tokens stagnant (timer spinner)
    local is_stuck="0"
    if [ "$hash_unchanged" = "1" ]; then
      is_stuck="1"
    elif [ "$jsonl_stale" = "1" ] && [ "$tokens_stagnant" = "1" ]; then
      is_stuck="1"
      log "DEEP_STUCK: $session — hash changing but JSONL stale (${jsonl_age}s) + tokens stagnant ($current_tokens)"
    fi

    if [ "$is_stuck" = "1" ]; then
      local unchanged_since
      unchanged_since=$(get_state "$session" "unchanged_since")
      if [ -z "$unchanged_since" ]; then
        # For deep-stuck, use JSONL age as a more accurate start time
        if [ "$hash_unchanged" = "0" ] && [ -n "$jsonl_age" ]; then
          unchanged_since=$((now - jsonl_age))
        else
          unchanged_since="$now"
        fi
        set_state "$session" "unchanged_since" "$unchanged_since"
      fi

      local stuck_dur=$((now - unchanged_since))

      if [ "$stuck_dur" -ge "$STUCK_THRESHOLD" ]; then
        local notified
        notified=$(get_state "$session" "stuck_notified")
        if [ "$notified" != "1" ]; then
          local reason="hash unchanged"
          [ "$hash_unchanged" = "0" ] && reason="JSONL stale + tokens stagnant"
          log "STUCK: $session for ${stuck_dur}s ($((stuck_dur / 60))min) [$reason]"
          log_event "stuck" "$session" "$((stuck_dur / 60))" "stuck: $reason for $((stuck_dur / 60))min" "none"
          notify_stuck "$session" "$((stuck_dur / 60))"
          set_state "$session" "stuck_notified" "1"
        fi

        if [ "$stuck_dur" -ge "$INTERVENE_THRESHOLD" ]; then
          local last_intervene
          last_intervene=$(get_state "$session" "last_intervene")
          if [ -z "$last_intervene" ] || [ $((now - last_intervene)) -ge "$INTERVENE_COOLDOWN" ]; then
            intervene "$session" "$stuck_dur"
          else
            log "SKIP intervene: $session in cooldown ($((now - last_intervene))s ago)"
          fi
        fi
      fi
    else
      # ── Not stuck: handle recovery + reset state ──
      local was_stuck
      was_stuck=$(get_state "$session" "stuck_notified")
      if [ "$was_stuck" = "1" ]; then
        local stuck_since
        stuck_since=$(get_state "$session" "unchanged_since")
        if [ -n "$stuck_since" ]; then
          local stuck_dur=$((now - stuck_since))
          log "RECOVERED: $session after $((stuck_dur / 60))min stuck"
          log_event "recovered" "$session" "$((stuck_dur / 60))" "output resumed after $((stuck_dur / 60))min stuck" "none"
          notify_recovered "$session" "$((stuck_dur / 60))"
        fi
      fi
      set_state "$session" "hash" "$current_hash"
      set_state "$session" "unchanged_since" ""
      set_state "$session" "stuck_notified" ""
    fi
  done
}

# ── Daemon control ──────────────────────────────────────────────────────────
# start_daemon spawns a background loop; stop_daemon kills it.
# A Python-based file lock prevents duplicate instances (macOS lacks flock(1)).
start_daemon() {
  # Check for existing instance via PID file
  if [ -f "$PID_FILE" ]; then
    local old_pid
    old_pid=$(cat "$PID_FILE")
    if kill -0 "$old_pid" 2>/dev/null; then
      echo "Watchdog already running (pid $old_pid)"
      echo "Use '$0 stop' first, or 'kill $old_pid'"
      return 1
    fi
    rm -f "$PID_FILE"
  fi

  # File lock via Python (macOS has no flock)
  local lock_result
  lock_result=$(python3 -c "
import fcntl, sys
try:
    f = open('$LOCK_FILE', 'w')
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    f.write(str(0))
    f.flush()
    print('ok')
except (IOError, OSError):
    print('locked')
" 2>/dev/null)
  if [ "$lock_result" != "ok" ]; then
    echo "Another watchdog instance is running (lock: $LOCK_FILE)"
    echo "If stale, remove it: rm $LOCK_FILE"
    return 1
  fi

  log "Starting watchdog daemon..."
  init_state

  (
    while true; do
      do_check >> "$LOG_FILE" 2>&1
      sleep "$SAMPLE_INTERVAL"
    done
  ) &

  local pid=$!
  echo $pid > "$PID_FILE"
  log "Watchdog started (pid $pid), checking every ${SAMPLE_INTERVAL}s"

  local count
  count=$(get_claude_sessions | wc -l | tr -d ' ')
  notify_daemon_start "$count"
}

stop_daemon() {
  if [ ! -f "$PID_FILE" ]; then
    echo "Watchdog not running"
    return 0
  fi
  local pid
  pid=$(cat "$PID_FILE")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    # Wait for process to exit and release lock
    local wait=0
    while kill -0 "$pid" 2>/dev/null && [ $wait -lt 10 ]; do
      sleep 0.5
      wait=$((wait + 1))
    done
    log "Watchdog stopped (pid $pid)"
    echo "Watchdog stopped"
  else
    echo "Watchdog process not found (stale pid $pid)"
  fi
  rm -f "$PID_FILE"
  rm -f "$LOCK_FILE"
}

show_status() {
  echo "=== Claude Code Watchdog Status ==="
  if [ -f "$PID_FILE" ]; then
    local pid
    pid=$(cat "$PID_FILE")
    if kill -0 "$pid" 2>/dev/null; then
      echo "Daemon: RUNNING (pid $pid)"
    else
      echo "Daemon: DEAD (stale pid $pid)"
    fi
  else
    echo "Daemon: STOPPED"
  fi
  echo ""
  echo "Config:"
  echo "  Sample interval: ${SAMPLE_INTERVAL}s"
  echo "  Stuck threshold: ${STUCK_THRESHOLD}s ($((STUCK_THRESHOLD / 60))min)"
  echo "  Intervene threshold: ${INTERVENE_THRESHOLD}s ($((INTERVENE_THRESHOLD / 60))min)"
  echo "  Intervene cooldown: ${INTERVENE_COOLDOWN}s ($((INTERVENE_COOLDOWN / 60))min)"
  echo "  JSONL stale threshold: ${JSONL_STALE_THRESHOLD}s ($((JSONL_STALE_THRESHOLD / 60))min)"
  echo "  Daily summary: ${DAILY_SUMMARY_HOUR}:00"
  echo ""
  echo "Tracked sessions:"
  for session in $(get_claude_sessions); do
    local unchanged notified
    unchanged=$(get_state "$session" "unchanged_since")
    notified=$(get_state "$session" "stuck_notified")
    if [ -n "$unchanged" ]; then
      local now stuck_dur
      now=$(date +%s)
      stuck_dur=$((now - unchanged))
      echo "  $session: UNCHANGED for ${stuck_dur}s ($((stuck_dur / 60))min)${notified:+ [NOTIFIED]}"
    else
      echo "  $session: active"
    fi
  done
  echo ""
  echo "Events file: $EVENTS_FILE ($(wc -l < "$EVENTS_FILE" 2>/dev/null || echo 0) events)"
  echo "Log file: $LOG_FILE"
}

# ── Foreground loop (for launchd) ──────────────────────────────────────────
# launchd expects the process to stay in foreground; this is the daemon entry point.
run_foreground() {
  log "Watchdog starting in foreground mode (for launchd)"
  init_state
  while true; do
    do_check
    sleep "$SAMPLE_INTERVAL"
  done
}

# ── Test notification ───────────────────────────────────────────────────────
test_notify() {
  echo "Sending test notifications (5 types)..."
  notify_stuck "test-session" "12"
  sleep 1
  notify_intervene "test-session" "16"
  sleep 1
  notify_recovered "test-session" "5"
  sleep 1
  notify_daemon_start "14"
  sleep 1
  # Send daily with sample data
  local sample_events="42" sample_stuck="3" sample_interrupt="2" sample_recovered="3" sample_avg="8"
  local sample_total="120" sample_total_stuck="18" sample_total_interrupt="12" sample_total_recovered="18"
  notify_from_template "daily" \
    "date=$(date '+%Y-%m-%d')" "today_events=$sample_events" \
    "today_stuck=$sample_stuck" "today_interrupt=$sample_interrupt" \
    "today_recovered=$sample_recovered" "avg_duration=$sample_avg" \
    "total_events=$sample_total" "total_stuck=$sample_total_stuck" \
    "total_interrupt=$sample_total_interrupt" "total_recovered=$sample_total_recovered" \
    "session_count=3"
  echo "Done."
}

# ── Entry point ─────────────────────────────────────────────────────────────
case "${1:-run}" in
  start)          start_daemon ;;
  stop)           stop_daemon ;;
  status)         show_status ;;
  run)            do_check ;;
  daemon)         run_foreground ;;
  test-notify)    test_notify ;;
  daily-summary)  send_daily_summary ;;
  *)
    echo "Usage: $0 {start|stop|status|run|daemon|test-notify|daily-summary}"
    exit 1
    ;;
esac
