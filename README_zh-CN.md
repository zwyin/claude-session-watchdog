# Claude Code 会话看门狗

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Version](https://img.shields.io/badge/version-2.0.7-brightgreen.svg)
[English](README.md) | 中文

自动监控 tmux 中的 Claude Code 会话，检测卡住的会话并自动恢复。

**[互动式项目导览](docs/interactive-guide.html)** — 在浏览器中打开，包含架构图、流程演示和自测题。

## 为什么需要

用 Claude Code 做了一段时间 agent 开发后，你很自然地会同时开很多会话——不同项目并行，甚至同一个项目里一个写需求、一个写代码。因为关闭 Claude CLI 会中断会话，tmux 就成了标准的解决方案，让你在公司、家里、路上都能管理和延续会话。

但随之而来的问题是**会话状态管理**。5 个、10 个会话跑着，你很难追踪哪些在正常推进、哪些卡在等待 API 响应、哪些已经悄悄停滞了。等你想起来去看某个会话，才发现它已经空闲了几个小时。看门狗解决的就是这个问题——持续监控所有会话，发现异常时通知你，还能自动恢复卡住的会话而不丢失上下文。

## 工作原理

![工作原理](docs/how-it-works.png)

```
每 15 秒扫描所有 tmux 窗格
  │
  ▼
三路联合检测：屏幕哈希 + JSONL 日志最后记录 + 输出 token 停滞
  │
  ▼
无有效输出 10 分钟 ──→ 记录卡住事件 + macOS 通知 + 飞书告警
  │
  ▼
无有效输出 15 分钟 ──→ 自动发送 Ctrl-C + "继续" 消息
  │
  ▼
输出恢复              ──→ 记录恢复事件
```

## 核心特性

- **三路联合检测** — 屏幕内容哈希（过滤计时器噪音）、JSONL 会话日志最后记录时间、输出 token 数停滞三路并行，全部一致才判定卡住
- **非破坏性自动恢复** — 卡住 15 分钟后自动发送 Ctrl-C + continue，不杀进程、不丢上下文，冷却期 10 分钟防止频繁干预
- **LLM 空闲分类** — 关键字匹配 + 可选 LLM 语义分析（主用 + 备用双端点），自动判断空闲原因：等待决策 / 任务完成 / 不明
- **飞书通知** — HMAC 签名 webhook，10 种模板类型（卡住、干预、恢复、启动、日报、早报、晚报、空闲决策、空闲完成、空闲不明）
- **macOS 本地通知** — `osascript` 原生通知，零配置即可使用
- **定时报告** — 早报 08:00（覆盖夜间）、晚报 22:00（覆盖白天），含各会话明细
- **事件自审** — LLM 回顾历史检测事件，发现误判和调优建议

## 前置条件

| 条件 | 说明 |
|---|---|
| **tmux** | 所有检测和干预（`send-keys`）依赖 tmux。Claude Code 会话必须在 tmux 中运行。 |
| **Python 3** | JSON 解析、HMAC 签名、飞书通知发送、空闲分类。 |
| **macOS** | 使用 `osascript` 发送本地通知、`md5` 计算哈希。Linux 需替换对应命令。 |
| **自动审批包装器** | 无人值守的会话需要 `claude-yes` 或 `agent-yes` 等包装器自动通过权限审批。否则看门狗无法区分"等待权限"和"真正卡住"。 |
| **飞书群聊机器人** *(可选)* | 在飞书群聊中添加机器人，实时推送告警（卡住、恢复、空闲分类等），无需主动查看 tmux 即可及时感知会话状态。 |

## 快速开始

```bash
# 克隆
git clone https://github.com/zwyin/claude-session-watchdog.git
cd claude-session-watchdog

# 配置通知（可选 — 跳过则仅使用 macOS 本地通知）
cp .env.example .env
# 编辑 .env，填入飞书 webhook / LLM API 凭证

# 执行一次检测
./scripts/watchdog.sh run

# 启动后台守护进程
./scripts/watchdog.sh start

# 前台运行（用于 launchd 或容器）
./scripts/watchdog.sh daemon
```

### 全部命令

| 命令 | 说明 |
|---|---|
| `./scripts/watchdog.sh run` | 单次检测（默认） |
| `./scripts/watchdog.sh start` | 启动后台守护进程 |
| `./scripts/watchdog.sh daemon` | 前台循环运行（用于 launchd） |
| `./scripts/watchdog.sh stop` | 停止后台守护进程 |
| `./scripts/watchdog.sh status` | 查看守护进程状态 |
| `./scripts/watchdog.sh sessions` | 列出所有 Claude 会话（模型、token 数、JSONL 年龄） |
| `./scripts/watchdog.sh health` | 健康检查（进程存活 + 日志新鲜度） |
| `./scripts/watchdog.sh log [N]` | 查看最近 N 行日志（默认 50） |
| `./scripts/watchdog.sh test-notify` | 发送测试通知（全部 10 种类型） |
| `./scripts/watchdog.sh daily-summary` | 手动发送日报 |
| `./scripts/watchdog.sh review [hours]` | LLM 审核近期检测事件（默认 12 小时） |

## 配置

### 环境变量（`.env`）

**基本使用无需配置 `.env`**。看门狗开箱即用，默认使用 macOS 本地通知和关键字匹配进行空闲分类。仅在需要对应功能时才配置以下变量。

#### 通知（可选）

| 变量 | 默认值 | 是否必须 | 说明 |
|---|---|---|---|
| `FEISHU_WEBHOOK` | *(空)* | 否 | 飞书机器人 webhook URL。`FEISHU_WEBHOOK` 和 `FEISHU_SECRET` 必须同时设置才能启用飞书通知。缺少任一项，仅发送 macOS 本地通知。 |
| `FEISHU_SECRET` | *(空)* | 否 | 飞书 webhook 签名密钥（HMAC 验证）。 |

#### LLM 空闲分类（可选）

| 变量 | 默认值 | 是否必须 | 说明 |
|---|---|---|---|
| `WATCHDOG_LLM_API_KEY` | *(空)* | 否 | 空闲分类 LLM 的 API Key。**不配置则完全跳过 LLM 分类**，空闲会话仅使用关键字匹配（准确率较低）。 |
| `WATCHDOG_LLM_BASE_URL` | `https://api.anthropic.com` | 否 | LLM API 地址，兼容 OpenAI 或 Anthropic 格式的任意端点。 |
| `WATCHDOG_LLM_MODEL` | `claude-haiku-4-5-20251001` | 否 | 空闲分类使用的模型名称。 |
| `WATCHDOG_LLM_FORMAT` | *(自动检测)* | 否 | API 格式：`anthropic` 或 `openai`。留空则根据 base URL 自动检测。 |

#### 备用 LLM 端点（可选）

仅在主用端点失败时使用。如果未设置 `WATCHDOG_LLM_API_KEY_2`，则不尝试备用端点。

| 变量 | 默认值 | 是否必须 | 说明 |
|---|---|---|---|
| `WATCHDOG_LLM_API_KEY_2` | *(空)* | 否 | 备用端点 API Key。 |
| `WATCHDOG_LLM_BASE_URL_2` | *(空)* | 否 | 备用端点地址。 |
| `WATCHDOG_LLM_MODEL_2` | *(空)* | 否 | 备用端点模型名称。 |
| `WATCHDOG_LLM_FORMAT_2` | `openai` | 否 | 备用 API 格式。 |

### 调优参数（`scripts/watchdog.sh` 内）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `SAMPLE_INTERVAL` | `15` | 采样间隔（秒） |
| `STUCK_THRESHOLD` | `600`（10 分钟） | 无有效输出多久后判定卡住并发送告警 |
| `INTERVENE_THRESHOLD` | `900`（15 分钟） | 无有效输出多久后自动发送 Ctrl-C + continue |
| `INTERVENE_COOLDOWN` | `600`（10 分钟） | 同一会话干预的最小间隔 |
| `JSONL_STALE_THRESHOLD` | `600`（10 分钟） | JSONL 日志最后记录多久未更新视为过期 |
| `IDLE_CLASSIFY_THRESHOLD` | `600`（10 分钟） | 空闲多久后触发分类通知 |
| `DAILY_SUMMARY_HOUR` | `22` | 晚报发送时间（0-23 时） |
| `MORNING_SUMMARY_HOUR` | `08` | 早报发送时间（0-23 时） |

## launchd 开机自启（macOS）

创建 `~/Library/LaunchAgents/com.claude.watchdog.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.watchdog</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/repo/claude-session-watchdog/scripts/watchdog.sh</string>
        <string>daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.claude/watchdog.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.claude/watchdog.log</string>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/repo/claude-session-watchdog</string>
</dict>
</plist>
```

```bash
# 启用
launchctl load ~/Library/LaunchAgents/com.claude.watchdog.plist

# 禁用
launchctl unload ~/Library/LaunchAgents/com.claude.watchdog.plist
```

## 架构

检测管线每 `SAMPLE_INTERVAL` 秒循环执行：

1. **发现** — 找到所有运行 `claude` 或 `claude-yes` 的 tmux 窗格。
2. **采集** — 抓取每个窗格的可见输出；过滤计时器/计数器噪音后计算内容哈希。
3. **交叉验证** — 读取会话 JSONL 日志的最后记录时间戳和当前输出 token 数。
4. **判定** — 三路信号全部一致（哈希不变、JSONL 过期、token 停滞）才判定卡住。
5. **执行** — `STUCK_THRESHOLD` 时告警；`INTERVENE_THRESHOLD` 时自动干预；空闲会话通过关键字和可选 LLM 分类。

状态以纯文件方式存储在 `~/.claude/watchdog-state/`，无需数据库。

## 文件说明

| 路径 | 用途 |
|---|---|
| `scripts/watchdog.sh` | 主脚本（检测、干预、守护进程管理） |
| `scripts/classify_idle.py` | 空闲会话分类器（关键字 + LLM） |
| `scripts/notify.py` | 飞书通知发送（HMAC 签名） |
| `scripts/llm_utils.py` | LLM API 调用工具（Anthropic + OpenAI 格式） |
| `scripts/review_events.py` | 历史事件 LLM 审核 |
| `scripts/report_summary.py` | 早报/晚报统计 |
| `scripts/jsonl_age.py` | JSONL 最后记录时间提取 |
| `scripts/notify-templates.json` | 飞书通知模板（10 种） |
| `docs/interactive-guide.html` | 互动式项目导览（浏览器打开） |
| `docs/how-it-works.png` | "工作原理"流程图 |
| `.env.example` | 配置模板 |
| `~/.claude/watchdog.pid` | 后台守护进程 PID |
| `~/.claude/watchdog.lock` | 进程锁（mkdir 原子锁） |
| `~/.claude/watchdog.log` | 运行日志 |
| `~/.claude/watchdog-state/` | 各会话采样状态 |
| `~/.claude/session-events.jsonl` | 卡住/恢复事件日志 |
| `~/Library/LaunchAgents/com.claude.watchdog.plist` | launchd 开机自启配置 |

## 许可证

[MIT](LICENSE) — 详见 [LICENSE](LICENSE) 文件

---

> **相关资源**: [设计文档](docs/design/)（问题背景、社区方案、渐进策略、监控计划） | [互动式导览](docs/interactive-guide.html)（架构图、流程演示和自测题） | [English](README.md)
