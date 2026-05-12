# 01 — 问题背景

## 现象

Claude Code 在 tmux 会话中执行任务时，planning 阶段会卡住 30-60 分钟无任何响应。
UI 表现为 spinner 持续显示（"thinking"、"planning" 等），但 token 使用量不增长，没有任何工具调用输出。

**关键特征：**
- 卡住后 Ctrl-C 中断，再发一条消息（如"继续，把任务拆小"）即可恢复
- 不是权限等待（已用 agent-yes / claude-yes 自动审批）
- 不是模型真的在思考（token 不涨，说明没有收到 API 响应）
- 同样的任务 Ctrl-C 后重试就能通过

**实际案例：**
- 2026-05-05 下午，某大型项目，从 14:00 开始 planning 阶段无实质进展
- 会话运行 7h44m，费用 $23.90，大量时间浪费在等待卡住的请求上

## 环境

| 组件 | 配置 |
|------|------|
| Claude Code 版本 | 2.1.118 |
| 模型 | third-party LLM (via API proxy) |
| 包装器 | agent-yes（claude-yes，自动审批权限） |
| 终端 | tmux（16+ 并行 session） |
| 操作系统 | macOS Darwin 24.6.0 |
| 运行方式 | 多个 tmux session，每个跑一个 claude 进程 |

## 根因分析

Claude Code 通过 HTTP/SSE 流式请求与模型通信。请求流程：

```
Claude Code CLI ──HTTP/SSE──→ API Proxy ──→ LLM Model
```

两种卡住模式（与 Anthropic 官方 API 上报告的问题一致）：

### 模式 1：SSE 流中途冻住
API 开始返回数据（spinner 渲染 "thought for 3s"），然后流静默停止。
再也收不到任何字节。Claude Code 的 HTTP 客户端没有流级读超时，永远等下去。

### 模式 2：长时间无任何响应
请求发出后，模型端（或代理层）长时间不返回任何数据。
在 Anthropic API 上表现为 epoll_wait 阻塞；在第三方代理上可能更常见。

**核心问题：Claude Code 对第三方模型的流式看门狗默认关闭。**

- `CLAUDE_ENABLE_BYTE_WATCHDOG`：字节级看门狗，默认仅对 Anthropic API 开启
- `CLAUDE_ENABLE_STREAM_WATCHDOG`：事件级看门狗，默认对所有第三方提供商关闭
- 参见官方文档：https://code.claude.com/docs/en/env-vars

## 一手 GitHub Issues

以下为 anthropics/claude-code 仓库中与 tmux 下卡住直接相关的 issue 原始记录：

### [#26224] [URGENT] Claude Code is hanging / freezing / stuck on heaps of prompts for 5-20 minutes or more
- **链接：** https://github.com/anthropics/claude-code/issues/26224
- **状态：** OPEN
- **报告日期：** 2026-02-17
- **关键描述：** Opus 4.6 发布后开始出现。Claude 卡在 "thinking" 5-20 分钟，token 使用量不涨，抓包显示挂在等 SSE 事件。有时发一条 follow-up 消息能踢活。
- **环境：** WSL2 Ubuntu + Windows Terminal + High thinking mode
- **模型：** Opus

### [#25979] Claude Code hangs indefinitely when API streaming connection stalls (no read timeout)
- **链接：** https://github.com/anthropics/claude-code/issues/25979
- **状态：** OPEN
- **报告日期：** 2026-02-15
- **关键描述：** 详细分析了两种卡住模式。进程存活但无进展（epoll_wait），只有 kill -9 能终止。与后台 agent 通知在 turn 之间到达相关。
- **建议修复：** 给 SSE 流加读超时（120s），给工具结果消费加超时，UI 层检测无进展提示用户。
- **临时方案：** 外部看门狗监控 JSONL 会话文件，5 分钟无写入就杀进程。
- **环境：** macOS + iTerm2 + tmux + Google Vertex AI + 远程 Linux 服务器

### [#20572] Claude Code freezes: static spinner, unresponsive input, ignores SIGTERM
- **链接：** https://github.com/anthropics/claude-code/issues/20572
- **状态：** OPEN（截至 2026-04-20 仍有活动）
- **报告日期：** 2026-01-24
- **关键描述：** spinner 停止动画、输入无响应、SIGTERM 无效，只有 kill -9。多个根因：SSE 流死亡、Bash 工具 mutex 死锁、僵尸进程管道泄漏。16 个 thumbs-up。
- **标签：** bug, has repro, platform:linux, area:tui, area:core

### [#53328] Claude Code CLI hangs indefinitely after a successful tool_use (tmux)
- **链接：** https://github.com/anthropics/claude-code/issues/53328
- **状态：** CLOSED（duplicate of #52544，2026-04-29）
- **报告日期：** 不详
- **关键描述：** tmux 下 tool_use 成功后 CLI 无限挂起。v2.1.123 仍复现。CLI 内置的 stuck-request 看门狗可能被 hot-polling 循环重置导致永远不触发。
- **临时方案：** `tmux send-keys -t <pane> Escape` 可以解冻

### [#43530] Notification hook fires ~8 seconds after permission prompt appears
- **链接：** https://github.com/anthropics/claude-code/issues/43530
- **状态：** CLOSED（duplicate of #5186，2026-04-08）
- **报告日期：** 2026-04-04
- **关键描述：** Notification hook 脚本本身 100ms 执行完，但 Claude Code 内部 8 秒后才触发事件。Stop hook 有类似延迟。不适合做实时通知。

### [#13294] Anthropic API: Extended Thinking timeout without user notification
- **链接：** https://github.com/anthropics/claude-code/issues/13294
- **关键描述：** Extended thinking 超时无用户通知。建议给 prompt/tool 调用加不活跃超时。

### [#22227] `claude --resume` session picker hangs indefinitely (tmux)
- **链接：** https://github.com/anthropics/claude-code/issues/22227
- **状态：** CLOSED（inactive，2026-03-03）
- **临时方案：** 用 `claude --continue` 或 `claude --resume <session-id>` 代替裸 `--resume`

## 与 Anthropic 官方 API 的区别

使用第三方 API 代理时：

1. **流式看门狗默认关闭** — byte watchdog 仅对 Anthropic API 默认开启，stream watchdog 对所有第三方默认关闭
2. **代理层是额外故障点** — API 代理可能有自己的超时、限流、错误处理
3. **错误信息可能不同** — 代理层可能吞掉或转换 Anthropic 的错误
4. **Claude Code 的内置重试可能不适用** — 某些错误模式是代理特有的
