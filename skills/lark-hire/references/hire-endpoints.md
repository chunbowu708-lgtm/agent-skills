# hire 接口全量表

> 每个 hire API 的完整 path / 参数 / 返回 / lark-cli 调用示例。优先调 `_lark_shared.hire_*()` 封装，裸调时查本表。

所有命令默认在项目根 `<PROJECT_ROOT>/` 执行，git-bash 环境需加 `MSYS_NO_PATHCONV=1` 前缀（封装函数已处理）。

## Talent

### POST /open-apis/hire/v1/talents/combined_create

全量建人才（基本信息+经历+教育+简历附件一步到位）。

**封装**：`_lark_shared.hire_combined_create(basic_info, att_id, careers=None, edus=None, self_eval=None) -> talent_id`

**请求体结构**：见 [`hire-fields.md`](hire-fields.md)。

```bash
MSYS_NO_PATHCONV=1 lark-cli api POST /open-apis/hire/v1/talents/combined_create \
  --as bot --data "@notes/_talent.json"
```

**返回**：`data.talent_id` 或 `data.talent.id`

**错误**：字段校验失败报 `field validation failed`，用最小化二分法定位（先只发 basic_info，逐个加字段）。

### POST /open-apis/hire/v1/talents/combined_update

更新人才（挂新简历/改字段）。

**封装**：`_lark_shared.hire_combined_update(talent_id, basic_info, att_id=None, ...) -> dict`

**关键约束**：即使只改一个字段（如 `resume_attachment_id`），也必须带 `basic_info` + `talent_id`，否则报 `basic_info is required`。沿用原值即可。

### GET /open-apis/hire/v1/talents/{talent_id}

**封装**：`_lark_shared.hire_get_talent(talent_id) -> dict`

**关键返回**：
- `data.talent.resume_attachment_list` — 附件列表（检测旧附件）
- `data.talent.resume_attachment_id_list` — 附件 ID 列表（上面为空时的兜底）

```bash
MSYS_NO_PATHCONV=1 lark-cli api GET /open-apis/hire/v1/talents/7092356709288453 \
  --as bot --params '{"user_id_type":"open_id"}'
```

## Application

### POST /open-apis/hire/v1/applications

建投递。

**封装**：`_lark_shared.hire_create_application(talent_id, job_id) -> (status, msg, app_id)`

返回 `status`：`"ok"` / `"exists"`（已投递，code 1002206）/ `"fail"`。

```bash
MSYS_NO_PATHCONV=1 lark-cli api POST /open-apis/hire/v1/applications \
  --as bot --data '{"talent_id":"7092356709288453","job_id":"7646359900507687195"}'
```

### GET /open-apis/hire/v1/applications/{application_id}

**封装**：`_lark_shared.hire_get_application(app_id) -> dict`

**关键返回**：`data.application.talent_attachment_resume_id`（投递挂的附件，检测旧附件比 talent 列表更可靠）。

### GET /open-apis/hire/v1/applications

列投递。

**封装**：`_lark_shared.hire_list_applications(job_id=None, talent_id=None, page_size=20) -> list[dict]`

**参数**（query string）：
- `job_id` / `talent_id` — 过滤
- `page_size` — 默认 20（不要超过）
- `page_token` — 翻页

### POST /open-apis/hire/v1/applications/{id}/transfer_stage

流转阶段（推进到下一轮）。

**封装**：`_lark_shared.hire_transfer_stage(app_id, stage_id) -> dict`

**请求体**：只有 `stage_id`。stage_id 从 `GET /hire/v1/job_processes` 取（不是 stage type，是具体流程节点的 ID）。

[官方文档](https://open.feishu.cn/document/server-docs/hire-v1/candidate-management/delivery-process-management/application/transfer_stage)

### POST /open-apis/hire/v1/applications/{id}/terminate

终止/淘汰。

**封装**：`_lark_shared.hire_terminate(app_id, termination_type) -> dict`

**请求体**：`{"termination_type": <int>}`（值查后台配置）。

**唯一正确接口**：`PUT /applications/{id}` 不存在会 404。调后 `active_status` 变 2，对账脚本自动过滤。

### POST /open-apis/hire/v1/applications/{id}/recover

恢复已终止投递（2026-08-18 新增）。无请求体。

**封装**：`_lark_shared.hire_recover_application(application_id) -> dict`

**⚠️ 写操作，逐案经用户确认后才能调**。比 terminate 后重建投递更优：保住原投递的历史面试记录/阶段流转。

**前置条件**：投递已终止、人才未入职、未锁定在其他投递。

**错误码**（封装已映射成中文提示）：

| code | 含义 |
|---|---|
| 1002225 | 投递未终止（本来就活跃，无需恢复） |
| 1002210 | 人才已被其他投递锁定 |
| 1002206 | 存在相似投递 |
| 1002209 | 人才已入职 |
| 1002201 | 投递不存在 |

## Job

### GET /open-apis/hire/v1/jobs

列岗位。⚠️ `page_size` 上限 20，>20 返回空。

**封装**：`_lark_shared.hire_list_jobs(keyword="", page_all=False) -> list[(code, job_id, title)]`

```bash
MSYS_NO_PATHCONV=1 lark-cli api GET /open-apis/hire/v1/jobs \
  --as bot --params '{"page_size":20}' --page-all --format ndjson > notes/_jobs_all.ndjson
```

### GET /open-apis/hire/v1/jobs/{job_id}

**封装**：`_lark_shared.hire_get_job(job_id) -> dict`

**关键返回**：`title` / `code` / `department.zh_name` / `create_user_id`（岗位归属过滤）/ `active_status`（1招聘中/2暂停/3关闭）。

## Interview / 面评

### GET /open-apis/hire/v1/interviews

列面试（含面评结论）。`application_id` / `interview_id` / `start_time` / `end_time`(毫秒) 四个过滤参数不许同时为空，`page_size≤100`，默认 `user_id_type=open_id`（`interview_record_list[].interviewer.id` 即 open_id）。

**封装**：`_lark_shared.hire_list_interviews(application_id) -> list[dict]`（按投递列）

**封装**：`_lark_shared.hire_get_interview_by_id(interview_id) -> dict`（按面试 ID 查单个，返回 `{}`=查不到）

**关键返回**：`data.items[].interview_record_list[].conclusion` — `1`通过 / `2`不通过 / 空=未提交面评。

```bash
MSYS_NO_PATHCONV=1 lark-cli api GET /open-apis/hire/v1/interviews \
  --as bot --params '{"application_id":"7092356709288453","page_size":10}'
```

### GET /open-apis/hire/v1/interview_tasks

按面试官查面试任务（**催面评权威数据源**，2026-08-18 实测可用）。scope=`hire:interview:readonly`，`page_size≤20`。

**封装**：`_lark_shared.hire_list_interview_tasks(user_id, activity_status=None) -> list[dict]`

**参数**：`user_id`（面试官 open_id，必填）、`activity_status`（1未开始/2未评价/3已评价/5已终止，催面评传 2）。

**返回 items**：`{id(面试ID), job_id, talent_id, application_id, activity_status}`。⚠️ 无时间字段——"拖了几天"要用 `application_id` 查 interviews 或 `interview_id` 走 `hire_get_interview_by_id`。

**消费方**：`_daily_review.py::fetch_interviewer_debt` 聚合成 `interviewer_feedback_debt`（对账契约）。

> 面试官枚举：官方 `GET /interviewers` 列表没用（只返回执行过"更新面试官信息"操作的用户，实测 0 条）。用 `notes/interviewers.json` 缓存 ∪ 面试记录 `interviewer.id` 反查。

### GET /open-apis/hire/v2/interview_records/{record_id}

查面评全文记录（v2，注意路径是 v2 不是 v1）。

**封装**：`_lark_shared.hire_get_interview_record(record_id) -> dict`

### GET /open-apis/hire/v1/interview_records/attachments

查面评 PDF 附件。

**封装**：`_lark_shared.hire_get_feedback_pdf(app_id, record_id) -> dict`

**返回**：PDF url（30 分钟有效）。下载后用 PyMuPDF/PyPDF2 提取文本。

```bash
MSYS_NO_PATHCONV=1 lark-cli api GET /open-apis/hire/v1/interview_records/attachments \
  --as bot --params '{"application_id":"APP_ID","interview_record_id":"REC_ID"}'
```

## 招聘任务/评估（2026-08-18 hire:evaluation:readonly 开通后可用）

### GET /open-apis/hire/v1/evaluation_tasks

按评估人查简历评估任务（谁欠评简历）。`page_size≤20`，`user_id` 必填。

**封装**：`_lark_shared.hire_list_evaluation_tasks(user_id, activity_status=None) -> list[dict]`

**activity_status**：1待评估 / 2已评估 / 3无需评估（催评估传 1）。

**返回 items**：`{id, job_id, talent_id, application_id, activity_status}`。

### GET /open-apis/hire/v1/evaluations

查简历评估列表（业务评估结论 API 直读，不再从阶段流转反推）。`page_size≤100`。

**封装**：`_lark_shared.hire_list_evaluations(application_id=None, update_start_time=None) -> list[dict]`

**参数**：`application_id`（投递过滤）、`update_start_time`（毫秒时间戳，最早更新时间）。

**返回 items**：`{id, application_id, evaluator_id, commit_status(1已提交/2未提交), conclusion(1通过/2未通过，未提交时null), content(评语), create_time, update_time}`。枚举映射在 `_lark_shared.EVALUATION_CONCLUSION_ZH / EVALUATION_TASK_STATUS_ZH`。

**消费方**：`_daily_review.py::fetch_evaluations_data` → `pending_evaluations`（未提交预警）+ ats 的 `evaluation_conclusion` join。

> `GET /todos` 待办接口不封装：只认 user_access_token（tenant 恒 99991663），`user_id` 参数无效（只能查登录用户自己的待办）。评估/面评/offer 三维已被本节 + interview_tasks + offers 覆盖。

## Attachment

### POST /open-apis/hire/v1/attachments

上传附件（带文件名）。

**封装**：`_lark_shared.upload_attachment_with_name(file_path, file_name) -> att_id`

**唯一保留 Python requests 直连的接口**：lark-cli `--file content=<path>` 只能传文件二进制，传不了 `file_name` 表单字段 → 文件名变成 `unknown-file` 且删不掉。必须用 multipart：

```python
# 封装内部实现（业务脚本不直接写这段，调封装函数）
import requests
H = {"Authorization": f"Bearer {token}"}  # token 来自 _lark_shared.get_hire_token()
with open(file_path, "rb") as f:
    files = {"content": (file_name, f, "application/pdf")}
    data = {"file_name": file_name, "file_type": "pdf"}
    r = requests.post("https://open.feishu.cn/open-apis/hire/v1/attachments",
                      headers=H, files=files, data=data)
att_id = r.json()["data"]["id"]
```

**⚠️ 删除附件 API 不存在**：unknown-file 附件一旦传上去删不掉。第一次就要传对文件名。

## Document AI（简历解析）

### POST /open-apis/document_ai/v1/resume/parse

解析简历 PDF/图片为结构化数据。

**封装**：`_lark_shared.parse_resume(pdf_path) -> dict`（返回 name/mobile/email/gender/date_of_birth/careers/educations/self_evaluation）

```bash
MSYS_NO_PATHCONV=1 lark-cli api POST /open-apis/document_ai/v1/resume/parse \
  --as bot --file "file=notes/_test.pdf"
```

## job_processes（流程节点）

### GET /open-apis/hire/v1/job_processes

查岗位的招聘流程节点（取 stage_id）。

**无封装**（低频）— 裸调：

```bash
MSYS_NO_PATHCONV=1 lark-cli api GET /open-apis/hire/v1/job_processes \
  --as bot --params '{"job_id":"7646359900507687195"}'
```

返回每个流程节点的 `id`(stage_id) 和 `type`(见 stage 枚举)。transfer_stage 用这里的 `id` 不是 `type`。

## 错误处理

| code | 含义 | 封装行为 |
|---|---|---|
| 0 | 成功 | 返回 data |
| 99991668 | 身份错 | 封装已默认 `--as bot`，不会触发 |
| 1002206 | 投递已存在 | `hire_create_application` 返回 `status="exists"`，不当错 |
| 其他非 0 | 失败 | 封装抛 Exception 含 code+msg |
