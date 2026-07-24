---
name: lark-hire
version: 1.0.0
description: "飞书招聘 OpenAPI 契约层：Use when calling /open-apis/hire/v1/* endpoints (talents/applications/jobs/interviews/interview_records/attachments/stage流转/document_ai resume parse). 调招聘 API 前先读本 skill 查接口/字段/枚举/错误码，不裸调。不覆盖：招聘业务编排(见 recruit-followup)、简历收集(见 collect-resumes)、面试排期(见 schedule-interview)。"
metadata:
  requires:
    bins: ["lark-cli"]
  cliHelp: "lark-cli api --help"
---

# hire (v1) — 飞书招聘 OpenAPI 契约

**CRITICAL — 开始前 MUST 先用 Read 工具读取 [`../lark-shared/SKILL.md`](../lark-shared/SKILL.md)，其中包含认证、身份切换、权限处理和 `_notice` 处理。**

## 核心概念

- **飞书招聘（Hire）API 不在 lark-cli 业务域**：lark-cli 没有 hire 子命令（只有 mail/base/calendar/im/drive 等域）。所有 hire 接口必须走 `lark-cli api <METHOD> /open-apis/hire/v1/...` 裸调。
- **本 skill 是契约层，不是编排层**：只定义"接口长什么样/参数叫什么/枚举值是多少"。业务流程（录入/对账/面评同步）由 `recruit-followup` skill 编排，本 skill 不重复。
- **调用优先级**：先调 `notes/_lark_shared.py` 里的 `hire_*` 封装函数 → 封装没有的查本 skill 的接口表 → 仍没有的查官方文档（URL 加 `.md` 拉 markdown 版）实测。

## 身份规则

| 操作类型 | 身份 | 说明 |
|---|---|---|
| 所有**写**接口（POST） | `--as bot` | 用 user 身份返 `99991668`。bot 身份靠 APP_ID+APP_SECRET 自动换 token，不依赖用户登录 |
| 读接口（GET） | `--as bot` 优先 | bot 读取范围更广（全公司候选人），user 只能读自己可见范围 |

封装函数已默认 `--as bot`，业务脚本不传 identity 参数。

## 关键原则

- **POST body 用 `--data`，不是 `--params`**（`--params` 是 query string，用于 GET）
- **jobs 接口 `page_size` 上限 20**，>20 返回空。翻页用 `page_token`
- **时间戳一律毫秒**（`"1667260800000"` 字符串或 int），不是 ISO 字符串
- **文件路径用 cwd 相对路径**，不用绝对路径（lark-cli 限制）
- **中文/大 body 写 `@file`**：`--data "@notes/_talent.json"`，避免 shell 转义
- **删除附件 API 不存在**：飞书招聘平台限制。unknown-file 附件一旦传上去删不掉，第一次就要传对文件名（用 `_lark_shared.upload_attachment_with_name()`）

## 接口速查

按资源分组。完整字段/参数/返回结构见 [`references/hire-endpoints.md`](references/hire-endpoints.md)。

### Talent（人才档案）

| 操作 | 接口 | 封装函数 |
|---|---|---|
| 全量建人才（含经历/教育/简历附件） | `POST /open-apis/hire/v1/talents/combined_create` | `_lark_shared.hire_combined_create()` |
| 更新人才（挂新简历） | `POST /open-apis/hire/v1/talents/combined_update` | `hire_combined_update()` |
| 查人才详情 | `GET /open-apis/hire/v1/talents/{talent_id}` | `hire_get_talent()` |

> 字段结构（basic_info/career_list/education_list）见 [`references/hire-fields.md`](references/hire-fields.md)。

### Application（投递）

| 操作 | 接口 | 封装函数 |
|---|---|---|
| 建投递 | `POST /open-apis/hire/v1/applications` | `hire_create_application()`（已处理 1002206） |
| 查投递详情 | `GET /open-apis/hire/v1/applications/{application_id}` | `hire_get_application()` |
| 列投递 | `GET /open-apis/hire/v1/applications` | `hire_list_applications()` |
| 流转阶段 | `POST /open-apis/hire/v1/applications/{id}/transfer_stage` | `hire_transfer_stage()` |
| 终止/淘汰 | `POST /open-apis/hire/v1/applications/{id}/terminate` | `hire_terminate()` |

> transfer_stage / terminate 是唯一正确接口；`PUT /applications/{id}` 不存在会 404。

### Job（岗位）

| 操作 | 接口 | 封装函数 |
|---|---|---|
| 列岗位 | `GET /open-apis/hire/v1/jobs`（`page_size≤20`） | `hire_list_jobs()` |
| 查岗位详情 | `GET /open-apis/hire/v1/jobs/{job_id}` | `hire_get_job()` |

### Interview / 面评

| 操作 | 接口 | 封装函数 |
|---|---|---|
| 列面试（含面评结论） | `GET /open-apis/hire/v1/interviews?application_id=X` | `hire_list_interviews()` |
| 查面评全文记录 | `GET /open-apis/hire/v2/interview_records/{record_id}` | `hire_get_interview_record()` |
| 查面评 PDF 附件 | `GET /open-apis/hire/v1/interview_records/attachments` | `hire_get_feedback_pdf()` |

### Attachment（附件）

| 操作 | 接口 | 封装函数 |
|---|---|---|
| 上传附件（带文件名） | `POST /open-apis/hire/v1/attachments` | `_lark_shared.upload_attachment_with_name()` |

> **唯一保留 Python requests 直连的接口**：lark-cli `--file` 传不了 `file_name` 表单字段，必须用 multipart 直传。封装函数内部用 `APP_SECRET` 换 token。

### 简历解析

| 操作 | 接口 |
|---|---|
| Document AI 解析简历 | `POST /open-apis/document_ai/v1/resume/parse --file "file=<路径>"` |

返回结构化简历（name/mobile/email/gender/date_of_birth/careers/educations/self_evaluation）。封装在 `_lark_shared.parse_resume()` 或业务脚本里。

## 阶段（Stage）枚举

招聘流程阶段 type：

| type | 含义 |
|---|---|
| 1 | 初筛 |
| 2 | 简历评估 |
| 4 | 面试 |
| 5 | Offer沟通 |
| 6 | 待入职 |
| 7 | 已入职 |

权威定义与 ATS↔跟踪表映射见 [`references/hire-stages.md`](references/hire-stages.md)。常量在 `_lark_shared.py` 顶部（`STAGE_INTERVIEW=4` 等）。

## 错误码

| code | 含义 | 处理 |
|---|---|---|
| `0` | 成功 | — |
| `99991668` | 身份错（用 user 调写接口） | 换 `--as bot` |
| `1002206` | 投递已存在 | 不当错，封装函数已处理为 `status="exists"` |
| — | `basic_info is required` | combined_update 即使改一个字段也要带 basic_info |

## 反模式（不要做）

- **不要在业务脚本里裸调 `requests.post("https://open.feishu.cn/open-apis/hire/...")`** — 全部走 `_lark_shared.hire_*()` 封装。唯一例外是 `upload_attachment_with_name`（已在封装内）
- **不要重复定义 `DEGREE_MAP` / `STAGE_TYPE` / `map_degree` / `map_gender`** — 全部 import 自 `_lark_shared`
- **不要用 `--as user` 调写接口** — 必 `--as bot`
- **不要查 lark-cli hire 子命令** — 不存在。hire 走 `lark-cli api` 裸调
- **不要传 ISO 日期** — 转毫秒（`_lark_shared.to_ms("2022-11-01")` → `"1667260800000"`）

## 参考

- [`references/hire-fields.md`](references/hire-fields.md) — talent/application 字段字典 + 枚举（gender/degree/stage/termination_type）
- [`references/hire-endpoints.md`](references/hire-endpoints.md) — 全量接口表（path/参数/返回/lark-cli 示例）
- [`references/hire-stages.md`](references/hire-stages.md) — Stage 枚举权威定义 + ATS↔跟踪表映射
- `../lark-shared/SKILL.md` — 认证/身份/_notice 处理
- 项目根 `CLAUDE.md` — 飞书招聘 API 易错清单（权威，本 skill 与之一致）
- `notes/_lark_shared.py` — hire_* 封装函数实现
- 业务编排见 `recruit-followup` skill（录入流程/对账/面评同步），本 skill 只提供 API 契约
