---
name: candidate-nurture
description: >
  daily-recruit-report 的后半场（不独立运行，前置 gate 见 SKILL 顶部）。读其对账产出的预警数据（停滞/面评欠收/待推进）
  + 信号文件（LLM判读的邀约/拒绝+用户决策）+ 保温状态文件（谁碰过几次/该不该升级），
  产出"今天该碰谁 + 怎么碰（话术）"的行动清单，并记录触达状态形成闭环，防候选人静默流失。
  触发词：保温、候选人保温、催面评、面评催收、谁该跟进、停滞预警、候选人冷了、跟进提醒。
  只要用户提到"保温/催面评/停滞/跟进提醒/谁该碰"，就使用这个skill。
  覆盖：面评催收清单、停滞候选人保温提醒、阶段化保温话术、话术升级、触达状态跟踪、信号交叉比对。
  不覆盖：自动发消息给候选人（只给话术，人工发）、面试时间协调（见schedule-interview）、简历筛选（见analyze-resumes）。
  依赖：
    - notes/_daily_review.json（每日对账产出，含stuck/to_advance/feedback_overdue/
      interviewer_feedback_debt/pending_evaluations，契约见 daily-recruit-report/references/review-contract.md）
    - notes/_signals.json（Agent判读的邀约/拒绝信号+用户决策，契约见 references/signals-contract.md）
    - notes/_nurture_state.json（保温触达状态，本skill的 nurture_state.py 维护）
    - 面评全文（可选，催面评时引用具体内容）
---

# 候选人保温与面评催收

## 前置 gate（开局第一动作，不可跳过）

本 skill 是 **daily-recruit-report 的后半场**，不独立产出数据。开局先校验数据就绪：

1. 读 `notes/_daily_review.json`，校验顶层 `date == 今天`。
2. 读 `notes/_signals.json`，校验顶层 `date == 今天`。
3. 任一不存在或非当天 → **停止，不产出任何清单**。告知用户：「保温是 daily-recruit-report 的后半场。请先执行早晨对账（`python notes/_daily_review.py`）并完成意图判读/决策全流程，再唤起保温。」
4. 两文件均为当天 → 继续。

不降级、不兜底、不产出残缺清单。

## 这个 skill 解决什么

招聘的本质是"把人留住"。`_daily_review.py` 已算出谁该跟进，本 skill 补上配套的"今天该碰谁 + 用什么话术碰"。

## 与 daily-recruit-report 的边界

| 能力 | 归属 | 说明 |
|------|------|------|
| 算预警数据（停滞/欠收/待推进） | **daily-recruit-report**（`_daily_review.py`） | 本skill**只读**其产出 `_daily_review.json` |
| LLM判读群消息意图 + 落盘 | **daily-recruit-report**（Agent执行） | 判读后写入 `_signals.json`，本skill只读 |
| 早晨决策落盘 | **daily-recruit-report**（Agent执行） | 用户决策后Agent更新 `_signals.json` 的 decisions |
| 产出"今天该碰谁+话术" | **本skill** | 读三路数据 → 匹配话术 → 行动清单 |
| 记录保温触达状态 | **本skill**（`nurture_state.py`） | 触达后写 `_nurture_state.json`，跨天延续 |
| 发消息给候选人 | **人工** | 本skill只给话术，不自动发 |

## 工作流

```
① 读预警数据     读 _daily_review.json 的 structured.stuck / feedback_overdue /
                 interviewer_feedback_debt / pending_evaluations / to_advance
    ↓
①.3 读信号数据   读 _signals.json（LLM判读的邀约/拒绝 + 用户早晨决策）
    ↓
①.5 读保温状态   读 _nurture_state.json（谁碰过了、碰了几次、该升级了）
    ↓
② 排优先级+过滤  排除：今天已碰过的（不重复）、用户已决定处理的、已终止的
                 升级：连续未回复的（轻量→正常→强催）
                 标注：escalation_level=3 的提示"建议终止"
                 补充：signals里未落地ATS的邀约→纳入清单
    ↓
③ 匹配话术       每个候选人 → 按"阶段+停滞原因+escalation_level"匹配话术
    ↓
④ 产出行动清单   "今天该碰谁 + 怎么碰"（话术+渠道+升级标注），用户确认后执行
    ↓
⑤ 记录保温动作   用户确认执行后 → nurture_state.py --touch 记录触达
```

---

## 阶段1：读预警数据

```bash
# 读报告（gate 通过后 _daily_review.json 已确认是当天）
python -c "
import json
d = json.load(open('notes/_daily_review.json', encoding='utf-8'))
s = d['structured']
for k in ['feedback_overdue','interviewer_feedback_debt','pending_evaluations','stuck','to_advance']:
    print(k, len(s.get(k,[])))
"
```

**五类预警数据**（`_daily_review.json` → `structured`，字段契约见 review-contract.md）：

| 数据 | 含义 | 流失风险 |
|------|------|---------|
| `feedback_overdue` | 面试已过但面试官没交面评（候选人视角，最新一场） | 🔴 高（候选人等结果，拖久=凉） |
| `interviewer_feedback_debt` | **面试官欠面评聚合**（interview_tasks API）：每人欠评数/陈年数/最久拖几天/池内阻塞者 | 🔴 高（重欠面试官=批量卡流程，零星私聊催不动） |
| `pending_evaluations` | 简历评估发起后一直没提交（evaluations API，commit_status=2） | 🟡 中（评估卡住→阶段流转不动） |
| `stuck` (dwell≥2天) | 卡在某阶段无进展 | 🟠 中高（看卡几天，≥5天=高危） |
| `to_advance` | 面评通过但没推进下一轮 | 🟡 中（候选人觉得"面了没下文"） |

---

## 阶段1.3：读信号数据（交叉比对 LLM 判读 + 用户决策）

> 数据来源：`notes/_signals.json`（daily-recruit-report 早晨对账时 Agent 判读 + `signals.py` 落盘）
> 契约：[`references/signals-contract.md`](references/signals-contract.md)

读两个列表：

**`signals`**（LLM 从群消息判读的意图）：
- `type=invite` 且 `ats_landed=false` → **未闭环邀约**，业务方说"约"但ATS没落地，最该追的人，**纳入保温清单最高优先级**
- `type=reject` → 业务方说"不推进"但还没终止 → 标注"业务已拒绝，确认是否终止"
- `type=hold` → 业务方说"先放放" → 轻量保温，不催
- `type=discuss` → 讨论性消息 → 忽略

**`decisions`**（用户早晨审查作战清单后的决策）：
- `decision="今天约"` → **排除**（用户已决定今天处理，不重复提醒）
- `decision="催面评"` → **排除**（用户已决定催，不重复提醒）
- `decision="再等等"` → 轻量保温（用户选择等，但不冷落候选人）
- `decision="终止"` → **排除**（用户已决定终止，不再保温）

---

## 阶段1.5：读保温状态（跨天延续）

```bash
python "…/candidate-nurture/scripts/nurture_state.py" --read
python "…/candidate-nurture/scripts/nurture_state.py" --stale
```

**读什么**（`notes/_nurture_state.json`）：

每个候选人的保温状态：
| 字段 | 含义 | 用途 |
|------|------|------|
| `last_touch_date` | 上次触达日期 | **去重**：今天已碰过的不重复 |
| `touch_count` | 累计触达次数 | 判断是否该升级 |
| `escalation_level` | 1轻量/2正常/3强催 | **话术分级**依据 |
| `last_response` | replied/no_reply/null | **升级触发**：no_reply + 多次碰→升级 |
| `status` | active/escalated/terminated | escalated→提示终止；terminated→排除 |

**三类过滤/升级**：
1. **去重**：`last_touch_date == 今天` → 今天已碰过，跳过（除非用户明确要追加触达）
2. **升级**：`last_response == "no_reply"` 且**这是第 2 次及以后的触达**（即累计触达 ≥2 次）且今天还没碰过 → escalation_level+1，话术升级。同一日多次触达不重复升级。
3. **终止提示**：`escalation_level == 3` 且 `last_response == "no_reply"` → 标注"⚠️建议终止"，先问用户

---

## 阶段2：阶段细分 + blocker 定位 + 排序

### 核心原则：先找 blocker，再定动作

> **核心原则**：每个卡住的候选人，一定卡在某个具体的人或环节上。先定位 blocker（卡在谁手上），再决定动作（该催谁/该碰谁）。错配动作对象（该催面试官却去碰候选人）会让两方都白等。

### 7 阶段 × blocker 模型（替代旧的 5 优先级平铺）

每个人按"卡在哪个环节 + blocker 是谁"归入以下 7 类，**每类对应完全不同的动作对象**：

| 阶段 | 含义 | blocker | 动作对象 | 数据来源 |
|------|------|---------|----------|----------|
| **① 邀约未落地** | 业务说约但没约成 | 你自己 | 录入ATS+约时间 | signals type=invite, ats_landed=false |
| **② 面试欠面评** | 面完了面试官没交 | **面试官** | 催面试官（不是碰候选人）| feedback_overdue（候选人视角）+ interviewer_feedback_debt（面试官聚合，重欠升级用）|
| **③ 面评通过待推进** | 面评过了流程没动 | 你自己/Bruce | 推进下一轮 or 走offer | to_advance |
| **④ offer 推进卡住** | 业务确认offer但ATS没转 | Bruce/Tina | 催Bruce/Tina+保温候选人 | signals type=invite + stage=面试+终试通过 |
| **⑤ 待入职流失风险** | 接了offer没入职 | HR/入职流程 | 确认入职+保温 | stage=待入职/Offer沟通 |
| **⑥ 长期停滞需决策** | 卡30天+没动静 | 业务方决策 | 问业务"还推不推" | stuck dwell≥30 或 to_advance dwell≥30 |
| **⑦ 暗坑待核查** | 面试阶段零记录 | 未知 | 去ATS核查 | stuck reason含"⚠️零记录" |

### 排序规则（产出执行队列，不是分类菜单）

> **输出是一个排好序的执行队列，不是分类菜单。** 用户只需逐条"执行"/"跳过"，决策成本趋近于零。

排序优先级（从高到低）：
```
第一梯队（今天必须做——不做会丢人/丢流程）：
  Q1. ④ offer推进卡住（blocker=Bruce/Tina）—— 竞品挖角窗口，每多拖一天流失概率飙升
  Q2. ② 面评欠收≥14天（urgency=🔴严重）—— 候选人面完两周没消息，快凉了
  Q3. ④/③ 业务确认推进但表漂移 —— 数据不一致会持续误报
  Q4. ⑦ 暗坑待核查（零记录≥7天）—— 最容易漏的人

第二梯队（今天该做——防止变凉）：
  Q5. ② 面评欠收4-14天（urgency=🟠常规）
  Q6. ① 邀约未落地 —— 录入+约面
  Q7. ⑤ 待入职 —— 确认入职准备
  Q8. 今天有面试的人 —— 面完即发D0保温

第三梯队（需用户决策——不擅自处理）：
  Q9.  ⑥ 长期停滞30天+ —— 问业务"还推不推"，不擅自催也不擅自终止
  Q10. ② 面评欠收<4天（urgency=🟡可缓）—— 今天不催，记一笔4天线复查
```

**同梯队内排序**：按 dwell_days 降序（卡得越久越优先）。
**同 blocker 合并**：同一面试官欠多人面评 → 合并成一条消息催（如涂萍欠裴偲宇+曾桥）。合并时以 `interviewer_feedback_debt[].in_scope` 为准（阻塞者名单，已滤旧轮僵尸）。
**重欠升级（interviewer_feedback_debt）**：面试官非陈年积欠（`pending_count − ancient_count`）≥10 条，或池内阻塞 ≥2 人 → 零星私聊已不够，队列单独标一条"建议业务群/leader 层面推"，附数据（欠 N 条·最久拖 N 天·其中阻塞我 N 人）。话术随场合升级：私聊礼貌催 → 群里@点名 → leader 层面推。
**评估未提交（pending_evaluations）**：欠 ≥3 天 → 催评估人提交（或判无需评估）；欠 ≥30 天 → 先问业务"还评不评"，不评就流转面试或终止，评估挂着只会阻塞阶段。

### 过滤规则（排优先级后应用）

| 条件 | 动作 | 理由 |
|------|------|------|
| 保温状态 `last_touch_date == 今天` | **跳过** | 今天已碰过，不重复（除非用户明确追加） |
| 保温状态 `status == "terminated"` | **排除** | 已终止，不再保温 |
| 信号 `decisions` 里 `decision == "今天约"` | **排除** | 用户已决定处理 |
| 信号 `decisions` 里 `decision == "催面评"` | **排除** | 用户已决定催 |
| 信号 `decisions` 里 `decision == "终止"` | **排除** | 用户已决定终止 |
| 信号 `decisions` 里 `decision == "再等等"` | **降级为轻量** | 用户选择等，不催但保温 |
| 保温状态 `escalation_level == 3` 且 `last_response == "no_reply"` | **标注"⚠️建议终止"** | 连续强催无回复，先问用户要不要继续 |

### 两套坐标系的关系（阶段2 blocker 模型 vs 保温节奏引擎）

> **它们是正交的两个维度，不是二选一**：
> - **阶段2 blocker 模型**（上面的 7 类 + 排序）回答 **"今天做什么 action"**——催谁、碰谁、核查谁。按 `dwell_days` + `urgency` 排出执行队列。
> - **保温节奏引擎**（阶段3）回答 **"碰候选人时用什么 tone"**——D0/2-3天/3-5天/拖一周的话术分级。按"距上次同步候选人多少天"选话术级别。
>
> 对同一个人的完整动作 = **阶段2 定 action 对象** × **节奏引擎定话术 tone**。例如 dwell_days=10 的 ②欠面评候选人：阶段2 说"催面试官"（action），节奏引擎说"同时保温候选人用'拖一周以上'话术"（tone）。两者不矛盾。
>
> **去重仲裁**：当节奏引擎和阶段2 过滤规则冲突时（如 last_touch_date=今天 → 节奏引擎不碰，但阶段2 说必须催），**以阶段2 的过滤规则为准**（:190-196）——今天已碰过的跳过是硬规则，节奏引擎的"该碰了"是软建议。

---

## 阶段3：匹配话术（详见 references/nurture-scripts.md）

按**阶段 × 停滞原因 × escalation_level**匹配话术。核心原则：

1. **催面评**（对面试官）：礼貌但明确，点出"候选人还在等"，必要时引用面评要点（面评全文可取，刚解锁）
2. **碰候选人**（对候选人）：不让对方觉得"被冷落"，给个具体进展/预期，不要空问候
3. **话术升级**：连续未回复时，从轻量→正常→强催逐级升级（升级规则见 references/nurture-scripts.md 开头）

### 话术匹配逻辑

| 场景 | 话术方向 | 示例（完整版见references） |
|------|---------|------------------------|
| 催面评（对面试官） | 礼貌催+候选人等结果 | "X总，张三一面过去3天了，候选人那边在等结果，方便尽快写下面评吗？" |
| 停滞在"等约面"（对候选人） | 给预期+保温 | "XX你好，这边还在协调面试官时间，预计这周内安排，你那边最近方便面试的时间是？" |
| 停滞在"面试后等结果"（对候选人） | 主动同步进展 | "XX你好，一面反馈我们在汇总中，预计X号前给你答复，感谢耐心等待～" |
| 面评通过待推进（对候选人） | 好消息+预期管理 | "XX你好，一面通过了🎉，这边在安排下一轮，预计下周，保持联系～" |
| 长期停滞（≥7天，对候选人） | 重新激活 | "XX你好，之前聊的XX岗位还在推进中，你那边近期看机会的意愿有变化吗？" |

### 保温节奏引擎（核心改进：保温是节奏，不是分类）

> **核心原则**：候选人最在意的不是结果，是"有人记得我"。保温不只针对 stuck 的人——**所有面试过/在途的候选人都该有节奏地保温**，区别只是话术级别随停留天数升级。面试通过待推进的人（③）最焦虑（觉得"面了没下文"），但也最容易被漏掉。

**所有在途候选人**（stuck + to_advance + feedback_overdue + 待入职 + offer阶段）都进保温队列，按"距上次同步候选人多少天"分配话术级别：

| 节奏级别 | 触发条件 | 话术方向 | 话术示例 |
|----------|----------|----------|----------|
| **D0 当天** | 今天面试 | 感谢+给预期 | "今天辛苦啦，有进展第一时间同步你～" |
| **2-3天无进展** | 面试后2-3天 | 主动同步（哪怕没进展）| "还在内部推进，暂时没最终结果，有消息第一时间联系你～" |
| **3-5天无进展** | 面试后3-5天 | 继续主动（哪怕一句）| "今天又跟进了一下流程，还在推进中，我会继续关注～" |
| **拖一周以上** | 面试后≥7天 | **给信息量**（说清楚为什么慢）| "主要在内部审批环节，耗时比面试阶段长，我持续帮关注～" |

**节奏引擎的关键规则**：
1. **"哪怕没有任何进展也发"**——2-3天级别的话术核心价值是"HR没消失"，不是传递新信息
2. **拖一周以上必须给信息量**——不能一直重复"还在流程中"，要说清楚卡在哪个环节（审批/评估/协调），候选人知道为什么慢，焦虑会少很多
3. **只同步事实，不解读原因**——别说"可能是因为XX"，只说"目前在XX环节"
4. **保温和催 action 是两件事**——offer卡住的人既要催Bruce（action）也要保温候选人（让他知道在推进），两件事都做

---

## 阶段4：产出优先级执行队列

> **核心改进**：输出是一个**排好序的执行队列**，不是分类菜单。每条 = 一个人 × 一个 blocker × 一个动作 × 一句话术。用户只需逐条"执行"/"跳过"，决策成本趋近于零。

**输出格式**：
```
=== 今日执行队列（YYYY-MM-DD）===
按"不做会出事 → 做了更好"排序。逐条 执行 / 跳过。

第一梯队：今天必须做（不做会丢人/丢流程）

Q1. 🔴 [④offer卡住] 刘弘毅 — 终试通过27天，Bruce已确认推进offer但ATS没转
    blocker：钟波(Bruce)+Tina
    → 动作：私聊Bruce催offer进度
    → 话术：Bruce，刘弘毅的offer流程目前推进到哪一步了？薪酬信息已经发过了，候选人那边在等结果，方便同步进度吗？
    → 同时保温候选人：刘弘毅你好，面试流程已全部走完，目前在推进offer审批环节，有进展第一时间同步你，感谢耐心等待🙏

Q2. 🔴 [②欠面评🔴严重] 裴偲宇 — 初试6-26，涂萍欠面评18天
    blocker：涂萍
    → 动作：私聊涂萍催面评（如涂萍还欠其他人，合并成一条）
    → 话术：涂萍你好，裴偲宇6-26初试的面评还没提交，候选人那边在等结果，方便尽快补一下吗？
    → 同时保温候选人（拖一周以上级别）：裴偲宇你好，不好意思让你久等了，今天跟进了一下，面试评估还在内部推进中，业务这边还需要一点时间确认，我会继续帮你关注，有消息第一时间同步🙏

Q3. 🔴 [⑦暗坑] 罗海贵 — 面试阶段33天零面试记录
    blocker：未知（需核查）
    → 动作：去飞书招聘核查——是否安排过面试？面试官是谁？

第二梯队：今天该做（防止变凉）

Q4. 🟠 [⑤待入职] 谭顺馨 — 接了offer7天，7-20入职
    → 动作：确认HR是否已发入职指引
    → 保温候选人：谭顺馨你好，距离7-20入职还有几天，入职相关事项我们在准备中，有需要配合的会提前同步～

第五梯队：需你决策（不擅自处理）

Q9. 🟡 [⑥长期停滞] 刘镇涛 — 面评通过42天没推进
    → 动作：问业务方"还继续推进吗？"，不擅自催也不擅自终止
```

**队列产出规则**：
1. **每条必须标 blocker**——没标 blocker = 没分析到位，blocker=未知 时标"需核查"
2. **同 blocker 合并**——同一面试官欠多人面评、同一业务方卡多个offer，合并成一条
3. **action 和保温分开标**——offer卡住的人既标"催Bruce"(action)又标"保温候选人"，两件事
4. **30天+的不盲目保温**——先在 Q9 确认"还推不推"，决策后再定是否保温
5. **末尾附全员保温节奏表**——所有在途候选人按节奏级别（D0/2-3天/3-5天/拖一周以上）归类，确保没人被遗忘

**AI 行为指导**：
- 队列生成后**先给用户看**，用户确认后再逐条执行（不自动发消息）
- 催面评的话术要让用户确认（措辞涉及对面试官的态度，不能太硬）
- `escalation_level=3` 且 `last_response=no_reply` 的候选人，**先问用户"这个还要不要继续跟"**，可能该终止了
- 未闭环邀约（signals里 `ats_landed=false`）要同时提示"追问业务方确认"
- **用户可批量授权**——说"Q1-Q5全执行"即可批量处理，不必逐条确认

---

## 阶段5：记录保温动作（闭环）

用户确认执行后，**Agent 调 `nurture_state.py` 记录触达**，使保温状态跨天延续：

```bash
# 记录触达（每个执行的候选人都记）
python "…/candidate-nurture/scripts/nurture_state.py" \
  --touch <talent_id> --name <姓名> --action "保温-正常" --channel "飞书IM"

# 记录催面评
python "…/candidate-nurture/scripts/nurture_state.py" \
  --touch <talent_id> --name <姓名> --action "催面评" --channel "飞书IM"

# 如果用户反馈"候选人回复了"
python "…/candidate-nurture/scripts/nurture_state.py" \
  --response <talent_id> --status replied

# 如果用户决定终止
python "…/candidate-nurture/scripts/nurture_state.py" \
  --reset <talent_id>
```

**为什么这步重要**：没有这步，明天跑 nurture 时不知道今天碰过谁，会重复碰同一个候选人、用同一级别话术——这就是之前保温不闭环的根因。

---

## 不做的事（显式边界）

- ❌ **不自动发消息**——只给话术+渠道建议，人工发（措辞+时机需人把关）
- ❌ **不算预警数据**——读 `_daily_review.json`，不重新算（那是 daily-recruit-report 的活）
- ❌ **不做 LLM 消息判读**——读 `_signals.json`（daily-recruit-report 判读后落盘的），不自己判读 raw_messages
- ❌ **不替用户做终止决策**——长期停滞的，提示用户判断，不自动终止；用户决定后调 `--reset`
- ❌ **不碰面评内容分析**——催面评只催"提交"，面评内容分析是 talent-review 的活
- ✅ **会记录触达状态**——用户执行后调 `nurture_state.py --touch`，使保温跨天延续（这是新增能力，之前不记录）

## references（按需加载）

- [`references/nurture-scripts.md`](references/nurture-scripts.md) — 完整保温话术库（按阶段×原因分场景，含催面评/约面停滞/面试后/通过/长期停滞/节日问候）
