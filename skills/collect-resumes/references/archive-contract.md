# 归档契约（单一真相源）

> 本文件是【简历归档结构】的唯一权威定义。`collect-resumes` 和 `analyze-resumes` 都**只引用、不重述**此处内容。改动归档结构只改本文件。

## 目录结构

```
$ARCHIVE_ROOT/                              ← <PROJECT_ROOT>/data/在招岗位候选人管理
├── {项目团队}/                             ← 如"山海弹珠项目"
│   ├── {岗位名}/已收集简历/{M.DD}_{N}份/   ← 无中间层
│   ├── {分组}/{岗位名}/已收集简历/{M.DD}_{N}份/  ← 有分组层（如长青工作室/美术端/3D场景设计师）
│   └── {团队名}/{岗位名}/已收集简历/{M.DD}_{N}份/  ← 有中间团队层（如全球发行业务/技术支持团队）
```

> 分组层（技术端/美术端/策划端）是长青工作室和山海弹珠项目采用的组织维度。

### `_暂定` 中转目录

`resolve_records` 在单条记录 resolve 时不知道整批人头数 N，统一落 **`{M.DD}_暂定/`** 中转目录。`verify_archive` 闸门运行时自动数人头 rename 为 `{M.DD}_{N}份`（在数量校验前完成，N 永远自洽）。空 `_暂定` 目录（无人头）会被删除。

- 下载阶段：文件落在 `已收集简历/{M.DD}_暂定/`
- 闸门阶段：`_暂定` → `_{N}份`（自动）
- 正常使用无需手动 rename 或手填 N

## 四项约定

| 项 | 约定 | 例 |
|---|---|---|
| 日期目录 | `{M.DD}_{N}份`。**M.DD = 邮件收到日**（manifest `received_at` 的月.日，resolve 阶段自动派生；`--date` 仅人工显式覆盖）。N=本目录人头数（按姓名去重）；resolve 阶段先落 `{M.DD}_暂定/`，闸门自动校正 | `7.01_7份` |
| 已收集简历夹 | 统一名 `已收集简历`（历史遗留的"收集到简历"不改） | — |
| 档位子目录 | 评估后按档位 `mv` 进这三个标准名之一：`强推` / `可推` / `待定·不推`（待定和不推共置） | `强推/` |
| 简历文件名 | 正职 `{姓名}_{岗位简称}_{经验}年.pdf`；实习 `{姓名}_{岗位简称}_{届}.pdf`。**不含薪资**。支持 `.pdf`/`.docx`/`.doc`（`.doc` 在脱敏阶段自动转 PDF 后脱敏，转换成功删原文件） | `林艺_游戏发行运营实习生_27届.pdf` |

## 计数规则（数量闸门依据）

- `_N份` 的 N = **人头数**，口径与 `_count_heads` 完全一致：递归（含档位子目录及任意子目录）、按 `parse_name` 解析出的姓名去重、只数简历文件（pdf/docx/doc + 关键词图片）和压缩包（zip/rar/7z）
- 非简历文件（备注.txt、评估表格等）不计数；`temp/临时/tmp` 命名文件排除
- 不在任何 `_N份` 目录内的简历/压缩包 = 散落文件 → 阻断
- 单文件模式（target 是文件）跳过数量闸门，只做姓名/薪酬/格式校验

## 闸门脚本

`verify_archive.py`（在 collect-resumes/scripts/）fail-closed。**阻断维度**（任何一项不符 → STOP）：

1. **数量**：`_N份` 标注 == 实际人头数（口径见上）。`_暂定` 目录在校验前自动收敛为 `_N份`
2. **薪酬**：所有可承载内容的格式（PDF/DOCX/.doc/ZIP 包内）都扫描，不得残留薪酬段；`.doc` 无法涂白——正常流程在脱敏阶段已自动转 PDF，仍以 `.doc` 形态命中薪酬 → STOP；`.doc` 文本提取工具缺失时为 warning（需人工看原件）
3. **manifest 闭环**（`--manifest`）：**绑定到本批次目录**（同父目录+同日期段）的 `blocked` 记录 → STOP；全库其他记录不拦（防止历史 blocked 劫持任意目录的闸门）

**warning 级**（列出供人工复核，不阻断）：姓名 miss/manual（BOSS 加密 PDF/先生文件名/英文名常态；下载一致性已由 SHA-256 绑定兜底）、提取阻断文件（加密 zip/图片型 PDF——图片型用 `enhance_ocr_pdf.py` 增强后复验）、嵌套归档警告（包内 zip/rar 未扫描，藏薪酬需人工拆包确认）。

**特殊文件形态的验证口径**：
- 美术岗作品 ZIP（含视频/图片但无简历成员）允许归档；包内无简历 → 跳过姓名/薪酬检查，简历 PDF 在 zip 外单独归档验证
- 文件名含「简历」的合体文件（如 `XX_简历和作品集.pdf`）按简历本体验证；纯「作品集」命名（无「简历」字样）跳过简历验证

`_暂定` 自动收敛的数据安全规则：同名冲突文件加 `_重投N` 后缀保留（不静默丢）；源目录只在确已清空后删除；目录 rename 在 Windows 句柄锁时 fallback 到 copytree+rmtree。

> **fail-closed 原则**：任何无法完整验证的情况都 STOP。只有 `🟢 全过` 才可进评估。
> SHA-256 缓存只做性能加速（内容哈希确认），不参与安全判定。

---

## collection_manifest.json 字段契约

> 产物：`<PROJECT_ROOT>/notes/collection_manifest.json`
> 生产者：`verify_mails.mjs`（生成 record）、各阶段脚本推进状态
> 消费者：`resolve_records`/`download_attachment`/`batch_download_links`/`merge_results`/`verify_archive`（按 record_id 驱动）；`analyze-resumes`（评估报告的"当日名单"口径按 `received_at`，见其 SKILL.md 阶段7）

**顶层结构**：
```
{
  schema_version: 1,
  batches: {},
  records: { "<record_id>": { ...record } },
  processed: { "<message_id>": "<ISO时间>" }   // verify_mails 增量标记：已核查过（含"确认零附件"结论）的邮件
}
```

**record_id 生成规则**（稳定，不受文件名影响）：
- 附件类：`sha256(message_id + "\0" + attachment_id)`，前缀 `sha256:`
- 链接类：`sha256(message_id + "\0" + normalizeUrl(url))`，前缀 `sha256:`

**record 字段**（按 source_type 部分不同）：

| 字段 | source_type | 含义 |
|---|---|---|
| `record_id` | 全部 | 稳定 ID（见上） |
| `message_id` | 全部 | 来源邮件 ID |
| `status` | 全部 | 状态机当前状态（见下） |
| `source_type` | 全部 | `mail_attachment` / `link` / `mail_detail` / `body_hint` |
| `received_at` | 全部 | **邮件真实收到时间**（verify 建记录时从快照写入；存量记录由 verify 每次运行按快照自动回填）。**"何时收到"的唯一判定依据**——collect 的下载 scope、resolve 的目录日期段、评估报告的名单口径都从这里派生。fallback 链（received_at → created_at → 今天）统一在 `scripts/lib/dates.mjs` 的 `recordDate`，不各自读时间戳 |
| `created_at` | 全部 | verify 首次入库时间（≈扫描时间）。**不是邮件时间**——历史积压邮件晚入库时 created_at 落在入库当天，不能判"何时收到" |
| `updated_at` | 全部 | 最近一次状态推进时间。核查/解析/下载都会刷新，**不能判"何时收到"** |
| `original_filename` | 附件 | 邮件里的原始文件名 |
| `target_dir` | 附件/链接 | 归档目标目录（resolve_records 填，`{M.DD}_暂定` 中转目录） |
| `target_filename` | 附件/链接 | 归档目标文件名（resolve_records 填，`target_dir`+`target_filename` 拼成完整路径） |
| `source_url` | 链接 | 提取到的下载链接 |
| `candidate_name` | 全部 | resolve_records 绑定的候选人名 |
| `job_name` | 全部 | resolve_records 绑定的岗位 |
| `target_path` | 全部 | merge 推进后写入的归档完整路径 |
| `sha256` | 全部 | 下载后校验的内容哈希 |
| `errors` | 全部 | `[{code, message, at}]` 错误历史（blocked 时追加） |
| `exclude_reason` | 全部 | `{code, message}` 排除原因（excluded 时必填） |

**状态机**（`lib/manifest.mjs` 的 `TRANSITIONS`，禁止跳过）与各状态的写入者：

| 状态 | 谁写入 | 说明 |
|---|---|---|
| `needs_resolution` | verify_mails（新建 record） | 待绑定候选人/岗位 |
| `verified` | resolve_records（唯一匹配 resolve） | 待下载 |
| `archived` | download_attachment / batch_download_links 批量尾部自动合并；merge_results 手动收尾 | 文件已落归档目录 |
| `duplicate` | resolve_records（同人同岗已归档） | 终态，重复投递跳过 |
| `excluded` | resolve_records `--exclude <id> --code <原因码>`；verify_mails 自愈历史详情 blocked（DETAIL_FETCH_RECOVERED）；readManifest 迁移历史非法状态 | 终态，必须有结构化 code |
| `blocked` | verify_mails（详情拉取失败/正文提示无来源）；resolve（路径逃逸等）；batch_download_links（LINK_EXPIRED） | 可修复后回到安全状态 |
| `validated` | 当前无脚本写入者（预留给后续人工/对账环节） | 终态 |

**消费约定**：所有阶段脚本通过 `record_id` 定位记录，状态只能按状态机推进（`transitionRecord` 校验）。`verify_archive` 的 manifest 闭环校验只拦**绑定本批次目录**的 `blocked` 记录；`archived`/`duplicate`/`validated`/`excluded` 视为已处理；`needs_resolution`/`verified` 为中间态仅提示。
