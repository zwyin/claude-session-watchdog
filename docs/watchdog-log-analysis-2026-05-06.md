# Watchdog 日志分析报告 — 2026-05-06

## 数据来源

- 日志文件：`~/.claude/watchdog.log`（136KB，5/5 20:19 ~ 至今）
- 事件文件：`~/.claude/session-events.jsonl`（16 条结构化记录）

---

## 统计总览（原始数据）

| 指标 | 数量 |
|------|------|
| STUCK（检测到 hang） | 7 次 |
| INTERVENE（发送干预） | 5 次 |
| RECOVERED（自行恢复） | 2 次 |

---

## 详细时间线

### 2026-05-05

| 时间 | 事件 | 会话 | 详情 |
|------|------|------|------|
| 20:36:53 | STUCK | gps-tmp | 无变化 722s (12min) |
| 20:36:56 | STUCK | kwcode | 无变化 722s (12min) |
| 20:36:58 | STUCK | main | 无变化 722s (12min) |
| 20:41:39 | STUCK | gps | 无变化 713s (11min) |
| 20:41:41 | INTERVENE | gps-tmp | hang 1008s，发送 Ctrl-C + continue |
| 20:41:48 | INTERVENE | kwcode | hang 1008s，发送 Ctrl-C + continue |
| 20:41:55 | INTERVENE | main | hang 1008s，发送 Ctrl-C + continue |
| 20:46:56 | INTERVENE | gps | hang 1017s，发送 Ctrl-C + continue |
| 22:24:01 | STUCK | gps | 无变化 717s (11min) |
| 22:28:54 | INTERVENE | gps | hang 1010s，发送 Ctrl-C + continue |

### 2026-05-06

| 时间 | 事件 | 会话 | 详情 |
|------|------|------|------|
| 01:07:40 | STUCK | gps | 无变化 628s (10min) |
| 01:09:54 | RECOVERED | gps | 自行恢复，hang 了 12min |
| 02:54:54 | STUCK | gps | 无变化 618s (10min) [hash unchanged] |
| 02:57:53 | RECOVERED | gps | 自行恢复，hang 797s 后回到 idle prompt |
| 16:14:01 | STUCK | kwcode | 无变化 612s (10min) [hash unchanged] |
| 16:18:51 | INTERVENE | kwcode | hang 902s，发送 Ctrl-C + continue |

---

## 误报过滤分析

### 判断依据

1. **v2.0.0 提交于 14:40，v2.0.1 提交于 16:31** — NBSP 修复、idle prompt 检测等 bug fix 都在 5/6 下午 4 点左右才完成
2. **早期版本没有 idle prompt 检测** — 日志中完全没有 `idle_prompt` 或 `is_idle` 相关记录，无法区分"正在处理"和"停在提示符等待输入"
3. **watchdog 被 restart 了 13 次** — 大量重启说明当时在调试阶段，代码不稳定

### 逐条判定

| # | 时间 | 会话 | 事件 | 判定 | 原因 |
|---|------|------|------|------|------|
| 1 | 5/5 20:36 | gps-tmp | STUCK | **误报** | watchdog 20:19 才启动，该会话很可能本身就在 idle prompt |
| 2 | 5/5 20:36 | kwcode | STUCK | **误报** | 同上，批量触发 = 无 idle 检测 |
| 3 | 5/5 20:36 | main | STUCK | **误报** | 同上 |
| 4 | 5/5 20:41 | gps | STUCK | **误报** | 同上，调试阶段 |
| 5 | 5/5 20:41 | gps-tmp | INTERVENE | **误报干预** | 对 idle 会话发送了 Ctrl-C |
| 6 | 5/5 20:41 | kwcode | INTERVENE | **误报干预** | 同上 |
| 7 | 5/5 20:41 | main | INTERVENE | **误报干预** | 同上 |
| 8 | 5/5 20:46 | gps | INTERVENE | **误报干预** | 同上 |
| 9 | 5/5 22:28 | gps | STUCK+INTERVENE | **可疑** | 调试阶段，多次重启中，无法确认 |
| 10 | 5/6 01:07 | gps | STUCK→RECOVERED | **可疑** | 调试阶段，但自行恢复了可能是真的 |
| 11 | 5/6 02:54 | gps | STUCK→RECOVERED | **可疑** | 同上 |
| 12 | 5/6 16:14 | kwcode | STUCK→INTERVENE | **可能真实** | v2.0.0 代码，有通知和干预回调，但 NBSP 修复尚未部署 |

### 过滤后统计

| 指标 | 原始 | 过滤后 |
|------|------|--------|
| STUCK 检测 | 7 次 | 3 次（可疑）+ 1 次（可能真实） |
| INTERVENE 干预 | 5 次 | 1 次（可疑）+ 1 次（可能真实） |
| 确认的真实 hang | — | **0-1 次**（5/6 16:14 kwcode 最可信） |

---

## 关键发现

1. **gps 是最容易 hang 的会话**：7 次 STUCK 中占 4 次（但多数为误报）
2. **5/5 晚 20:36-20:41 出现批量 hang**：gps-tmp、kwcode、main、gps 四个会话同时 hang，实际是无 idle 检测导致的批量误报
3. **干预后没有确认机制**：5 次 INTERVENE 发送了 Ctrl-C + continue，但日志中没有记录干预是否生效（没有对应的 RECOVERED 或干预结果日志）
4. **所有事件都发生在调试阶段**：v2.0.1 之后（5/6 16:31 提交，16:25 最终重启）日志里再没有出现过 STUCK 事件

## 结论

所有 STUCK 事件都发生在调试阶段（5/5 晚 ~ 5/6 下午 4 点），当时的代码没有 idle prompt 检测，无法区分"真的 hang 住"和"会话在等待用户输入"。

**要拿到可信的数据，需要从 v2.0.1 部署后（5/6 16:25）重新积累。当前的有效样本量不够下结论。**

## 建议

1. 干预动作后应增加回查，记录干预结果（成功恢复 / 仍然 hang）
2. STUCK 事件应记录会话当时的 phase（idle prompt / processing / tool_call），方便事后判断是否误报
3. 版本变更时自动写入一条版本标记日志，便于区分不同版本的行为
