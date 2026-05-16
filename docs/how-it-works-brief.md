# Watchdog 工作原理（简版）

## 系统定位

每 15 秒采样所有 tmux Claude Code 会话，检测卡住 → 告警 + 自动恢复；检测空闲 → 语义分类 → 通知；定时发早晚报。

## 核心流程

```
每 15 秒
  │
  ▼
发现 Claude 会话（ps + tmux PID 匹配）
  │
  ▼
判断每个会话状态
  │
  ├── 空闲（❯ 提示符）──→ 等 10min ──→ 关键字 + LLM 分类 ──→ 通知
  │                                                │
  │                                     ┌─────────┼─────────┐
  │                                     ▼         ▼         ▼
  │                                  需要决策   任务完成    原因不明
  │                                  (黄色)    (绿色)     (蓝色)
  │
  ├── 卡住（三路信号联合检测）──→ 10min 告警 ──→ 15min 自动干预
  │        │                                    (Ctrl-C + 继续)
  │        │
  │   屏幕哈希不变 ─┐
  │   JSONL 日志停滞 ├─→ 任一或组合命中 = 卡住
  │   输出 Token 停滞┘
  │
  └── 恢复（卡住后输出恢复）──→ 通知（绿色）
```

## 模块关系

```
watchdog.sh（主循环，Bash）
  ├── classify_idle.py（空闲分类：关键字 + LLM）
  │     └── llm_utils.py（LLM 调用：主用 + 备用端点）
  ├── jsonl_age.py（JSONL 日志年龄查询）
  ├── notify.py（通知渲染 + HMAC 签名 + 飞书推送）
  │     └── notify-templates.json（10 种通知模板）
  └── report_summary.py（早晚报统计）
```

## 关键阈值

| 参数 | 值 | 含义 |
|------|-----|------|
| SAMPLE_INTERVAL | 15s | 采样频率 |
| STUCK_THRESHOLD | 10min | 卡住 → 发送告警 |
| INTERVENE_THRESHOLD | 15min | 卡住 → 自动干预 |
| IDLE_CLASSIFY_THRESHOLD | 10min | 空闲 → 触发分类 |

## 状态机

```
ACTIVE ⇄ IDLE（❯ 提示符）
  │
  └→ STUCK（三路信号命中）→ NOTIFIED（10min）→ INTERVENED（15min）→ RECOVERED
```

## 持久化

| 路径 | 用途 |
|------|------|
| `~/.claude/watchdog-state/` | 每会话采样状态（哈希、时间戳、标记） |
| `~/.claude/session-events.jsonl` | 全部事件日志（stuck/intervene/recovered/idle_*） |
| `~/.claude/watchdog.log` | 运行日志 |

## 外部依赖

- **tmux**：会话发现、输出捕获、键盘模拟（干预）
- **LLM API**：空闲分类 + 事件审查（Anthropic/OpenAI 兼容）
- **飞书 Webhook**：推送通知卡片（不配置则仅 macOS 本地通知）
- **launchd**：macOS 开机自启（`watchdog.sh daemon` 前台模式）
