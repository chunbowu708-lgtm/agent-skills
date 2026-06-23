---
name: recruit-followup
description: >
  候选人跟进：从业务邀约信号到飞书招聘录入、面试流转、面评同步、跟踪表更新。
  触发词：跟进候选人、录入候选人、录到岗位、安排面试、扫群、招聘日报、今日待办、面评、催面评、推进流程。
  只要用户提到候选人面试、邀约、飞书招聘录入、招聘跟踪表、招聘群扫描，就使用这个skill。
  覆盖：飞书招聘候选人录入（Document AI 解析+附件+建talent+建投递）、招聘群邀约信号扫描、面评卡片读取、跟踪表自动建行与状态更新、今日待办文档产出。
  不覆盖：简历收集归档（见 collect-resumes skill）、招聘平台打招呼、发面试链接（人工关键操作，不自动化）。
  依赖：lark-cli（hire/document_ai/im/base/docs 域，bot 身份已授权，权限已全，不查 auth）、岗位缓存 JSON（{job_id:{code,title,dept}}）。
---

# 候选人跟进

## 配置

所有凭证/路径走环境变量，见 [`.env.example`](.env.example)。下文用 `$LARK_APP_ID` / `$LARK_APP_SECRET` / `$TRACKING_BASE_TOKEN` 等指代。

| 项 | 来源 |
|---|---|
| 项目根 | `$PROJECT_ROOT` |
| 飞书招聘应用 | `$LARK_APP_ID` / `$LARK_APP_SECRET`（开放平台获取） |
| 岗位缓存 | `$PROJECT_ROOT/notes/jobs_map.json`（{job_id:{code,title,dept}}） |
| 跟踪表 | Bitable base `$TRACKING_BASE_TOKEN` / 表 `$TRACKING_TABLE_ID` |
| 候选人主库 | Bitable 同 base / 表 `$CANDIDATE_TABLE_ID` |
| 简历下载默认落点 | `$DOWNLOAD_DIR`（留空则 `~/Downloads`） |
| lark-cli | `$LARK_CLI_PATH`（留空则假定在 PATH），hire/document_ai 域用 bot 身份 |
| 录入速查 | `$PROJECT_ROOT/notes/hire_record.md`（命令模板，完整版） |

---

## 任务1：录入候选人到飞书招聘（核心，已全自动）

**输入**：候选人简历 PDF + 岗位编号（如 A105045）
**目标**：API 录入 = HR 后台手动上传简历的完全等价效果（PDF 进去，解析内容 + 可下载简历都在）

### 核心认知（最重要）

**两种录入路径，优先 B（更稳更省），A 是兜底：**

**路径 B（推荐，待验证）**：传附件 → combined_create 只填 basic_info（name + mobile + email，用于去重和联系）→ 让飞书招聘**自己解析附件**填 career/education。
- **假设**：combined_create 带 attachment 会触发飞书招聘原生简历解析（和 HR 后台手动上传同源）
- **验证方法**：挑一个新候选人，只传附件 + minimal basic_info，**不传 career_list/education_list**。建完立即查 talent.career_list，再过 5 分钟、30 分钟各查一次，确认是否自动补全 + 时间差
- 验证通过 → 彻底改用 B，删除路径 A 的手动映射；验证失败 → 保留 A 为标准

**路径 A（当前默认，兜底）**：Document AI 解析 PDF → 手动映射字段 → combined_create 全量写入。
- Document AI 和飞书招聘原生解析同源（都飞书出品），但手动映射有损耗且坑多（gender/degree 枚举、时间戳、字段名 field_of_study）

### 5 步流程（路径 A：手动映射；验证 B 成功后②④可大幅简化）

> **路径 B（验证后）只需：③传附件 → ④combined_create（只 basic_info + attachment_id）→ ⑤建投递**。Document AI（②）和手动映射（④的大部分）可跳过。

#### ① 查 job_id（先查本地缓存，O(1)）
```bash
# 本地缓存查
python -c "import json;d=json.load(open('notes/jobs_map.json',encoding='utf-8'));[print(k,v['title'],v['dept']) for k,v in d.items() if v['code']=='A105045' or '关键字' in v['title']]"
# 缓存过期（岗位增删）才刷新：
MSYS_NO_PATHCONV=1 lark-cli api GET /open-apis/hire/v1/jobs --as bot --params "{\"page_size\":20}" --page-all
```

#### ② Document AI 解析 PDF（路径 A 必需；路径 B 可跳过，让飞书招聘原生解析）
```bash
MSYS_NO_PATHCONV=1 lark-cli api POST /open-apis/document_ai/v1/resume/parse --as bot --file "file=<简历相对路径>" > notes/_parse.json
```
返回：name/mobile/email/birthday/gender/careers/educations/projects/self_evaluation

#### ③ 上传简历附件（挂简历，可下载）
```bash
MSYS_NO_PATHCONV=1 lark-cli api POST /open-apis/hire/v1/attachments --as bot --file "content=<简历相对路径>"
```
→ attachment_id。⚠️ 字段名是 **content**。

**lark-cli 局限（通用认知）**：`--file "k=v"` 只认文件路径，**传不了纯字符串字段值**（v 不是文件就报 cannot open file）。所以 multipart 表单只要含字符串字段（如 file_name），lark-cli 就做不到，要用 Python requests。

**带文件名上传（Python requests，每次都用这个）**——飞书 `/attachments` 支持 `file_name` + `file_type`，带上才不会变 `unknown-file`。**凭证从环境变量读，不要硬编码**：
```python
import os, requests
APP_ID     = os.environ['LARK_APP_ID']      # 从 .env / 环境变量，绝不硬编码
APP_SECRET = os.environ['LARK_APP_SECRET']
t = requests.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                  json={'app_id': APP_ID, 'app_secret': APP_SECRET}).json()['tenant_access_token']
H = {'Authorization': f'Bearer {t}'}
with open('<简历绝对路径>', 'rb') as f:
    files = {'content': ('<文件名.pdf>', f, 'application/pdf')}
    data  = {'file_name': '<文件名.pdf>', 'file_type': 'pdf'}
    att_id = requests.post('https://open.feishu.cn/open-apis/hire/v1/attachments',
                           headers=H, files=files, data=data).json()['data']['id']
```
⚠️ **第一次就要传对文件名**——飞书招聘**没有删除 talent 附件的 API**，传错的 unknown-file 删不掉。

#### ④ 建/更新 talent + 写回解析结果
**先解析确认给用户看，再写**（用户已确认此行为）：
- 把②解析出的姓名/手机/工作经历/要投岗位列出来给用户
- 用户说"录入"才执行
- talent 不存在 → combined_create
- talent 已存在（去重命中旧档）→ combined_update 补字段

**路径 B（验证后用这个，最简）**：combined_create 只填 basic_info（name + mobile + email）+ resume_attachment_id，**不传 career_list/education_list**，让飞书招聘自己解析附件补全。建完 5 分钟后查 talent.career_list 确认补全。

**路径 A（当前默认，手动全量映射）**：请求体用 Python 脚本生成（写文件避免 cmd 转义中文；脚本顺便做枚举映射/时间戳转换）：
```bash
MSYS_NO_PATHCONV=1 lark-cli api POST /open-apis/hire/v1/talents/combined_create --as bot --data "@notes/_talent.json"
# combined_update 路径：POST /open-apis/hire/v1/talents/combined_update，body 带 talent_id + basic_info
```
⚠️ **combined_update 即使只改一个字段（如换附件），也必须带 `basic_info`**（报 `basic_info is required`，沿用原值即可）。

枚举/时间戳转换（脚本里做）：
- `birthday`/`start_time`/`end_time`："2022-11-01" → 毫秒时间戳字符串 `"1667260800000"`；空或"至今" → 不传该字段
- `gender`：AI 返回 0 → 不传（只认 1男/2女/3其他）
- `degree`："本科" → int 6（5大专/6本科/7硕士/8博士）
- `field_of_study`：专业字段名是这个，不是 major
- `mobile`：要同时带 `mobile_code:"86"` + `mobile_country_code:"CN_1"`；空（如隐私保护）→ 不传，单留 email

#### ⑤ 建投递
```bash
MSYS_NO_PATHCONV=1 lark-cli api POST /open-apis/hire/v1/applications --as bot --data "@notes/_app.json"
```

### Document AI → Hire API 字段映射（关键易错点）

| Document AI 返回 | Hire API 写入 | 注意 |
|---|---|---|
| mobile | basic_info.mobile | 配 mobile_code:"86" + mobile_country_code:"CN_1" |
| email | basic_info.email | |
| date_of_birth "1994-11-02" | basic_info.birthday | **转毫秒时间戳字符串** |
| gender | basic_info.gender | ⚠️ **AI 返回 0 不能直接塞**，hire 只认 1男/2女/3其他，未知就不传 |
| careers[].company/title/job_description | career_list[].company/title/desc | |
| careers[].start_date "2022-11-01" | career_list[].start_time | **转毫秒时间戳字符串** |
| careers[].type | career_list[].career_type | 1实习/2全职 |
| educations[].school | education_list[].school | |
| educations[].degree "本科" | education_list[].degree | ⚠️ **转 int**：5大专/6本科/7硕士/8博士 |
| educations[].major | education_list[].field_of_study | ⚠️ 字段名是 field_of_study，不是 major |

---

## 任务2：早晚扫描（识别邀约信号 + 状态校准）

### 信号边界（铁律）

只跟进两类信号：
- **A 类**：业务群里我发的简历，业务明确表态"约下/聊聊/安排面试"
- **B 类**：招聘负责人 @我 + 明确邀约指令
- ❌ **负责人只发简历没 @我 → 不跟进**

### 早上 9:20 审查
```
1. 扫招聘群近 24h 消息（chat-messages-list --order desc）
   → 抓"@我 + 动词(约/安排/聊聊/面试) + 候选人"
   → 排除负责人未 @我 的纯发简历
2. 扫飞书招聘 applications（bot）→ 对比跟踪表，找阶段变化、新面评
3. 飞书招聘面评卡片（user 身份 messages-search "面试有新反馈"）
4. 三向对账：群信号 × 跟踪表 × 飞书招聘投递 → 找差异
5. 输出「今日待办」文档（红黄绿分级）
```

### 招聘沟通群

群 chat_id 从环境变量 `$RECRUIT_CHAT_IDS`（逗号分隔）读取。在飞书群设置里拿到 chat_id 后填入 `.env`。

### 自动建跟踪表行（用户已确认此行为）

扫到 A/B 类信号就**自动建行**，状态"待约面"，**不再每次问用户**。用户只否决异常的。
```bash
chcp 65001 >nul && lark-cli base +record-upsert \
  --base-token "$TRACKING_BASE_TOKEN" \
  --table-id "$TRACKING_TABLE_ID" \
  --json "{\"字段\":\"值\"}"
```

### 状态更新规则
- **通过/不通过**：一律以**飞书招聘面试官面评**为准，不看口头/群里转述
- **催面评**：当天面试当天催，不隔夜
- **不通过**：面评一出，跟踪表状态→终止，不拖延
- **通过**：推进下一轮，跟踪表轮次+1

---

## 任务3：邀约的 4 步拆解（AI 只做①）

业务说"约下"后，不是一步，是 4 步：
```
① 飞书招聘录入候选人    ← ★本 skill 全自动（任务1）
② 跟候选人敲时间        ← 人工
③ 跟面试官确认          ← 人工
④ 发面试链接            ← 人工（关键操作，不外包）
```

---

## 反模式（不要做）

- **不要硬编码 App Secret** — 用 `os.environ['LARK_APP_SECRET']`，凭证进 `.env`（.gitignore 已忽略）
- **不要凭经验猜 API 字段名/类型/可选值** — 调飞书 API 前先查官方文档。文档 markdown 路径模板 `https://open.feishu.cn/document/{slug}.md`
- **不要自己抠 PDF 填字段** — 让 Document AI 解析（同源同质，可靠）。自己抠不一致
- **不要手动映射 career/education（路径 B 验证后）** — 若 combined_create 带 attachment 能触发原生解析（待验证），让飞书招聘自己填更全更省。手动映射只用于"录入前要先给用户预览"的场景
- **不要传附件就以为有简历信息** — combined_update 不解析，必须配合 Document AI
- **不要把 gender 传 0** — hire 只认 1/2/3，AI 返回 0（未知）就不传
- **不要只传 career_list 不带 basic_info** — basic_info 必填（combined_create 和 combined_update 都要）
- **不要把 degree 传字符串** — 是 int（6=本科），不是 "本科"
- **不要用 --params 传 POST body** — 用 --data，--params 是 query
- **不要用 user token 调 hire 写接口** — 报 99991668，必须 --as bot
- **不要拿历史候选人数据验证当前流程** — 历史完整数据是历史投递遗产，不代表新录流程
- **不要在 stdout 输出中文** — Windows GBK 乱码，写文件再 Read
- **不要给 lark-cli --file 传绝对/中文路径** — 报 cannot open file。**一律用 cwd 相对路径**（中文路径也走得通，前提是相对）
- **不要给 lark-cli --file 传字符串字段值** — `--file "file_type=pdf"` 会被当文件路径。multipart 的字符串字段用 Python requests
- **不要 jobs 列表传 page_size>20** — hire API 上限 20，超了报 field validation failed
- **不要试图删除 talent 附件** — 飞书招聘没有这个 API（HR 后台也删不了），传错只能留着

## 调试方法论

**field validation failed 不知道哪个字段错**：lark-cli 的报错不带 field_violations，**改用 Python requests 同样的请求**，返回里会列出具体哪个字段错（`field_violations[].field`）。

**最小化二分法**：
1. 先发最小请求（只 basic_info），确认基础格式对
2. 逐个加字段（career_list → education_list → ...），每加一个测一次
3. 一加就报错的那个就是元凶

**响应字段不一致**：同一接口不同情境返回结构可能不同（如 combined_create 返回 `data.talent.id`，有的返回 `data.talent_id`）。解析时两个位置都取：`data.get('talent_id') or data.get('talent',{}).get('id')`。

## 已验证可用的 API（权限全通 ✅）

| 能力 | 接口 |
|---|---|
| 读 talents/jobs/applications/阶段/面评 | GET /hire/v1/{resource} |
| 读面试 | GET /hire/v1/interviews?application_id= |
| 读 Offer | GET /hire/v1/offers/:id |
| 建/更新人才 | POST /hire/v1/talents/combined_create\|update |
| 建投递 | POST /hire/v1/applications |
| 推进阶段（约面/淘汰） | POST /hire/v1/applications/:id/transfer_stage |
| 上传简历附件 | POST /hire/v1/attachments （字段 content + file_name + file_type；lark-cli 只能传 content 要用 Python；删除附件无 API） |
| **Document AI 解析简历** | POST /document_ai/v1/resume/parse （字段 file） |
| 建岗位 | POST /hire/v1/jobs/combined_create（建议手动建更省事） |

## 参考文档

按需加载：
- `docs/prd/候选人跟进流程.md` — 完整流程说明（本文的操作版，需自行整理）
- `notes/hire_record.md` — 录入命令速查 + 字段映射 + 踩坑清单（需自行整理）
- `notes/jobs_map.json` — 岗位 job_id 缓存（需自行生成）
- 官方文档索引：[招聘开发指南](https://open.feishu.cn/document/server-docs/hire-v1/recruitment-development-guide?lang=zh-CN)
