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

### `_暂定` 中转目录（2026-07-29）

`resolve_records` 在单条记录 resolve 时不知道整批人头数 N，统一落 **`{M.DD}_暂定/`** 中转目录。`verify_archive` 闸门运行时自动数人头 rename 为 `{M.DD}_{N}份`（在数量校验前完成，N 永远自洽）。空 `_暂定` 目录（无人头）会被删除。

- 下载阶段：文件落在 `已收集简历/{M.DD}_暂定/`
- 闸门阶段：`_暂定` → `_{N}份`（自动）
- 正常使用无需手动 rename 或手填 N

## 四项约定

| 项 | 约定 | 例 |
|---|---|---|
| 日期目录 | `{M.DD}_{N}份`，N=本目录人头数（按姓名去重）；resolve 阶段先落 `{M.DD}_暂定/`，闸门自动校正 | `7.01_7份` |
| 已收集简历夹 | 统一名 `已收集简历`（历史遗留的"收集到简历"不改） | — |
| 档位子目录 | 评估后按档位 `mv` 进这三个标准名之一：`强推` / `可推` / `待定·不推`（待定和不推共置） | `强推/` |
| 简历文件名 | 正职 `{姓名}_{岗位简称}_{经验}年.pdf`；实习 `{姓名}_{岗位简称}_{届}.pdf`。**不含薪资**。支持 `.pdf`/`.docx`/`.doc`（.doc 经 antiword 校验） | `林艺_游戏发行运营实习生_27届.pdf` |

## 计数规则（数量闸门依据）

- `_N份` 的 N = **人头数**（按姓名去重），不是文件数
- 档位子目录整体参与计数：`强推/`+`可推/`+`待定·不推/` 内的 PDF 递归计入人头，子目录名本身不当条目
- 临时暂存目录（`temp`、`_tmp` 等）排除

## 闸门脚本

`verify_archive.py`（在 collect-resumes/scripts/）五重阻断校验，read-only（fail-closed）。阻断维度与代码 `stop` 判定一致：

1. **数量**：`_N份` 标注 == 实际人头数（档位目录+同级文件同时统计；无标注 → STOP）。`_暂定` 目录在闸门 collect 前自动 rename 为 `_{N}份`（2026-07-29）
2. **姓名**：文件名姓名 ∈ 简历正文署名（挡 MID 填串）
3. **薪酬**：所有可承载内容的格式（PDF/DOCX/.doc/ZIP 包内）都扫描，不得残留薪酬段
4. **格式**：PDF/DOCX/.doc/ZIP 均通过统一提取器（.doc 经 antiword）；图片型 PDF OCR 不可用 → STOP；不支持/损坏/加密 → STOP（统称"阻断文件"）。**.doc 命中薪酬 → STOP**（antiword 只读不可涂白，需转 PDF 后脱敏）
5. **manifest 闭环**（`--manifest`）：来源→归档→哈希→状态逐项核销，未达 validated 的记录阻断

> **fail-closed 原则**：任何无法完整验证的情况都 STOP，不提供"人工确认后自动放行"。只有 `🟢 全过` 才可进评估。
> SHA-256 缓存只做性能加速（mtime/size 快筛 + 内容哈希确认），不参与安全判定。

档位子目录名（`强推/可推/待定·不推`）已硬编码进脚本的 `TIER_DIR_NAMES`，改名需同步。

---

## collection_manifest.json 字段契约

> 产物：`<PROJECT_ROOT>/notes/collection_manifest.json`
> 生产者：`verify_mails.mjs`（生成 record）、各阶段脚本推进状态
> 消费者：`resolve_records`/`download_attachment`/`merge_results`/`verify_archive`（按 record_id 驱动）

**顶层结构**（`lib/manifest.mjs:128`）：
```
{ schema_version: 1, batches: {}, records: { "<record_id>": { ...record } } }
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
| `original_filename` | 附件 | 邮件里的原始文件名 |
| `target_dir` | 附件/链接 | 归档目标目录（resolve_records 填，`{M.DD}_暂定` 中转目录） |
| `target_filename` | 附件/链接 | 归档目标文件名（resolve_records 填，`target_dir`+`target_filename` 拼成完整路径） |
| `source_url` | 链接 | 提取到的下载链接 |
| `candidate_name` | 全部 | resolve_records 绑定的候选人名 |
| `job_name` | 全部 | resolve_records 绑定的岗位 |
| `target_path` | 全部 | merge_results 推进后写入的归档完整路径（= `target_dir`/`target_filename`） |
| `sha256` | 全部 | 下载后校验的内容哈希（download/merge 阶段填） |
| `errors` | 全部 | `[{code, message, at}]` 错误历史（blocked 时追加） |
| `exclude_reason` | 全部 | `{code, message}` 排除原因（excluded 时必填） |

**状态机**（`lib/manifest.mjs` 的 `TRANSITIONS`，禁止跳过）：
```
discovered → needs_resolution → verified → downloading → downloaded → archived → validated
                                                      ↘ duplicate（终态，重复投递跳过）
                                                                    ↘ excluded（终态）
任何阶段 → blocked（可修复后回到安全状态）
```

**消费约定**：所有阶段脚本通过 `record_id` 定位记录，状态只能按状态机推进（`transitionRecord` 校验，非法转换抛 `INVALID_TRANSITION`）。`verify_archive` 的 manifest 闭环校验：`blocked` 记录阻断 STOP；`archived`/`duplicate`/`validated`/`excluded` 视为已处理；`needs_resolution`/`verified` 为流水线中间态仅提示不阻断。
