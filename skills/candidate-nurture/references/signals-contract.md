# signals-contract.md — 信号与决策 JSON 数据契约

> 生产者：Agent（recruit-followup 早晨对账时执行，非脚本）
> 产物：`F:/miniwanob/notes/_signals.json`
> 消费者：candidate-nurture（交叉比对预警数据 + 保温状态，产出保温清单）
>
> **铁律：本文件由 Agent 手动写，不由 `_daily_review.py` 产出。不同生产者 = 不同文件。**
> 与 `review-contract.md`（脚本产出的 `_daily_review.json` 契约）互补，不重复。

## 为什么需要这个文件

`_daily_review.json` 的 `structured.stuck/feedback_overdue/to_advance` 是**脚本算的确定性数据**（谁卡几天、谁欠面评）——准但慢，要等候选人进了 ATS 才算得出。

但招聘场景里有大量**信号在 ATS 之外**：业务方在群里说"张三可以约下"但还没录入 ATS；业务方说"李四先放放"但还没终止。这些信号靠 LLM 从 `raw_messages` 判读，判读完如果**不落盘**，candidate-nurture 就读不到——导致未闭环邀约被漏、已拒绝的人还在被保温。

`_signals.json` 就是**把 LLM 判读结果和用户决策落盘**，让 candidate-nurture 能交叉比对三路数据（预警 + 信号 + 保温状态），不再"两张皮"。

## 文件与新鲜度

| 项 | 值 |
|---|---|
| 路径 | `notes/_signals.json`（项目根相对） |
| 生成方式 | Agent 写（非脚本） |
| 生成时机 | recruit-followup 早晨对账 step 3.5（判读完 raw_messages 后）+ step 4.5（用户决策后） |
| 新鲜度 | 顶层 `date`（YYYY-MM-DD）；消费者用前校验 `date == 今天`，过期提示重跑 |
| 不存在时 | candidate-nurture 降级运行（只用预警+保温状态），提示"信号文件不存在，未闭环邀约可能遗漏" |

## 顶层结构

```json
{
  "date": "2026-07-10",
  "generated_at": "2026-07-10T09:35:00+08:00",
  "signals": [
    {
      "talent_id": "7657445983664163113",
      "name": "张三",
      "type": "invite",
      "source": "中台产品招聘",
      "evidence": "李袭和龙青林可以过简历筛选，我想等下bruce那边谈完...",
      "time": "2026-07-09 19:16",
      "ats_landed": false
    }
  ],
  "decisions": [
    {
      "talent_id": "7657445983664163113",
      "name": "张三",
      "decision": "今天约",
      "decided_at": "2026-07-10T09:40:00+08:00",
      "notes": ""
    }
  ]
}
```

## signals.* 字段

每项代表一条 LLM 从群消息/私聊判读出的**候选人相关意图信号**。

| 字段 | 类型 | 说明 |
|---|---|---|
| `talent_id` | string | 候选人 talent_id（主键，从 ATS 匹配；匹配不到时传空字符串，用 name 降级） |
| `name` | string | 候选人姓名 |
| `type` | string | 信号类型：`invite`（邀约）/ `reject`（拒绝）/ `hold`（暂缓）/ `discuss`（讨论噪声） |
| `source` | string | 消息来源（群名/私聊对象，对应 `_daily_review.json` 的 `raw_messages[].source`） |
| `evidence` | string | 原消息摘要（判读依据，便于复核。不是全文，截取关键句即可） |
| `time` | string | 消息时间（对应 `raw_messages[].time`，人类可读格式） |
| `ats_landed` | bool | 是否已落地到 ATS（`true`=已有 application，`false`=只有群消息没有 ATS 记录） |

### type 枚举说明

| type | 含义 | 判读依据 | candidate-nurture 怎么用 |
|---|---|---|---|
| `invite` | 业务方主动邀约 | "约下/聊聊/安排面试/推进下一步/可以面" | `ats_landed=false` → 纳入保温清单最高优先级（未闭环邀约） |
| `reject` | 业务方拒绝 | "不推进/不适合/做备选/不太合适/hold一下/先放放" | 标注"业务已拒绝，确认是否终止" |
| `hold` | 业务方暂缓 | "先看看/等一下/后面再说" | 轻量保温，不催 |
| `discuss` | 讨论性消息 | 不含明确邀约/拒绝/暂缓意图 | 忽略（不进 signals，或 type=discuss 时 consumer 跳过） |

> **判读规则**：遵循 recruit-followup SKILL.md「任务2：信号判读」的规则——拆句判读、不用关键词、注意隐藏邀约和拒绝话术。只记录 A 类（业务方邀约）和 B 类（Bruce @吴春波 + 明确动词）信号，噪声不记。

## decisions.* 字段

每项代表用户审查作战清单后对某个候选人做出的**今日决策**。

| 字段 | 类型 | 说明 |
|---|---|---|
| `talent_id` | string | 候选人 talent_id（主键） |
| `name` | string | 候选人姓名 |
| `decision` | string | 决策类型：`今天约` / `催面评` / `再等等` / `终止` |
| `decided_at` | string | 决策时间（ISO 8601） |
| `notes` | string | 备注（可选，如"等业务确认后约"） |

### decision 枚举说明

| decision | 含义 | candidate-nurture 怎么用 |
|---|---|---|
| `今天约` | 用户决定今天处理邀约 | **排除**（不重复提醒） |
| `催面评` | 用户决定今天催面评 | **排除**（不重复提醒） |
| `再等等` | 用户选择等待 | 轻量保温（不催，但不冷落） |
| `终止` | 用户决定终止该候选人 | **排除**（不再保温；Agent 可触发 `nurture_state.py --reset`） |

## 消费约定

| 消费者 | 读什么 | 不许做什么 |
|---|---|---|
| candidate-nurture | `signals`（未落地邀约+拒绝信号）+ `decisions`（用户决策） | 不重新判读 raw_messages、不写 signals/decisions |
| recruit-followup 日报 | `signals`（引用判读结果） | 不写 decisions（那是用户审查后才写的） |

## 写权方

| 部分 | 写权方 | 时机 |
|---|---|---|
| `signals` | Agent（recruit-followup step 3.5） | LLM 判读完 raw_messages 后立即写 |
| `decisions` | Agent（recruit-followup step 4.5） | 用户审查作战清单并做决策后写 |
| 整文件 | Agent | 每天早晨覆盖写（同一天的判读结果累积更新） |

> **注意**：`_signals.json` 是**当天有效**的快照，不跨天累积。历史的 signals 不需要保留（它们要么变成了 ATS 记录，要么已经失效）。如果需要历史追溯，查 `notes/history/` 下的快照（如果未来加入归档）。

## 与 _daily_review.json 的关系

| 维度 | `_daily_review.json` | `_signals.json` |
|---|---|---|
| 生产者 | 脚本（`_daily_review.py`） | Agent（LLM 判读 + 用户决策） |
| 数据性质 | 确定性计算（谁卡几天、谁欠面评） | 意图判读（谁被邀约了、谁被拒绝了） + 人工决策 |
| 数据来源 | ATS API + 跟踪表 + 飞书日程 + 消息原文 | Agent 对 `raw_messages` 的 LLM 判读 + 用户审查后的决策 |
| 新鲜度依赖 | 独立（脚本自己拉数据） | 依赖 `_daily_review.json` 的 `raw_messages`（先有对账才能判读） |
| candidate-nurture 消费 | `structured.stuck/feedback_overdue/to_advance` | `signals` + `decisions` |
| 不存在时 | nurture 拒绝运行（前置条件） | nurture 降级运行（只用预警+保温状态） |
