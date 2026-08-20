# review-contract.md — 每日对账 JSON 数据契约（单一真相源）

> 生产者：`<PROJECT_ROOT>/notes/_daily_review.py`（daily-recruit-report skill 调度）
> 产物：`<PROJECT_ROOT>/notes/_daily_review.json`
> 消费者：candidate-nurture（保温清单）、pipeline-dashboard（管道看板）、daily-recruit-report 日报
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
| `ats` | ATS 全量在途投递（**人员主数据，看板漏斗/矩阵数据源**） | `talent_id, name, job, dept, stage, dwell_days, interview_count, latest_conclusion, evaluation_conclusion, evaluation_conclusion_display`。`dept`=工作室/团队（非固定枚举，随组织调整变化，不硬编码）；`latest_conclusion`=最新面评结论(`1`=通过/`2`=不通过/`null`=未出)，是作战清单模块二「面评结论」列的数据源；`evaluation_conclusion`=业务简历评估结论（evaluations API 直读，`1`通过/`2`未通过/无此字段=无评估记录，2026-08-18 新增），`evaluation_conclusion_display` 为中文展示 |
| ~~`ats[].stage_type`~~ | **不输出**（dashboard 需要时按 `stage` 字符串归一化，见 `_normalize_stage`） | — |
| `stuck` | 卡住 ≥2 天 | `talent_id, name, stage, dwell_days, level, reason`。**reason 含"⚠️零记录"**：面试阶段零面试记录，既不在 feedback_overdue 也不在 to_advance，最易漏报。简历评估阶段的 reason 按评估结论细分：`业务评估不通过，待终止决策` / `业务评估通过，待流转面试` / `简历评估阶段无进展`（2026-08-18） |
| `to_advance` | 面评通过待推进下一轮 | `talent_id, name, stage, passed_round, dwell_days` |
| `feedback_overdue` | **最新一场**面试已过无面评（不追溯旧场）| `talent_id, name, round, round_type, interview_time, interviewers[], overdue_days, urgency`。`interviewers`=面试官名字列表（催面评直用）；`urgency`=🔴严重(≥14天)/🟠常规(4-14天)/🟡可缓(<4天) |
| `interviewer_feedback_debt` | **面试官欠面评聚合**（interview_tasks API，2026-08-18 新增；候选人视角之外补面试官视角，含他人岗位/非最新场） | 每项 `{interviewer, open_id, pending_count, ancient_count, in_scope_count, oldest_overdue_days, in_scope[]}`。`pending_count`=该面试官全部未评价任务数；`ancient_count`=陈年数（池外且面试结束>90天，终止/遗忘投递的残留，不可催）；`in_scope_count`=阻塞我流程的欠评数（**与 feedback_overdue 人数对账应一致**）；`in_scope[]`=`{name, application_id, overdue_days, interview_time}`（最新一场未出结论的阻塞者，旧轮僵尸已滤）；`oldest_overdue_days`=非陈年里最久拖欠。排序：in_scope_count 降序、同级 pending_count 降序。数据源= interviewers.json 缓存 ∪ 活跃投递面试记录反查（interviewers 官方列表接口只返回 API 更新过的人，没用） |
| `pending_evaluations` | 简历评估未提交预警（evaluations API `commit_status=2`，2026-08-18 新增） | 每项 `{name, job, dept, application_id, talent_id, evaluator_id, create_time, days_pending}`。池外（他人岗位/已终止）条目 `dept=""`、姓名来自详情富化，`name` 兜底显示 `投递:<id>` |
| `today_interviews` | 今日面试 | `talent_id, name, time, weekday, summary, job, dept, interview_type, location, meeting_room, round, interviewer, video_url`。`job`/`dept`=岗位/团队（2026-08-13 补，供 1A 时间线标注项目团队岗位，与 upcoming_interviews/ats 同源）。后 6 个字段从日历 description 解析：`interview_type`=现场面试/视频面试；`location`=线下地址（视频面试为空）；`meeting_room`=会议室房间名；`round`=轮次；`interviewer`=面试官；`video_url`=视频链接（仅视频面试）。`weekday`=今天周几（中文，如"周二"，脚本算好供 Agent 直接用，**根除手算星期高频出错**） |
| `upcoming_interviews` | **本周未来面试**（今天之后、+7天内已排期） | `talent_id, name, time, weekday, summary, job, dept, interview_type, meeting_room, round, interviewer`。`weekday`=该面试日期周几（中文）。直接遍历日历 events 兜底（含非我岗位，防 `_my_jobs.json` 过期漏报），通过 application_id 关联 ATS 富化 name/job/dept。同投递同轮次去重，按时间排序。供作战清单模块一 1D 前瞻视图 |
| `module2` | **作战清单模块二渲染树**（2026-08-14 新增，`build_module2()` 产出） | 三层树 `[{dept, count, jobs: [{job, count, candidates: [...]}]}]`。排序：工作室/岗位按在途人数降序；岗位内按变现程度倒序（待入职>Offer>HR面>终试>复试>初试>初筛），同级停留久在前，failed 沉底。每个候选人对象**自包含模块二 8 列字段**：`name, talent_id, stage, stage_bucket`(阶段列推断，规则见 SKILL.md「阶段列推断规则」), `failed`(不通过), `dwell_days, dwell_flag`(🔴≥5天/🟠≥3天/""，与 stuck level 同源), `rounds_display`(如"初试✅ 复试⏳欠6天",脚本渲染), `stuck_reason, to_advance`(bool), `feedback`(催面评详情对象或 null), `offer_status, offer_status_display`(2026-08-14：offer/待入职阶段及通过≥3轮的人实查 `offers` API，display 如"审批中"/"候选人已接受"；offer 未发起时为 None/"")。**消费者：Agent 照树渲染模块二/模块三矩阵，禁止自行分组或重算阶段**——树候选人总数恒等于 `ats` 长度（同一数据源分组，不存在树漏人） |
| `hire_bot_events` | 招聘 bot 通知事件 | （原样透传，字段以实际为准） |

## raw_messages 每项字段

`source, sender, sender_is_wubo, time, content_full, mentions_wubo, message_id, reply_to, msg_type`

- `content_full` 是完整原文，不截断（截断会破坏含多候选人的长消息拆句判读）。
- `message_id` / `reply_to` / `msg_type`：消息引用关系与类型骨架字段，判读指代对象（"这个/约下"指谁）的必需信息。
  - `reply_to` 非空 → 本条是引用回复，指代对象在 reply_to 指向的父消息（通常是一条简历文件消息）。缺失则引用类邀约无法定位候选人。
  - `msg_type == "file"` 时 `content_full` 已格式化为 `[文件] 候选人名_岗位.pdf`（从 `<file name="..."/>` 提取），可直接读候选人姓名。
  - 需展开引用链时用 `im +threads-messages-list --thread <reply_to>` 或 `im +messages-mget`（后者自动 expand thread）。
- 意图判读（邀约/拒绝/待定）是 **L3 语义判断**：交主会话 LLM 做，禁止关键词匹配。完整判读规则见 `daily-recruit-report/SKILL.md`「信号判读规则（该 AI 的地方）」节（真相源）。

## 消费约定

| 消费者 | 读什么 | 不许做什么 |
|---|---|---|
| candidate-nurture | `structured.stuck / feedback_overdue / to_advance / interviewer_feedback_debt / pending_evaluations` | 不重算预警、不读 `raw_messages` |
| candidate-nurture（信号交叉） | `notes/_signals.json`（见下方「关联产物」） | 不重新判读 raw_messages、不写 signals/decisions |
| pipeline-dashboard | `structured.ats`（主）、`stuck`（降级兜底） | 不重新拉飞书 API |
| daily-recruit-report 日报 | `summary` + `structured.*` + `raw_messages`（LLM 判读） | 姓名不做主键，一律 `talent_id` |

---

## 关联产物：_signals.json

> 本文件由 **Agent** 产出（非脚本），是 `_daily_review.json` 的补充而非替代。
> 完整契约见 [`candidate-nurture/references/signals-contract.md`](../../candidate-nurture/references/signals-contract.md)

| 项 | 值 |
|---|---|
| 路径 | `notes/_signals.json` |
| 生产者 | Agent（daily-recruit-report 早晨对账 step 3.5 + 4.5） |
| `signals` 部分 | LLM 从 `raw_messages` 判读的意图（invite/reject/hold），step 3.5 写 |
| `decisions` 部分 | 用户审查作战清单后的决策（今天约/催面评/再等等/终止），step 4.5 写 |
| 消费者 | candidate-nurture（交叉比对：未落地邀约→最高优先级；用户决策→过滤/降级） |
| 不存在时 | candidate-nurture **拒绝执行**（前置 gate 失败，不产出清单），引导用户先完成 daily-recruit-report 早晨对账。孤儿状态不降级、不兜底 |
| 与 `_daily_review.json` 的关系 | 互补不替代：`_daily_review.json` 是脚本算的确定性数据，`_signals.json` 是 LLM 判读的意图+人工决策 |

---

## 关联产物：_hire_result.json（录入结果契约）

> 生产者：`<PROJECT_ROOT>/notes/_hire.py`（candidate-entry 录入流程）
> 产物：`<PROJECT_ROOT>/notes/_hire_result.json`
> 消费者：`_hire.py` 内联的 `finalize`（录入后原子化收尾：`verify_person` 两段闸门校验——人才对账+投递对账。原"建跟踪表行"环节已随跟踪表退役删除，2026-08-18）

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
| `path` | ✅ | 简历**绝对路径**（排查/重跑用）|
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
- `_hire.py` 的 `finalize`（内部 `verify_person`）：只校验 `ok==true && talent_id` 非空的元素；用 talent_id 反查 talent 姓名邮箱（防误关联）、查投递岗位（防投错）。

---

## ~~跟踪表分层共管~~（已退役，2026-08-18）

面试跟踪表（YOUR_TRACKING_TABLE_ID）整体退役：客观层/主观层/主键层写权划分、`guard_track_write.py` hook、
`_daily_review.py --write` 回写、`_sync_tables.py` 定时任务全部移除。ATS 是唯一事实源。
决策与替代路径见 `docs/decisions/跟踪表退役.md`。

### ~~岗位表同步~~（已随定时任务取消停止，2026-08-18）

原 `_sync_tables.py` 每晚 18:30 同步岗位表「目前招聘进展/已发Offer人数/已入职人数」，任务已删除，
数据停止自动更新。需要恢复时从 `notes/_archive/track-retirement-2026-08-18/` 取回脚本单独跑。

### ATS 阶段过滤规则

`active_apps` 过滤条件（`_daily_review.py` 主流程）：
- `termination_type` 非空 → 已淘汰，排除
- `stage.type == 7`（STAGE_EMPLOYED 已入职）→ 终态，排除（入职后归 HR，不进作战清单/看板/保温）
- 其余 type（1初筛/2简历评估/4面试/5Offer/6待入职）→ 保留，正常跟进

### talent_id 主键约定

- **talent_id 是跨数据源对账主键**（治同名错配）：`_daily_review.json` 的 `structured.ats`、`_signals.json`（signals.py 自动匹配）、跟踪表时代的历史数据都以 talent_id 精确关联，不用姓名
- 同名多档案时在 signals.py 输入里显式传 `talent_id` 指定（契约见 candidate-nurture/references/signals-contract.md）
