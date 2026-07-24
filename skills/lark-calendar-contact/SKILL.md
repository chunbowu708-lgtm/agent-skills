---
name: lark-calendar-contact
version: 1.0.0
description: "飞书日历+通讯录 OpenAPI 契约层：Use when calling calendar/freebusy/events/create-event or contact/search-user/get-user endpoints. 调日历/通讯录 API 前先读。不覆盖：面试时间协调业务编排(见 schedule-interview)。"
metadata:
  requires:
    bins: ["lark-cli"]
  cliHelp: "lark-cli calendar --help"
---

# calendar + contact (v1) — 飞书日历与通讯录 OpenAPI 契约

**CRITICAL — 开始前 MUST 先用 Read 工具读取 [`../lark-shared/SKILL.md`](../lark-shared/SKILL.md)，其中包含认证、身份切换、权限处理和 `_notice` 处理。**

## 核心概念

- **calendar 和 contact 都在 lark-cli 业务域**：有 `calendar` / `contact` 子命令。优先级：**Shortcut（`+freebusy`/`+search-user`） > typed command（`calendar freebusys list`） > `api` 裸调**。
- **合并一个 skill 的原因**：本项目里它俩只在 `schedule-interview` 一起用（搜面试官 open_id → 查日程），独立使用场景为零。
- **跟 hire 的根本区别**：身份默认 `--as user`（不是 bot）。calendar/contact 的资源属于用户个人（我的日历、我的通讯录），bot 身份只能访问 bot 自己的空日历。

## 身份规则（跟 hire 相反，最重要）

| 操作 | 身份 | 说明 |
|---|---|---|
| calendar 所有操作（freebusy/create/agenda/list） | `--as user` | bot 查用户日历返回空列表。lark-cli calendar 域默认 user，可不传 |
| contact `+search-user`（姓名查 open_id） | `--as user` | bot 不支持，必传 `--as user` |
| contact `+search-user --user-ids`（open_id 查详情） | `--as user` | 同上 |
| contact `+get-user`（open_id 查自己/他人） | `--as user` 或 `--as bot` | **唯一接受 bot 的 contact 命令** |
| contact `user_profiles batch_query` | `--as user` | typed command |

> ❌ 反模式：`lark-cli calendar +freebusy --as bot` 返回空列表，是新手最常踩的坑。

## 关键原则

- **freebusy 参数是 `--start`/`--end`**（不是 Google 日历的 `--time-min`/`--time-max`，飞书不认）
- **`+search-user` 别加 `--has-chatted`**：陌生人面试官（没聊过）会被过滤掉
- **ISO 8601 带时区**：`2026-07-21T14:00+08:00`，不是 `2026-07-21 14:00`
- **subprocess 调 lark-cli 用全路径 .cmd**：Windows 下裸 `"lark-cli"` 报 WinError 2。封装函数 `_lark_shared.cli()` 已处理
- **freebusy 返回平铺数组**：不在 `data.` 下，需兼容 `data` / `data.items` / `data.freebusy_list` 三种结构
- **create-event 的 `--rrule`**：不支持 COUNT，必须转 UNTIL（rfc5545）

## 接口速查

完整字段/参数/返回见 [`references/calendar-endpoints.md`](references/calendar-endpoints.md) 和 [`references/contact-endpoints.md`](references/contact-endpoints.md)。

### Calendar

| 操作 | 命令 | 身份 |
|---|---|---|
| 查用户忙闲（ground truth） | `calendar +freebusy --user-id ou_xxx --start ISO --end ISO` | user |
| 建事件+邀请参会人 | `calendar +create --summary X --start ISO --end ISO --attendee-ids ou_a,ou_b` | user |
| 看日程 | `calendar +agenda [--start ISO --end ISO]` | user |
| 搜事件 | `calendar +search-event --query X --start ISO --end ISO` | user |
| 列日历 | `calendar calendars list` | user |
| 取主日历 ID | `calendar calendars primary` | user |
| 建议时段（**schedule-interview 已弃用**） | `calendar +suggestion` | user |

> ⚠️ `+suggestion` 返回飞书"挑出来的建议"不是完整空闲列表（人越空漏得越离谱）。schedule-interview 改用 `+freebusy` 反推。

### Contact

| 操作 | 命令 | 身份 |
|---|---|---|
| 姓名查 open_id | `contact +search-user --query 姓名`（**别加 `--has-chatted`**） | user |
| open_id 查详情（单个） | `contact +search-user --user-ids ou_a,ou_b` | user |
| open_id 查（bot 可用） | `contact +get-user --user-id ou_xxx` | user 或 bot |
| 批量查 profile | `contact user_profiles batch_query --data '{"user_ids":[...]}'` | user |

## 错误码

| code / 错误 | 含义 | 处理 |
|---|---|---|
| `190014` `interval too large` | suggestion 查询区间 > 7 天 | 改用 freebusy（无此限制） |
| WinError 2 | subprocess 找不到 lark-cli | 用全路径 `.cmd`（封装已处理） |
| `+freebusy` 返回空 | 用了 `--as bot` | 改 `--as user` |
| 空列表 | `+search-user` 带了 `--has-chatted` | 去掉该参数 |

## 权限 scope

| 操作 | scope |
|---|---|
| freebusy 读 | `calendar:calendar.free_busy:read` |
| event 读写 | `calendar:calendar.event:create` + `calendar:calendar.event:update` |
| search-user | `contact:user.email:read` 等（`auth login --domain contact`） |

## 反模式（不要做）

- ❌ 用 `--as bot` 查用户日历/通讯录（返回空）
- ❌ 用 `--time-min`/`--time-max`（Google 参数名，飞书不认）
- ❌ `+search-user` 加 `--has-chatted`（漏陌生人）
- ❌ 用 `+suggestion` 算空闲（漏报严重，已弃用）
- ❌ subprocess 裸调 `lark-cli`（Windows WinError 2，用全路径或 `_lark_shared.cli()`）

## 参考

- [`references/calendar-endpoints.md`](references/calendar-endpoints.md) — calendar 全量接口表（path/参数/返回/示例）
- [`references/contact-endpoints.md`](references/contact-endpoints.md) — contact 全量接口表
- [`references/calendar-pitfalls.md`](references/calendar-pitfalls.md) — freebusy 返回结构 / 实时性 / 时区格式 / 业务约定
- `../lark-shared/SKILL.md` — 认证/身份/_notice 处理
- 业务编排见 `schedule-interview` skill（freebusy 反推空闲 + 候选人时间求交集 + 草稿生成）
