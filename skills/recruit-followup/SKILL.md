---
name: recruit-followup
description: >
  招聘主入口 skill。候选人跟进全流程：每日对账（ATS 中轴）→ LLM判读消息意图并落盘 _signals.json → 录入飞书招聘 → 面试流转 → 面评同步 → 跟踪表更新 → 日报。
  用户问今日作战清单/邀约/推进/跟进/对账时先走本 skill；保温是本 skill 的后半场（candidate-nurture），由本 skill step6 接棒触发，不独立唤起。
  每日对账产出固定三模块作战清单（今日时效/候选人推进/管道健康度，见 SKILL「作战清单输出模板 v3」）。
  触发词：跟进候选人、录入候选人、安排面试、扫群、招聘日报、今日待办、面评、催面评、推进流程、对账、今日哪些需要邀约/跟进。
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

> **Base ID / 表 ID / 应用 ID / lark-cli 路径 / 简历落点** 等全局常量统一在 [`AGENTS.md` 关键路径表](../../../AGENTS.md)维护，本表不重复，只列 skill 专有脚本路径。

| 项 | 值 |
|---|---|
| **对账脚本** | `notes/_daily_review.py`（ATS 中轴，6 路并行） |
| **表格同步脚本** | `notes/_sync_tables.py`（复用对账引擎，同步跟踪表客观层+岗位表，定时调度） |
| **对账共享库** | `notes/_lark_shared.py`（收口 api/cli/extract_json/时间转换，所有脚本统一 import） |
| **录入脚本** | `notes/_hire.py`（录入+建表+校验一条龙，见下方「录入候选人」） |
| **建跟踪表** | `<skill>/scripts/track_after_hire.py`（转发器，逻辑在 `_hire.py`，单独调用仅调试用） |
| **录入闸门** | `<skill>/scripts/verify_hire.py`（转发器，逻辑在 `_hire.py`，单独调用仅调试用） |
| **换最新简历** | `<skill>/scripts/swap_resume.py`（存量 talent 投递绑旧版时，terminate+重建让投递用最新附件） |
| **晚审脚本** | `notes/_evening_review.py`（扫面评 conclusion、催未提交、不通过先问再终止） |
| **定时同步** | Windows 计划任务 `MiniwaRecruitDailySync`（每天 18:30 跑 `_sync_tables.py`，日志 `notes/_sync_log.txt`） |
| 岗位缓存 | `notes/jobs_map.json`（可能过期，以 API 实查为准） |
| 简历落点 | `F:/Users/wuchunbo/Downloads`（先查这里），兜底 `data/在招岗位候选人管理/`（`find data -iname "*姓名*"`） |

`<skill>` = `…/recruit-followup`

---

## 执行顺序

### 每日早晨（最高优先级）

> 表格客观层每晚 18:30 已自动同步（见下方「表格自动同步」），早晨 Agent 读到的是最新状态——但仍需重跑对账拿最新消息做意图判读（18:30 后的消息、今天面试结果）。

```
第1步  跑对账脚本        python notes/_daily_review.py
         （表格客观层昨晚已同步，本步重算今日作战数据 + 拉最新消息）
第2步  Agent 读 _daily_review.json
         ├─ 读 structured（脚本算好的确定性结果）
         └─ 读 raw_messages（群+私聊+bot 全量原文，7天）
第3步  Agent 用 LLM 判读 raw_messages 意图（邀约/拒绝/决策/讨论）
第3.5步 Agent 写 _signals.json（判读结果落盘，格式见 candidate-nurture/references/signals-contract.md）
第4步  Agent 合并 structured + 判读结果 → 产出作战清单（按「作战清单输出模板 v3」三模块结构，见下方专章）
第4.5步 用户审查作战清单后，Agent 更新 _signals.json 的 decisions（记录用户决策）
第5步  输出给用户（见「输出前自检」）
第6步  接棒 candidate-nurture 出保温清单（见下方「接棒保温」）
```

> **接棒保温（step 6，不可省）**：作战清单输出、用户确认决策后，Agent **必须主动继续执行 candidate-nurture**——读 `_signals.json`（刚写的）+ `_daily_review.json` 的 stuck/feedback_overdue/to_advance + `_nurture_state.json`（触达历史），产出"今天该碰谁+话术+升级标注"的保温清单。不需用户重新喊"保温"。用户确认执行后，调 `nurture_state.py --touch` 记录触达，闭合保温环。作战清单管"今天处理谁"，保温清单管"今天碰谁"，两者在同一次早晨对账里连续产出。

**用户看到的是固定三模块结构的作战清单**（见下方「作战清单输出模板 v3」），不是数据大表。Agent 负责把"今天注意力放哪"讲清楚。

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

**判读产出前自检（step 3.5 落盘前必跑，6 条全过才能写文件）**：

判读是 LLM 最容易滑向"流畅但失真"的环节——抓关键词、概括、脑补连贯叙事。自检的作用是强制在落盘前质疑自己的每条 signal。

0. **先穷尽再判读（前置）**：对每个涉及候选人，先把 `raw_messages` + `hire_bot_events` 里**全部**相关条目拉全（按 name 筛，含未到场/通过/拒绝/接受各类事件），基于全集判读。**不许基于印象或部分消息判读**——漏看一条未到场/不通过事件就会产出失真 signal。
1. **evidence 可倒查**：evidence 里每个事实能否在 `raw_messages` / `hire_bot_events` 找到字面原文？找不到 = 删掉该事实，或整条降级 `待核实`。**禁止概括、禁止转述、禁止把多天事件揉成一句**——跨天事件要分别列各自的精确时间戳。
2. **sender 如实**：evidence 每句引号的说话人是消息原文的 sender 吗？吴春波说的 ≠ Bruce 说的 ≠ 业务方说的——sender 错位会让结论性质反转。转述别人意见时必须标"X转述Y的评估"。
2. **sender 如实**：evidence 每句引号的说话人是消息原文的 sender 吗？吴春波说的 ≠ Bruce 说的 ≠ 业务方说的——sender 错位会让结论性质反转。转述别人意见时必须标"X转述Y的评估"。
3. **时间戳不跨天错位**：evidence 的时间戳是事件**实际发生当天**，还是为拼凑结论安上去的？尤其"未到场/通过/未回复"类事件，要在 `hire_bot_events` 核它的真实日期，不许挪到今天。
4. **type 与原文自洽，矛盾降级**：拿 evidence 原文反问 type——type=invite 时，原文是否真有"推进/约/可以"的支撑？原文若有反向证据（已联系过/去别处了/未到场/还在犹豫）→ **强制改 hold 或 `待核实`**，不许强行归类。判读遇到任何单条原文无法确证的事实，一律 `待核实`，禁止 LLM 自补连贯叙事。
5. **ats_landed 实查**：`ats_landed` 是查了 `structured.ats` 的实际结果，还是推断的？去 ats 列表按 name/talent_id 实查有无记录，不靠猜。

**反模式（判读禁止）**：把多条消息的关键词拼成一个"看起来连贯但查无原文"的结论；用"今天""此前""后来"等模糊时间词替代精确时间戳；sender 缺失时默认归给最常说话的人。

**判读结果落盘（step 3.5）**：自检全过后写入 `notes/_signals.json`（不是口头呈现在作战清单里就完了）。这是 candidate-nurture 交叉比对的数据源——不落盘 nurture 读不到意图。写入格式见 [`candidate-nurture/references/signals-contract.md`](../../candidate-nurture/references/signals-contract.md)。

**用户决策落盘（step 4.5）**：用户审查后，Agent 按决策更新 `_signals.json` 的 `decisions` 部分。candidate-nurture 读 decisions 过滤——用户已决定"今天约"的不重复提醒，决定"终止"的不再保温。不落盘 = nurture 和早晨作战清单两张皮。

---

### 录入候选人（业务邀约后）

**一条命令完成录入+建跟踪表+自校验**（track/verify 已内联进 `_hire.py`，自动执行）：

```bash
python notes/_hire.py --by-name 白向庭,李毅 --job 海外游戏数据产品经理
# 录人才 → 建投递 → 建跟踪表行(写 talent_id) → 三段闸门校验(人才/投递/表)，一条命令全做完
```

闸门未过会打印 `🔴 STOP` 并退出码 1——不许继续后续约面。可选参数：`--no-track`/`--no-verify` 跳过（仅调试用）、`--time "姓名=YYYY-MM-DD HH:MM,..."` 把面试时间写进跟踪表"下一步动作"。

`--by-name` 三级级联查找简历（每级找不到自动降级）：

| 优先级 | 来源 | 匹配规则 |
|--------|------|----------|
| ① | 本地归档库 `data/在招岗位候选人管理/` | 文件名以「姓名_」开头 |
| ② | Downloads `F:/Users/wuchunbo/Downloads` | 文件名含姓名即可 |
| ③ | **飞书群聊/私聊**（自动搜+下载）| `im +messages-search` 搜姓名的文件消息，下载到 Downloads |

**批量/跨岗位录入**（精确控制每份简历路径时用）：

```bash
python notes/_hire.py --jobs 关键词                 # 查 job_code
# 写清单 notes/_hire_list.txt（每行：简历路径|岗位编号|姓名）
python notes/_hire.py notes/_hire_list.txt --list   # 录入+建表+校验一条龙
```

#### 录入铁律

- **岗位准入闸门**：录入目标岗必须「我创建 + 开放中(active_status=1)」，`_hire.py` 内置 `job_filter_ok()` 自动校验。暂停/已关闭的岗飞书删不掉，靠准入过滤而非手工避让。
- **job_code 自己查，别问用户**：`python notes/_hire.py --jobs 关键词`；`--by-name` 的 `--job` 直接传岗位关键词（脚本自动解析成 code 并校验）。**绝不手撸 jobs API。**
- **岗位名匹配放宽（踩坑 2026-08-04）**：简历文件名/用户口头说的岗位名 ≠ 飞书岗位全名（例：文件名"游戏运营"实际岗叫「游戏内容运营」）。`--jobs 关键词` 只命中实习/校招岗或查无结果时，**别停下来反问用户**——先用宽关键词重查（去掉岗位名后段，如 `--jobs 运营`），从输出直接挑「全职·社招」的 job_code 传给 `--job <code>`。同名岗（全职+实习并存）直接传 job_code 避开歧义，别传岗位名触发重跑。
- **闸门不过（🔴 STOP）不许继续**——talent 误关联/投递缺失/投错岗位都是真问题。
- **Document AI 解析 → combined_create 一次写全**，不要先 create 再 update，不要传附件指望自动解析。
- **talent_id 是对账主键**：录入时自动写入跟踪表 talent_id 列，对账用它精确匹配（不用姓名，治同名错配）。
- **重复录入安全**：同一候选人重录时，`create_application` 返回 `1002206 same application exist`，脚本识别为"跳过"（⏭️）不当错，跟踪表走 UPDATE 不建重复行。
- **新岗位录入**：跟踪表"岗位"下拉没选项时 → 用 `base +field-update`（PUT 全量语义，必带原有所有选项+新增项）补选项。岗位名映射靠运行时动态匹配（`_hire.py` 的 `resolve_job_pos`），无需改源码。
- **存量 talent 旧简历风险**：录入复用存量 talent 时，新附件挂档案但**投递仍绑旧附件**（面试官首屏看旧简历）。`_hire.py` 已自动区分两类告警（2026-08-03 起）：
  - `⚠️ 投递绑定旧附件`（`app_binds_old=true`）→ **必跑 swap_resume**，汇总段已直接给出完整命令（`swap_resume.py --talent <id> --pdf <本次简历绝对路径>`），复制即用
  - `仅档案残留旧附件`（`app_binds_old=false`）→ 投递已是新版，只需提醒用户去飞书后台手动清理档案，**不用跑 swap**
  - ⚠️ swap 仅限早期阶段（terminate 会重置状态，面试/Offer 阶段投递禁用，只能让用户去飞书后台手动换）。
- **⚠️ 两套附件 ID 体系（2026-08-05 踩坑，判断新旧必须比文件名，不比 ID）**：`upload_attachment_with_name` 返回的 att_id 是"上传文件记录 id"，combined_create/update 落档后飞书**重新生成**"档案附件 id"，两者永远不相等。所以：
  - `check_stale_resume` **禁止**拿 `att_id != 档案附件 ID` 判断新旧——永远误报"旧附件残留"。已改为按 **Name == 本次上传 basename** 判断（`_hire.py` 2026-08-05 修）
  - `swap_resume.py` **禁止** `att_id=None` 调 combined_update（旧 bug：新简历根本没挂档案，回读把唯一旧附件当"最新"，terminate+重建后投递仍绑旧简历，验证用同一旧 id 自证 → 假阳性"🟢 完成"）。必须 `att_id = upload_attachment_with_name(...)` 保留返回值传给 combined_update（2026-08-05 修）
- **同名岗位区分**：`--jobs 关键词` 输出已带「招聘类型·流程 | 部门」（如 `A82422 | 游戏内容运营 | 全职·社招 | 用户和生态运营团队`），同名岗靠此区分，不必再全量拉岗位详情。

#### track/verify 单独调用（仅调试/补建用）

`track_after_hire.py` / `verify_hire.py` 已改为转发器（逻辑在 `_hire.py`，单一真相源），日常录入自动执行无需单独跑。仅在需单独补建跟踪表或重跑校验时用：

```bash
python scripts/track_after_hire.py          # 从 _hire_result.json 补建跟踪表行
python scripts/verify_hire.py               # 重跑闸门校验
```

#### 字段映射 / 踩坑清单

Document AI → Hire API 的字段映射、易错点、手动排错命令模板见 [`notes/hire_record.md`](../../../../../miniwanob/notes/hire_record.md)（手动排错时才查；日常录入走 `_hire.py` 脚本不需要看）。

---

### 晚上审查（面评跟进）

```bash
python notes/_evening_review.py   # 扫今天面试 conclusion → 催面评/问是否终止/推进
```

---

## 面评与终止

**接口/字段/错误码契约**：读面评结论、读面评全文、推进阶段、终止/淘汰等 hire API 的完整接口路径与字段，统一在 [`lark-hire` skill](../lark-hire/SKILL.md)（飞书招聘 OpenAPI 契约层）。本文件只列执行规则，不重复接口定义。

**淘汰要点**：
- 调 terminate 后 `active_status` 变 2，对账脚本已过滤 `active_status!=1`，不再出现在作战清单。
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

## 作战清单输出模板（v3，2026-08-04 确立）

> **这是 Agent 每天早晨对账后的固定输出格式。** 不是建议，是契约——用户靠这个清单分配一天注意力，结构稳定 = 用户不用每次重新适应。读完 `_daily_review.json` + 判读完消息后，**必须按下面三模块结构产出**，不许自由发挥列字段或调换模块顺序。
>
> **v3 相对 v2 的变更**（对抗式审查结论，见下方设计依据⑦-⑩）：
> - 模块二从「按阶段分层(2A/2B/2C)」改为「按工作室→岗位→候选人」组织，贴合 HR 真实运营视角
> - 模块二每行从 5 列扩到 8 列，新增面试时间/面评结论/面试官意见列（决策需上下文）
> - 模块三 3A 从文字描述改为「岗位×阶段计数矩阵」（岗位健康度要看分布）
> - 模块一新增 1D「本周面试前瞻」（脚本 `upcoming_interviews` 字段支撑）

### 设计依据（第一性原理）

清单要解决的根本问题：**HR 一天的注意力是稀缺资源，清单的唯一价值 = 帮用户把注意力分配到"能最大化推动入职、最小化候选人流失"的动作上。** 所以清单的计量单位是**动作**，不是数据。从这点推出的 10 条事实，决定清单必须长这样：

| 根本事实 | 推出的要求 | 对应模块 |
|---|---|---|
| ① 注意力有限 → 必须分级排优先级 | 全局红/橙/黄分级，不平等铺 | 全局 |
| ② 候选人不进则退（不动=流失） | 必须盯"下一步动作 + 保温" | 模块二 |
| ③ 面试/offer 有时间刚性（错过=事故） | "今日时效"置顶、带时间线 | 模块一 |
| ④ 异常（未到场/不通过/超期）是事故源头 | 必须**独立显性化**，不能埋在列表里 | 模块一·1C |
| ⑤ 岗位有冷热（缺口/阻塞不同） | 指导"剩余精力投向哪" | 模块三 |
| ⑥ 决策不落地 = 心智负担 + 重复劳动 | 用户确认后落盘 `_signals.json` decisions | 全流程 |
| ⑦ **HR 按工作室/岗位运营招聘，不是按"阶段"** | 模块二按工作室→岗位组织，不按阶段分层 | 模块二 |
| ⑧ **决策需上下文（面评+面试官意见），不是只看"下一步"** | 模块二每行带面评列+意见列 | 模块二 |
| ⑨ **岗位健康度要看候选人分布，不是文字描述** | 模块三矩阵：岗位×阶段计数 | 模块三·3A |
| ⑩ **前瞻能防事故（撞档/面试官档期）** | 模块一加 1D 未来面试 | 模块一·1D |

> **⑦⑧ 是 v3 对 v2 的核心纠偏**：v2 模块二按"Offer/复试/初试"阶段分层，导致同一工作室的候选人被拆到三处，用户要在脑子里重新按工作室聚合才能判断"坤灵主美推进到哪了"。v3 改为按工作室→岗位组织——这是 HR 真实的运营单元。同时 v2 只给"下一步动作"不给"上一轮评价如何"，决策缺上下文；v3 每行带面评结论+面试官意见。

### 固定三模块结构

```
模块一 · 今日时效（今天必须发生的，最高优先）
  1A 今日面试时间线 —— 时间·候选人·面试类型(线上/线下)·会议室/地址·面试官·轮次·盯什么/异常
  1B 今日邀约（新动作）—— 今天要录入/约面的人
  1C 异常与风险      —— 未到场/不通过待终止/超期未闭环邀约
  1D 本周面试前瞻 【v3新增】—— 未来7天已排期面试（含现场/视频类型）·撞档预警

模块二 · 候选人推进（按工作室→岗位→候选人组织）【v3重构】
  按工作室分组，组内按岗位聚合：
    岗位标题行：岗位名（在途N人·缺口M人）
    候选人行（8列）：候选人 | 轮次·停留 | 面试时间 | 面试结论 | 面试评价 | 下一步 | 保温 | ⚠️
  同岗位内排序：接近变现程度倒序（终试>复试>初试>初筛）

模块三 · 管道健康度（工作室分组·岗位阶段表）【v3.3 定稿】
  3A 每个工作室一张表 —— 行=岗位，**固定8列**：岗位 | 初筛 | 初试 | 复试 | 终试 | HR面 | Offer | 状态
       单元格放**候选人名字**，没人的单元格留空
       **所有表的列头必须完全一致**（即使某工作室没人到终试，终试列也要在、留空）
       列位置稳定=用户扫表时不用每次重新找信息在哪
  3B 推送提醒 —— 仅列必须今天处理的（急缺/高危/严重阻塞），带原因和动作
  不用 emoji 进度条、不依赖 HTML 看板——纯文字表格最清晰
```

> **模块三定位（v3.3 定稿）**：模块二按人展开详情（下一步/面评/保温），模块三按岗位看分布——"每个岗位的人卡在哪个阶段、哪个岗位堵了/空了"。**单元格放人名不放数字**：人名比"2"信息量大，且不用回模块二交叉查。**固定8列**（岗位 + 初筛/初试/复试/终试/HR面/Offer/状态）：不同工作室的表结构完全一致，空格子留空但列必须在——列位置稳定才不用每次重新找。
>
> **阶段列推断规则**（Agent 用 `interview_count` + `latest_conclusion` 推断每人所在阶段）：
> - `latest_conclusion==2`（不通过）→ 归到他最后面试的阶段列，状态标"不通过"
> - `latest_conclusion==1`（通过）→ `interview_count` 1轮→复试列、2轮→终试列、3轮→HR面列、4轮→Offer列
> - `latest_conclusion==null`（等评/刚面）→ `interview_count` 1轮→初试列、2轮→复试列、3轮→终试列
> - stage 含"Offer"→Offer列；含"初筛"且 interview_count==0→初筛列

**排序逻辑（不许改）**：
- **模块一置顶**：面试/offer 有时间刚性（事实③），错过即事故，优先级最高。
- **模块二按工作室→岗位组织**：HR 按工作室/岗位运营招聘（事实⑦），同一工作室的候选人在一处看全，不用跨段拼凑。
- **模块三放最后**：是"剩余精力投向"的指南针，不是即时动作。
- **1C 异常独立成块**：异常不显性化 = 必然变事故（未到场/不通过待终止/超期未闭环都归此块）。
- **每行带"保温"列**：保温不是单独任务，是每个候选人推进动作的伴随项。

### 模块二行格式（8列，v3.1）

模块二每个岗位下的每一行**必须**包含这 8 列，缺数据写"—"，不许省列：

| 列 | 来源 | 示例 |
|---|---|---|
| 候选人 | ATS name | 庞然杰 |
| 轮次·停留 | structured.to_advance / stuck / ats | 一面通过·今天终试·停4天 |
| 面试时间 | today_interviews / upcoming_interviews / signals | 今天11:00·杨智勇·终试 |
| 面试结论 | ats.latest_conclusion + feedback_overdue | 一面✅ 二面⏳（纯符号，简洁直观：✅通过 ❌不通过 ⏳等待面评。今天刚面的标⏳） |
| 面试评价 | hire_bot_events + raw_messages 判读 | 金海:作品扎实，角色塑造力强 |
| 下一步 | signals + to_advance | 看终试结果→走offer |
| 保温 | 据停留天数匹配话术 | 主动同步防冷 |
| ⚠️ | 异常标记（空则省略整格） | 撞档/超期/漏建行 |

> **v3.2 面试结论列**（用户反馈"面评太简陋"）：原来的"面评结论"一列混了两个维度——通过/不通过的**结论**，和面试官评价的**内容**。拆成两列：**面试结论**列写清楚每轮通过/不通过/等待面评/今天面（说清楚状态，如"一面通过；二面等评欠3天"），**面试评价**列写1-2句话概括面试官说了什么（如"张书瑞:技术基本功好，风格匹配度高"）。两列职责分明：面试结论列管状态，评价列管内容。

> **为什么 8 列**：v2 的 5 列只告诉用户"该干嘛"，缺"上一轮评价如何""面试官怎么说""什么时候面"——这些是决策的上下文依据。8 列让决策链完整：**谁→卡在哪→什么时候面谁→通过没→评价如何→下一步→怎么保温→有无异常**。

### 模块二数据来源映射（每列从哪取）

| 列 | 数据源 | 取法 |
|---|---|---|
| 候选人 | `structured.ats[].name` | 直接读 |
| 轮次·停留 | `structured.to_advance[]`（有 passed_round+dwell_days）/ `stuck[]`（有 dwell_days+reason）/ `ats[]`（有 interview_count+dwell_days） | 三源合并。**"已面完等评"不写"通过N轮"**——判据：`interview_count > passed_round` 且该轮在 `feedback_overdue` → 写"一面通过·二面X月X日已面·等评"，不写"通过1轮"（会误读为"只面了1轮"） |
| 面试时间 | `today_interviews[]`（今天，带 `weekday` 字段）+ `upcoming_interviews[]`（本周未来，带 `weekday` 字段）+ `_signals.json` signals（已约未落地的时间） | 格式必须带**轮次+面试官**，**日期带周几**（从数据的 `weekday` 字段直接取，**不手算星期**——手算星期是高频出错点）：今天面标"今天11:00·杨智勇·终试"，未来面标"08-06(周四)15:00·熊乐·初试"，已约未落地标"待约(Bruce周四)·熊乐·初试"。**光写时间信息量不够**——用户要看是谁面、第几轮 |
| 面试结论 | `ats[].latest_conclusion`（1=通过/2=不通过/null=未出）+ `feedback_overdue`（等评状态） | **纯符号，简洁直观**：✅通过 / ❌不通过 / ⏳等待面评(欠N天) / ⏳今天面。多轮分别标："一面✅ 二面⏳欠3天"。不写文字只写符号——一眼扫完。**评价内容另进"面试评价"列** |
| 面试评价 | `hire_bot_events[]`（系统结构化的面评结论，但只有通过/不通过，无文本）+ `raw_messages` 中面试官的评价性发言（Agent step3 判读时提取 → `_signals.json` signals[].note） | **用1-2句话概括评价要点**，不照搬原文也不只写"通过"。如 raw_messages 里张书瑞说"只会纯写实的特效"→ 概括为"张书瑞:写实特效强，手绘特效弱"。没有评价内容则"—"。**只摘评价性内容，不摘邀约/拒绝** |
| 下一步 | `_signals.json` signals[].type + note + `to_advance` | invite→"录ATS+约面"，hold→"等业务确认"，to_advance→"推进下一轮"，reject→"终止"。催面评也写这里（"催X交面评"） |
| 保温 | 据 `dwell_days` 匹配 | <7天"正常跟进"，7-14天"主动同步进度"，15-29天"防流失触达"，≥30天 🔴"高危立即触达" |
| ⚠️ | `untracked_in_ats`（漏建行）/ 今日面试撞档 / `feedback_overdue`（超期） | 有异常标"⚠️漏建行"/"⚠️撞档"/"⚠️面评欠X天"，无则留空 |

### 模块二工作室排序

工作室/团队按**在途候选人数**降序排列（人多的在前，因为管理负担最大）。岗位同理。这样用户的视线自然先落在"候选人最多、最需要关注"的工作室。

### 填写规范

- **今日数据从 `structured.today_interviews` / `upcoming_interviews` / `to_advance` / `feedback_overdue` / `stuck` 来**——这些是脚本算的确定性数据，直接用。
- **1A 面试详情字段直接读 `today_interviews`**（脚本已从日历 description 解析好，Agent 不用再查日历）：
  - `interview_type`（现场面试/视频面试）→ 用 🏢/💻 图标区分
  - `meeting_room`（会议室房间名，如"B区-兔美美(6)"）→ 线上线下都要带（视频面试也有会议室用于面试官端）
  - `location`（线下地址，视频面试为空）→ 线下面试带完整地址
  - `round`（轮次，如"1 面（初试）"）、`interviewer`（面试官）
  - `video_url`（视频链接）→ 仅视频面试有，用户需要时再单独发，不在表格里铺开
  - **字段缺失（解析失败）→ 补查日历**：个别面试 description 格式异常解析不出时，Agent 用 `calendar +agenda` 手动查当天该事件的 description 补齐，不能留空让用户自己去翻。
- **1D 本周面试前瞻从 `upcoming_interviews` 来**（v3 新字段）：**按日期分组**呈现（每天一个子标题 `📅 MM-DD（周X）· N场`，**周几必标**——上班族看日期习惯带星期），不要用一个扁平大表。每天表格列：时间·类型·候选人·岗位·面试官·轮次。**面试类型列用 🏢现场/💻视频 图标**（读 `upcoming_interviews[].interview_type`，和 1A 同源同图标，不写成文字）——前瞻的意义在于提前发现场地/档期冲突，现场还是视频直接决定是否需要会议室、候选人是否要赶来，是撞档判断的前置信息，不能省。**重点标撞档**（同一时间段多个面试）和**面试官扎堆**（同一面试官一天多场）。空档的工作日（如周四~周五暂无排期）显式写出"暂无排期"并提示可用于安排新约，不要省略——用户需要知道本周后几天还有没有档期。
- **意图信号从 `_signals.json` 来**——LLM 判读的邀约/拒绝/决策，已落盘，直接引用。
- **面试官/业务意见在 step3 判读时一并提取**：读 raw_messages 时，除了判读邀约/拒绝/hold，还要摘录面试官对候选人的**评价性发言**（"这个人只会纯写实特效""口臭""作品不错"等），写入对应 signal 的 note 字段。这些是模块二"面试官/业务意见"列的数据源。
- **跨源合并**：同一候选人可能在 ATS 有进度（模块二）又有新消息信号（模块一·1B 或 1C），按"今天的动作归模块一、持续追踪归模块二"归类，不要重复列两处。
- **停留天数用红色标注积压**：≥30 天必须 🔴，提醒用户这是流失风险。
- **催面评按面试官聚合**（模块三·3B）：同一面试官欠多人合并列，别逐条——催的时候是一次沟通。
- **开头一句话总结**：清单最上方给一句"今天 N 场面试要盯、M 个未闭环邀约、K 个不通过待终止、本周后面还有 X 场"——用户 3 秒抓重点。

### 当数据稀疏时（模块收缩规则）

不是每天都满载。模块允许按数据量收缩，但**结构不变**：
- 今日无面试 → 1A 写"今日无面试"，不删模块
- 无未闭环邀约 → 1B 写"无新增邀约"
- 某工作室无候选人 → 该工作室整块不出现（不是写"暂无"，是直接不列——空工作室不该占用户视线）
- 本周无未来面试 → 1D 写"本周暂无已排期面试"
- 模块三照常（每天都要看管道健康度）

**反例（禁止）**：因为"今天没异常"就不输出 1C——异常是负面信号，没异常本身就是好消息，要明确写"无异常"让用户安心，而不是让用户猜"是没异常还是没查"。

---

## 输出前自检（Agent 出作战清单前必查）

每次给用户输出作战清单前，Agent 必须自查这 10 条，全过才能输出（括号是对应 v3 模板的哪个模块）：

1. **今日面试 0 条时，确认不是漏**（→ 1A）：查日程有没有今天的面试（脚本可能因 app_id 匹配失败漏报，或候选人岗位不在 `_my_jobs.json` 快照里被过滤掉）。日程有但 today_interviews 空 → 手动补，并检查 `_my_jobs.json` 是否需要刷新。
2. **@我的消息每条都判读了**（→ 1B/1C 的信号来源）：raw_messages 里 `mentions_wubo=true` 的消息，逐条确认意图。不能只看含"约/安排"的。
3. **拒绝信号没被吞**（→ 1C）：含多个候选人的消息必须拆句，拒绝不能被同条的邀约盖过。
4. **模块二按工作室聚合完整**（→ 模块二，v3 新增）：用 `structured.ats` 核对——ats 有 N 个活跃候选人，模块二就要有 N 行（按 dept→job 分组）。漏了某个工作室/岗位 = 用户看不到那批人的进度。用 ats 的 dept 值域对照模块二的工作室分组，确保无遗漏。
5. **未闭环邀约进 1B/1C**：群里有邀约但 ATS/日程无落地的，是"今日邀约"（1B）还是"异常超期"（1C）按时间判断——当天决策的归 1B，拖了 ≥2 天的归 1C 的"超期未闭环"。不能埋在模块二的持续追踪里。
6. **只关注我负责的 + 未淘汰的**（全局）：脚本已过滤（按 `_my_jobs.json` + `termination_type`）。如果出现别人的岗位或已终止的人 → 是过滤漏了，报告给用户。
7. **用户决策已落盘**（step 4.5 校验，跨模块）：用户审查完作战清单做了决策后，`_signals.json` 的 `decisions` 数组必须已更新（不能是空的 `[]`）。空的 = 用户的"今天约谁/催谁/终止谁"决策丢了，candidate-nurture 接棒时会重复提醒已决策的人。如果用户还没审查或还没做决策，先等用户确认再写 decisions，再接棒 step 6。
8. **面试官意见是否提取**（→ 模块二「面试官/业务意见」列，v3 新增）：raw_messages 里面试官/业务方对候选人的**评价性发言**（非邀约/拒绝），是否已摘录到 `_signals.json` 对应 signal 的 note 字段，并在模块二"面试官/业务意见"列体现。如张书瑞说"面试难受死我了，他只会纯写实的特效"→ 庞然杰那行的意见列应体现。没有评价性发言的候选人该列写"—"，不许空着。
9. **停留天数用红色标注积压**（→ 模块二停留天数）：≥30 天必须 🔴 标注；15-29 天 🟠 标注。提醒用户这是流失风险。
10. **模块三所有表列头完全一致**（→ 模块三 3A）：每个工作室的表必须是固定8列 `岗位 | 初筛 | 初试 | 复试 | 终试 | HR面 | Offer | 状态`——即使某工作室只有1人在初试，复试/终试/HR面/Offer列也要在、留空。**不许因数据稀疏省列**——不同表列数不一致=用户每次都要重新定位列=认知负担。

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

> Windows/Shell/飞书操作的通用铁律（MSYS_NO_PATHCONV、user token 报 99991668、lark-cli 子命令查 --help 等）统一在 [`AGENTS.md`](../../../AGENTS.md)「工作方法铁律」「Windows/Shell 操作铁律」「飞书操作铁律」三节，本处只列 **本 skill 专有** 的反模式。

- **不要"表自己查自己"** — 跟踪表会漂移，必须以 ATS 为中轴交叉校验
- **不要全量翻页 applications 死循环** — 翻到 has_more=false 或安全上限 10 页
- **不要用关键词匹配消息意图** — 交给 LLM（详见 decisions.md）
- **不要传附件指望自动解析** — combined_create/update 都不解析，必须 Document AI 解析后映射
- **不要 /talents?mobile= 去重** — mobile 参数不生效（传假号也返回数据），会把 A 的投递误关联到 B 的 talent
- **不要给 lark-cli --file 传绝对/中文路径** — 报 cannot open file，用 cwd 相对路径
- **不要为录入另写解析/核对脚本** — `_hire.py` 已封装录入+建表+校验全流程，现造脚本=现造 bug
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
- 作战清单样例（v2 模板实战参照）：`notes/_battle_list_sample.md`
- signals 判读样例（含 v1 错误反例对照 + evidence 倒查校验脚本）：`notes/_signals_sample.md`
