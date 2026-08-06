# hire 字段字典与枚举

> talent / application / interview 接口的字段定义和枚举值。所有字段的权威源——业务脚本只认这里。

## talent basic_info（人才基本信息）

`POST /hire/v1/talents/combined_create` 和 `combined_update` 的 `basic_info` 对象。

| 字段 | 类型 | 必填 | 说明 / 枚举 |
|---|---|---|---|
| `name` | string | ✅ | 姓名 |
| `mobile` | string | ❌ | 手机号（纯数字，去掉+86/-） |
| `mobile_code` | string | ❌ | 国家码，配合 mobile。中国大陆 = `"86"` |
| `mobile_country_code` | string | ❌ | 国家码枚举。中国大陆 = `"CN_1"`（实测唯一确认值）。海外号不传（枚举未知，传了必失败，靠邮箱去重） |
| `email` | string | ❌ | 邮箱 |
| `gender` | int | ❌ | `1`男 / `2`女。未知/其他不传（传 0 会报错） |
| `birthday` | string | ❌ | 毫秒时间戳字符串 `"783705600000"` |

**手机三件套**：传 `mobile` 必须同时带 `mobile_code:"86"` + `mobile_country_code:"CN_1"`，否则校验失败。封装函数 `_lark_shared._build_basic_info()` 已处理。

## career_list（工作经历，数组）

| 字段 | 类型 | 说明 |
|---|---|---|
| `company` | string | 公司名 |
| `title` | string | 职位 |
| `start_time` | string | 毫秒时间戳 |
| `end_time` | string | 毫秒时间戳（至今/现在/目前 → 不传） |
| `career_type` | int | 经历类型：`1`实习经历 / `2`工作经历（默认 2）。⚠️ 与 Document AI `resume_career.type` 枚举方向一致（也是 1实习/2工作），可直接透传。 |
| `desc` | string | 工作描述 |

## education_list（教育经历，数组）

| 字段 | 类型 | 说明 |
|---|---|---|
| `school` | string | 学校名 |
| `degree` | int | 见下方 degree 枚举 |
| `field_of_study` | string | 专业（注意字段名不是 `major`） |
| `start_time` | string | 毫秒时间戳 |
| `end_time` | string | 毫秒时间戳 |

## talent 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `basic_info` | object | 见上 |
| `resume_attachment_id` | string | 简历附件 ID（`upload_attachment_with_name` 的返回值） |
| `career_list` | array | 工作经历 |
| `education_list` | array | 教育经历 |
| `self_evaluation` | object | `{"contents": [{"text": "..."}]}` 自我评价 |
| `talent_id` | string | combined_update 必填（标识改哪个 talent） |

## application（投递）

### POST body（建投递）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `talent_id` | string | ✅ | 人才 ID |
| `job_id` | string | ✅ | 岗位 ID |

### GET 返回字段（投递详情）

| 字段 | 说明 |
|---|---|
| `id` / `application_id` | 投递 ID |
| `talent_id` | 人才 ID |
| `job_id` | 岗位 ID |
| `stage` | 当前阶段对象，含 `id`(stage_id) 和 `type`(见 stage 枚举) |
| `active_status` | `1`招聘中 / `2`暂停/已淘汰 / `3`已关闭 |
| `talent_attachment_resume_id` | 投递挂的简历附件 ID（检测旧附件用） |

## 枚举值总表

### gender

| 值 | 含义 |
|---|---|
| 1 | 男 |
| 2 | 女 |
| — | 未知/其他（不传） |

### degree（学历）

| 值 | 含义 | 简历常见词 |
|---|---|---|
| 3 | 高中 | 高中 |
| 4 | 中专 | 中专 |
| 5 | 大专 | 大专、专科 |
| 6 | 本科 | 本科、学士 |
| 7 | 硕士 | 硕士、研究生 |
| 8 | 博士 | 博士 |

映射函数：`_lark_shared.map_degree("本科")` → `6`。

### stage type（招聘流程阶段）

见 [`hire-stages.md`](hire-stages.md)。

### active_status（投递活跃状态）

| 值 | 含义 |
|---|---|
| 1 | 招聘中（活跃） |
| 2 | 暂停（含已淘汰，terminate 后变 2） |
| 3 | 已关闭 |

对账脚本只关注 `active_status==1`。

### termination_type（淘汰原因，terminate 接口用）

`POST /applications/{id}/terminate` 的 body 字段。具体枚举值查[官方文档](https://open.feishu.cn/document/server-docs/hire-v1/candidate-management/delivery-process-management/application/terminate)（值会随后台配置变化，不固化）。

## mobile_country_code 已知值

| 值 | 国家/地区 |
|---|---|
| `CN_1` | 中国大陆（唯一实测确认） |

海外号枚举未知——遇到海外号不传手机，靠邮箱去重。
