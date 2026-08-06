# calendar 踩坑（实测固化）

> 不是 API 字段说明，是"调用时容易犯的错"。schedule-interview 实战踩过。

## 1. freebusy 返回结构（三种都要兼容）

`calendar +freebusy` 的 JSON 返回，`freebusy_list` 可能挂在三个位置之一：

```python
items = data.get("data", [])
if isinstance(items, dict):
    items = items.get("items", []) or items.get("freebusy_list", [])
```

三种形态：
- `{"data": [{start_time, end_time}, ...]}` — 平铺数组（最常见）
- `{"data": {"items": [...]}}` — 包一层 items
- `{"data": {"freebusy_list": [...]}}` — 包一层 freebusy_list

`match_schedule.py:269-274` 已做三态兼容。

## 2. freebusy 是实时的（不是脚本 bug）

同一区间隔几分钟查两次，结果可能不同——面试官期间又往日历加了会。

**含义**：
- 输出必须明文展示忙碌段（让用户能跟飞书日历对照核实）
- 用户报"输出和日历对不上"→ 重跑即可（日程可能刚变）
- 不要做结果缓存

## 3. ISO 8601 时区格式

**正确**：`2026-07-21T14:00+08:00`（带 `T` 和 `+08:00`）
**错误**：`2026-07-21 14:00`（空格分隔，无时区）

freebusy 的 `--start`/`--end` 接受 date-only（`2026-07-21`）默认当天 00:00 / 23:59，但精确时段必须完整 ISO。

## 4. 工作时间 / 午休（业务约定，非 API 约定）

飞书 calendar API **不定义**工作时间和午休——这些是 schedule-interview 的业务规则：

| 约定 | 值 | 说明 |
|---|---|---|
| 工作时段 | 09:00-18:00 | freebusy 反推后按此时段截断（否则会约到凌晨） |
| 午休 | 12:00-13:30 | 作为"忙碌段"加入反推（不算真实会议） |
| 黄金时段 | 11:00, 15:00-18:00 | best_slot() 优先选这些时段 |
| 避开 | 09:00 过早 / 12-13 午休 / 18:00+ 偏晚 | 默认排除 |

晚上面试有效时，`match_schedule.py --work-end 21` 扩展工作时段。

## 5. 跨天空闲段按自然天拆分

freebusy 反推出的空闲段可能横跨午夜（如 `22:00-次日 09:00`），必须按自然天拆分后再按工作时间截断，否则会显示 `22:00-09:00` 这种倒序段。

`match_schedule.py:300-310` 已实现拆分逻辑。

## 6. subprocess 调 lark-cli 用全路径

Windows 下裸 `"lark-cli"` 报 `WinError 2`（找不到文件）。必须用：

```python
CLI = os.environ.get("LARK_CLI_PATH", "lark-cli")
```

或走 `_lark_shared.cli()`（已封装全路径 + `MSYS_NO_PATHCONV=1` + utf-8 encoding）。

## 7. `+suggestion` 别用（已弃用）

曾用 `calendar +suggestion` 查空闲，实测漏报严重：
- 返回飞书"挑出来的建议"，不是完整空闲列表
- 人越空漏得越离谱（周五只忙 1 小时的人，suggestion 可能只返零星时段）
- 查询区间 ≤ 7 天（超了报 `190014`）

**替代**：`+freebusy` 拿真实忙碌段，本地反推空闲段（`全天 - 会议 - 午休`）。这是 schedule-interview 的核心正确性保证，决策记录见 `schedule-interview/references/decisions.md`。
