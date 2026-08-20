---
name: collect-resumes
description: >
  从飞书邮箱收集简历并归档到本地：扫描→核查→解析→下载→脱敏→闸门，一条命令（collect.mjs）或分步执行。
  触发词：收简历、整理简历、处理简历、下载简历、分类简历。
  只要用户提到简历、邮箱、候选人、作品集、归档，就使用这个skill。
  覆盖：标准附件下载、链接类附件（QQ超大附件/云盘）、美术岗作品集打包、多附件合并。
  不覆盖：Bitable写入、BOSS直聘打招呼（见boss-recruit skill）、群聊文件简历（走 candidate-entry 的 _hire.py --by-name 级联下载）。
  依赖：lark-cli（mail 域已授权，不查 auth）、node v24+、Python + PyMuPDF + python-docx + easyocr（图片型PDF）、Playwright（链接类附件，装在 <PROJECT_ROOT>/node_modules）。**Python 用项目 venv**（`python`，collect.mjs 自动探测，`PYTHON` 环境变量可覆盖）；`.doc` 转 PDF 需要本机 Word/WPS（Word COM）或 LibreOffice（soffice），无需 antiword。
---

# 简历收集与归档

## 配置

| 项 | 值 |
|---|---|
| 路径常量真相源 | `scripts/lib/paths.mjs`（JS）/ `scripts/paths.py`（Python）——manifest、归档根、Downloads 全部只在这里定义，改路径改这里 |
| 脚本目录 | `…/collect-resumes/scripts/`（下文 `…/` 均指此处） |
| manifest 事实源 | `<PROJECT_ROOT>/notes/collection_manifest.json`（所有命令的默认 --manifest，日常无需显式传） |

---

## 快路径：一条命令（日常推荐）

```
node "…/scripts/collect.mjs"                # 今天收到的邮件全流程：扫描→核查→自动解析→下载→合并→脱敏→闸门
node "…/scripts/collect.mjs" --backlog      # 清历史积压（下载所有 verified 记录，不限收到日）
node "…/scripts/collect.mjs" --no-finish    # 只到下载+合并，人工接管脱敏/闸门
node "…/scripts/collect.mjs" --date 7.29    # 只处理收到日=7.29 的记录（默认今天收到的）
```

- 退出码：`0`=全绿；`2`=有**新到**人工事项（看汇总）；`1`=流程错误（中止在某阶段）
- 汇总分两层：**新到（近 7 天收到）完整列出置顶**（含邮件主题+收到时间+已挂天数；挂≥2天会标注催落终态）/ 历史积压报数**附构成分类**（系统邮件/老投递/其他）——exit 2 只由新到事项触发
- 增量语义：已核查过的邮件自动跳过详情拉取；**首轮运行会对历史邮件做一次性全量核查（约几分钟），属正常**
- 下载 scope = 指定日（默认今天）∪ 近 7 天 verified 未下载（防深夜邮件/处理滞后滑进积压）；历史全量积压用 `--backlog`
- 归档目录日期段 = 各记录邮件收到日（历史积压按真实收到日落档，不混进操作日）
- auto 解析有**失败缓存**：解析失败过的记录（`auto_failed_at`）不再每日重试（重试必然再失败）；汇总也不每日重刷。看全量失败用 `--auto --force`

## Agent 必做的人工环节（collect 汇总之后）

collect 只自动处理"唯一匹配"，以下决策归 Agent/用户：

1. **needs_resolution 清单**（岗位歧义/文件名不可解析/图片附件）：
   - 能确定岗位 → 单条 resolve（`--job` 传完整路径前缀）：
     ```
     node "…/scripts/resolve_records.mjs" --record <id> --name 张三 \
       --job "长青工作室/美术端/游戏UI设计师" --filename 张三_游戏UI设计师_5年.pdf
     ```
     重复投递会自动标 `duplicate` 跳过（⏭️ 正常退出）。
   - **候选人是更新版简历**（同名同岗已归档、本次是补充AI版/新版）→ `--new-version` 重开归档，文件名加 `_补充AI版` 等后缀：
     ```
     node "…/scripts/resolve_records.mjs" --record <id> --name 张三 --job "…" \
       --filename 张三_岗位_年限_补充AI版.pdf --new-version
     ```
   - 非简历材料/确认无法处理 → 结构化排除（不手改 JSON）：
     ```
     node "…/scripts/resolve_records.mjs" --exclude <id> --code NOT_RESUME --note "背调材料"
     ```
     常用 code：`NOT_RESUME`（非简历材料）/ `NO_MATERIAL`（取不到材料）/ `IRRELEVANT`（与招聘无关）/ `STALE`（过期投递，岗位已停/太久远）/ `OTHER`
   - **一封邮件整批排除**（背调材料一封 13-25 个附件）：`--exclude-mail <id1,id2,...> --code NOT_RESUME`（支持逗号分隔多封一次排除）
2. **系统/流程邮件（当场排除，别留给"明天"）**：主题含「视频面试邀约 / 欢迎加入 / 【资料收集】/ 薪酬(学历)资料 / 信息征集 / 信息记录 / 测试题」的邮件是面试流程/入职材料，不是简历投递——读正文确认后**当场** `--exclude-mail NOT_RESUME`。⚠️ 边界：候选人**回信继承通知主题**但带简历附件/链接的，附件/链接核查照常走（这类是真简历，2026-08-14 有静默丢简历事故，见 notifications.mjs 头注释）；只有"无附件且无链接"的纯通知才排除
3. **⛔ 当日收尾核对（每天 collect 处理完必做，2026-08-18 铁律）**：汇总里"新到待解析/blocked"清零了吗？没清零的每一条都要么 resolve 要么 --exclude **落终态**——决策停在对话里=明天原样再现（姚宇舟 5 条挂 4 天的教训：08-17 说了"已转发相关负责人"却没落盘，08-18 还在干扰）。汇总标注「⏳已挂N天」的就是漏网
4. **blocked 记录**：`LINK_EXPIRED`（链接失效）→ 联系候选人重发或 `--exclude`；详情拉取失败 → 重跑 collect 自动重试自愈
5. **闸门 STOP 的目录**：按 verify_archive 输出修复（数量不符/薪酬残留/本批次 blocked），修复后单跑闸门复验
6. **图片型/乱码 PDF 闸门报"提取阻断"**：先确认文本层确实为空/乱码，再跑
   ```
   python "…/scripts/enhance_ocr_pdf.py" <图片型或乱码PDF> [--name 候选人真名]
   ```
   嵌入 OCR 文本层后重跑闸门；`--name` 纠正 OCR 对生僻字的形近字误识。

### 岗位→工作室归属核对（resolve 前必做）

- `--job` 优先传**完整路径前缀**（含 `/` 即触发路径精确匹配，不走模糊）
- 只传岗位名时，投递标题的岗位名必须与归档目录**末级名逐字匹配**，不取近义词（"游戏UI设计师"≠"UIUE设计师"）
- UI / 特效 / 客户端类岗位跨工作室撞名高发：不带路径前缀的这类 resolve，逐个人工确认归属

### 投递标题 vs 简历实际方向核对（闸门/评估阶段必做）

- 归档以邮件标题为起点不是终点；简历文本可读后，核对正文"求职意向/实际经验方向"与归档岗位是否一致
- 方向打架（标题 A 岗、简历方向是另一个在招 B 岗）→ 标"疑挪岗 B"问用户，确认后挪岗重评，不闭眼归 A
- 简历全是跨行经验（B 不是在招岗）→ 按 A 岗评估但注明"疑投错岗"

---

## 分步命令（fallback / 单阶段重跑 / 排障）

| 阶段 | 命令 | 要点 |
|---|---|---|
| 1 扫描 | `node "…/scripts/scan_all.mjs"` | 全量穷尽分页，原子发布快照；异常不覆盖旧快照。**不支持 --date**。增量：读上次快照，边界命中后连续 2 页零新邮件才停（防服务端排序扰动漏件），合并去重后发布 |
| 2 核查 | `node "…/scripts/verify_mails.mjs" [--date 8.14] [--force]` | 增量（已核查邮件跳过）；`--date` 按邮件收到日过滤；`--force` 全量重拉。通知邮件**不按关键词丢弃**。存量记录自动回填 `received_at` 和 `subject`（按快照邮件）。链接提取覆盖：闭合/无闭合 `<a>`、纯文本裸 URL、img src；兜底关键词含网盘/下载/链接/主页 |
| 3 解析 | `node "…/scripts/resolve_records.mjs" --auto [--dry-run] [--force]` | 附件类从文件名、**链接类从邮件主题**（「姓名-岗位」模式）提取岗位+姓名，唯一匹配才落盘；歧义保留 needs_resolution。目标目录日期段默认=该记录邮件收到日（`--date` 仅人工显式覆盖）。`--dry-run` 先预览。**失败有缓存**：失败过的不再重试（`--force` 强制全量重试），失败清单只列近 7 天 |
| 4a 附件下载 | `node "…/scripts/download_attachment.mjs" --pending` 或 `--records id1,id2` | 事务式（.part→校验→原子提交，目标存在绝不覆盖）；**并发 3**（CLI 冷启动占大头，串行慢 3 倍）；限流自动退避重试；完成自动合并推进 archived |
| 4b 链接下载 | `node "…/scripts/batch_download_links.mjs" --records id1,id2`（不传=全部 verified 链接） | Playwright headless；失效自动标 blocked(LINK_EXPIRED)；完成自动合并。当天收简历**用 --records 精准下**。**大附件（≳50MB）默认不自动下载**：提醒用户手动下载到 Downloads，Agent 走手动归档（见下方"用户手动下载文件的归档"）；仅用户明确让 Agent 下载时才跑本脚本 |
| 5 合并 | `node "…/scripts/merge_results.mjs"` | 修复工具：中断恢复时手动收尾（常规路径已自动合并；并行收尾禁止，见反模式） |
| 6 脱敏+闸门 | `python "…/scripts/redact_salary.py" --dir <目录> [--report-json <p.json>]` → `python "…/scripts/verify_archive.py" <目录>` | **先脱敏再闸门**（顺序反了闸门必 STOP）；rar 自动转 zip 并删原包；**`.doc` 自动转 PDF（Word COM / soffice）后脱敏，转换成功自动删原 .doc**；闸门输出 `🟢 全过` 才进评估。批量跑闸门时目录会被自动收敛改名（`_暂定`→`_N份`），报"没找到简历"先 ls 确认实际目录名再重跑 |

单文件特殊场景（批量模式覆盖不到时）：

```
# 用户手动下载的大附件归档（不跑下载脚本，三步走命令入口，不手改 manifest）：
# 1. mv "Downloads 目录/<原文件>" "<target_dir>/<规范文件名>"
# 2. 写 notes/_download_results/sha256_<record_id 的 : 换成 _>.result.json
#    内容: {"record_id":"sha256:...","outcome":"committed","sha256":"<文件哈希>","target_path":"<目标路径>","at":"<ISO时间>"}
# 3. node "…/scripts/merge_results.mjs"   （blocked 状态先 resolve --record 重推到 verified 再 merge）
# rar 转规范命名 zip（--output 必须绝对路径）
python "…/scripts/redact_salary.py" <简历.rar> --output "F:/…/姓名_岗位_年限_简历加作品.zip"
# 图片型简历（jpg/图片型PDF，7z 包内图片简历先解出）薪酬脱敏一条命令
python "…/scripts/redact_image_salary.py" <简历.jpg|图片型.pdf>   # 定位命中块→白块→输出验证图复核
# 图片型 PDF 主会话视觉定位薪酬后传坐标
python "…/scripts/redact_salary.py" <图片型.pdf> --redact-rects '[{"page":1,"dpi":200,"bbox":[x,y,x,y]}]'
```

---

## 反模式（不要做）

- **不要自己写循环连发下载** —— 用 `--records` / `--pending`（内置限流退避 + 间隔）
- **不要把取 URL 和下载拆两步** —— auth code 有时效，原子完成
- **不要用 curl 下载链接类附件** —— 只会拿到 HTML 跳转页，必须走浏览器
- **不要手动解压→脱敏→重打包** zip/rar —— `redact_salary --dir` 一条命令搞定
- **不要逐个文件跑脱敏/闸门** —— 用 `--dir` / 传目录
- **不要给 scan_all 传 --date**；**不要手改 manifest JSON** —— 每个状态转移都有命令入口
- **不要把闸门 warning 当阻断** —— `🔴 STOP` 才是阻断；`⚠️` 是人工复核提示（加密 PDF 姓名缺失等）
- **不要用 created_at / updated_at / 目录名判定"何时收到"** —— 唯一依据 manifest `received_at`（字段语义见 archive-contract.md）
- **不要把链接下载失败的记录晾着** —— Playwright 报 no_button/下载取消的 QQ 链接，重跑一次往往能成（风控页是暂时的）；连续失败才列给用户人工下载
- **不要并行跑多个下载/合并任务** —— merge_results 是读-改-写 manifest，两个任务同时收尾会互相覆盖状态；多批记录下载完统一跑一次 merge
- **不要对图片型作品集 PDF 逐页 OCR** —— 薪酬只出现在简历信息页（前几页），后续是作品图；redact_salary 对图片型 PDF 默认只 OCR 前 3 页（`--ocr-pages 0` 才全页），独立快路径用 `redact_image_salary.py`

---

## 脚本

| 脚本 | 用途 |
|------|------|
| `…/scripts/collect.mjs` | **编排器**：一条命令跑完全流程（复用下列脚本，自身无业务逻辑） |
| `…/scripts/scan_all.mjs` | 阶段1 全量扫描，原子发布快照 |
| `…/scripts/verify_mails.mjs` | 阶段2 附件+链接核查，生成/推进 manifest records（并发 fetch，增量跳过） |
| `…/scripts/resolve_records.mjs` | 阶段3 绑定候选人/岗位/目标路径（--auto 批量；--record 单条；--exclude 结构化排除） |
| `…/scripts/download_attachment.mjs` | 阶段4a record 驱动事务式附件下载（--pending/--records，自动合并） |
| `…/scripts/batch_download_links.mjs` | 阶段4b 链接类附件 Playwright 批量下载（失效→blocked，自动合并） |
| `…/scripts/merge_results.mjs` | 阶段5 result.json → manifest 状态推进（修复工具） |
| `…/scripts/redact_salary.py` | 阶段6a 薪酬脱敏（白色覆盖；PDF/DOCX/.doc/ZIP/RAR；--dir 批量+报告；OCR fallback） |
| `…/scripts/verify_archive.py` | 阶段6b 归档闸门（数量/薪酬/manifest 闭环阻断；姓名/格式 warning；_暂定→_N份 收敛） |
| `…/scripts/enhance_ocr_pdf.py` | 图片型/乱码 PDF 的 OCR 文本层增强（easyocr；jpg/png 转多页 PDF） |
| `…/scripts/redact_image_salary.py` | 图片型简历（jpg/png/图片型PDF）薪酬脱敏：OCR 前 N 页定位命中块→白块覆盖→输出验证图（复用 salary_pattern 正则） |
| `…/scripts/lib/` | 共享模块：manifest（状态机/原子写）、dates（邮件时间判定单一源）、cli_helpers（参数/lark执行器）、retry（限流退避）、file_identity（SHA-256/类型/原子提交）、html_links、lark_mail、notifications（仅展示标签）、paths（路径真相源） |
| `…/scripts/content_extractors.py` | Python 侧统一内容提取（PDF/OCR/DOCX/.doc） |
| `…/scripts/archive_safety.py` | ZIP 安全检查（穿越/加密/炸弹/嵌套）+ 中文文件名还原 |
| `…/scripts/salary_pattern.py` | 薪酬正则单一真相源（redact + verify 共用） |

## 参考文档（按需加载）

| 文件 | 什么时候读 |
|------|-----------|
| `references/archive-contract.md` | 改归档结构、查 manifest 字段/状态机契约、看闸门判据定义 |
| `references/job-aliases.md` | 岗位名匹配不到目录时查别名表和歧义规则（Unity 三岗等） |
| `references/archive-naming.md` | 美术岗打包命名规则 |
| `references/link-attachments.md` | 链接下载失败后的 fallback（Playwright MCP 手动操作 / 用户手动下载） |
| `references/decisions.md` | 想知道某条规则"为什么存在"时（历史决策与事故记录，不在执行路径上） |
