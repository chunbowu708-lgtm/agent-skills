# review-contract.md — 每日对账 JSON 数据契约（单一真相源）

> 生产者：`<PROJECT_ROOT>/notes/_daily_review.py`（recruit-followup skill 调度）
> 产物：`<PROJECT_ROOT>/notes/_daily_review.json`
> 消费者：candidate-nurture（保温清单）、pipeline-dashboard（管道看板）、recruit-followup 日报
>
> **铁律：改 `_daily_review.py` 的输出结构，必须同步改本文件和所有消费者；消费者不许猜字段名，一律以本文件为准。**

## 文件与新鲜度

| 项 | 值 |
|---|---|
| 路径 | `notes/_daily_review.json`（项目根相对） |
| 生成命令 | `python notes/_daily_review.py`（默认只读不写表） |
| 新鲜度 | 顶层 `date`（YYYY-MM-DD）与 `generated_at`；消费者用前必须校验 `date == 今天`，过期就提示先重跑对账 |

## 顶层结构

```
{
  "date": "2026-07-06",
  "generated_at": "...",
  "summary": { 中文键的计数摘要（展示用，勿做程序判断依据） },
  "structured": { 结构化数据，程序消费入口，见下 },
  "raw_messages": [ 群消息原文，交 Agent LLM 判读意图，脚本不判 ]
}
```

## structured.* 各列表及字段

| 列表 | 含义 | 每项字段 |
|---|---|---|
| `ats` | ATS 全量在途投递（**人员主数据，看板漏斗/矩阵数据源**） | `talent_id, name, job, dept, stage, dwell_days, interview_count, latest_conclusion`。`dept`=工作室/团队（非固定枚举，随组织调整变化，不硬编码）；`latest_conclusion`=最新面评结论(`1`=通过/`2`=不通过/`null`=未出)，是作战清单模块二「面评结论」列的数据源 |
| ~~`ats[].stage_type`~~ | **不输出**（dashboard 需要时按 `stage` 字符串归一化，见 `_normalize_stage`） | — |
| `stuck` | 卡住 ≥2 天 | `talent_id, name, stage, dwell_days, level, reason`。**reason 含"⚠️零记录"**：面试阶段零面试记录，既不在 feedback_overdue 也不在 to_advance，最易漏报 |
| `to_advance` | 面评通过待推进下一轮 | `talent_id, name, stage, passed_round, dwell_days` |
| `feedback_overdue` | **最新一场**面试已过无面评（不追溯旧场）| `talent_id, name, round, round_type, interview_time, interviewers[], overdue_days, urgency`。`interviewers`=面试官名字列表（催面评直用）；`urgency`=🔴严重(≥14天)/🟠常规(4-14天)/🟡可缓(<4天) |
| `today_interviews` | 今日面试 | `talent_id, name, time, weekday, summary, in_track, interview_type, location, meeting_room, round, interviewer, video_url`。后 6 个字段从日历 description 解析：`interview_type`=现场面试/视频面试；`location`=线下地址（视频面试为空）；`meeting_room`=会议室房间名；`round`=轮次；`interviewer`=面试官；`video_url`=视频链接（仅视频面试）。`weekday`=今天周几（中文，如"周二"，脚本算好供 Agent 直接用，**根除手算星期高频出错**） |
| `upcoming_interviews` | **本周未来面试**（今天之后、+7天内已排期） | `talent_id, name, time, weekday, summary, job, dept, interview_type, meeting_room, round, interviewer`。`weekday`=该面试日期周几（中文）。直接遍历日历 events 兜底（含非我岗位，防 `_my_jobs.json` 过期漏报），通过 application_id 关联 ATS 富化 name/job/dept。同投递同轮次去重，按时间排序。供作战清单模块一 1D 前瞻视图 |
| `track_vs_ats_gaps` | 跟踪表落后 ATS | `talent_id, name, ats_stage, track_status, match, mismatch, _rid` |
| `untracked_in_ats` | ATS 有、跟踪表无（漏建行） | `talent_id, name, job, stage, dwell_days` |
| `hire_bot_events` | 招聘 bot 通知事件 | （原样透传，字段以实际为准） |

## raw_messages 每项字段

`source, sender, sender_is_wubo, time, content_full, mentions_wubo, message_id, reply_to, msg_type`

- `content_full` 是完整原文，不截断（截断会破坏含多候选人的长消息拆句判读）。
- `message_id` / `reply_to` / `msg_type`：消息引用关系与类型骨架字段，判读指代对象（"这个/约下"指谁）的必需信息。
  - `reply_to` 非空 → 本条是引用回复，指代对象在 reply_to 指向的父消息（通常是一条简历文件消息）。缺失则引用类邀约无法定位候选人。
  - `msg_type == "file"` 时 `content_full` 已格式化为 `[文件] 候选人名_岗位.pdf`（从 `<file name="..."/>` 提取），可直接读候选人姓名。
  - 需展开引用链时用 `im +threads-messages-list --thread <reply_to>` 或 `im +messages-mget`（后者自动 expand thread）。
- 意图判读（邀约/拒绝/待定）是 **L3 语义判断**：交主会话 LLM 做，禁止关键词匹配。完整判读规则见 `recruit-followup/SKILL.md`「信号判读规则（该 AI 的地方）」节（真相源）。

## 消费约定

| 消费者 | 读什么 | 不许做什么 |
|---|---|---|
| candidate-nurture | `structured.stuck / feedback_overdue / to_advance` | 不重算预警、不读 `raw_messages` |
| candidate-nurture（信号交叉） | `notes/_signals.json`（见下方「关联产物」） | 不重新判读 raw_messages、不写 signals/decisions |
| pipeline-dashboard | `structured.ats`（主）、`stuck`（降级兜底） | 不重新拉飞书 API |
| recruit-followup 日报 | `summary` + `structured.*` + `raw_messages`（LLM 判读） | 姓名不做主键，一律 `talent_id` |

---

## 关联产物：_signals.json

> 本文件由 **Agent** 产出（非脚本），是 `_daily_review.json` 的补充而非替代。
> 完整契约见 [`candidate-nurture/references/signals-contract.md`](../../candidate-nurture/references/signals-contract.md)

| 项 | 值 |
|---|---|
| 路径 | `notes/_signals.json` |
| 生产者 | Agent（recruit-followup 早晨对账 step 3.5 + 4.5） |
| `signals` 部分 | LLM 从 `raw_messages` 判读的意图（invite/reject/hold），step 3.5 写 |
| `decisions` 部分 | 用户审查作战清单后的决策（今天约/催面评/再等等/终止），step 4.5 写 |
| 消费者 | candidate-nurture（交叉比对：未落地邀约→最高优先级；用户决策→过滤/降级） |
| 不存在时 | candidate-nurture **拒绝执行**（前置 gate 失败，不产出清单），引导用户先完成 recruit-followup 早晨对账。孤儿状态不降级、不兜底 |
| 与 `_daily_review.json` 的关系 | 互补不替代：`_daily_review.json` 是脚本算的确定性数据，`_signals.json` 是 LLM 判读的意图+人工决策 |

---

## 关联产物：_hire_result.json（录入结果契约）

> 生产者：`<PROJECT_ROOT>/notes/_hire.py`（recruit-followup 录入流程）
> 产物：`<PROJECT_ROOT>/notes/_hire_result.json`
> 消费者：`track_after_hire.py`（建跟踪表行）、`verify_hire.py`（录入闸门校验）

| 项 | 值 |
|---|---|
| 路径 | `notes/_hire_result.json` |
| 生成命令 | `python notes/_hire.py --by-name ...` 或 `python notes/_hire.py <list> --list` |
| 结构 | JSON 数组，每元素为一名候选人的录入结果 |

**每元素字段**：

| 字段 | 必有 | 含义 |
|---|---|---|
| `name` | ✅ | 姓名（解析失败时为传入的 name_hint 或"未知名"）|
| `file` | ✅ | 简历文件名 |
| `path` | ✅ | 简历**绝对路径**（供 track_after_hire.py 推断部门/职能）|
| `job_code` | ✅ | 岗位编号（A 开头）|
| `ok` | ✅ | bool，是否录入成功 |
| `talent_id` | 成功时 | 飞书人才 id |
| `name_parsed` | 成功时 | Document AI 解析出的姓名（可能比 name 更准）|
| `job_title` | 成功时 | 岗位名（用于 JOB_MAP 查映射）|
| `skipped` | 可选 | true=重复投递被跳过（`1002206 same application exist`），不当错 |
| `reused` | 可选 | true=combined_create 命中存量 talent 档案（邮箱/手机去重命中老档）。信号来源：`_warn_stale` 检测到旧附件时同步打标——见 `_hire.py::_warn_stale`。汇总计数靠它区分新建/复用 |
| `has_stale_resume` | 可选 | true=该 talent 档案上有旧简历附件（见 decisions.md「存量 talent 旧简历残留风险」）。`reused=True` 时一般 `has_stale_resume` 也 True |
| `stale_attachments` | 可选 | 旧附件列表 `[{id, name}]` |
| `error` | 失败时 | 错误信息 |

**消费约定**：
- `track_after_hire.py`：只处理 `ok==true` 的元素；读 `name/talent_id/job_title/path` 建跟踪表行。
- `verify_hire.py`：只校验 `ok==true && talent_id` 非空的元素；用 talent_id 反查 talent 姓名邮箱（防误关联）、查投递岗位（防投错）、核对跟踪表是否建行。

---

## 跟踪表分层共管

> **根因**：ATS 是唯一事实源（铁律1），但跟踪表曾接受两路部分写入（脚本 + 人工），必然漂移。
> **解法**：字段分三层，每层只有一个写权方。
> 决策背景见 `decisions.md`。

### 字段写权划分

| 层 | 字段 | 写权方 | 谁禁改 |
|---|---|---|---|
| **客观层** | 状态、当前轮次、面试时间、进入阶段日期 | `_daily_review.py --write` 或 `_sync_tables.py`（定时同步，ATS 同步） | 禁止人/agent 手改（guard_track_write.py 拦截） |
| **主观层** | 下一步动作、优先级、备注、面试官 | 人工（脚本只填空槽，不覆盖已有值） | 脚本 `--write` / `--sync` 跳过非空主观字段 |
| **主键层** | talent_id | `track_after_hire.py`（新录入）/ `backfill_talent_id.py`（存量回填） | 禁止人改 |

### 岗位表同步（YOUR_JOB_TABLE_ID）

> `_sync_tables.py` 每晚 18:30 从 ATS 聚合同步。字段映射缓存 `notes/_job_table_map.json`。

| 字段 | 写权方 | 同步逻辑 |
|---|---|---|
| 目前招聘进展 | `_sync_tables.py` | 该岗位**在途候选人**（不含已入职）的最高阶段：初筛→初面、面试→复面、offer→Offer、待入职→入职 |
| 已发Offer人数 | `_sync_tables.py` | ATS offer+待入职+已入职累计 |
| 已入职人数 | `_sync_tables.py` | ATS 已入职人数 |
| 岗位名称/团队/招聘数量/招聘负责人 | 人工 | 脚本不碰（岗位创建是业务决策，脚本不自动建行） |

**岗位名匹配**：ATS job_title 与岗位表"岗位名称"归一化匹配（去空格/全半角括号差异）。匹配不上的（ATS 有但表无）报告不建行。

---

## 关联产物：_sync_result.json（定时同步报告）

> 生产者：`notes/_sync_tables.py`（Windows 计划任务每晚 18:30 触发）
> 产物：`notes/_sync_result.json`

| 项 | 值 |
|---|---|
| 路径 | `notes/_sync_result.json` |
| 生成命令 | `python notes/_sync_tables.py`（计划任务自动触发） |
| 顶层字段 | `date, generated_at, dry_run, track{}, job{}` |
| `track` | `{synced: bool, active_candidates: int}`（跟踪表客观层同步结果） |
| `job` | `{matched, updated, total_actions, unmatched_ats[], failures[]}`（岗位表同步结果） |
| 消费约定 | 早晨 Agent 检查 `date==今天` 确认昨晚同步成功；`unmatched_ats` 提示需人工建岗位行 |

### write_back 行为（_daily_review.py --write）

- **客观字段**：全量从 ATS 同步，先读现值再写，与 ATS 不一致时以 ATS 为准覆盖（客观层 ATS 拥有），报告覆盖条数。
- **主观字段**：只填空槽位（尊重人工已填值）。
- **失败行**：写入 `notes/_write_failures.json`，post_bash hook 捕获并提示补写。

### ATS 阶段过滤规则

`active_apps` 过滤条件（`_daily_review.py` 主流程）：
- `termination_type` 非空 → 已淘汰，排除
- `stage.type == 7`（STAGE_EMPLOYED 已入职）→ 终态，排除（入职后归 HR，不进作战清单/看板/保温）
- 其余 type（1初筛/2简历评估/4面试/5Offer/6待入职）→ 保留，正常跟进

### ATS → 跟踪表客观字段映射

> "当前轮次"语义 = **当前所在轮次**(不是"已通过轮数"),按 `passed_rounds`(已通过面试的轮数)+ 是否有面试记录推断。

| ATS stage.type | 状态 | 当前轮次 |
|---|---|---|
| 1/2（初筛/评估） | 等测题 | 已发简历 |
| 4（面试）+ 有面试时间 | 已排期 | 按当前所在轮次（见下表）|
| 4（面试）+ 无面试时间 | 待安排 | 同上 |
| 5（Offer沟通） | 通过 | 四面(部门负责人) |
| 6（待入职） | 通过 | 四面(部门负责人) |

**面试阶段"当前轮次"推断表**（`passed_rounds` = 已通过面试 conclusion=1 的轮数）:

> 判据"有面试记录"= `len(interviews) > 0`(不是 `latest_interview_begin_ms`,后者是历史最大值,passed=0 但已不通过的人该字段仍非空,会误判)。

| passed_rounds | 有面试记录 | 当前轮次 |
|---|---|---|
| 0 | 否 | 待约面（还没开始面）|
| 0 | 是 | 一面(技术面)（一轮已排/进行中/等结论/未通过待终止）|
| 1 | — | 二面(业务负责人) |
| 2 | — | 三面(HR面) |
| 3 | — | 四面(部门负责人) |
| ≥4 | — | 四面(部门负责人)（兜底）|

> 轮次命名权威定义见 `docs/decisions/面试流程.md` 的"轮次命名权威表"。

### talent_id 主键约定

- `track_after_hire.py` 录入时自动写入 `talent_id` 列（fldSTYVNJ2）
- **录入侧查重也用 talent_id 主键**（与对账侧一致）：传入 talent_id 精确命中 → UPDATE；表里同 name 行 talent_id 空 → UPDATE 补 tid；表里同 name 行 talent_id 非空且不匹配 → 跳过防同名错配；双索引都不命中 → CREATE
- UPDATE 时不写 talent_id 字段（主键层不被覆盖）；只有 CREATE 才写 talent_id（含可能为空串占位）
- 存量行用 `backfill_talent_id.py` 一次性回填（dry-run 确认后 --write）
- 对账用 `talent_id` 精确匹配，降级姓名（同名多人跳过不盲填）
- `track_vs_ats_gaps` 的 `match` 字段值：`talent_id`（精确命中）或 `姓名`（降级）
