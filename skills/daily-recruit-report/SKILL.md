---
name: daily-recruit-report
description: >
  每日招聘晨报 skill。每日早晨跑对账脚本 → LLM 判读消息意图并落盘 _signals.json → 产出三模块作战清单（今日时效/候选人推进/管道健康度）→ 用户决策落盘。
  保温是本 skill 的后半场（candidate-nurture），由 step 6 接棒触发，不独立唤起。
  触发词：每日晨报、今日待办、今日哪些需要邀约/跟进、对账、招聘日报、作战清单、面评催收。
  不覆盖：候选人录入（candidate-entry）、简历收集归档（collect-resumes）、BOSS 打招呼（boss-recruit）、保温话术匹配与触达记录（candidate-nurture）。
---

# 每日招聘晨报

> 修改本文件前必读 `docs/skill-doc-standard.md`（冷清单铁律）：不加时间戳/人名/案例/戏剧性措辞；why 进 references/decisions.md，考古进 CHANGELOG.md。

## 最高原则

1. **ATS 是唯一事实源。** 飞书招聘（applications + interviews + stage_time_list）是一切实时事实的中轴。一切对账以 ATS 为准。
2. **该脚本的地方脚本，该 AI 的地方用 AI。** 确定性计算交给脚本；意图判读交给 LLM。两者职责不混淆。
3. **一条命令全包。** 用户跑 `python notes/_daily_review.py`，Agent 读结果 + 判读消息，直接产出最终作战清单。
4. **查实不盲调。** 接口/字段/命令/路径的查证优先级与禁令统一按 AGENTS.md「工作方法铁律」执行。
5. **不漏不错 > 速度 > 功能丰富。**
6. **不通过先问再终止。** 发现不通过面评（conclusion=2）→ 报告用户 → 用户确认后才调 terminate 接口。

> 规则背后的 why 见 [`references/decisions.md`](references/decisions.md)（维护者参考，日常执行不看）。

---

## 配置

| 项 | 值 |
|---|---|
| 对账脚本 | `notes/_daily_review.py`（ATS 中轴，多路并行） |
| 对账共享库 | `notes/_lark_shared.py`（收口 api/cli/时间转换 + hire 域直连） |
| 信号落盘脚本 | `<skill>/scripts/signals.py` |
| 晚审 | 重跑 `notes/_daily_review.py` + 读 `feedback_overdue`/`to_advance` |
| 岗位实时缓存 | `notes/_jobs_cache.json`（TTL 6h，强制刷新删此文件） |
| 岗位范围快照 | `notes/_my_jobs.json`（会过期，岗位增减后跑 `refresh_my_jobs.py`） |

`<skill>` = `…/daily-recruit-report`

全局常量（Base ID / 表 ID / 应用 ID / lark-cli 路径 / 简历落点）统一在 AGENTS.md 关键路径表维护，本表不重复。

---

## 执行顺序

### 每日早晨（最高优先级）

```
第1步  跑对账脚本        python notes/_daily_review.py
第2步  Agent 读 _daily_review.json
         ├─ 读 structured（脚本算好的确定性结果）
         └─ 读 raw_messages（群+私聊+bot 全量原文，7天）
第3步  Agent 用 LLM 判读 raw_messages 意图（邀约/拒绝/决策/讨论）
第3.5步 判读结果落盘 _signals.json（signals.py --set，自动补 talent_id、校验枚举、原子写）
第4步  Agent 合并 structured + 判读结果 → 按「作战清单输出模板」三模块结构产出
第4.5步 用户审查后，signals.py --decide 落盘 decisions
第5步  输出给用户（见「输出前自检」）
第6步  接棒 candidate-nurture 出保温清单（见下方「接棒保温」）
```

**各步细节入口：**
- 对账脚本做什么、确定性计算公式、数据源拉取铁律 → [`references/execution-details.md`](references/execution-details.md)
- 消息意图判读规则（信号边界/判读规则/落盘前自检）→ [`references/signal-rules.md`](references/signal-rules.md)
- 作战清单固定三模块结构与渲染骨架（1A~1D / 模块二8列 / 模块三）→ [`references/battle-list-template.md`](references/battle-list-template.md)
- 对账 JSON 字段契约（消费者约定单一真相源）→ [`references/review-contract.md`](references/review-contract.md)
- base 子命令参数速查 → [`references/lark-cli-base-commands.md`](references/lark-cli-base-commands.md)
- signals 格式契约 → `../candidate-nurture/references/signals-contract.md`

### 接棒保温（step 6，不可省）

作战清单输出、用户确认决策后，Agent **必须主动继续执行 candidate-nurture**——读 `_signals.json` + `_daily_review.json` 的 stuck/feedback_overdue/to_advance + `_nurture_state.json`（触达历史），产出"今天该碰谁+话术+升级标注"的保温清单。不需用户重新喊"保温"。用户确认执行后，调 `nurture_state.py --touch` 记录触达，闭合保温环。

### 晚上审查（面评跟进）

无独立脚本。晚审 = 重跑对账 + 读结果：

```bash
python notes/_daily_review.py
# 读 structured：
#   feedback_overdue → 今天面试面评未交的，催面试官
#   to_advance      → 面评通过待推进下一轮/offer 的
#   stuck(不通过)    → 不通过仍 active，先问用户再 terminate
```

---

## 面评与终止

**接口/字段/错误码契约**：统一在 [`../lark-hire/SKILL.md`](../lark-hire/SKILL.md)（飞书招聘 OpenAPI 契约层）。本文件只列执行规则。

- terminate 后 `active_status` 变 2，对账脚本已过滤 `active_status!=1`，不再出现在作战清单。
- **不通过先问用户**：发现 conclusion=2 但仍 active=1 → 报告用户确认 → 确认后才调 terminate。

---

## 输出前自检

每次输出作战清单前逐条自查，全过才能输出。格式规则完整定义在 [`references/battle-list-template.md`](references/battle-list-template.md)，本清单只列检查点：

1. 今日面试 0 条时确认不是漏（查日程 / 查 `_my_jobs.json` 是否过期）
2. @我的消息每条都判读了
3. 拒绝信号没被吞（含多候选人的消息必须拆句）
4. 模块二结构与 module2 树逐节点一致（工作室/岗位/总行数）
5. 未闭环邀约进 1B/1C
6. 只关注我负责的 + 未淘汰的
7. 用户决策已落盘（_signals.json 的 decisions 已更新）
8. 面试官意见已提取进 signals note 并体现于模块二"面试评价"列
9. 停留天数颜色照抄 dwell_flag，不自行计算
10. 模块三所有表列头完全一致（固定8列）
11. 模块一四个子模块全部表格化
12. 1A/1D 类型列逐行标注 + 时段分栏
13. 1B 拆「🆕 首次邀约 / 🔁 流程推进」两张子表

---

## 反模式

- 不要另建跟踪表 — 一切以 ATS 为中轴
- 不要全量翻页 applications 死循环 — 翻到 has_more=false 或安全上限 10 页
- 不要用关键词匹配消息意图 — 交给 LLM
- 不要 /talents?mobile= 去重 — 走 find_existing_talent
- 不要给 lark-cli --file 传绝对/中文路径 — 用 cwd 相对路径
- 不要盲信 `_my_jobs.json` 是最新的 — 它是快照，新岗位需刷新

---

## 参考文档

- [招聘开发指南（官方）](https://open.feishu.cn/document/server-docs/hire-v1/recruitment-development-guide)
- [`../lark-hire/SKILL.md`](../lark-hire/SKILL.md) — 飞书招聘 OpenAPI 契约层
- [`references/review-contract.md`](references/review-contract.md) — 对账 JSON 数据契约
- [`references/lark-cli-base-commands.md`](references/lark-cli-base-commands.md) — base 子命令参数速查
- [`references/decisions.md`](references/decisions.md) — 设计约束（why）
- [`CHANGELOG.md`](CHANGELOG.md) — 版本演进与踩坑历史（考古）
- [`notes/hire_record.md`](<PROJECT_ROOT>/notes/hire_record.md) — 录入手动排错手册
- 作战清单样例：`notes/_battle_list_sample.md`
- signals 判读样例：`notes/_signals_sample.md`
