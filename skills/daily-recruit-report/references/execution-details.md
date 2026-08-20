# 执行细节（对账/数据源/反模式）

> 执行路径细节，按需展开。

#### 对账脚本做什么（理解用，不用手做）

**多路并行拉取**（独立数据源必须并行）：
1. **ATS applications（按岗拉取，中轴）**：对 `_my_jobs.json` 的每个岗位查 `applications?job_id=X&active_status=1&page_size=200`（每岗一页，8 线程并行），只拉我的岗位活跃投递，详情含 `stage_time_list`
2. **ATS interviews**：对面试阶段(type=4)的查面试详情，算面评状态
3. **offer 状态**：对 offer/待入职阶段及通过≥3轮的人查 `offers?talent_id=`，挂 `offer_status_display`（审批中/已发出/已接受…）
4. **飞书日程 + 消息**：日程从 description 提 application_id（不靠 summary 子串）；群消息自动发现+7 天原文全量（含成员名小群）

**确定性计算**（脚本直接算）：

| 计算 | 公式 | 业务问题 |
|---|---|---|
| 卡住 | `now - stage_time_list无exit_time.enter_time > 2天` | "哪些面试流程卡了" |
| 待推进 | `interview_record_list 有 conclusion==1 且 stage=面试` | "哪些人该进下一步" |
| 催面评 | `面试 end_time 已过 且无 feedback_submit_time` | "哪些面试官该催" |
| 表落后ATS | talent_id 精确比对 | "表哪里漂移了" |

**消息意图判读**（这是"该 AI 的地方"，Agent 读 JSON 做）：
- 脚本导出 `raw_messages`（不截断，7 天）+ `structured.hire_bot_events`（系统卡片已结构化）
- Agent 用 LLM 逐条判读：邀约指令/拒绝信号/决策结论/讨论噪声

**信息源（自动发现，不硬编码）**：
- **群**：脚本调 `discover_recruit_chats()` 自动发现招聘相关群，排除 external=true 的外部社群（HR 社区等噪声）
- **私聊 + 系统通知**：钟波(Bruce) 私聊 `oc_37218905e0782cfd5f239b5106162fe0`（决策指令最密集）、飞书招聘通知 `oc_eceb45fc66a90be574089b72ffca7565`（面试反馈/接受/未到场卡片）

**输出 `notes/_daily_review.json`**：数据契约见 [`references/review-contract.md`](references/review-contract.md)（字段名、消费者约定的单一真相源）。改 `_daily_review.py` 输出结构必须同步改契约。


## 数据源拉取铁律

| 数据源 | 关键铁律 |
|---|---|
| ATS 投递 | 详情在 `data.application`（嵌套一层，不是平铺）；列表层按 `job_id + active_status=1 + page_size=200` 逐岗拉（全量分页有 200 条截断风险，按岗拉取规避）；jobs 列表才有 `page_size>20` 的坑，applications 实测 200 可用 |
| ATS 面试 | **必须带 application_id**，裸调报 1002002；conclusion 在 interview_record_list |
| 消息 | 在 `data.messages`（不是 data.items）；text 消息 content 是纯文本（@吴春波 已解析成文名）；窗口 7 天 |
| 日程 | 从 description 用正则提 application_id（比 summary 子串可靠）；start 是 `{'datetime':'...'}` 要提取 |
| 岗位列表 | **`page_size>20` 返回空**，翻页必须 ≤20；item 就是 job 本身（不是嵌套在 `item.job`） |

**性能**：6 路数据相互独立，必须 `ThreadPoolExecutor` 并行（max_workers=6）。ATS 详情查完后 talent 姓名 + job 信息也要批量并行（max_workers=8）。

**base 子命令参数**：调 base API 前先查 [`references/lark-cli-base-commands.md`](references/lark-cli-base-commands.md)（参数速查 + 易错对照表）。不许凭印象猜参数名，猜错就是盲试。

---

## 反模式（不要做）

> 完整清单见 SKILL.md「反模式」节（执行路径单一真相源，本文件不重复）。通用铁律（MSYS_NO_PATHCONV、user token 报 99991668、lark-cli 子命令查 --help 等）统一在 [`AGENTS.md`](../../../AGENTS.md)「工作方法铁律」「Windows/Shell 操作铁律」「飞书操作铁律」三节。

---

## 参考文档

- [招聘开发指南（官方）](https://open.feishu.cn/document/server-docs/hire-v1/recruitment-development-guide)
- [转移投递阶段（官方）](https://open.feishu.cn/document/server-docs/hire-v1/candidate-management/delivery-process-management/application/transfer_stage)
- [`../lark-hire/SKILL.md`](../lark-hire/SKILL.md) — **飞书招聘 OpenAPI 契约层**（接口/字段/枚举/错误码，调 hire API 前先查这里）
- [`references/review-contract.md`](references/review-contract.md) — 对账 JSON 数据契约（字段名、消费者约定的单一真相源）
- [`references/lark-cli-base-commands.md`](references/lark-cli-base-commands.md) — base 子命令参数速查 + 易错对照表
- [`references/decisions.md`](references/decisions.md) — 设计约束（每条规则为什么是这样，修改/审查时看）
- [`../CHANGELOG.md`](../CHANGELOG.md) — 版本演进与踩坑历史（考古，不进执行路径）
- [`notes/hire_record.md`](../../../../../miniwanob/notes/hire_record.md) — 录入手动排错手册（字段映射 + 命令模板）
- 作战清单样例（实战参照，头部已注明 module2 为骨架数据源）：`notes/_battle_list_sample.md`
- signals 判读样例（含错误反例对照 + evidence 倒查校验脚本）：`notes/_signals_sample.md`