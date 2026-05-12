# Watchdog 检测流程设计

## 核心原则

**10 分钟没动静就检测**——不管 session 处于什么状态（空闲或执行中），
只要终端输出 10 分钟无有效变化，就触发一次检测，判断具体情况。

## 检测流程（统一，不分两套）

```
每 15 秒采样一次
  │
  ├─ 终端输出有变化 → 重置计时器，清除状态
  │
  └─ 终端输出无变化，持续 10 分钟
       │
       ├─ session 在 idle prompt（空闲）
       │     │
       │     └─ 空闲分类（classify_idle.py）
       │           │
       │           ├─ 第一级：关键字匹配（本地，快速）
       │           │     ├─ decision_needed：Claude 等待用户决策
       │           │     ├─ task_complete：任务完成，等待验收
       │           │     ├─ ambiguous：关键特征冲突，交给第二级
       │           │     └─ idle_unknown：无明确匹配
       │           │
       │           └─ 第二级：LLM 语义分析（仅 ambiguous 触发）
       │                 └── 主备双端点（dual LLM endpoints (primary + fallback)）
       │
       └─ session 在执行中（非空闲）
             │
             └─ 卡住检测（三信号联合）
                   │
                   ├─ 路径 A：屏幕 hash 不变（经典检测）
                   ├─ 路径 B：hash 在变，但 JSONL 停滞 + token 不变
                   │         （计时器/动画在转，但 API 实际已挂起）
                   │
                   └─ 分级响应
                         ├─ 10 分钟 → 告警通知
                         └─ 15 分钟 → 自动干预（Ctrl-C + 继续任务）
```

## 当前实现状态 vs 设计

### 已实现（正确）
- 执行中卡住检测：hash / JSONL / token 三信号联合，10 分钟告警 + 15 分钟干预
- 空闲分类：关键字匹配 + LLM 降级，分类 decision_needed / task_complete / idle_unknown
- 噪音过滤：去除计时器、分隔线、状态栏等干扰行
- 三信号检测覆盖了"表面在动但实际卡住"的场景

### 需要注意的问题
- **当前空闲和卡住是互斥分支**：`is_idle_prompt` 为 true 时直接 `continue`，
  不进卡住检测逻辑。这在当前实现中是正确的——空闲 prompt 下 hash 不会变，
  不需要走 hash 检测，走空闲分类即可。
- **检测周期统一为 10 分钟**：空闲分类（`IDLE_CLASSIFY_THRESHOLD`）
  和卡住告警（`STUCK_THRESHOLD`）都是 600s。

## 配置参数

| 参数 | 值 | 含义 |
|------|-----|------|
| `SAMPLE_INTERVAL` | 15s | 采样间隔 |
| `STUCK_THRESHOLD` | 600s (10min) | 无变化 → 告警通知 |
| `INTERVENE_THRESHOLD` | 900s (15min) | 无变化 → 自动干预 |
| `INTERVENE_COOLDOWN` | 600s (10min) | 干预冷却期 |
| `IDLE_CLASSIFY_THRESHOLD` | 600s (10min) | 空闲 → 触发分类 |
| `JSONL_STALE_THRESHOLD` | 600s (10min) | JSONL 停滞判定 |

## 空闲分类输出

| 分类 | 含义 | 通知模板 |
|------|------|----------|
| `decision_needed` | Claude 等待用户做非 trivial 决策 | 需要你的决策 |
| `task_complete` | 任务完成，等待验收 | 任务已完成 |
| `idle_unknown` | 无法判断，单纯空闲 | 空闲中 |
| `ambiguous` → LLM | 关键字冲突，交给 LLM 判断 | 根据 LLM 结果选模板 |

## 早晚报时段

| 报告 | 触发时间 | 覆盖时段 |
|------|----------|----------|
| 早报 | 08:00 | 昨晚 22:00 → 今早 08:00 |
| 晚报 | 22:00 | 当天 08:00 → 22:00 |

两个触发时间点（08:00、22:00）就是全天的时间分界线。
