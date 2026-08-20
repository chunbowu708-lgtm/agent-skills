---
name: pipeline-dashboard
description: >
  人才管道管理看板（前端HTML）。把飞书招聘 ATS 数据可视化成招聘漏斗——
  岗位×阶段矩阵、停滞预警、漏斗转化率，一眼看出"卡在哪、谁堆积了"。
  多维表格当数据库，HTML看板做可视化层（表格不好用→前端补）。
  触发词：管道看板、漏斗看板、招聘看板、整体进展、卡在哪、谁堆积了、岗位进展总览。
  只要用户提到"看板/漏斗/管道/整体进展/全盘"，就使用这个skill。
  覆盖：岗位×阶段漏斗图、停滞候选人高亮、分岗位明细、转化率计算。
  不覆盖：每日作战清单（见daily-recruit-report日报，短期行动导向）、
  数据计算（读_daily_review.py产出，不重算）、候选人保温（见candidate-nurture）。
  依赖：notes/_daily_review.json（每日对账产出，契约见 daily-recruit-report/references/review-contract.md）。
---

# 人才管道管理看板

## 这个 skill 解决什么

光看逐人列表**看不出全局**——看不出"哪个岗位堆积了""整体卡在哪个阶段"。你需要的是一个**全局视图**：所有岗位 × 所有阶段的漏斗，一眼定位瓶颈。

本 skill 把多维表格（数据库）+ 飞书招聘数据，渲染成**离线 HTML 看板**，解决"整体怎么看"的问题。

## 与日报的边界（你定的：短期 vs 长期不冲突）

| 维度 | 日报（daily-recruit-report） | 管道看板（本skill） |
|------|------------------------|-------------------|
| 时间 | 每日 | 随时（按需生成） |
| 导向 | **行动**（今天干啥） | **管理**（整体卡在哪） |
| 颗粒 | 人（谁该碰） | 岗位×阶段（哪个环节堵） |
| 形态 | 飞书文档 | HTML看板（浏览器看） |

两者不替代——日报驱动每日行动，看板驱动管理决策（"该给哪个岗位加力了"）。

---

## 工作流

```
① 读数据      _daily_review.json（ATS数据+日程）
    ↓
② 聚合        按岗位×阶段聚合人数，算停滞天数
    ↓
③ 渲染看板    HTML（分工作室卡片 + 色块漏斗 + 预警区）
    ↓
④ 落盘+打开   HTML存notes/，浏览器打开
```

---

## 看板包含三个区域（v2，2026-08-04 重写）

### 区域1：全局漏斗（横向色块条）

所有岗位汇总的阶段分布，用 CSS 色块条呈现（初筛灰→初试蓝→复试黄→终试橙→HR面绿→Offer深绿），每个色块显示人数，鼠标悬停看该阶段候选人列表。瓶颈阶段（人数最多）高亮。

### 区域2：分工作室管道卡片

每个工作室一张卡片，卡片内每岗位一行，用**色块圆点**（每人一个圆点，颜色=所在阶段）展示漏斗分布，鼠标悬停看候选人姓名+停留天数。卡片末尾标该岗位关键信号（堵面评/高危停滞/仅N人）。

### 区域3：预警与推送提醒

三类预警分卡片呈现：
- 🔴 **高危停滞**（≥15天，流失风险）：候选人+岗位+停留天数+动作
- 🟣 **面评阻塞**（已面完等评）：候选人+面试官+欠评天数+动作
- ⚡ **急需补人**（在途≤2人岗位）：岗位+人数+动作

---

## 脚本

### `scripts/generate_dashboard.py`

读 `_daily_review.json` → 聚合 → 输出 HTML。

```bash
# 前置：先跑对账生成报告
python notes/_daily_review.py

# 生成看板
python "…/pipeline-dashboard/scripts/generate_dashboard.py" \
  --report notes/_daily_review.json \
  --output notes/pipeline-dashboard.html

# 浏览器打开
start notes/pipeline-dashboard.html
```

脚本逻辑（纯数据聚合 + HTML 渲染，不调 AI）：
1. 读 `_daily_review.json` 的 `structured.ats`（含 name/job/dept/stage/dwell_days/latest_conclusion/interview_count/talent_id，契约见 review-contract.md）
2. 用 `interview_count` + `latest_conclusion` 推断每人所在漏斗阶段（初筛/初试/复试/终试/HR面/Offer）
3. 按 dept（工作室）→ job（岗位）分组
4. 从 `structured.feedback_overdue` 提取面评阻塞
5. 算高危停滞（dwell≥15）、急需补人（在途≤2人）
6. 内嵌 CSS + JS 渲染 HTML（完全自包含，不读外部模板）

> 与 talent-profile 不同：pipeline-dashboard 是**纯数据聚合**（不调 AI 判断），脚本可独立跑完。
>
> **注**：v2 脚本完全自包含（CSS/JS 内嵌），不再使用外部 HTML 模板。旧版 `assets/dashboard-template.html` 已删除。

---

## 不做的事（显式边界）

- ❌ **不重复算对账数据**——读 `_daily_review.json`，不重新拉飞书 API
- ❌ **不替代日报**——看板是管理视图，日报是行动清单，两者并存
- ❌ **不做候选人保温**——停滞预警只列出来，保温动作归 candidate-nurture
- ❌ **不做面评分析**——看板只看"流程进展"，面评内容分析归 talent-review（未来）
