---
name: collect-resumes
description: >
  从飞书邮箱扫描简历邮件，按岗位归档到本地文件夹。
  触发词：收简历、整理简历、处理简历、下载简历、分类简历。
  只要用户提到简历、邮箱、候选人、作品集、归档，就使用这个skill。
  覆盖：标准附件下载、链接类附件（QQ超大附件/云盘）、美术岗作品集打包、多附件合并。
  不覆盖：Bitable写入（按需执行）、BOSS直聘打招呼（见boss-recruit skill）、**群聊文件简历**（见下方「群聊文件来源」）。
  依赖：lark-cli（mail 域已授权，权限已存本地，不查 auth）、node v24+（跑 .mjs 脚本）、Python + PyMuPDF + python-docx（闸门/脱敏脚本）、Playwright MCP（链接类附件下载 fallback，主力是 batch_download_links.mjs）。
---

# 简历收集与归档

## 群聊文件来源（不归档，即用即录）

业务方常在**飞书群/私聊**里直接发简历文件（不是邮件附件）。这类简历是**低频即用即下**场景（发一份、录一份），不走本 skill 的批量扫描归档管线，由 `_hire.py --by-name` 的**第三级级联**自动处理（本地归档库 → Downloads → 飞书群聊下载）：

```bash
python notes/_hire.py --by-name 白向庭 --job 海外游戏数据产品经理
```

`--by-name` 按优先级查找简历，前两级（归档库、Downloads）找不到时，自动从飞书群聊搜文件消息并下载到 Downloads，再走 Document AI 解析 + 录入。底层调 `_download_chat_file.py`（复用 lark-cli `im +messages-search` + `+messages-resources-download`）。详见 `recruit-followup` skill 的「录入候选人」。

## 配置

| 项 | 值 |
|---|---|
| 归档根目录 | `<PROJECT_ROOT>/data/在招岗位候选人管理` |
| 用户下载目录 | **`F:/Users/wuchunbo/Downloads`**（F 盘，不是 C 盘） |
| lark-cli | `LARK_CLI_PATH`，mail 域已授权 |
| node | 已装 v24，用于跑脚本 |
| **manifest 事实源** | **`<PROJECT_ROOT>/notes/collection_manifest.json`**（来源→候选人→岗位→目标路径→SHA-256 绑定） |

---

## 安全管线流程（manifest 驱动）

> **核心不变量**：每封相关邮件、每个附件和链接都有明确去向，不能静默消失；
> 来源→候选人→岗位→目标路径不可拆分绑定；无法验证即阻断（fail-closed）。

```
阶段1 扫描（scan_all）     → 完整快照 _scan_all.json（原子发布）
阶段2 核查（verify_mails）  → collection_manifest.json records
阶段3 解析（resolve_records）→ 绑定候选人/岗位/目标路径
阶段4 下载（download --record）→ .part → 校验 → 原子提交
阶段5 合并（merge_results）  → manifest 状态推进到 archived
阶段6 闸门（verify_archive） → 数量/姓名/薪酬/格式 + manifest 闭环 → validated
```

### 阶段1：扫描（只读）

```
node "…/collect-resumes/scripts/scan_all.mjs"
```

- **全量穷尽分页**（无 `--date` 参数，脚本不解析它；日期过滤在阶段2 `verify_mails --date` 做）。

- 穷尽分页到 `has_more=false`，按 `message_id` 去重。
- **任何异常（JSON 损坏、游标失效、CLI 错误）都不覆盖上一份完整快照**，部分结果写诊断文件，非零退出。
- 通知关键词只打 `is_notification` 标签，不删除邮件。

> ⚠️ 脚本在 **skill 目录**下，不在项目根。一律用绝对路径。

### 阶段2：核查附件 + 链接

```
node "…/collect-resumes/scripts/verify_mails.mjs" [--date 2026-07-10]
```

- 对每封邮件严格解析详情（JSON 损坏 → blocked，不静默记"零附件"）。
- 用 HTML 解析器提取**全部** href（支持 `&amp;` 实体解码），覆盖 QQ/网易大附件、126、云盘、ArtStation、普通作品站。
- 附件和链接各生成 manifest record（稳定 ID：`sha256(message_id + attachment_id)`）。
- 正文提示有材料但无附件无可提取链接 → blocked。
- **并发 fetch**（2026-07-29）：`--concurrency N`（默认5）并行查详情，29 封从几分钟降到几十秒。结果与串行完全一致。

### 阶段3：解析记录（绑定身份）

```
# 单条手动（需人工判断岗位归属/文件名时用）
node "…/collect-resumes/scripts/resolve_records.mjs" \
  --record <record_id> --name 张三 --job 特效设计师 --filename 张三_特效设计师_5年.pdf \
  [--manifest <PROJECT_ROOT>/notes/collection_manifest.json] [--date 7.29]

# 【推荐】批量自动解析（2026-08-13 新增，先 --dry-run 预览再正式跑）
node "…/scripts/resolve_records.mjs" --auto [--manifest <path>] [--date 7.29] [--dry-run]
```

- **`--auto` 自动解析（2026-08-13，对抗式审查新增）**：扫所有 `needs_resolution` 的 `mail_attachment` 记录，从文件名自动提取岗位+姓名（BOSS 投递格式 `【岗位_城市_薪资】姓名_年限.ext`），`matchJobDir` **唯一匹配才 resolve**，歧义/岗位不存在/文件名无法解析 → 保留 `needs_resolution` 并列出需人工处理清单。**第一性原理：简历文件名本身是结构化信息，不该让 AI 逐条手动转录一遍**（这正是 7-14 积压 455 条、漏一个月的根因）。`--dry-run` 只报告不写盘。
- 岗位目录从归档根**动态发现**（不再依赖手工别名表复制路径）。
- 歧义岗位（如 Unity 三岗）→ 保持 `needs_resolution`，不自动归档。
- **岗位名归一化匹配（2026-08-13）**：`matchJobDir` 精确匹配无果时，按「去空格/横线」归一化再精确匹配一次，消解 `AI Native 游戏服务端` vs `AI Native游戏服务端` 这类书写差异导致的漏匹配。仍唯一命中才生效，多个命中照样判歧义（不静默选错）。
- 路径逃逸 → blocked。
- **target_dir 落 `{M.DD}_暂定/` 中转目录**（2026-07-29）：resolve 时 N（整批人头数）未知，统一落 `_暂定`，由阶段6闸门自动 rename 为 `_{N}份`。`--date` 默认今天。
- **归档日期 = 操作日（今天），不是邮件收到日**（2026-07-31 铁律）：昨晚收到但今天才归档的简历，归到**今天**的文件夹 `{M.DD}`（符合工作习惯：你按归档日翻文件夹找）。**除非用户明确指定 `--date`，否则一律不传、用默认今天。** 洪健敏邮件 7.29 收的，今天归档 → 落 `7.31_1份`，不落 `7.29`。
- **重复投递自动跳过**（2026-07-31 铁律）：resolve 时检查 manifest，**同人同岗已 archived/validated 的 → 标 `duplicate` 跳过，不重复归档**。候选人改简历重投、隔几天再投是常态，已有归档+评估记录的不该再进新批次污染状态。比对键：`candidate_name + job_name`。命中时脚本输出 `⏭️ 跳过`（exit 0，正常退出）。
  - **Agent 责任**：resolve 前先扫一遍 manifest，对每个 needs_resolution 记录判断是否已存在同人同岗的 archived 记录。这一步脚本已内置，但 Agent 不要"为了批量归档"而绕过——重复就是跳过，不是"提示用户注意去重"。

> **⛔ 岗位→工作室归属核对（2026-08-06 铁律，resolve 前必做）**
>
> 教训：8.05 把"游戏ui设计师"（长青工作室）误归到"UIUE设计师"（坤灵项目），两个不同工作室的不同岗位，评估维度完全不同。根因是 Agent 凭"意思接近"猜岗位目录，没核对工作室前缀。
>
> **resolve 前逐个候选人核对**：
> 1. **BOSS 投递标题的岗位名 → 优先在归档目录里找逐字匹配的末级目录名**，不是"意思接近"的近义词。"游戏ui设计师"→"游戏UI设计师"（逐字），不是"UIUE设计师"（近义但不同岗位）。
> 2. **`--job` 支持传完整路径前缀**（推荐）：`--job 长青工作室/美术端/游戏UI设计师` → 脚本按路径后缀精确匹配，绝不走模糊匹配，彻底消除跨工作室撞名风险。含 `/` 的 jobName 触发路径前缀匹配模式。
> 3. **UI/特效/美术类岗位是跨工作室撞名重灾区**——长青有"游戏UI设计师"，坤灵有"UIUE设计师"；长青有"Unity特效设计师"，山海弹珠有"特效设计师"。遇到这类岗位，**必须**带工作室前缀或逐个确认归属。
> 4. **resolve 后扫一遍 target_dir 的工作室段**：如果总览表里同一岗位名跨工作室出现（如"UI设计师"同时在坤灵和长青），停下来核对归属是否正确。

> **⛔ 投递标题 vs 简历实际方向核对（2026-08-12 铁律，闸门/评估时必做）**
>
> 教训：8.12 陈伟嘉邮件标题是"UIUE设计师"（BOSS 简历文件名），agent 机械按标题归到坤灵 UIUE，但简历求职意向写"游戏动作"、通篇是网易/米哈游游戏动作经验——实际方向是 3D 动作，归 UIUE 是大错（UIUE 判不推→挪岗 3D 动作重评变强推）。根因：agent 只看投递标题，没核对简历实际方向。
>
> **核对规则**：resolve 按邮件标题归档是**起点不是终点**。简历文本可读后（阶段6闸门提取了文本 / analyze 阶段读简历时），**逐个核对简历"求职意向"+实际经验方向 与 归档岗位是否一致**：
> 1. 简历正文/求职意向里的方向（如"求职意向：游戏动作"、"特效设计师"、"服务端开发"）vs 归档岗位名——这是候选人**自报方向**，比 BOSS 投递标题更可信（投递可能选错/BOSS 归类错）。
> 2. **方向打架**（标题 A 岗但简历方向是 B，且 B 是另一个在招岗位）→ **不要闭眼归 A**：在 analyze 评估时标"方向存疑，疑挪岗 B"，或直接问用户确认实际岗位后挪岗重评。
> 3. **简历全是跨行经验**（投 A 岗但简历全是 B 方向、且 B 不是在招岗）→ 标方向不符，按 A 岗评大概率不推，但注明"疑投错岗"让用户判断。
> 4. 岗位名相近但不同（UIUE设计师 vs 游戏UI设计师 vs 3D动作设计师）是重灾区——看简历**做什么**（动作/UI/特效），不是看标题**叫什么**。

### 阶段4：下载（record 驱动，事务式）

**附件类（lark-cli API 下载）：**

```
# 单条
node "…/collect-resumes/scripts/download_attachment.mjs" \
  --record <record_id> [--manifest <PROJECT_ROOT>/notes/collection_manifest.json]

# 批量（多个 record，逐个下载 + 间隔，不连发打爆限流）
node "…/scripts/download_attachment.mjs" \
  --records "sha256:aaa,sha256:bbb,sha256:ccc" [--throttle 2000] [--manifest <path>]
```

- **只接受 `--record` / `--records`**，MID/附件ID/目标路径全部从 manifest 派生（旧 `MID + OUT` 默认拒绝）。
- 下载到 `.part` → 校验 magic bytes + content-length + SHA-256 → 原子 rename 提交。
- **目标已存在绝不覆盖**：同哈希幂等，异哈希冲突阻断。
- 应急模式 `--unsafe-manual <MID> <Downloads内路径>` 只能写 Downloads 隔离目录，不进 manifest。
- **限流重试**（2026-07-30）：`download_url` 接口极易限流（1234029/99991400），现已内置退避重试（指数退避 5/10/20s，最多3次）。`download()` 加 content-length 校验（防 drive-stream 返回 HTTP200+损坏/截断字节流静默通过）。
- **批量必须用 `--records` 不要自己写循环连发**：飞书 mail API 限流窗口窄，18 连发必触限流雪崩。`--records` 逐个下载 + 每个间隔 `--throttle`（默认 0.5 秒，有 `runWithRetry` 退避兜底一般无需调大），单条失败不中断整批（记 failed 列表，最后汇总，可单独 `--record` 重跑）。

**链接类（QQ/网易大附件，Playwright 批量下载）：**

```
# 【推荐】当天收简历：指定 record ID 精准下载（只下今天的，不碰历史积压）
node "…/collect-resumes/scripts/batch_download_links.mjs" \
  --records "sha256:xxx,sha256:yyy" --manifest <PROJECT_ROOT>/notes/collection_manifest.json

# 清理历史积压：从 manifest 读所有 verified 的 link 记录全量下载
node "…/scripts/batch_download_links.mjs" --manifest <path>

# 直接传 URL（不走 manifest）
node "…/scripts/batch_download_links.mjs" --urls "https://wx.mail.qq.com/..." "https://..."
```

- **⚠️ 当天收简历必须用 `--records` 精准下**：`--manifest` 全量扫会把所有历史 verified 的 link（可能几十条、几个GB）全拖出来串行下载，今天的候选人排在最后。教训（2026-08-04）：全量扫27条历史积压（含1.8GB文件），今天的2人排28/29，白等几小时。
- 下载成功后**自动写 result.json + 移文件到 target_dir**（2026-08-04 修复）：`merge_results.mjs` 靠 result.json 推进状态，旧版只下到 Downloads 不写 result.json → 状态卡 verified → 下次重复下载。
- Playwright headless 批量下载，自动检测链接失效。命令参数、fallback 策略详见 `references/link-attachments.md`。

### 阶段5：合并下载结果

```
node "…/collect-resumes/scripts/merge_results.mjs"
```

单进程串行合并并行下载的独立结果 JSON 到 manifest，校验哈希一致后推进状态到 `archived`。

### 阶段6：归档闸门（fail-closed）

**先脱敏，再跑闸门。** verify_archive 扫到薪酬会 STOP，所以闸门前必须先脱敏。

```
# 【推荐】批量脱敏整个目录：一条命令递归处理所有 pdf/docx/zip/rar（2026-08-13 新增 --dir）
python "…/collect-resumes/scripts/redact_salary.py" --dir <简历目录> --dry-run  # 先预览
python "…/scripts/redact_salary.py" --dir <简历目录>                             # 确认后正式脱敏

# 单文件模式（批量预览发现个别文件需单独处理时用）
python "…/scripts/redact_salary.py" <简历.pdf|docx|zip|rar> --dry-run           # 预览单个
python "…/scripts/redact_salary.py" <作品集.zip>                                 # 脱敏 zip（自动解压→脱敏→重打包）
python "…/scripts/redact_salary.py" <黄东亮-场景原画.rar> --output "黄东亮_游戏场景原画_6年_简历加作品.zip"  # rar 转 zip + 脱敏 + 规范命名（铁律：rar 必须转 zip）
python "…/scripts/redact_salary.py" <图片型简历.pdf>                             # 图片型/BOSS加密 PDF 自动 fallback OCR 脱敏
python "…/scripts/redact_salary.py" <BOSS加密简历.pdf> --redact-rects '[{"page":1,"dpi":200,"bbox":[x,y,x,y]}]'  # 外部坐标兜底

# 闸门
python "…/collect-resumes/scripts/verify_archive.py" \
  <简历目录> [--manifest <PROJECT_ROOT>/notes/collection_manifest.json] [--report-json <path>]
```

- **批量模式 `--dir` 已覆盖 zip/rar 自动解压脱敏重打包**，不再逐个文件跑；批量 fail-closed：任一文件存在无法脱敏的命中（定位不到矩形/安全阻断）→ 统一退出非0，列出需人工处理的文件清单。
- 图片型/BOSS加密 PDF 的 OCR 脱敏：依赖 easyocr（`pip install easyocr`），无 OCR 引擎时提示用 `--redact-rects` 外部坐标模式（主会话视觉模型定位薪酬后传坐标）。
- 五重阻断校验（数量/姓名/薪酬/格式/manifest闭环）详见 `references/archive-contract.md`。
- **`_暂定` 目录自动校正**（2026-07-29）：闸门 collect 前扫所有 `_暂定` 目录，数人头 rename 为 `_{N}份`，N 永远自洽。
- **`.doc` 支持**：经 antiword 提取做姓名/薪酬校验；命中薪酬 → STOP（提示转 PDF 后脱敏，antiword 无法涂白矩形）。

输出 `🟢 全过 — 可进评估` 才放行；任何无法验证的情况 `🔴 STOP`。

> **图片型/乱码 PDF 的闸门 STOP 处理**（tesseract OCR 质量兜底，2026-08-12）：
> verify_archive 对设计稿/扫描/字体编码损坏的 PDF 报"图片型 PDF"或"正文无姓名"STOP，根因是闸门 OCR 后端 tesseract 对艺术字体/图形化排版识别差（**不是文件归档错误**）。先用 extract_text 确认文本层为空/乱码（别对正常 PDF 误用），再用 enhance_ocr_pdf.py 嵌入 easyocr 文本层：
> ```
> python "…/scripts/enhance_ocr_pdf.py" <图片型或乱码PDF> [--name 候选人真名]
> ```
> - easyocr 对中文设计稿识别强于 tesseract；enhance 后闸门能提取姓名、查薪酬，重跑 verify_archive 确认 🟢
> - `--name` 修正 easyocr 对生僻字的形近字误识（如"珣"→"珀"，传真名强制嵌入）
> - jpg/png 图片简历自动转多页 PDF（长图切片避免 OOM）；enhance 一次性，下游 analyze-resumes 复用文本层
> - 依赖 easyocr（`pip install easyocr`）；脚本内嵌 salary_pattern 薪酬涂白（白色覆盖）

---

## 反模式（不要做）

- **不要把取 URL 和下载拆成两步** — auth code 有时效，原子完成
- **不要自己写循环连发批量下载附件** — 飞书 mail API 限流窗口窄，连发必雪崩拿损坏数据。用 `download_attachment.mjs --records id1,id2,...`（内置逐个+间隔+限流重试）
- **不要手动解压→脱敏→重打包美术岗 zip** — `redact_salary.py` 已支持 zip/rar 自动解压脱敏重打包，一个命令搞定
- **不要逐个文件跑脱敏** — 用 `redact_salary.py --dir <目录>` 批量脱敏（2026-08-13），一条命令递归处理所有 pdf/docx/zip/rar
- **不要逐个手动浏览器下载 QQ 大附件** — 用 `batch_download_links.mjs` 批量下载
- **不要用 curl 下载链接类附件** — 只拿到 HTML 跳转页，必须用浏览器
- **不要 cp/mv 后不数文件** — `verify_archive.py` 数量闸门兜底
- **不要跳过 verify_archive 直接进评估** — 无法验证的文件必须阻断
- **不要给 scan_all 传 --date** — 脚本不解析它，全量扫描；日期过滤在阶段2 verify_mails 做
- **不要当天收简历用 `batch_download_links --manifest` 全量扫** — 会把所有历史 verified 链接（可能几十条、几个GB）全拖出来串行下载，今天的人排在最后。用 `--records` 精准下

---

## 脚本

> 所有脚本在 **skill 目录** `…/collect-resumes/scripts/`。

| 脚本 | 用途 |
|------|------|
| `…/scripts/scan_all.mjs` | 全量扫描邮箱，原子发布完整快照 |
| `…/scripts/verify_mails.mjs` | 严格核查附件+链接，生成 manifest records（并发 fetch `--concurrency`） |
| `…/scripts/resolve_records.mjs` | 绑定候选人/岗位/目标路径到记录（落 `_暂定` 中转目录）；**`--auto` 批量自动解析**（从文件名提取岗位+姓名，唯一匹配才 resolve，2026-08-13）；岗位名归一化匹配（去空格/横线） |
| `…/scripts/download_attachment.mjs` | record 驱动事务式下载（`.part`→校验→原子提交；支持 PDF/DOCX/**.doc**/ZIP；**限流退避重试**；`--records` 批量逐个+间隔） |
| `…/scripts/batch_download_links.mjs` | **链接类附件批量下载**（Playwright headless，QQ/网易大附件，自动检测失效） |
| `…/scripts/merge_results.mjs` | 合并并行下载结果到 manifest（幂等状态推进，单条失败不中断整批） |
| `…/scripts/redact_salary.py` | 薪酬脱敏（白色覆盖，PDF/DOCX/**.doc**/**ZIP/RAR**；.doc 命中薪酬→阻断提示转PDF；rar自动转zip；`--output`规范命名；**`--dir`批量脱敏整目录**（2026-08-13，一条命令代替逐个跑）；解压前过安全关口+中文名还原；**图片型/BOSS加密PDF自动OCR脱敏**；`--redact-rects`外部坐标兜底） |
| `…/scripts/verify_archive.py` | 归档闸门（数量/姓名/薪酬/格式/manifest 闭环） |
| `…/scripts/enhance_ocr_pdf.py` | **图片型/乱码PDF的OCR文本层增强**（easyocr逐页OCR→涂白薪酬→嵌入不可见文本层；tesseract对设计稿识别差的兜底；支持jpg/png转多页PDF；`--name`修正生僻字误识；依赖easyocr） |
| `…/scripts/lib/manifest.mjs` | 稳定 ID/状态机/原子写入 |
| `…/scripts/lib/retry.mjs` | **限流退避重试**（识别 1234029/99991400，指数退避，最多3次；可复用） |
| `…/scripts/lib/lark_mail.mjs` | 严格 lark-cli 响应解析 |
| `…/scripts/lib/html_links.mjs` | HTML 链接提取与分类 |
| `…/scripts/lib/file_identity.mjs` | SHA-256/类型检测/冲突保护 |
| `…/scripts/lib/paths.mjs` | 路径常量单一真相源 |
| `…/scripts/lib/notifications.mjs` | 通知邮件关键词/过滤 |
| `…/scripts/content_extractors.py` | PDF/OCR/DOCX 统一内容提取 |
| `…/scripts/archive_safety.py` | ZIP 安全检查（路径穿越/加密/嵌套/炸弹） |
| `…/scripts/salary_pattern.py` | 薪酬正则单一真相源（verify_archive + redact_salary 共用） |
| `…/scripts/paths.py` | Python 路径常量（SEVEN_ZIP/缓存目录） |

> `…/` = `…/collect-resumes`

## 参考文档

按需加载：
- `references/archive-contract.md` — 归档结构单一真相源
- `references/job-aliases.md` — 岗位别名和歧义规则（路径以磁盘为准）
- `references/archive-naming.md` — 美术岗打包规则
- `references/link-attachments.md` — 链接类附件下载策略
