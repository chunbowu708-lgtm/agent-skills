---
name: recruit-followup
description: >
  候选人跟进全流程：每日对账（ATS 中轴）→ LLM判读消息意图并落盘 _signals.json → 录入飞书招聘 → 面试流转 → 面评同步 → 跟踪表更新 → 日报。
  触发词：跟进候选人、录入候选人、安排面试、扫群、招聘日报、今日待办、面评、催面评、推进流程、对账。
  只要用户提到候选人面试、邀约、飞书招聘录入、招聘跟踪表、招聘群扫描，就使用这个 skill。
  不覆盖：简历收集归档（collect-resumes）、BOSS 打招呼（boss-recruit）、发面试链接（人工关键操作）、
  保温话术匹配和触达记录（见 candidate-nurture，本skill只产出信号数据供其消费）。
---

# 候选人跟进

## 最高原则

1. **ATS 是唯一事实源，不是跟踪表。** 飞书招聘（applications + interviews + stage_time_list）是实时事实；跟踪表是人工维护的、会漂移的。一切对账以 ATS 为中轴。

2. **该脚本的地方脚本，该 AI 的地方用 AI。** 确定性计算（谁卡几天、谁该推进、面评状态）交给脚本；意图判读（这条消息是邀约还是拒绝）交给 LLM。两者职责不混淆。

3. **一条命令全包。** 用户跑 `python notes/_daily_review.py`，Agent 读结果 + 判读消息，直接产出最终作战清单。用户只看清单 + 确认异常。

4. **不猜，按优先级查实。** 凡是接口路径、字段名、token 用法、返回结构、命令语法——先看现成的再造轮子，严格按下面顺序，不许跳级试探：
   - **① 有脚本/skill 的看脚本**：`_hire.py`/`_lark_shared.py`/`_daily_review.py` 已封装好鉴权、路径、字段映射，import 其函数复用。skill 文档（本文件 + `CLAUDE.md` + `notes/hire_record.md`）也写了铁律和易错点。
   - **② 脚本没有的查官方文档**：[招聘开发指南](https://open.feishu.cn/document/server-docs/hire-v1/recruitment-development-guide)，URL 末尾加 `.md` 拉 markdown。
   - **③ 都没有再最小化实测**：用最小请求二分定位，这是最后手段。
   - ❌ **禁止试探式盲调**：不知道接口就试一个看 404 再换、不知道 token 就乱包一层——这是最大的浪费时间来源。

5. **不漏不错 > 速度 > 功能丰富。**

6. **不通过先问再终止。** 发现不通过面评（conclusion=2）→ 报告用户 → 用户确认后才调 terminate 接口。用户没确认不擅自动 ATS。

> 规则背后的 why 见 [`references/decisions.md`](references/decisions.md)（维护者参考，日常执行不看）。

---

## 配置

| 项 | 值 |
|---|---|
| 项目根 | `F:/miniwanob` |
| lark-cli | `C:/Users/wuchunbo/AppData/Roaming/npm/lark-cli.cmd`（subprocess 必须全路径） |
| 飞书招聘应用 | `cli_aa84d938c8259bd6`（hire/document_ai 域用 bot 身份） |
| 跟踪表 | Bitable `KRAQbxQR0aj2ymsuZRvcwHLdnKQ` / 表 `tblFZcRms15NlGkr` |
| 候选人主库 | 同 base / 表 `tbl31hGzsIJrzTWN` |
| 招聘岗位表 | 同 base / 表 `tbl0f9ynYsdQhYDo`（field 映射缓存 `notes/_job_table_map.json`） |
| JD库表 | 同 base / 表 `tbl1AW11ezEQsADH`（人工维护展示层，暂不自动同步） |
| **对账脚本** | `notes/_daily_review.py`（ATS 中轴，6 路并行） |
| **表格同步脚本** | `notes/_sync_tables.py`（复用对账引擎，同步跟踪表客观层+岗位表，定时调度） |
| **对账共享库** | `notes/_lark_shared.py`（收口 api/cli/extract_json/时间转换，所有脚本统一 import） |
| **录入脚本** | `notes/_hire.py`（一键全量录入，含去重+批量） |
| **建跟踪表** | `<skill>/scripts/track_after_hire.py`（录入后建行，吃 `_hire_result.json`，幂等） |
| **录入闸门** | `<skill>/scripts/verify_hire.py`（read-only 三重对账，录入后必跑） |
| **换最新简历** | `<skill>/scripts/swap_resume.py`（存量 talent 投递绑旧版时，terminate+重建让投递用最新附件） |
| **晚审脚本** | `notes/_evening_review.py`（扫面评 conclusion、催未提交、不通过先问再终止） |
| **定时同步** | Windows 计划任务 `MiniwaRecruitDailySync`（每天 18:30 跑 `_sync_tables.py`，日志 `notes/_sync_log.txt`） |
| 岗位缓存 | `notes/jobs_map.json`（可能过期，以 API 实查为准） |
| 简历落点 | `F:/Users/wuchunbo/Downloads`（先查这里），兜底 `data/在招岗位候选人管理/`（`find data -iname "*姓名*"`） |

`<skill>` = `C:/Users/wuchunbo/.agents/skills/recruit-followup`

---

## 执行顺序

### 每日早晨（最高优先级）

> **背景**：每晚 18:30 计划任务 `MiniwaRecruitDailySync` 已自动跑 `_sync_tables.py`，把 ATS 数据同步到跟踪表（客观层）+ 岗位表（招聘进展/offer/入职人数）。早晨 Agent 读到的表格已是最新状态——但仍需重跑对账拿最新消息做意图判读（18:30 后的消息、今天面试结果）。

```
第1步  跑对账脚本        python notes/_daily_review.py
         （表格客观层昨晚已同步，本步重算今日作战数据 + 拉最新消息）
第2步  Agent 读 _daily_review.json
         ├─ 读 structured（脚本算好的确定性结果）
         └─ 读 raw_messages（群+私聊+bot 全量原文，7天）
第3步  Agent 用 LLM 判读 raw_messages 意图（邀约/拒绝/决策/讨论）
第3.5步 Agent 写 _signals.json（判读结果落盘，格式见 candidate-nurture/references/signals-contract.md）
第4步  Agent 合并 structured + 判读结果 → 产出作战清单（红黄绿分级）
第4.5步 用户审查作战清单后，Agent 更新 _signals.json 的 decisions（记录用户决策）
第5步  输出给用户（见「输出前自检」）
第6步  接棒 candidate-nurture 出保温清单（见下方「接棒保温」）
```

> **接棒保温（step 6，不可省）**：作战清单输出后，Agent **必须主动继续执行 candidate-nurture**——读 `_signals.json`（刚写的）+ `_daily_review.json` 的 stuck/feedback_overdue/to_advance + `_nurture_state.json`（触达历史），产出"今天该碰谁+话术+升级标注"的保温清单。
>
> **为什么必须接棒**：保温清单和作战清单是**同一个早晨决策流的两半**——作战清单管"今天处理谁"（邀约/录入/推进），保温清单管"今天碰谁"（候选人保温/催面评）。不接棒 = 信号文件白写、保温状态空转、候选人静默流失。这是之前最大的断点：recruit-followup 写完 `_signals.json` 就停了，nurture 在等一个永远不会来的触发。
>
> **接棒方式**：Agent 直接按 candidate-nurture 的 SKILL.md 工作流执行（不需用户重新喊"保温"）——读三路数据 → 排优先级 → 匹配话术 → 产出保温清单给用户确认。用户确认执行后，调 `nurture_state.py --touch` 记录触达，闭合保温环。

**用户看到的是分级作战清单**，不是数据大表。Agent 负责把"今天注意力放哪"讲清楚。

> ⚠️ **作战清单不是终点——step 6 必须接棒保温**。作战清单管"今天处理谁"，保温清单管"今天碰谁"。两个清单在同一次早晨对账里**连续产出**，不是分开的两次任务。Agent 输出作战清单、用户确认决策后，**立即继续**按 candidate-nurture 出保温清单。

#### 表格自动同步（每晚 18:30，无人值守）

计划任务 `MiniwaRecruitDailySync` 每晚 18:30 跑 `notes/_sync_tables.py`：
- **跟踪表**：复用 `_daily_review.write_back`，客观层从 ATS 全量同步（状态/轮次/面试时间/进入阶段日期），主观层只填空槽（不覆盖人工值）
- **岗位表**：从 ATS 按岗位聚合，更新「目前招聘进展/已发Offer人数/已入职人数」
- **不做意图判读**：那是 LLM 的活，定时任务只做确定性同步，`_signals.json` 仍由早晨 Agent 会话产出
- 日志：`notes/_sync_log.txt`；报告：`notes/_sync_result.json`
- 手动触发：`schtasks /run /tn MiniwaRecruitDailySync`
- 注册/卸载：`powershell -ExecutionPolicy Bypass -File notes/setup_daily_sync.ps1`

#### 对账脚本做什么（理解用，不用手做）

**6 路并行拉取**（独立数据源必须并行）：
1. **ATS applications（全量分页，中轴）**：所有活跃投递，含 `stage_time_list`
2. **ATS interviews**：对面试阶段(type=4)的查面试详情，算面评状态
3. **跟踪表**：含 talent_id 列
4. **飞书日程**：从 description 提 application_id（不靠 summary 子串）
5. **群消息（自动发现 + 7 天）**：原文全量导出，不做关键词分类
6. **私聊 + 飞书招聘 bot**：Bruce 私聊 + 系统通知，原文全量导出

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
- **不用关键词**（"安排不太合适"含"安排"但是反对；"需要hr帮忙面一轮"无邀约词但是真邀约）

**信息源（自动发现，不硬编码）**：
- **群**：脚本调 `discover_recruit_chats()` 自动发现招聘相关群，排除 external=true 的外部社群（HR 社区等噪声）
- **私聊 + 系统通知**：钟波(Bruce) 私聊 `oc_37218905e0782cfd5f239b5106162fe0`（决策指令最密集）、飞书招聘通知 `oc_eceb45fc66a90be574089b72ffca7565`（面试反馈/接受/未到场卡片）

**输出 `notes/_daily_review.json`**：数据契约见 [`references/review-contract.md`](references/review-contract.md)（字段名、消费者约定的单一真相源）。改 `_daily_review.py` 输出结构必须同步改契约。

#### 信号判读规则（该 AI 的地方）

**信号边界（只跟这两类，其余噪声）**：
- **A 类**：业务方主动邀约（"约下/聊聊/安排面试/推进下一步"）
- **B 类**：Bruce/钟波 @吴春波 + 明确动词
- ❌ Bruce 只发简历没 @我 → 不跟

**判读规则**（对抗审查证实的坑，必须遵守）：
1. **拆句判读**：一条消息可能含多个意图（"第一位可邀约，第二位不适合，第三位不推进" = 1邀约+2拒绝）
2. **不用关键词**："安排不太合适"含"安排"但是反对；"没有时间帮忙面试"含"面试"但是推迟
3. **拒绝话术**：业务用"不推进/不适合/做备选/不太合适/hold一下/先放放"，全不在关键词表里，但都是拒绝
4. **隐藏邀约**："需要hr帮忙面一轮"无邀约词，但是真邀约
5. **系统卡片**：飞书招聘通知的卡片已在 `structured.hire_bot_events` 结构化，直接用，不重新判读

**判读结果落盘（step 3.5）**：判读完成后必须写入 `notes/_signals.json`（不是口头呈现在作战清单里就完了）。这是 candidate-nurture 交叉比对的数据源——不落盘 nurture 读不到意图。写入格式见 [`candidate-nurture/references/signals-contract.md`](../../candidate-nurture/references/signals-contract.md)。

**用户决策落盘（step 4.5）**：用户审查后，Agent 按决策更新 `_signals.json` 的 `decisions` 部分。candidate-nurture 读 decisions 过滤——用户已决定"今天约"的不重复提醒，决定"终止"的不再保温。不落盘 = nurture 和早晨作战清单两张皮。

---

### 录入候选人（业务邀约后）

**统一入口（日常用这个）**——`--by-name` 自动按优先级查找简历：

```bash
第1步  python notes/_hire.py --by-name 白向庭,李毅 --job 海外游戏数据产品经理
第2步  python scripts/track_after_hire.py          # 建跟踪表行+写 talent_id
第3步  python scripts/verify_hire.py               # 闸门：必跑，不过则 STOP
第4步  (可选) verify_hire 告警"旧简历待清理"时 → python scripts/swap_resume.py --talent <id> --pdf <最新简历>
       # 存量 talent 投递绑了旧版，terminate+重建让它用最新附件（仅早期阶段投递，见下方铁律）
```

`--by-name` 三级级联查找（每级找不到自动降级）：

| 优先级 | 来源 | 匹配规则 |
|--------|------|----------|
| ① | 本地归档库 `data/在招岗位候选人管理/` | 文件名以「姓名_」开头（collect-resumes 归档规范）|
| ② | Downloads `F:/Users/wuchunbo/Downloads` | 文件名含姓名即可 |
| ③ | **飞书群聊/私聊**（自动搜+下载）| `im +messages-search` 搜姓名的文件消息，下载到 Downloads |

> ③ 简历只在群里发过、本地没有时，自动从飞书群聊搜到并下载。底层调 `_download_chat_file.py`。

**完整路径**（跨岗位批量，或要精确控制每份简历路径时用）：

```bash
第0步  python notes/_hire.py --jobs 关键词                  # 查 job_code
第1步  写清单 notes/_hire_list.txt（每行：简历路径|岗位编号|姓名）
第2步  python notes/_hire.py notes/_hire_list.txt --list   # 录人才+建投递
第3步  python scripts/track_after_hire.py                   # 建跟踪表行+写 talent_id
第4步  python scripts/verify_hire.py                        # 闸门：必跑，不过则 STOP
```

#### 录入铁律

- **原子流程，4 步不许拆开**。建跟踪表割裂成独立手敲步骤 = AI 每次从零试错。详见 decisions.md。
- **岗位准入闸门**：录入目标岗必须同时满足「我创建 + 开放中(active_status=1)」。`_hire.py` 内置 `job_filter_ok()` 自动校验，即使直接传 A 编号也会校验状态+归属。暂停/已关闭的岗飞书删不掉，靠准入过滤而非手工避让。
- **job_code 自己查，别问用户**：`python notes/_hire.py --jobs 关键词` 一条命令搞定；`--by-name` 的 `--job` 直接传岗位关键词（脚本自动解析成 code 并校验）。`jobs_map.json` 会过期，别依赖它。同名岗位才需用户确认。**绝不手撸 jobs API。**
- **`verify_hire` 不过（🔴 STOP）不许继续**——talent 误关联/投递缺失/投错岗位都是真问题。
- **Document AI 解析 → combined_create 一次写全**，不要先 create 再 update，不要传附件指望自动解析（实测不解析）。
- **talent_id 是对账主键**：`track_after_hire.py` 录入时自动写入 talent_id 列，对账用它精确匹配（不用姓名，治同名错配）。
- **重复录入安全**：同一候选人重录时，`create_application` 返回 `1002206 same application exist`，脚本识别为"跳过"（⏭️）不当错。
- **新岗位首次录入**：跟踪表"岗位"下拉可能没这个选项 → 先用 `base +field-update`（PUT 全量语义，必带原有所有选项+新增项）补选项，再在 `track_after_hire.py` 的 JOB_MAP 加映射。两步别只做一步。
- **存量 talent 旧简历风险（关键坑，2026-07-23 实测修正）**：录入时复用存量 talent，本次上传的新附件会挂到档案，但**投递的 `talent_attachment_resume_id` 绑的是旧附件**（面试官首屏看到旧简历）。`_hire.py` 会告警 `⚠️ 旧简历待清理`。
  - **飞书没有「换投递主简历」API**：投递只读字段 + 无 update/patch 接口（update/patch/partial_update 路径全 404）。
  - **解法 = terminate 旧投递 + 重建**：用同一 talent 在同岗位建投递，飞书自动绑定档案里**最新上传**的附件。旧附件保留在档案（删不掉，但不影响默认展示）。一条命令：`python scripts/swap_resume.py --talent <id> --pdf <最新简历>`（脚本固化了流程 + 3 个坑：路径自动定位 / att_id 以回读档案为准 / list 延迟重试）。
  - **⚠️ 仅限早期阶段投递**：terminate 会重置阶段状态。已到面试/Offer 阶段的投递不能用此法（会丢面评/审批），那种只能让用户去飞书后台手动换简历。
  - **判断「主简历是否最新」**：以 `combined_update` 后**回读档案**拿到的附件 id 为准（第一个是最新），不能用 `upload_attachment_with_name` 的返回值——combined_update 后档案里挂的 id 会重新生成，和上传返回值不同。

#### 字段映射 / 踩坑清单

Document AI → Hire API 的字段映射、易错点、手动排错命令模板见 [`notes/hire_record.md`](../../../../../miniwanob/notes/hire_record.md)（手动排错时才查；日常录入走 `_hire.py` 脚本不需要看）。

---

### 晚上审查（面评跟进）

```bash
python notes/_evening_review.py   # 扫今天面试 conclusion → 催面评/问是否终止/推进
```

---

## 面评与终止

| 能力 | 接口 | 关键字段 |
|---|---|---|
| 读面评**结论** | `GET /hire/v1/interviews?application_id=X` | `interview_record_list[].conclusion`（1通过/2不通过/空=未提交） |
| 读面评**全文** | `GET /hire/v1/interview_records/attachments?application_id=X&interview_record_id=Y` | 返回 PDF url（30分钟有效），含逐项打分+综合评价 |
| 推进阶段 | `POST /hire/v1/applications/:id/transfer_stage` | **请求体只有 `stage_id`**（[官方文档](https://open.feishu.cn/document/server-docs/hire-v1/candidate-management/delivery-process-management/application/transfer_stage)）。stage_id 从 `GET /hire/v1/job_processes` 取 |
| **终止/淘汰** | `POST /hire/v1/applications/:id/terminate` | **唯一正确接口**（`PUT /applications/:id` 不存在会 404）。请求体 `{"termination_type":<int>}`，调后 active_status=2 |

**淘汰要点**：
- terminate 是唯一正确接口；为什么不是 transfer_stage / PUT applications 见 decisions.md。
- 调后 `active_status` 变 2，对账脚本已过滤 `active_status!=1`，不再出现在作战清单。
- **不通过先问用户**：发现 conclusion=2 但仍 active=1 → 报告用户确认 → 确认后才调 terminate。
- **改投递到新岗**（录错岗需转移）：在新岗建投递 + terminate 旧投递，talent 复用不重建。

### 岗位范围过滤（只关注我负责的）

对账脚本只关注我负责的岗位（`create_user_id=我`）。判断依据 `notes/_my_jobs.json`（我创建的岗位 job_id 快照）。
- ⚠️ **快照会过期**：新创建的岗位不在文件里 → 该岗投递被过滤 → 候选人漏报。岗位增减后必须刷新：`python notes/refresh_my_jobs.py`（全量翻页 `page_size≤20`，>20 返回空）。

### 取面评全文命令链

```bash
# 1. 拿面试列表（取 interview_record_id）
MSYS_NO_PATHCONV=1 lark-cli api GET /open-apis/hire/v1/interviews --as bot --params '{"application_id":"APP_ID","page_size":10}'
# 2. 拿面评 PDF
MSYS_NO_PATHCONV=1 lark-cli api GET /open-apis/hire/v1/interview_records/attachments --as bot --params '{"application_id":"APP_ID","interview_record_id":"REC_ID"}'
# 3. curl 下载 PDF，fitz/PyPDF2 提取文本
```

---

## 输出前自检（Agent 出作战清单前必查）

每次给用户输出作战清单前，Agent 必须自查这 7 条，全过才能输出：

1. **今日面试 0 条时，确认不是漏**：查日程有没有今天的面试（脚本可能因 app_id 匹配失败漏报，或候选人岗位不在 `_my_jobs.json` 快照里被过滤掉）。日程有但 today_interviews 空 → 手动补，并检查 `_my_jobs.json` 是否需要刷新。
2. **@我的消息每条都判读了**：raw_messages 里 `mentions_wubo=true` 的消息，逐条确认意图。不能只看含"约/安排"的。
3. **拒绝信号没被吞**：含多个候选人的消息必须拆句，拒绝不能被同条的邀约盖过。
4. **卡住的人按紧急度排**：近期（2-4天）和积压（5天+）分开列，积压按原因聚类不逐条。
5. **未闭环邀约单列**：群里有邀约但 ATS/日程无落地的，这是最该追的，不能埋在待邀约里。
6. **只关注我负责的 + 未淘汰的**：脚本已过滤（按 `_my_jobs.json` + `termination_type`）。如果出现别人的岗位或已终止的人 → 是过滤漏了，报告给用户。
7. **用户决策已落盘**（step 4.5 校验）：用户审查完作战清单做了决策后，`_signals.json` 的 `decisions` 数组必须已更新（不能是空的 `[]`）。空的 = 用户的"今天约谁/催谁/终止谁"决策丢了，candidate-nurture 接棒时会重复提醒已决策的人。如果用户还没审查或还没做决策，先等用户确认再写 decisions，再接棒 step 6。

---

## 数据源拉取铁律

| 数据源 | 关键铁律 |
|---|---|
| ATS 投递 | 详情在 `data.application`（嵌套一层，不是平铺）；全量分页到 has_more=false |
| ATS 面试 | **必须带 application_id**，裸调报 1002002；conclusion 在 interview_record_list |
| 消息 | 在 `data.messages`（不是 data.items）；text 消息 content 是纯文本（@吴春波 已解析成文名）；窗口 7 天 |
| 跟踪表 | **必须用 `--field-id` 投影固定列顺序**（默认列顺序不稳定）；record_id 在 `record_id_list`（复数） |
| 日程 | 从 description 用正则提 application_id（比 summary 子串可靠）；start 是 `{'datetime':'...'}` 要提取 |
| 岗位列表 | **`page_size>20` 返回空**，翻页必须 ≤20；item 就是 job 本身（不是嵌套在 `item.job`） |

**性能**：6 路数据相互独立，必须 `ThreadPoolExecutor` 并行（max_workers=6）。ATS 详情查完后 talent 姓名 + job 信息也要批量并行（max_workers=8）。

**base 子命令参数**：调 base API 前先查 [`references/lark-cli-base-commands.md`](references/lark-cli-base-commands.md)（参数速查 + 易错对照表）。不许凭印象猜参数名，猜错就是盲试。

---

## 反模式（不要做）

- **不要"表自己查自己"** — 跟踪表会漂移，必须以 ATS 为中轴交叉校验
- **不要全量翻页 applications 死循环** — 翻到 has_more=false 或安全上限 10 页
- **不要用关键词匹配消息意图** — 交给 LLM（详见 decisions.md）
- **不要传附件指望自动解析** — combined_create/update 都不解析，必须 Document AI 解析后映射
- **不要 /talents?mobile= 去重** — mobile 参数不生效（传假号也返回数据），会把 A 的投递误关联到 B 的 talent
- **不要用 user token 调 hire 写接口** — 报 99991668，必须 `--as bot`
- **不要给 lark-cli --file 传绝对/中文路径** — 报 cannot open file，用 cwd 相对路径
- **不要为录入另写解析/核对脚本** — `_hire.py` 已封装全流程，现造脚本=现造 bug
- **不要凭印象记 lark-cli 子命令** — 不确定 `+<cmd> --help` 查
- **不要直接在 Bash 里裸跑 lark-cli api POST/GET** — git-bash MSYS2 会把 `/open-apis/...` 转成 `C:/Program Files/Git/...` → 404。`_lark_shared.cli()` 已设 `MSYS_NO_PATHCONV=1` 免疫；绕过 `cli()` 直接调时必须手动加 `MSYS_NO_PATHCONV=1` 前缀
- **不要盲信 `_my_jobs.json` 是最新的** — 它是快照，新创建岗位不刷新进去 → 漏报候选人。详见 decisions.md

---

## 参考文档

- [招聘开发指南（官方）](https://open.feishu.cn/document/server-docs/hire-v1/recruitment-development-guide)
- [转移投递阶段（官方）](https://open.feishu.cn/document/server-docs/hire-v1/candidate-management/delivery-process-management/application/transfer_stage)
- [`../lark-hire/SKILL.md`](../lark-hire/SKILL.md) — **飞书招聘 OpenAPI 契约层**（接口/字段/枚举/错误码，调 hire API 前先查这里）
- [`references/review-contract.md`](references/review-contract.md) — 对账 JSON 数据契约（字段名、消费者约定的单一真相源）
- [`references/lark-cli-base-commands.md`](references/lark-cli-base-commands.md) — base 子命令参数速查 + 易错对照表
- [`references/decisions.md`](references/decisions.md) — 决策记录（为什么这样设计，维护者参考）
- [`notes/hire_record.md`](../../../../../miniwanob/notes/hire_record.md) — 录入手动排错手册（字段映射 + 命令模板 + 踩坑清单）
- 信号判读报告样例：`notes/_signal_report.md`
