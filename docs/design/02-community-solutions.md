# 02 — 社区方案全景

调研日期：2026-05-05

## 一、Claude Code 内置流式看门狗（官方）

### 相关环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CLAUDE_ENABLE_BYTE_WATCHDOG` | Anthropic API 默认开启 | 字节级看门狗：网络连接上 N 分钟没收到任何字节，自动中断连接 |
| `CLAUDE_ENABLE_STREAM_WATCHDOG` | **默认关闭**（第三方提供商必须手动开启） | 事件级看门狗：SSE 流 N 分钟没有新事件，自动中断连接 |
| `CLAUDE_STREAM_IDLE_TIMEOUT_MS` | 300000（5 分钟） | 看门狗超时时间，最低 5 分钟，更低值会被静默钳位 |

### 适用场景
- 解决"SSE 流断了但连接还在"的卡死
- 对第三方模型（GLM、Bedrock、Vertex、Foundry）尤其重要，因为这些提供商默认两个看门狗都不开

### 局限
- 只解决"流停了"的情况
- 如果模型确实在缓慢返回数据（只是很慢），不会触发
- 官方文档：https://code.claude.com/docs/en/env-vars

---

## 二、amux — Claude Code 自愈看门狗

- **GitHub：** https://github.com/mixpeek/amux
- **官网：** https://amux.io

### 功能
- 每个 Claude Code 实例包裹在独立 tmux session 中
- 解析 ANSI-stripped tmux 输出判断状态
- 自愈看门狗：检测卡死/崩溃 → 自动重启 → 重放最后一条消息
- 上下文快满时自动发 `/compact`
- YOLO 模式下自动放行权限审批
- SQLite 任务协调（防止多代理重复工作）
- Web 面板（localhost:8822）+ 手机 PWA 监控

### 检测条件

| 条件 | 动作 |
|------|------|
| 上下文低于阈值（20%/50%） | 发 `/compact`（5 分钟冷却） |
| thinking-block 损坏错误 | 杀掉 session → 重启 → 重放最后消息 |
| 闲置且有 CC_AUTO_CONTINUE=1 | 根据输出内容自动继续或审批 |
| YOLO 模式卡在权限审批 | 自动放行工具确认 |

### 安装
```bash
git clone https://github.com/mixpeek/amux && cd amux && ./install.sh
amux register myproject --dir ~/myproject --yolo
amux start myproject
amux serve  # Web 面板 localhost:8822
```

### 优劣
- **优点：** 功能最全面，单会话也支持，有 Web 面板
- **缺点：** 重启是杀进程（破坏性），只重放最后一条消息（上下文丢失）；MIT + Commons Clause 许可（商用需授权）

---

## 三、primeline-ai/claude-tmux-orchestration — tmux 编排系统

- **GitHub：** https://github.com/primeline-ai/claude-tmux-orchestration

### 功能
- 完整的 tmux 编排框架：orchestrator + 多 worker
- heartbeat.sh 后台循环监控 tmux pane 状态
- 闲置检测：`tmux capture-pane` 扫描最后 12 行，匹配 spinner 或 idle prompt
- 通过 `.ready` 文件握手防止 send-keys 冲突
- 自适应轮询：卡住 30s / 正常 120s / 闲置 300s
- rate-limit watchdog：检测 429 错误，65 秒后自动重试（带指数退避）
- 6 种 worker 状态：SAFE_TO_RESTART / DO_NOT_INTERRUPT / CONTEXT_LOW_CONTINUE / RATE_LIMITED_WAIT / ERROR_STATE / UNKNOWN

### 关键技术细节
```bash
# 闲置检测
grep -qE '(Running|thinking|Searching|Reading|Writing|Editing)'  # spinner = 忙
grep -qE '(❯[\s ]*$|>\s*$|waiting for input)'                     # idle prompt = 闲

# send-keys 正确姿势
tmux send-keys -t "$SESSION:w1" -l "$PROMPT_TEXT"  # -l = literal 模式
sleep 0.5
tmux send-keys -t "$SESSION:w1" Enter               # Enter 分开发送

# 多行文本用 paste-buffer
echo "$PROMPT" | tmux load-buffer -b "buf-w1" -
tmux paste-buffer -p -d -b "buf-w1" -t "$SESSION:w1"
tmux send-keys -t "$SESSION:w1" Enter
```

### 优劣
- **优点：** 最成熟的心跳检测 + send-keys 自动化；有 rate-limit 重试逻辑
- **缺点：** 面向多 worker 编排（偏重）；对单会话监控来说过度设计；MIT 许可

---

## 四、yurukusa/claude-code-hooks → cc-safe-setup — 安全 hooks 套件

- **GitHub（旧）：** https://github.com/yurukusa/claude-code-hooks
- **GitHub（新）：** https://github.com/yurukusa/cc-safe-setup
- **快速安装：** `npx cc-safe-setup`

### 功能
- 16 个生产级 hooks + 6 个模板，来自 700+ 小时自主运行经验
- 核心 hooks：context-monitor（上下文预警）、syntax-check（语法检查）、branch-guard（分支保护）、destructive-guard（危险命令拦截）
- 明确标注：**"Watchdog for hangs/idle — requires external tmux script"**
- 即：hooks 内部无法检测闲置，需要外部脚本配合

### 关联文章
- [I Slept While My AI Completed 88 Tasks](https://dev.to/yurukusa/i-slept-while-my-ai-completed-88-tasks-heres-what-happened-55ie)
  - 实际使用的 tmux 闲置检测 + 自动续命模式
  - 闲置检测：从 tmux pane 底部往上扫，找 `?` prompt，距底部 <= 4 行且过 90 秒 → 触发
  - 续命操作：`Escape` → `C-c` → `/exit` → `Enter`（不是杀进程）

---

## 五、lassare — Slack 远程审批 + Stop hook

- **GitHub：** https://github.com/lassare-hq/agent-configs
- **官网：** https://lassare.com

### 功能
- MCP server + hooks 组合，将 Claude Code 的权限请求和停止事件路由到 Slack
- `permission-approve.sh`：权限请求发到 Slack DM，手机上点 Approve/Deny
- `stop-notify.sh`：Claude 要停止时先问 Slack "还有别的吗？"，给新任务就继续
- `session-start.sh`：新会话启动时提醒使用 Slack 模式

### 适用场景
- AFK（不在键盘前）时需要远程审批权限或追加任务
- 已支持 Claude Code、Cursor、Gemini CLI、GitHub Copilot

---

## 六、通知工具

### ccnotifs — macOS 原生通知
- **GitHub：** https://github.com/polyphilz/ccnotifs
- 点击通知直接跳转到对应 tmux pane
- 需要安装 alerter

### tap-to-tmux — 手机推送通知
- **GitHub：** https://github.com/flavio87/tap-to-tmux
- agent 完成、需要权限、或出错时推送到手机
- 点击通知跳回对应 tmux session

---

## 七、OpenWhip — 桌面 GUI 玩具

- **GitHub：** https://github.com/GitFrog1111/OpenWhip
- Electron 桌面应用，点击鞭子图标发送 Ctrl-C + 随机吐槽话术
- **不适用 tmux 环境**（需要 GUI）
- 微信公众号文章：https://mp.weixin.qq.com/s/LMokKDY6lS2ShlhTRDjuYQ

---

## 八、其他相关资源

### samwize — Claude Code 持续运行
- **博客：** https://samwize.com/2026/03/14/how-i-got-claude-code-to-monitor-slack-while-i-was-on-holiday/
- 用 `/loop` + tmux + macOS `launchctl` 让 Claude Code 7x24 运行
- `/loop` 3 天过期，用外部调度绕过

### Dicklesworthstone/claude_code_agent_farm
- **GitHub：** https://github.com/Dicklesworthstone/claude_code_agent_farm
- 20-50 个 Claude Code 代理并行运行
- tmux 监控面板 + 锁机制协调

### absmartly/Tmux-Orchestrator
- **GitHub：** https://github.com/absmartly/Tmux-Orchestrator
- AI 驱动的 tmux 会话编排器，24/7 自主代理

---

## 方案对比

| 方案 | 检测卡死 | 不杀进程 | 通知用户 | 自动续命 | 复杂度 | tmux 兼容 |
|------|---------|---------|---------|---------|--------|----------|
| 内置 Stream Watchdog | SSE 流级别 | ✅ | ❌ | 自动重试 | 极低 | ✅ |
| amux | ✅ | ❌ 杀进程 | ❌ | ✅ 重启 | 中 | ✅ |
| tmux-orchestration | ✅ capture-pane | ✅ send-keys | ❌ | ✅ | 高 | ✅ |
| cc-safe-setup hooks | ❌ 需外部脚本 | — | ❌ | ❌ | 低 | ✅ |
| lassare | ❌ 不检测闲置 | ✅ | ✅ Slack | ✅ Stop hook | 中 | ✅ |
| ccnotifs | ❌ | ✅ | ✅ macOS | ❌ | 低 | ✅ |
| tap-to-tmux | ❌ | ✅ | ✅ 手机 | ❌ | 低 | ✅ |
| OpenWhip | ❌ 手动触发 | ✅ | ❌ | ❌ | 低 | ❌ 需 GUI |
