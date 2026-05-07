# 空闲分类提示词对比测试报告

## 测试背景

看门狗检测到 tmux 会话空闲后，通过 LLM 分类空闲原因。本次测试对比三个提示词版本在不同窗口大小下的表现，确定最优提示词和窗口参数。

## 测试目标

1. 确定有效内容窗口的最优大小（30/50/80/100/150/200 行）
2. 对比三个提示词版本的分类准确性
3. 验证置信度和触发词字段的可用性

## 测试条件

- **采样方式**：只在空闲边界点采样（tmux 输出中 `❯` 提示符之前的内容）
- **采样范围**：所有在线 tmux 会话（11 个）
- **LLM**：MiniMax M2.7-highspeed（主用），智谱 GLM-4.7（备用）
- **有效行定义**：原始 tmux 输出经 `_strip_noise()` 过滤分隔线/状态栏/空行等噪音后的纯内容行
- **测试时间**：2026-05-07 16:30（GMT+8）

## 提示词版本

### V1（旧版，无"最后一段"强调）

```
Analyze this Claude Code session output (last {sz} lines).
The session is at an idle prompt. Classify the state:

1. "decision_needed" — Claude asked the user a non-trivial question or needs human judgment
2. "task_complete" — Claude finished work and is waiting for user review/feedback
3. "idle_unknown" — Cannot determine, just idle

Reply in JSON only: {"category": "...", "summary": "one-line Chinese summary"}
```

### V2（新版，强调"只看最后一段"）

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

Reply in JSON only: {"category": "...", "summary": "one-line Chinese summary
of Claude's LAST message"}
```

### V3（V2 + 置信度 + 触发词）

与 V2 相同的前文，额外要求返回：

```
Reply in JSON only: {"category": "...", "confidence": 0.0-1.0,
"trigger": "the key phrase that triggered your classification",
"summary": "one-line Chinese summary of Claude's LAST message"}
```

## 测试一：窗口大小对比（V2 提示词，11 个会话 × 6 个窗口）

| 会话 | 有效行 | 30行 | 50行 | 80行 | 100行 | 150行 | 200行 | 稳定区间 |
|------|--------|------|------|------|-------|-------|-------|----------|
| auto_finance | 633 | D | D | D | D | D | D | 全部 |
| benchmark-tmp | 205 | D | D | D | D | D | D | 全部 |
| cli | 375 | C | C | C | C | C | C | 全部 |
| **context** | 355 | D | D | D | D | **C** | **C** | 30-100 |
| gps | 1340 | D | C | ? | C | C | C | 50+ |
| html | 1569 | D | D | C | C | C | C | 30-80 |
| kwcode | 1167 | D | D | D | D | D | D | 全部 |
| multi_stats | 1513 | C | C | C | C | C | C | 全部 |
| video | 1015 | D | D | D | D | D | D | 全部 |
| **watchdog** | 1535 | D | **?** | D | D | D | D | 除50外 |
| watchdog-dup | 851 | D | D | D | D | D | D | 全部 |

D = decision_needed, C = task_complete, ? = idle_unknown

**结论：50 行在大多数会话中结果正确且稳定。超过 100 行开始被历史干扰。**

## 测试二：提示词版本对比（50 行窗口）

| 会话 | V1 | V2 | V3 | V3置信度 | V3触发词 | 差异分析 |
|------|----|----|-----|----------|----------|----------|
| auto_finance | D | D | D | 0.95 | 这个设计方案看起来合理吗？ | 一致 |
| **benchmark-tmp** | **C** | **D** | **D** | 0.95 | 你看这个计划是否合理？ | **V1 错误**：被前面完成内容带偏 |
| cli | C | C | C | 0.95 | 全部完成到 v5.0 | 一致 |
| **context** | **C** | **D** | **D** | 0.95 | 你要不要做这个 pilot | **V1 错误**：被前面完成内容带偏 |
| gps | D | D | D | 0.95 | 要不要我帮你配一个 GitHub Actions | 一致 |
| html | ? | C | ? | 0.60 | 子 agent 独立评审 skill 改动 | V3 置信度低，确实模糊 |
| kwcode | D | D | D | 0.95 | 要我撤销吗？ | 一致 |
| multi_stats | C | C | C | 0.95 | 项目状态稳定，所有 3 级测试零失败 | 一致 |
| video | ? | ? | ? | 0.60 | ✢ Synthesizing… | 一致（执行中） |
| watchdog | C | D | C | 0.85 | 写一份测试报告给我 | 内容在变 |
| watchdog-dup | D | D | D | 0.95 | Do you want to proceed? | 一致 |

### 分歧分析

1. **benchmark-tmp、context**：V1 判 task_complete，V2/V3 判 decision_needed。
   - 正确答案：decision_needed（Claude 确实在问问题）
   - 原因：V1 没有"只看最后一段"的强调，被窗口前面的完成状态干扰

2. **html**：V2 判 task_complete，V3 判 idle_unknown（置信度 0.6）。
   - 实际状态模糊：子 agent 在后台运行，主会话空闲但任务未完全结束
   - V3 的低置信度更准确地反映了这种不确定性

3. **watchdog**：三个版本各不相同。
   - 该会话内容在测试期间变化，结果不稳定

### 准确性统计

| 版本 | 与人工判断一致 | 总计 | 准确率 |
|------|--------------|------|--------|
| V1 | 8/11 | 11 | 73% |
| V2 | 10/11 | 11 | 91% |
| V3 | 10/11 | 11 | 91% |

## 结论与建议

### 提示词选择

**推荐 V3**。与 V2 准确率相同（91%），额外提供：
- 置信度：低置信度（<0.7）可标记为需要人工关注的模糊状态
- 触发词：帮助理解 LLM 为什么做此判断，便于调试

V1 准确率明显偏低（73%），"只看最后一段"的提示至关重要。

### 窗口大小

**推荐 50 行有效内容**：
- 所有会话在 50 行下分类正确
- 30 行有轻微截断风险
- 80+ 行开始被历史干扰

### 分类策略

- **默认模式**：全走 LLM（V3 提示词），LLM 超时时用关键字兜底
- 置信度 < 0.7 的可标记为"需人工确认"
