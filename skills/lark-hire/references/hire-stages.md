# hire Stage 枚举（权威定义）

> 2026-07-20 实测自 `GET /open-apis/hire/v1/job_processes`，迷你玩租户两个流程（校招/社招）阶段一致。本表是全项目唯一权威源，业务脚本只用这里的值。

## stage type → 名称

| type | zh_name | en_name | 常量（`_lark_shared`）|
|---|---|---|---|
| 1 | 简历初筛 | Resume screening | `STAGE_SCREEN` |
| 2 | 简历评估 | Resume evaluation | `STAGE_EVALUATE` |
| 4 | 面试 | Interview | `STAGE_INTERVIEW` |
| 5 | Offer沟通 | Offer | `STAGE_OFFER` |
| 6 | 待入职 | To be onboarded | `STAGE_ONBOARD` |
| 7 | 已入职 | Onboarded | `STAGE_EMPLOYED`（终态，不进作战清单/看板/保温）|

**没有 type=3**（笔试）。迷你玩租户未启用笔试阶段。

## type=3 的历史幽灵（已消除）

旧脚本 `_weekly_enrich.py` / `_weekly_report_query.py` 曾用错误的 `STAGE_TYPE = {1:"待筛选", 2:"初筛", 3:"笔试", 4:"面试", 5:"offer", 6:"入职", 7:"入职"}`，问题：
- `1` 应是"简历初筛"不是"待筛选"
- `2` 应是"简历评估"不是"初筛"
- `3` 笔试阶段不存在
- `6`/`7` 一个是"待入职"一个是"已入职"，不能都叫"入职"

已统一改为 import `_lark_shared.STAGE_TYPE`，本表为唯一源。

## 关键常量

`_lark_shared.py` 顶部：

```python
STAGE_SCREEN = 1       # 简历初筛
STAGE_EVALUATE = 2     # 简历评估
STAGE_INTERVIEW = 4    # 面试
STAGE_OFFER = 5        # Offer沟通
STAGE_ONBOARD = 6      # 待入职
STAGE_EMPLOYED = 7     # 已入职（终态）

STAGE_TYPE = {
    1: "简历初筛",
    2: "简历评估",
    4: "面试",
    5: "Offer沟通",
    6: "待入职",
    7: "已入职",
}
```

## stage_id ≠ stage type

- **`stage.type`**（上表的 1-7）：流程阶段类型，全局固定
- **`stage.id`**（如 `7376529083536165147`）：具体流程节点的 ID，每个流程独立

`transfer_stage` 接口要的是 `stage.id`（具体节点），不是 `stage.type`。从 `GET /hire/v1/job_processes` 取。

## stage 与投递活跃状态的关系

| 字段 | 含义 | 用途 |
|---|---|---|
| `application.stage.type` | 当前在哪个阶段（1-7） | 判断流程位置 |
| `application.active_status` | 投递活跃度 | `1`招聘中 / `2`暂停(含淘汰) / `3`已关闭 |

`terminate` 后 `active_status` 变 2（但 stage.type 不一定变），对账脚本以 `active_status==1` 判断"还在流程里"。

## ATS ↔ 跟踪表映射

跟踪表（多维表格）的"状态"字段是人工/脚本填写的主观层，ATS 的 `stage.type` 是客观层。映射关系见 `recruit-followup/references/review-contract.md`，本文件不重复。
