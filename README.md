# Claude Code 会话看门狗

自动监控 tmux 中所有 Claude Code 会话，检测卡住并自动干预。

## 工作原理

```
每 15 秒采样一次所有 tmux pane 的输出
  ↓
三路联合检测：屏幕 hash + JSONL 日志最后记录 + 输出 token 数
  ↓
10 分钟无有效输出 → 记录 stuck 事件 + macOS 通知 + 飞书推送
  ↓
15 分钟无有效输出 → 自动 Ctrl-C + 发送"继续"消息
  ↓
输出恢复变化 → 记录 recovered 事件
```

## 前置条件

1. **tmux** — 所有检测和干预（send-keys）都依赖 tmux。Claude Code 会话必须运行在 tmux session 中。
2. **自动权限审批** — Claude Code 默认会在权限确认时等待用户输入。无人值守场景下，必须通过以下方式之一绕过：
   - 使用 [claude-yes](https://github.com/anthropics/claude-code) / agent-yes 等 wrapper 自动审批
   - 或在 Claude Code 配置中开启 `--dangerously-skip-permissions`（不推荐，仅限隔离环境）
   - 否则会话会卡在权限确认处，看门狗无法区分"等权限"和"真卡住"
3. **macOS** — 当前仅支持 macOS（使用 `osascript` 发送本地通知、`md5` 计算哈希）。Linux 需要替换对应命令。
4. **Python 3** — 用于 JSON 解析、HMAC 签名、飞书通知发送。

## 通知配置（可选）

支持飞书机器人通知。复制配置模板并填入你的 webhook 信息：

```bash
cp .env.example .env
# 编辑 .env，填入 FEISHU_WEBHOOK 和 FEISHU_SECRET
```

不配置 `.env` 则仅发送 macOS 本地通知，不推送飞书。

## 快速开始

```bash
# 单次检测（默认命令）
./scripts/watchdog.sh run

# 启动后台守护进程
./scripts/watchdog.sh start

# 前台持续运行（用于 launchd）
./scripts/watchdog.sh daemon

# 查看状态
./scripts/watchdog.sh status

# 查看所有 Claude 会话详情（模型、token、JSONL 年龄）
./scripts/watchdog.sh sessions

# 健康检查（进程存活 + 日志活跃度）
./scripts/watchdog.sh health

# 查看运行日志（默认最近 50 行）
./scripts/watchdog.sh log
./scripts/watchdog.sh log 100

# 停止
./scripts/watchdog.sh stop

# 发送测试通知（stuck/intervene/recovered/start/daily 五种）
./scripts/watchdog.sh test-notify

# 手动发送日报
./scripts/watchdog.sh daily-summary
```

## 开机自启

```bash
# 启用（加载 launchd）
launchctl load ~/Library/LaunchAgents/com.claude.watchdog.plist

# 禁用
launchctl unload ~/Library/LaunchAgents/com.claude.watchdog.plist
```

## 文件位置

| 文件 | 说明 |
|------|------|
| `scripts/watchdog.sh` | 主脚本 |
| `scripts/notify-templates.json` | 飞书通知模板 |
| `.env.example` | 通知配置模板 |
| `~/.claude/watchdog.pid` | 后台进程 PID |
| `~/.claude/watchdog.lock` | 进程锁文件 |
| `~/.claude/watchdog.log` | 运行日志 |
| `~/.claude/watchdog-state/` | 各 session 的采样状态 |
| `~/.claude/session-events.jsonl` | stuck/recovered 事件记录 |
| `~/Library/LaunchAgents/com.claude.watchdog.plist` | 开机自启配置 |

## 配置参数

在 `scripts/watchdog.sh` 顶部修改：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| SAMPLE_INTERVAL | 15s | 采样间隔 |
| STUCK_THRESHOLD | 600s (10min) | 判定卡住 + 通知 |
| INTERVENE_THRESHOLD | 900s (15min) | 自动 Ctrl-C 干预 |
| INTERVENE_COOLDOWN | 600s (10min) | 干预冷却期 |
| JSONL_STALE_THRESHOLD | 600s (10min) | JSONL 日志无新记录判定 |
| DAILY_SUMMARY_HOUR | 22 (22:00) | 日报发送时间 |

## 文档索引

| 文件 | 内容 |
|------|------|
| [01-problem-background.md](01-problem-background.md) | 问题现象、环境、根因分析 |
| [02-community-solutions.md](02-community-solutions.md) | 社区方案全景 |
| [03-progressive-approach.md](03-progressive-approach.md) | 渐进策略（三层防线） |
| [04-monitoring-plan.md](04-monitoring-plan.md) | 量化监控方案 |
