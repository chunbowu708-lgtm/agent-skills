# 决策记录（decisions）

> 本文件归档 collect-resumes 脚本的历史变更记录（"旧版怎么错 → 新版怎么改"）。
> 不在执行路径上——Agent 执行任务时无需阅读此文件。

## 2026-07-29 工具链缺陷修复 + 对抗式审查加固

> 背景：用户报告 5 个缺陷（29 封邮件实战后），经第一性原理 + 对抗式 code review 确认根因，
> **纠正了 2 个误诊**，并发现 3 个未报告的关键缺陷。全部修复，测试覆盖。

**两个误诊纠正（用户报告 vs 代码实查）：**
1. "download 落根目录导致 merge_results 路径校验失败" → **错误**：merge 两侧都派生自同一 `rec.target_dir`，永远自洽。真正破坏的是 `verify_archive` 数量闸门（根目录文件无 `_N份` 标注 → STOP）。
2. "dry-run 扫 zip 报无薪酬但闸门检出" → **不完整**：对文本型 PDF，dry-run 用 `get_text()+SALARY` 与 verify 按构造相同。真正分歧是**真实(非dry-run)脱敏** `_search_page` 当 `page.search_for` 无法定位矩形时**静默丢弃命中**——dry-run 反而误导操作员。

**缺陷①根治（download→日期子目录→闸门路径打通）：**
- `resolve_records.mjs`：target_dir 从"已收集简历"根改为"已收集简历/{M.DD}_暂定/"。新增 `--date` 参数。
  - 旧版：落根目录 → 闸门因无 `_N份` 标注 STOP → 手动归整 37 文件 → merge 路径对不上。
  - 新版：统一落 `_暂定` 中转目录；只创建此目录，不碰岗位目录。
- `verify_archive.py`：新增 `_finalize_pending_dirs`，闸门 collect 前扫所有 `_暂定` 目录，数人头后自动 rename 为 `{M.DD}_{N}份`。N 永远自洽，fail-closed 语义不变（数量不符仍 STOP）。空 `_暂定` 目录删除。
- `resolve_records`：collected_dir/target_dir 统一正斜杠存储（M2：旧版 path.join 产生反斜杠，下游字符串比较出错）。

**缺陷②根治（dry-run 与真实脱敏一致性）：**
- `redact_salary.py:_search_page`：当 `search_for` 找不到矩形时不再静默丢弃——先用 `get_text('dict')` 把匹配区间映射回 span bbox 涂白；仍找不到则标记 `__UNLOCATABLE__` fail-closed（报告命中、退出非0，不假装已脱敏）。
  - 旧版：dry-run 报命中、真实脱敏漏删 → 闸门检出残留 → 返工。
  - 新版：dry-run 与真实脱敏命中集合一致。

**缺陷③⑤根治（.doc 全链路支持）：**
- `file_identity.mjs:detectTypeFromBuffer`：加 OLE2/CFB 头检测（`D0 CF 11 E0 A1 B1 1A E1`）→ 返回 `'doc'`。
  - 旧版：无此分支 → 返回 unknown → `DOWNLOAD_TYPE_UNKNOWN`，只能 `--unsafe-manual`。
- `content_extractors.py`：新增 `extract_doc`（subprocess 调 antiword），dispatch 加 `.doc` 分支。antiword 不可用/失败 → 阻断（fail-closed）。
- `archive_safety.py`：`.doc` 从 `PORTFOLIO_MEDIA_EXTS` 移到 `RESUME_MEMBER_EXTS` + `ALLOWED_MEMBER_EXTS`，zip 内 .doc 参与姓名/薪酬校验。
- `verify_archive.py`：`RESUME_EXTS` 加 `.doc`；walk 用 `_is_resume_file` 统一判断（修 M5：top-level .doc 不再静默消失）。
- `redact_salary.py`：新增 `redact_doc`（antiword 只读不可涂白 → 命中薪酬即阻断提示转 PDF）。
- **决策**：antiword 校验 + 阻断含薪酬的（用户确认）。无需装 LibreOffice。

**缺陷④修复（verify_mails 并行化）：**
- `verify_mails.mjs`：新增 `runVerifyParallel`（mapPool 并发 fetch 限流5 + 串行处理保证 manifest 确定性）。CLI 改用 async runner（`execAsync`）。
  - 旧版：for + execSync 全串行，29 封几分钟。
  - 新版：并发 fetch，结果与串行完全一致（新增一致性测试）。
- `fetchMessageDetail`：兼容同步/异步 runner。

**H3 修复（merge_results downloaded 状态卡死）：**
- `merge_results.mjs`：状态机推进改幂等（`if status==='downloading' → downloaded`，不再从 downloaded→downloaded 抛 INVALID_TRANSITION）；per-record 转换包进 try/catch，单条失败记入 errors 继续不中断整批。
  - 旧版：'downloaded' 状态记录会抛错（在 try/catch 外）→ 整批中止且重跑卡死。

**H2 修复（redact_salary 解压前安全关口前置）：**
- `redact_zip` 开头调 `archive_safety.check_zip`，blocked 则拒绝 extractall。
- `redact_rar` 开头新增 `_check_rar_safety`（7z l 列成员，阻断嵌套归档/绝对路径/超成员数）。
  - 旧版：直接 extractall/7z x，绕过 verify_archive 的单一安全关口。

**H4 修复（salary 正则假阴性）：**
- `salary_pattern.py`：加裸万/W 区间（`25万-30万`、`20W-30W`）；加英文单值（`Salary: 30K`，手写大小写变体）。
  - 旧版：裸万/W 区间无关键词前缀漏匹配；英文单值无区间漏匹配。扫删同漏。

## 2026-07-16 消除代码重复

- SALARY 正则从 verify_archive.py / redact_salary.py 提取到 `salary_pattern.py` 单一源，两个脚本改为 import（此前是手工复制，靠注释"保持一致"维持，迟早漂移）。
- NOTIF_RE 通知关键词从 scan_all.mjs / verify_mails.mjs 提取到 `lib/notifications.mjs` 单一源（此前同样手工复制）。

## 2026-07-15 性能优化

- 美术岗解压脱敏重打包：redact_salary.py 一个命令搞定（此前需手动解压→脱敏→重打包三步）。
- QQ 大附件批量下载：新增 batch_download_links.mjs（Playwright headless，此前需逐个手动浏览器下载）。
- 链接下载快速失败：download 事件等待 15 秒（此前需登录的页面会无限等待）。

## 2026-07-13 运行 bug 修复

- download_attachment.mjs result.json 文件名冒号→下划线（Windows NTFS ADS bug：冒号被当 NTFS 备用数据流分隔符截断文件名）。

## 2026-07-10 安全管线全面重构

**verify_archive.py（归档闸门）：**
1. 旧版图片型 PDF 只告警不阻断 → 新版 fail-closed（OCR 不可用即 STOP）
2. 旧版 DOCX 完全绕过姓名/薪酬 → 新版用 content_extractors 统一提取
3. 旧版 ZIP 解压失败只告警 → 新版 archive_safety 阻断
4. 旧版缓存用 mtime+size → 新版用 SHA-256（mtime/size 只做性能快筛）
5. 旧版无 _N份 标注只告警 → 新版 STOP
6. 旧版档位目录与同级文件并存时漏计 → 新版同时统计
7. 新增 --manifest 闭环对账（来源→归档→哈希→状态）

**scan_all.mjs（扫描）：**
1. 旧版中间页 JSON 解析失败时 break 但未设 truncated，仍覆盖 _scan_all.json 并 exit 0 → 新版：任何异常都不覆盖上一份完整快照，部分结果写诊断文件，非零退出
2. 旧版通知关键词直接从 candidates 里删掉 → 新版：只打 is_notification 标签，不删除
3. 原子发布：先写 .tmp 再 rename，保证读者不会看到半截 JSON

**verify_mails.mjs（核查）：**
1. 旧版 JSON 解析失败时返回 ok:true → "零附件" 被静默记录 → 新版：严格 parseCliJson，失败抛错，邮件进 blocked
2. 旧版链接二次白名单漏了 126.com，普通域名作品集也不提取 → 新版：extractLinks 解析全部 href + 锚文本分类
3. 旧版直接写 _verified.json → 新版：生成 collection_manifest.json records（source_type + status 状态机）

**download_attachment.mjs（下载）：**
1. 旧版接受任意 MID + 任意 OUT，可人为串件 → 新版：常规模式只接受 --record，MID/附件ID/目标路径全部从 manifest 派生
2. 旧版失败时 writeFileSync 仍写到目标，重试会覆盖正确文件 → 新版：下载到 .part，校验通过后原子 rename；目标存在绝不覆盖
3. 旧版下载成功直接改 manifest → 新版：写独立结果 JSON（.result），由 merge_results.mjs 串行合并（避免并发覆盖）

**file_identity.mjs（文件身份）：**
- 同 download_attachment 第2点：校验失败仍 writeFileSync 到目标路径 → 下载到 .part 校验通过后原子提交

**lark_mail.mjs（CLI 解析）：**
- 旧版 verify_mails 在 JSON 解析失败时返回 ok:true → 真实附件被静默漏掉 → 新版 parseCliJson fail-closed

**html_links.mjs（链接提取）：**
- 旧版用简单正则提取 URL 且二次白名单漏了 126.com → 新版解析所有 `<a href>` + 锚文本分类
