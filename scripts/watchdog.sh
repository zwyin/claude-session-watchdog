#!/usr/bin/env bash
# Claude Code tmux session watchdog v2.0.0
# Monitors all tmux sessions running claude-yes/claude, detects stuck sessions,
# logs events, sends notifications, and auto-intervenes.
#
# Detection v2: hash-based + JSONL last record + output token stagnation
# Usage: ./watchdog.sh [start|stop|status|run|daemon|test-notify|daily-summary|log|sessions|health]

set -euo pipefail

# launchd 不加载 shell profile，需要手动补充 brew 路径
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# 加载 .env 配置（飞书 webhook 凭证），不配置则仅发 macOS 本地通知
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/../.env" ]; then
  # shellcheck disable=SC1090
  source "$SCRIPT_DIR/../.env"
fi

# ── 前置依赖检查 ──────────────────────────────────────────────────────────
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

# ── 配置参数 ────────────────────────────────────────────────────────────────
# 所有持久化状态统一放在 ~/.claude/ 目录下
EVENTS_FILE="$HOME/.claude/session-events.jsonl"   # stuck/recovered 事件日志（JSONL 格式）
PID_FILE="$HOME/.claude/watchdog.pid"               # 守护进程 PID
LOCK_FILE="$HOME/.claude/watchdog.lock"              # 进程锁（mkdir 原子操作实现）
LOG_FILE="$HOME/.claude/watchdog.log"                # 运行日志
STATE_DIR="$HOME/.claude/watchdog-state"             # 各 session 采样状态

SAMPLE_INTERVAL=15         # 采样间隔（秒）
STUCK_THRESHOLD=600        # 无变化判定卡住 → 通知（600s = 10 分钟）
INTERVENE_THRESHOLD=900    # 无变化判定深度卡住 → 自动干预（900s = 15 分钟）
INTERVENE_COOLDOWN=600     # 干预冷却期，防止频繁重试（600s = 10 分钟）
DAILY_SUMMARY_HOUR=22      # 日报发送时间（22:00）
JSONL_STALE_THRESHOLD=600  # JSONL 日志无新记录判定阈值（600s = 10 分钟）
IDLE_CLASSIFY_THRESHOLD=300  # 空闲多久后触发分类通知（300s = 5 分钟）

# 飞书机器人通知（通过 .env 或环境变量设置，不配置则仅发 macOS 通知）
FEISHU_WEBHOOK="${FEISHU_WEBHOOK:-}"
FEISHU_SECRET="${FEISHU_SECRET:-}"

# 模型名称：从 tmux 状态栏实时提取，支持 MODEL_NAME 环境变量覆盖
# 用法: MODEL_NAME=custom ./scripts/watchdog.sh run
get_model_name() {
  local session="${1:-}"
  if [ -n "${MODEL_NAME:-}" ]; then
    echo "$MODEL_NAME"
    return
  fi
  if [ -n "$session" ]; then
    local model
    model=$(tmux capture-pane -t "$session" -p -S -12 2>/dev/null \
      | sed $'s/\xc2\xa0/ /g' \
      | grep -oE '模型:[[:space:]]*[^ |]+' | head -1 | sed 's/模型:[[:space:]]*//' | tr -d '[:space:]' || true)
    if [ -n "$model" ]; then
      echo "$model"
      return
    fi
  fi
  echo "unknown"
}

# ── 日志输出 ─────────────────────────────────────────────────────────────────
# 双路输出：同时写日志文件和 stdout（launchd 会捕获 stdout）
log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ── 状态管理 ─────────────────────────────────────────────────────────────────
# 每个 session 用 STATE_DIR 下的平铺文件存储状态（如 gps.hash, gps.unchanged_since）
# 调试时直接 cat/rm 即可，不需要数据库
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

# ── 事件记录 ─────────────────────────────────────────────────────────────────
# 每个事件追加一行 JSON，字段定义见 04-monitoring-plan.md
log_event() {
  local event="$1" session="$2" duration="$3" notes="${4:-}"
  local intervention="${5:-none}"
  mkdir -p "$(dirname "$EVENTS_FILE")"
  # 只有 recovered 事件设置 recovered=true
  local recovered_val="false"
  if [ "$event" = "recovered" ]; then
    recovered_val="true"
  fi
  local model
  model=$(get_model_name "$session")
  # 转义 JSON 特殊字符（反斜杠和双引号），防止破坏 JSON 结构
  session=$(printf '%s' "$session" | sed 's/\\/\\\\/g; s/"/\\"/g')
  notes=$(printf '%s' "$notes" | sed 's/\\/\\\\/g; s/"/\\"/g')
  model=$(printf '%s' "$model" | sed 's/\\/\\\\/g; s/"/\\"/g')
  printf '{"timestamp":"%s","event":"%s","session":"%s","project":"%s","duration_minutes":%s,"model":"%s","phase":"unknown","intervention":"%s","recovered":%s,"notes":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$event" \
    "$session" \
    "$session" \
    "$duration" \
    "$model" \
    "$intervention" \
    "$recovered_val" \
    "$notes" \
    >> "$EVENTS_FILE"
}

# ── 通知模板引擎 ─────────────────────────────────────────────────────────────
# 委托 scripts/notify.py 渲染模板 → HMAC 签名 → 发送飞书
# 用法: notify_from_template "stuck" "session=gps" "duration=12" ...
TEMPLATE_FILE="$SCRIPT_DIR/notify-templates.json"

notify_from_template() {
  local section="$1"
  shift
  FEISHU_WEBHOOK="$FEISHU_WEBHOOK" FEISHU_SECRET="$FEISHU_SECRET" \
    python3 "$SCRIPT_DIR/notify.py" "$TEMPLATE_FILE" "$section" "$@" \
    2>&1 | while IFS= read -r line; do
      log "$line"
    done || true
}

# ── 会话上下文提取 ─────────────────────────────────────────────────────────
# 从 tmux pane 中提取可读的状态信息，用于通知内容

# 读取底部 12 行，过滤出 Claude Code 状态栏关键字
get_session_status_line() {
  local session="$1"
  tmux capture-pane -t "$session" -p -S -12 2>/dev/null \
    | grep -E '(模型:|输入:|会话:|目录:|⏵⏵|────)' \
    | tail -5 || true
}

# 读取底部 80 行，去除状态栏和空白行，返回最后 8 行有效输出
get_session_last_lines() {
  local session="$1"
  tmux capture-pane -t "$session" -p -S -80 2>/dev/null \
    | grep -v -E '(模型:|输入:|会话:|目录:|────|⏵⏵|^❯$|^[[:space:]]*$)' \
    | sed 's/[[:space:]]*$//' \
    | tail -8 || true
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

# ── 空闲分类通知 ──────────────────────────────────────────────────────────
# 调用 classify_idle.py 做关键字分类（+ LLM 降级），根据结果选模板通知
notify_idle_classified() {
  local session="$1" duration="$2"
  local date_str time_str
  date_str=$(date '+%Y-%m-%d')
  time_str=$(date '+%H:%M:%S')

  local classify_result category summary last_lines
  classify_result=$(python3 "$SCRIPT_DIR/classify_idle.py" "$session" --llm 2>/dev/null || echo '{}')
  category=$(echo "$classify_result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('category','idle_unknown'))" 2>/dev/null || echo "idle_unknown")
  summary=$(echo "$classify_result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('summary',''))" 2>/dev/null || echo "")
  last_lines=$(echo "$classify_result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('last_lines',''))" 2>/dev/null || echo "")

  # LLM 超时时 category 为 llm_timeout，在通知中说明
  local template="idle_unknown"
  local label="空闲（原因不明）"
  case "$category" in
    decision_needed)
      template="idle_decision"
      label="等待人工决策"
      ;;
    task_complete)
      template="idle_complete"
      label="任务完成，等待验收"
      ;;
    ambiguous)
      template="idle_decision"
      label="需要人工查看（多种状态交叉）"
      [ -n "$summary" ] && summary="[交叉情况] $summary"
      ;;
    llm_timeout)
      template="idle_unknown"
      label="空闲（LLM 分析超时）"
      summary="$summary"
      ;;
  esac

  log "IDLE $label: $session (${duration}min)"
  log_event "idle_$category" "$session" "$duration" "idle: $label" "none"
  osascript -e "display notification \"$session $label ${duration}min\" with title \"Watchdog: idle\"" 2>/dev/null || true
  notify_from_template "$template" \
    "session=$session" "duration=$duration" "date=$date_str" "time=$time_str" \
    "summary=$summary" "last_output=$last_lines"
}

# ── 日报统计 ─────────────────────────────────────────────────────────────────
# 从 JSONL 事件文件中聚合当日和累计数据，发送飞书卡片通知
send_daily_summary() {
  local today total total_stuck total_interrupt total_recovered avg_dur
  today=$(date '+%Y-%m-%d')
  total=$(wc -l < "$EVENTS_FILE" 2>/dev/null || echo 0)
  total_stuck=$(grep -c '"event":"stuck"' "$EVENTS_FILE" 2>/dev/null || echo 0)
  total_interrupt=$(grep -c '"event":"auto_interrupt"' "$EVENTS_FILE" 2>/dev/null || echo 0)
  total_recovered=$(grep -c '"event":"recovered"' "$EVENTS_FILE" 2>/dev/null || echo 0)

  local today_events today_stuck today_interrupt today_recovered
  today_events=$(grep "\"timestamp\":\"$today" "$EVENTS_FILE" 2>/dev/null | wc -l | tr -d ' ' || echo 0)
  today_stuck=$(grep "\"timestamp\":\"$today" "$EVENTS_FILE" 2>/dev/null | grep -c '"event":"stuck"' || echo 0)
  today_interrupt=$(grep "\"timestamp\":\"$today" "$EVENTS_FILE" 2>/dev/null | grep -c '"event":"auto_interrupt"' || echo 0)
  today_recovered=$(grep "\"timestamp\":\"$today" "$EVENTS_FILE" 2>/dev/null | grep -c '"event":"recovered"' || echo 0)

  local avg_min="0"
  if [ "$today_stuck" -gt 0 ]; then
    avg_min=$(grep "\"timestamp\":\"$today" "$EVENTS_FILE" 2>/dev/null | grep '"event":"stuck"' \
      | grep -oE '"duration_minutes":[0-9]+' | cut -d: -f2 \
      | awk '{s+=$1; n++} END {if(n>0) printf "%d", s/n; else print 0}')
  fi

  local active_count
  active_count=$(get_claude_sessions | wc -l | tr -d ' ')

  notify_from_template "daily" \
    "date=$today" "today_events=$today_events" \
    "today_stuck=$today_stuck" "today_interrupt=$today_interrupt" \
    "today_recovered=$today_recovered" "avg_duration=$avg_min" \
    "total_events=$total" "total_stuck=$total_stuck" \
    "total_interrupt=$total_interrupt" "total_recovered=$total_recovered" \
    "session_count=$active_count"

  log "DAILY SUMMARY sent: today=${today_events} events, total=${total} events"
}

# ── 发现 Claude 会话 ────────────────────────────────────────────────────────
# 遍历 tmux sessions，找到子进程为 claude-yes/agent-yes/claude 的会话
get_claude_sessions() {
  for s in $(tmux list-sessions -F '#{session_name}' 2>/dev/null); do
    local pane_pid
    pane_pid=$(tmux list-panes -t "$s" -F '#{pane_pid}' 2>/dev/null | head -1)
    if ps -o pid,ppid,command 2>/dev/null | grep -E "agent-yes|claude-yes|/claude$" | grep -v grep | awk -v ppid="$pane_pid" '$2 == ppid {found=1; exit} END {exit !found}' 2>/dev/null; then
      echo "$s"
    fi
  done
}

# ── 屏幕 hash 计算（去除计时器干扰） ─────────────────────────────────────
# 计时器（如 "2m 15s"）和时间戳会导致 hash 持续变化，产生误判。
# 先用 sed 将这些模式归一化为固定字符串，再计算 md5。
get_pane_hash() {
  local session="$1"
  tmux capture-pane -t "$session" -p -S -50 2>/dev/null \
    | tail -20 \
    | sed -E 's/[0-9]+m [0-9]+s/TIMER/g; s/[0-9]+m[0-9]+s/TIMER/g; s/[0-9]+:[0-9]+(am|pm)?/TIME/g' \
    | md5 2>/dev/null | cut -d' ' -f1 || md5sum 2>/dev/null | cut -d' ' -f1 || echo ""
}

# ── JSONL 最后记录时间（秒） ──────────────────────────────────────────────
# 返回 JSONL 日志最后一条记录距今的秒数，失败时返回空
# 委托 scripts/jsonl_age.py 处理
get_jsonl_age_seconds() {
  local session="$1"
  python3 "$SCRIPT_DIR/jsonl_age.py" "$session" 2>/dev/null
}

# ── 输出 token 数提取 ─────────────────────────────────────────────────────
# 从状态栏提取 "输出:" 字段的数值（如 "228.9k"）。
# 用于停滞检测：如果输出 token 不变，说明模型没有产出新内容（即使屏幕在变化）。
get_output_tokens() {
  local session="$1"
  tmux capture-pane -t "$session" -p -S -8 2>/dev/null \
    | grep -oE '输出:[[:space:]]*[0-9.]+[kKmM]?' \
    | grep -oE '[0-9.]+[kKmM]?' \
    | head -1 || true
}

# ── 空闲判定 ───────────────────────────────────────────────────────────────
# 处于 ❯ 提示符等空闲状态的 session 不应触发卡住检测
# 匹配模式：❯ 提示符、权限确认、超时标记、取消提示等
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

# ── 自动干预：Ctrl-C + 继续任务 ──────────────────────────────────────────
# 模拟人工操作：先发 Escape + Ctrl-C 打断挂起的 API 请求，
# 等待提示符出现后发送继续消息。不杀进程，与手动操作完全一致。
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

# ── 主检测循环（v2：hash + JSONL + token 三路联合检测）──────────────────────
# 路径 A：屏幕 hash 不变 → 经典卡住检测
# 路径 B：hash 在变但 JSONL 停滞且 token 不变 → "深度卡住"
#         （如计时器在转圈更新屏幕，但 API 实际已挂起）
do_check() {
  init_state
  local now
  now=$(date +%s)

  # 在配置时间发送日报（每天只发一次）
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
    # ── 空闲 session：恢复检测 + 空闲分类通知 ──
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
        clear_state "$session"
      fi

      # 空闲分类：超过阈值后用关键字 + LLM 分类，发通知
      local idle_since
      idle_since=$(get_state "$session" "idle_since")
      if [ -z "$idle_since" ]; then
        set_state "$session" "idle_since" "$now"
        idle_since="$now"
      fi
      local idle_dur=$((now - idle_since))
      local idle_notified
      idle_notified=$(get_state "$session" "idle_notified")

      if [ "$idle_dur" -ge "$IDLE_CLASSIFY_THRESHOLD" ] && [ "$idle_notified" != "1" ]; then
        notify_idle_classified "$session" "$((idle_dur / 60))"
        set_state "$session" "idle_notified" "1"
      fi
      continue
    fi

    # 非空闲：清除空闲状态
    local prev_idle_since
    prev_idle_since=$(get_state "$session" "idle_since")
    if [ -n "$prev_idle_since" ]; then
      set_state "$session" "idle_since" ""
      set_state "$session" "idle_notified" ""
    fi

    # ── 信号 1：屏幕 hash 检测（已去除计时器干扰） ──
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

    # ── 信号 2：JSONL 日志最后记录时间 ──
    local jsonl_age=""
    jsonl_age=$(get_jsonl_age_seconds "$session")
    local jsonl_stale="0"
    if [ -n "$jsonl_age" ] && [ "$jsonl_age" -ge "$JSONL_STALE_THRESHOLD" ] 2>/dev/null; then
      jsonl_stale="1"
    fi

    # ── 信号 3：输出 token 停滞检测 ──
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

    # ── 联合卡住判定 ──
    # 路径 A：hash 不变（经典检测）
    # 路径 B：hash 在变但 JSONL 停滞 + token 不变（计时器干扰场景）
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
        # 深度卡住时用 JSONL 年龄作为更准确的起始时间
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
      # ── 未卡住：处理恢复事件 + 重置状态 ──
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

# ── 守护进程控制 ──────────────────────────────────────────────────────────
# start_daemon：后台启动监控循环
# stop_daemon：停止进程
# mkdir 锁防止重复启动（跨平台原子操作）
start_daemon() {
  # 检查是否已有实例运行
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

  # 原子锁：mkdir 成功则获得锁，失败则说明已有实例
  if ! mkdir "$LOCK_FILE" 2>/dev/null; then
    echo "Another watchdog instance is running (lock: $LOCK_FILE)"
    echo "If stale, remove it: rm -rf $LOCK_FILE"
    return 1
  fi

  log "Starting watchdog daemon..."
  init_state

  (
    trap 'rm -f "$PID_FILE"; rm -rf "$LOCK_FILE"' EXIT
    while true; do
      do_check
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
    echo "Watchdog not running (no PID file)"
    return 0
  fi
  local pid
  pid=$(cat "$PID_FILE")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    # Wait for process to exit
    local wait=0
    while kill -0 "$pid" 2>/dev/null && [ $wait -lt 10 ]; do
      sleep 0.5
      wait=$((wait + 1))
    done
    log "Watchdog stopped (pid $pid)"
    echo "Watchdog stopped"
    # 提醒 launchd KeepAlive 会自动重启
    if launchctl list | grep -q 'com.claude.watchdog' 2>/dev/null; then
      echo "Note: launchd KeepAlive will restart this. To disable: launchctl unload ~/Library/LaunchAgents/com.claude.watchdog.plist"
    fi
  else
    echo "Watchdog process not found (stale pid $pid)"
  fi
  rm -f "$PID_FILE"
  rm -rf "$LOCK_FILE"
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

# ── 前台循环模式（供 launchd 使用）──────────────────────────────────────
# launchd 要求进程保持前台运行，这是守护入口点
# 写入 PID 文件以便 status 命令正确识别
run_foreground() {
  echo $$ > "$PID_FILE"
  trap 'rm -f "$PID_FILE"; rm -rf "$LOCK_FILE"' EXIT
  log "Watchdog starting in foreground mode (for launchd, pid $$)"
  init_state
  while true; do
    do_check
    sleep "$SAMPLE_INTERVAL"
  done
}

# ── 测试通知（发送 5 种类型的样例通知）────────────────────────────────────
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

# ── 查看日志（最近 N 行）────────────────────────────────────────────────────
show_log() {
  local lines="${2:-50}"
  if [ ! -f "$LOG_FILE" ]; then
    echo "No log file: $LOG_FILE"
    return
  fi
  tail -"$lines" "$LOG_FILE"
}

# ── 列出所有 Claude 会话详情 ──────────────────────────────────────────────
show_sessions() {
  local sessions
  sessions=$(get_claude_sessions)
  if [ -z "$sessions" ]; then
    echo "No claude sessions found in tmux"
    return
  fi
  echo "=== Claude Code Sessions ==="
  for session in $sessions; do
    local model tokens jsonl_age idle
    model=$(get_model_name "$session")
    tokens=$(get_output_tokens "$session")
    jsonl_age=$(get_jsonl_age_seconds "$session")
    if is_idle_prompt "$session" 2>/dev/null; then
      idle="idle"
    else
      idle="active"
    fi
    echo ""
    echo "  Session: $session"
    echo "  Model:   ${model:-unknown}"
    echo "  Tokens:  ${tokens:-N/A}"
    echo "  JSONL:   ${jsonl_age:-N/A}${jsonl_age:+s} since last record"
    echo "  State:   $idle"
    # Show stuck status if tracked
    local unchanged notified
    unchanged=$(get_state "$session" "unchanged_since")
    notified=$(get_state "$session" "stuck_notified")
    if [ -n "$unchanged" ]; then
      local now stuck_dur
      now=$(date +%s)
      stuck_dur=$((now - unchanged))
      echo "  Stuck:   ${stuck_dur}s ($((stuck_dur / 60))min)${notified:+ [NOTIFIED]}"
    fi
  done
}

# ── 健康检查（验证 daemon 存活 + 最近有日志输出）───────────────────────────
health_check() {
  local healthy="true"

  # 检查进程存活
  if [ -f "$PID_FILE" ]; then
    local pid
    pid=$(cat "$PID_FILE")
    if kill -0 "$pid" 2>/dev/null; then
      echo "Process: ALIVE (pid $pid)"
    else
      echo "Process: DEAD (stale pid $pid)"
      healthy="false"
    fi
  else
    echo "Process: NOT RUNNING (no PID file)"
    healthy="false"
  fi

  # 检查日志是否在更新
  if [ -f "$LOG_FILE" ]; then
    local log_age
    log_age=$(( $(date +%s) - $(stat -f %m "$LOG_FILE" 2>/dev/null || stat -c %Y "$LOG_FILE" 2>/dev/null || echo 0) ))
    if [ "$log_age" -lt 300 ] 2>/dev/null; then
      echo "Log:     FRESH (${log_age}s ago)"
    else
      echo "Log:     STALE (${log_age}s ago)"
      healthy="false"
    fi
  else
    echo "Log:     MISSING"
    healthy="false"
  fi

  # 检查 session 数量
  local count
  count=$(get_claude_sessions | wc -l | tr -d ' ')
  echo "Sessions: $count tracked"

  if [ "$healthy" = "true" ]; then
    echo ""
    echo "Status: HEALTHY"
  else
    echo ""
    echo "Status: UNHEALTHY"
    return 1
  fi
}

# ── 入口 ────────────────────────────────────────────────────────────────────
case "${1:-run}" in
  start)          start_daemon ;;
  stop)           stop_daemon ;;
  status)         show_status ;;
  run)            do_check ;;
  daemon)         run_foreground ;;
  test-notify)    test_notify ;;
  daily-summary)  send_daily_summary ;;
  log)            show_log "$@" ;;
  sessions)       show_sessions ;;
  health)         health_check ;;
  *)
    echo "Usage: $0 {start|stop|status|run|daemon|test-notify|daily-summary|log|sessions|health}"
    exit 1
    ;;
esac
