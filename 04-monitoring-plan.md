# 04 — 量化监控方案

## 目标

用数据回答这些问题：
1. 会话卡住有多频繁？（每周几次？）
2. 每次卡住多长时间？
3. 加了 Stream Watchdog 之后是否好转？
4. 哪些项目/任务类型更容易卡住？

## 数据采集

### 事件定义

| 事件类型 | 触发条件 | 采集方式 |
|----------|---------|---------|
| `stuck` | 会话无有效输出超过 10 分钟 | 自动 |
| `auto_interrupt` | 看门狗自动 Ctrl-C 干预 | 自动 |
| `recovered` | 干预后恢复正常输出 | 自动 |

### 数据格式

JSONL 文件，每行一个事件，存储在 `~/.claude/session-events.jsonl`：

```json
{
  "timestamp": "2026-05-05T14:23:00Z",
  "event": "stuck",
  "session": "gps",
  "project": "gps",
  "duration_minutes": 35,
  "model": "GLM-5.1",
  "phase": "unknown",
  "intervention": "none",
  "recovered": false,
  "notes": "stuck: hash unchanged for 35min"
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| timestamp | ISO 8601 (UTC) | 事件发生时间 |
| event | string | 事件类型（stuck / auto_interrupt / recovered） |
| session | string | tmux session 名称 |
| project | string | 当前等同于 session 名 |
| duration_minutes | number | 卡住时长 |
| model | string | 模型名（固定 GLM-5.1） |
| phase | string | 当前阶段（固定 unknown） |
| intervention | string | 干预方式（none / auto_watchdog） |
| recovered | boolean | 是否恢复 |
| notes | string | 自动生成的描述 |

## 采集方式

### 全自动采集（已实现 v2.0.0）

看门狗脚本每 15 秒采样一次，三路联合检测：

1. **屏幕 hash**：`tmux capture-pane` 去除计时器后比较 MD5
2. **JSONL 日志**：检查会话项目目录下最近修改的 `.jsonl` 文件的最后记录时间
3. **输出 token**：从状态行提取数值，检测停滞

检测到卡住 → 自动记录事件 → 通知 → 15 分钟后自动干预。

## 数据分析

### 周报统计

```bash
# 统计本周卡住次数
cat ~/.claude/session-events.jsonl | \
  python3 -c "
import sys, json
from datetime import datetime, timedelta
events = [json.loads(l) for l in sys.stdin]
week_ago = datetime.now().isoformat(timespec='hours')[:10]
recent = [e for e in events if e['timestamp'][:10] >= week_ago]
stucks = [e for e in recent if e['event'] == 'stuck']
print(f'本周卡住次数: {len(stucks)}')
if stucks:
    durations = [e['duration_minutes'] for e in stucks if 'duration_minutes' in e]
    print(f'平均卡住时长: {sum(durations)/len(durations):.0f} 分钟')
    print(f'最长卡住时长: {max(durations):.0f} 分钟')
"
```

### 趋势对比

| 指标 | 启用 Watchdog 前 | 启用 Watchdog 后 | 变化 |
|------|-----------------|-----------------|------|
| 周卡住次数 | ? | ? | ? |
| 平均卡住时长 | ? | ? | ? |
| 人工干预次数 | ? | ? | ? |
| 自动恢复次数 | 0 | ? | ? |

## 实施步骤

1. ~~创建 JSONL 文件~~（自动创建）
2. 配置飞书通知：`cp .env.example .env` 并填入凭证
3. 启动看门狗：`./scripts/watchdog.sh start`（或通过 launchctl 开机自启）
4. 日常查看状态：`./scripts/watchdog.sh status`
5. 每日 22:00 自动发送日报到飞书

## 注意事项

- JSONL 文件会持续增长，定期检查大小
- 分析脚本可以用 Python 或 jq，按需编写
- 数据仅为自用，不包含敏感信息
