# calendar 接口全量表

> 飞书日历 API 完整契约。优先调 `schedule-interview/scripts/match_schedule.py` 封装，裸调时查本表。

所有命令 git-bash 环境下走 `_lark_shared.cli()`（已设 `MSYS_NO_PATHCONV=1`）。身份默认 `--as user`。

## calendar +freebusy

查用户真实忙闲段（ground truth）。

**Shortcut**：`calendar +freebusy --user-id ou_xxx --start ISO --end ISO --format json`

| 参数 | 必填 | 说明 |
|---|---|---|
| `--user-id` | ✅ | `ou_` 前缀的 open_id。不传=当前用户 |
| `--start` | ❌ | ISO 8601（默认今天 00:00）。**不是 `--time-min`** |
| `--end` | ❌ | ISO 8601（默认 start 当天 23:59） |
| `--format` | ❌ | `json`（默认）/`pretty`/`ndjson` |
| `--as` | ❌ | 默认 `user`。**别用 bot，返回空** |

**返回**：平铺数组（不在 `data.` 下），每项：
```json
{
  "start_time": "2026-07-21T10:00+08:00",
  "end_time": "2026-07-21T10:30+08:00",
  "rsvp_status": "accept|tentative|decline|needs_action|removed"
}
```

⚠️ 返回结构可能包在 `data` / `data.items` / `data.freebusy_list` 下，代码要三种都兼容（见 [`calendar-pitfalls.md`](calendar-pitfalls.md)）。

```bash
lark-cli calendar +freebusy --user-id ou_5fb5... --start 2026-07-21T00:00+08:00 --end 2026-07-21T23:59+08:00 --format json
```

## calendar +create

建事件 + 邀请参会人。

**Shortcut**：`calendar +create --summary X --start ISO --end ISO --attendee-ids ou_a,ou_b`

| 参数 | 必填 | 说明 |
|---|---|---|
| `--summary` | ❌ | 标题（不含时间/地点/人名） |
| `--start` | ✅ | ISO 8601 |
| `--end` | ✅ | ISO 8601 |
| `--description` | ❌ | 描述（支持 HTML） |
| `--attendee-ids` | ❌ | CSV，支持 `ou_`（用户）/`oc_`（群）/`omm_`（会议室） |
| `--calendar-id` | ❌ | 默认主日历 |
| `--rrule` | ❌ | rfc5545。**不支持 COUNT，必须转 UNTIL** |
| `--dry-run` | ❌ | 预览不发 |

**自动默认值**：`attendee_ability=can_modify_event`、`free_busy_status=busy`、`reminders=[{minutes:5}]`、`vchat={vc_type:"vc"}`（自动加视频会议）。

**失败保护**：参会人添加失败时回滚已建空事件。

```bash
lark-cli calendar +create --summary "面试-张三-前端岗" \
  --start 2026-07-21T15:00+08:00 --end 2026-07-21T16:00+08:00 \
  --attendee-ids ou_5fb5...,ou_ea48... --description "视频面试"
```

## calendar +agenda

看日程（默认今天）。

```bash
lark-cli calendar +agenda                              # 今天
lark-cli calendar +agenda --start 2026-07-21 --end 2026-07-25
```

## calendar +search-event

按关键词/时间/参会人搜事件。

```bash
lark-cli calendar +search-event --query "站会" --start 2026-07-21 --end 2026-07-25
```

## calendar calendars list / primary

```bash
lark-cli calendar calendars list --page-size 50       # 列所有日历
lark-cli calendar calendars primary                    # 取主日历 ID（不过滤）
```

`calendars list` 参数：`--page-size`（50-1000，默认 500）、`--page-token`、`--sync-token`。

## calendar +suggestion（⚠️ 已弃用）

**schedule-interview 不用这个接口**，记录在此仅为避坑。

```bash
lark-cli calendar +suggestion --user-id ou_xxx --start ISO --end ISO
```

**弃用原因**（实测）：
- 返回飞书"挑出来的建议"不是完整空闲列表
- 人越空漏得越离谱（周五全天只忙 1 小时的人，suggestion 只返回零星时段甚至"无空档"）
- 查询区间 ≤ 7 天（超了报 `190014 interval too large`）

**替代方案**：`+freebusy` 拿真实忙碌段，本地反推空闲段（`全天 - 会议 - 午休`）。

## calendar +update / +rsvp / +meeting / +room-find

低频操作，详细参数查 `lark-cli calendar +update --help`：

| 命令 | 用途 |
|---|---|
| `+update` | 增量改事件（加/删参会人，不改整体） |
| `+rsvp` | 回复事件邀请（accept/decline/tentative） |
| `+meeting` | 取事件的会议信息（meeting_id / meeting_note） |
| `+room-find` | 找可用会议室 |
