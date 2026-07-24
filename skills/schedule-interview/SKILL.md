---
name: schedule-interview
description: >
  面试时间协调：批量查面试官空闲，和候选人给定的时间求交集，吃 ATS 数据产出**完整可转发草稿**（含岗位+团队+进展+形式+时间）。
  触发词：对时间、约面试、面试官空闲、协调面试、面试时间、看看XX有空没、匹配面试时间。
  只要用户提到"面试官时间/空闲/档期"+"候选人时间"，就使用这个 skill。
  覆盖：单/多面试官共同空闲查询、多候选人批量匹配、时段去重防撞档、吃 ATS 自动出完整草稿。
  不覆盖：替用户发消息给面试官（草稿拟好用户自发）、在飞书招聘建面试流程（无公开API，用户手点）、问候选人时间（用户自己沟通）、改跟踪表（见 recruit-followup）。
  依赖：lark-cli（calendar 域 + contact 域已授权，契约见 lark-calendar-contact skill）、interviewers.json 缓存、notes/_daily_review.json（ATS 数据源）。
---

# 面试时间协调

## 最高原则（先读，做事前默念）

> **这个 skill 的产物是"一条让面试官 5 秒内能决策并回复的消息"，脚本直接出完整草稿，AI 只在 ATS 数据缺失时介入。**

四条铁律（违反任何一条，草稿就是废稿）：
1. **候选时间是硬约束，面试官空闲是优化目标**——在候选人能来的窗口里找面试官最舒服的时段，不能反过来。新人最常犯的错就是面试官中心论。
2. **消息必须自包含**：岗位、候选人、哪轮/角色、形式（视频/现场）、时间——面试官收到消息不能有任何追问，一追问就废了一轮。
3. **产出的是待确认草案，不是定局**——面试官点头后还要回去跟候选人敲定具体时刻。措辞是"拟安排/可以吗"，不是"已安排"。
4. **形式默认视频，但必须显式出现在草稿**——默认了不写=面试官不知道要准备什么。脚本 `--form` 参数控制（默认"视频"）。

## 三段式流程

```
第1段【明确信息】→ 闸门：面试官+候选人+时间三要素齐全？
   ├─ 面试官（姓名）
   ├─ 候选人（姓名 + 可以面试的时间原话）
   ├─ 候选人 talent_id（可选，传了脚本精确匹配 ATS，没传按姓名模糊匹配）
   └─ 形式（默认视频，用户没说也传 --form 视频）
   ❌ 缺面试官/候选人/时间 → 问用户补齐，不许猜

第2段【查日程+匹配+出草稿】→ 脚本自动：
   ├─ freebusy 真实空闲 ∩ 候选人时间窗口
   ├─ 吃 ATS 取岗位/团队/进展/轮次角色（_daily_review.json structured.ats）
   └─ 拼五要素齐全的完整草稿（岗位+形式 / 候选人+进展+角色 / 时间原话+拟时间）

第3段【AI 只处理异常】→ 脚本标【需问用户】的，AI 问用户补齐后改草稿
```

> 第2段从"AI 补全骨架"升级为"脚本吃 ATS 出完整草稿"（2026-07-21）。脚本拿不到 ATS 数据的候选人，草稿对应位置标 `【需问用户：岗位】`/`【需问用户：上轮进展+这轮角色】`，AI 看标记介入。

## 能力边界

| 环节 | 谁做 |
|------|------|
| ① 收集候选人时间 | **用户**（必须人沟通） |
| ② 查面试官空闲 | ✅ **脚本自动**（freebusy 反推） |
| ③ 算交集、定时间 | ✅ **脚本自动**（候选时间硬约束 + 黄金时段优化） |
| ④ 取岗位/进展/轮次角色 | ✅ **脚本自动**（吃 _daily_review.json 的 ATS 数据） |
| ⑤ 拟完整草稿 | ✅ **脚本自动**，AI 只在 ATS 缺失时补 |
| ⑥ 发消息给面试官 | 🟡 **用户自发**（草稿拟好用户转发，措辞用户把关） |
| ⑦ 飞书招聘建面试 | **用户手点**（无公开"安排面试"API） |

> ⚠️ 飞书招聘**没有公开的"安排面试"API**，只有"创建外部面试/面评"（导入外部系统数据用）。第⑦步用户在 HR 后台手动建。

## 配置

| 项 | 值 |
|---|---|
| 项目根 | `F:/miniwanob` |
| lark-cli | `C:/Users/wuchunbo/AppData/Roaming/npm/lark-cli.cmd`（subprocess 全路径，否则 WinError 2） |
| 面试官缓存 | `F:/miniwanob/notes/interviewers.json`（姓名/alias → open_id，首次用自动写回） |
| **ATS 数据源** | **`F:/miniwanob/notes/_daily_review.json`**（recruit-followup 产出，`structured.ats[*]` 含 talent_id/name/job/dept/stage/interview_count/latest_conclusion） |
| 核心脚本 | `scripts/match_schedule.py`（calendar/contact API 契约见 `lark-calendar-contact` skill） |

## 标准输入（用户这样跟我说即可）

```
面试官：谢坤（可多人，逗号分隔）
候选人时间：
  罗艺 - 周四
  陈思宇 - 周四
  刘涵辰 - 周五
（可选）面试时长：60分钟
（可选）面试形式：视频 / 现场（默认视频）
```

AI 把自然语言转成命令行参数。候选人参数格式：
- `姓名=时间` — 基础格式
- `姓名=时间@岗位方向` — 带岗位方向（草稿里"姓名（岗位方向）"展示用，ATS 缺时兜底）

候选人时间支持的写法：
- `周四` / `周五` — 模糊日（默认未来最近的那天）
- `周三下午` / `周四晚上` — 日 + 时段偏好
- `周四 16:00` — 精确时刻
- `7-3` / `7月3日` — 绝对日期
- `今天` / `明天` — 相对日

## 标准命令

```bash
# ⭐ 推荐：传 talent_ids 让脚本精确匹配 ATS，出完整草稿
python "C:/Users/wuchunbo/.agents/skills/schedule-interview/scripts/match_schedule.py" \
  --interviewer 谢坤 \
  --candidates "范晶昌=周四,汤娟=周四" \
  --talent-ids "7664545205329381651,7663322896458549558" \
  --form 视频 --duration 60

# 不传 talent_ids（按姓名模糊匹配 ATS，同名歧义时脚本告警）
python ".../match_schedule.py" --interviewer 谢坤 --candidates "罗艺=周四" --duration 60

# 多面试官（取共同空闲）
python ".../match_schedule.py" --interviewer "谢坤,潘腾飞" --candidates "张三=周四" --duration 60

# 先 dry-run 验证 ATS 匹配（不查飞书）
python ".../match_schedule.py" --interviewer 谢坤 --candidates "..." --talent-ids "..." --dry-run
```

**参数说明**：
- `--talent-ids`：与 `--candidates` 顺序对齐，用于精确匹配 ATS。**用户说"罗艺周四面试"时，AI 先从 _daily_review.json 查罗艺的 talent_id，传给脚本**
- `--form 视频|现场`：面试形式，默认视频
- `--team`：团队名覆盖（默认取 ATS dept）
- `--work-end 21`：晚上面试有效时扩展工作时段

**Windows 铁律**：脚本路径用绝对路径 + 正斜杠。`--candidates` / `--talent-ids` 含中文/逗号，**必须用双引号包住整个值**。

## 输出解读（透明五段）

脚本 stdout 直出（不写文件，git-bash UTF-8 不乱码）：

```
=== 面试官日程（freebusy 实查）===
面试官：谢坤
形式：视频｜查询区间：7-21 ~ 7-23｜工作时段 09:00-18:00｜每天 12:00-13:30 午休已排除

【忙碌时间段】（真实会议）                    ← freebusy 原始会议（ground truth）
  周二(7-21): 10:00-11:00, 17:30-18:00

【空闲时间段】（反推空闲 ∩ 工作时段）         ← 全天-会议-午休，每段能往回追溯
  周二(7-21): 09:00-10:00, 11:00-12:00, 13:30-17:30

【ATS 数据匹配】                              ← 每个候选人的 job/dept/stage/interview_count
  ✅ 范晶昌 (tid=766454520532...): 岗位=3D场景设计师 团队=长青工作室 阶段=面试 已面1轮 上轮结论=None

【候选人匹配】（空闲 ∩ 候选人日期，黄金时段优先）
  范晶昌    周四(7-23) → ✅ 可约 11:00/15:00/16:00（建议 11:00）

=== 可转发草稿（完整版，AI 只在【需问用户】处介入）===
长青工作室3D场景设计师视频面试              ← 团队+岗位+形式（吃 ATS）
范晶昌（初面）：                            ← 候选人+轮次角色（ATS interview_count 推）
面试可以时间：周四                          ← 候选人时间原话
拟安排：周四上午11点                        ← 拟时间
安排初面，坤哥你这边                        ← 进展+称谓（ATS + interviewers.json alias）

安排这个时间可以吗（已避开日程忙碌时间段）

=== 不可约的（如有，需重新和候选人协调）===
（无）
```

**读法**：①忙碌段是 ground truth（可和飞书日历对照）→ ②空闲段是算出来的 → ③ATS 段展示数据源 → ④匹配段是"空闲 ∩ 候选人日期" → ⑤草稿直接转发。**读时若发现忙碌段和飞书日历对不上，说明日程变了，重跑即可**。

**AI 行为指导**：
- 把"匹配结果"和"草稿"都展示给用户，让用户扫一眼草稿能不能直接转发
- ❌ 的候选人要明确提示"需回去重新和候选人协调时间"
- 草稿里有 `【需问用户：XXX】` 标记 → 问用户补齐后改草稿（这是 AI 唯一需要动手的场景）
- 草稿是建议，用户可改。用户说"陈思宇改到周五"→ 重跑脚本

## AI 何时介入

**只有两种情况 AI 需要动手**：

1. **脚本标 `【需问用户：XXX】`**：ATS 没匹配到该候选人（talent_id 错/候选人不在 ATS/同名歧义）。AI 问用户：岗位是什么？上轮进展？这轮角色？拿到答案后手改草稿。
2. **候选人时间需还原原话**：脚本接收的是 AI 解析后的 `--candidates "罗艺=周四"`，但用户原话可能是"我这边周四的话可以进行面试"。AI 拟草稿时把"周四"还原成原话。

**其他情况脚本都搞定了**：岗位/团队（ATS job+dept）、进展/轮次角色（progress_text 从 interview_count 推）、形式（--form 参数）、称谓（interviewers.json alias）、时间（freebusy 匹配）、格式（块状/单人）。AI 不再临场编。

## 踩坑固化（脚本内部已处理，理解用）

1. **⭐ freebusy 反推空闲，不用 suggestion**。suggestion 返回的是飞书"挑出来的建议"，**不是完整空闲列表**——人越空漏得越离谱。现用 `calendar +freebusy` 拿真实忙碌段，本地反推空闲段，多人取交集。**这是本 skill 的核心正确性保证。**（背景见 `references/decisions.md`）
2. **查询区间由 `--days` 控制（默认 7 天）**。注意：suggestion 有 ≤7 天硬限制（超报 `190014 interval too large`），**freebusy 无此限制**——本项目已弃用 suggestion 改用 freebusy，所以 `--days` 可以加大到 14/30。脚本以候选人最早日期为起点 + `--days` 为窗口长度查询，候选人日期超出窗口会被截断告警。
3. **freebusy 参数是 `--start`/`--end`**（不是 `--time-min`/`--time-max`，那是 Google 日历的参数名，飞书不认）。freebusy 返回平铺数组（不在 `data.` 下），脚本已兼容。
4. **工作时间截断必须有**：freebusy 反推的是全天空闲段（含凌晨），必须按 `--work-start`(默认9) ~ `--work-end`(默认18) 截断，否则会约到凌晨。晚上面试有效时传 `--work-end 21`。
5. **跨天空闲段按自然天拆分**：反推出的空闲段可能横跨午夜，脚本按天拆分后再按工作时间截断，避免显示"15:30-11:00"倒序段。
6. **星期计算用 datetime，绝不手算**（手算必错，特别是跨周边三计算）。
7. **多候选人去重**：同一面试官的同一时段不能分给两人，脚本用 `_has_conflict` 按 duration 区间重叠判断。
8. **subprocess 调 lark-cli 用全路径 .cmd**：Windows 下 `"lark-cli"` 报 WinError 2 找不到。
9. **午休自动排除**：每天 12:00-13:30 作为"忙碌段"加入反推，避免约到午休。
10. **面试官搜索别带 `--has-chatted`**：陌生人面试官（没聊过）会被过滤掉。（背景见 `references/decisions.md`）
11. **⭐ freebusy 结果会随日程动态变化**：同一区间隔几分钟查两次，结果可能不同——面试官期间又往日历加了会。**这不是脚本 bug，是 freebusy 是实时的**。正因为如此，输出必须明文展示忙碌段，让用户能和飞书日历对照核实。若发现输出和飞书日历对不上，重跑即可。
12. **ATS 数据要当天新鲜**：`_daily_review.json` 是 recruit-followup 每天产出的快照，跨天用会漏新候选人。用前确认 `generated_at` 是今天。

## 不做（显式边界，避免越界）

- ❌ 不替用户发 IM 给面试官（草稿拟好用户自发，措辞用户把关）
- ❌ 不碰飞书招聘 API（无"安排面试"公开接口，第⑦步用户手点）
- ❌ 不解析候选人简历（那是 collect-resumes 的活）
- ❌ 不建/改跟踪表（那是 recruit-followup 的活，本 skill 只读面试官日程）
- ❌ 不替用户问候选人时间（必须人沟通）

## 已验证可用的能力

| 能力 | 命令 | 结果 |
|------|------|------|
| 搜姓名拿 open_id | `contact +search-user --query 姓名`（**别加 `--has-chatted`**） | ✅ |
| 查真实忙闲 | `calendar +freebusy --user-id ou_xxx --start ISO --end ISO` | ✅ 返回真实忙碌段 |
| **反推空闲**（多人取交集） | `match_schedule.py` 内部用 freebusy 反推 | ✅ suggestion 漏报已弃用 |
| **吃 ATS 出完整草稿** | `match_schedule.py --talent-ids ... --form 视频` | ✅ 五要素齐全（2026-07-21 验证） |
| ATS 缺失告警 | 脚本标【需问用户：XXX】 | ✅ AI 只在此处介入 |

## 参考文档

- [`../lark-calendar-contact/SKILL.md`](../lark-calendar-contact/SKILL.md) — **calendar+contact OpenAPI 契约层**（接口/参数/踩坑权威源）
- [`references/decisions.md`](references/decisions.md) — 决策记录（为什么这样设计，维护者参考）
- ATS 数据契约见 `recruit-followup/references/review-contract.md`（`structured.ats[*]` 字段定义）
