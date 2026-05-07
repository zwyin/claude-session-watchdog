# 空闲分类流程（v2.0.4 — V3 提示词）

## 触发

watchdog.sh 检测到 tmux 会话空闲超过阈值（默认 10 分钟），调用 `classify_idle.py`。

## 三种模式

| 参数 | 关键字 | LLM | 超时兜底 | 场景 |
|------|--------|-----|----------|------|
| （无，默认） | 兜底 | 全部走 LLM | 关键字结果 | 生产默认，准确率最高 |
| `--llm` | 先行 | ambiguous + unknown | 关键字结果 | 省 API 费用 |
| `--keyword-only` | 先行 | 不调 | 无 | 无 API key 降级 |

## 默认模式流程（--llm-only）

```
 tmux 抓 300 行原始
         │
         ▼
 _strip_noise() 过滤
 分隔线/状态栏/空行/提示符/box drawing
         │
         ▼
 取最后 50 行有效内容
         │
         ├──── 同时并行 ──────────────────┐
         │                                │
         ▼                                ▼
 关键字匹配（兜底备用）          调 LLM 分类
         │                        │
         │                        ├─ 主用：MiniMax M2.7
         │                        │    ↓ 失败
         │                        ├─ 备用：智谱 GLM-4.7
         │                        │    ↓ 失败
         │                        └─ llm_timeout
         │                                │
         │                     ┌──────────┴──────────┐
         │                     │                      │
         │                  LLM 成功              LLM 超时
         │                     │                      │
         │                     ▼                      ▼
         │              用 LLM 结果           用关键字兜底
         │                     │                      │
         │                     └──────────┬───────────┘
         │                                │
         ▼                                ▼
 输出 JSON { category, confidence, trigger, summary, last_lines }
         │
         ▼
 watchdog.sh 路由到通知模板
```

## --llm 混合模式流程

```
 50 行有效内容
        │
        ▼
 关键字匹配
        │
        ├─ decision_needed → 直接输出，不调 LLM
        ├─ task_complete   → 直接输出，不调 LLM
        ├─ ambiguous       ──┐
        └─ idle_unknown     ──┤
                             ▼
                          调 LLM（主用→备用）
                             │
                             ├── LLM 成功 → 用 LLM 结果
                             └── LLM 超时 → 用关键字结果兜底
                             │
                             ▼
                          输出 JSON
```

## LLM 提示词（完整）

```
Below is the tail of a Claude Code session. The session has just become idle
(paused at the ❯ prompt).

IMPORTANT: Focus ONLY on Claude's LAST message — the most recent output block
before the idle prompt. Earlier interactions are completed and irrelevant.

Based on Claude's LAST message only, classify why the session is idle:
1. "decision_needed" — Claude's last message asks a question, proposes options,
   or needs human decision/approval
2. "task_complete" — Claude's last message reports work finished, all tasks done,
   waiting for review
3. "idle_unknown" — Cannot determine from the last message
   (e.g. mid-execution, unclear state)

Reply in JSON only: {"category": "...", "confidence": 0.0-1.0,
"trigger": "the key phrase that triggered your classification",
"summary": "one-line Chinese summary of Claude's LAST message"}

Session output (last 50 effective lines, noise filtered):
{context}
```

## 噪音过滤规则

`_strip_noise()` 移除以下行：
- 纯分隔线（`────`、`═══` 等连续 3+ 字符）
- Box drawing（`┌─┐`、`╔═╗` 等）
- 空行和纯空格行
- 纯 `❯` 提示符
- 状态栏（`模型:`、`输入:`、`会话:`、`目录:`）
- `⏵⏵` 状态指示
- 省略号行（`...`）
- 长分隔线（`─` 连续 10+）

过滤后保留的是纯内容行：代码、commit 输出、Claude 的回复文本等。
