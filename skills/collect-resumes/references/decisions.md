# 决策记录（decisions）

> 本文件归档 collect-resumes 脚本的历史变更记录（"旧版怎么错 → 新版怎么改"）。
> 不在执行路径上——Agent 执行任务时无需阅读此文件。

## 2026-08-14 晚 「何时收到」判定链根治（received_at 单一事实源）

> 背景：7.14 积压 22 人 + 3 条历史漏网被当成"8.14 今日"处理，评估报告 38→20→17 三次返工。
> 根因不是单个 bug，是三个时间戳三种语义、消费点各选各的：

**根因链（每个环节单独看都"合理"，连起来错）：**
1. collect 判"今日"用 `updated_at` —— 增量核查会刷新历史记录的 updated_at → 7.14 的 22 人全部"变今天"。
2. 改用 `created_at` 仍错 —— 历史邮件被后续运行**首次建记录**时 created_at 落在当天（8.8/8.5/8.10 三条漏网）。created_at 语义是"入库时间"不是"邮件时间"。
3. 真正的事实只有一个：**邮件真实收到时间**（快照 `m.date`）。
4. resolve 的目录日期段默认=**归档操作日** → 就算 scope 判对，积压邮件仍会落进操作日的 `_暂定` 目录 → analyze 按目录名出报告 → 报告混人。
5. 报告未核对名单就发飞书（38 人版）——发布前的最后一道人工核对缺位。

**修复（全部回归测试覆盖，node 38 / python 44）：**
- **`lib/dates.mjs` 单一实现**：parseMailDate / parseDateArg / sameLocalDay / mdd / recordDate（fallback 链 received_at→created_at→今天只在这里）。collect scope、resolve 日期段、verify --date 全部从它派生，禁止各自读时间戳。
- **verify_mails 存量回填**：`backfillReceivedAt` 每次运行按快照 message_id 给缺 received_at 的记录补齐（幂等、不调 API）——修复时 manifest 922 条记录 received_at 全空，只改新建逻辑等于没修存量。
- **resolve 目录日期段 per-record**：默认=该记录 `recordDate`（邮件收到日），`--date` 降级为人工显式覆盖。积压邮件从此按真实收到日落档（7.14 邮件进 7.14 目录），结构上消灭"积压混进今日目录"。
- **collect `--date` 语义统一**：从"归档日期段"改为"按收到日过滤 scope"（与 verify --date 同语义），不再传给 resolve 当目录日期。
- **verify_mails `--date` 修复**：旧实现 `m.date.startsWith("8.14")` 对快照格式 `"2026-08-14 18:50"` 永远不命中（文档广告的分步用法静默无效）；改 `filterByDate` 按本地日比较，"8.14"/"2026-08-14" 都接受。
- **salary_pattern 双修**：①裸金额分支在 alternation 首尾复制了两份（尾部是死代码，"单一真相源"文件内部冗余）→ 删；②`(月薪|…|期望).{0,4}\d{4,}` 会误命中"期望到岗时间2026年8月"（期望+到岗时间+2026）→ 加 `(?![年]|[-/]\d)` 日期 guard（连字符区间由区间分支兜住，不漏"月薪30000-35000"）。
- **跨 skill 契约**：analyze-resumes 阶段7 增加发布前核对——报告名单必须与 manifest `received_at` 当日记录比对（人数+姓名），不符先查再发。契约在 archive-contract.md 字段表。

**移入的历史教训（原在执行路径/代码注释上）：**
- "8.8 余世坤 / 8.5 常庆龙 / 8.10 何博被误当 8.14"（created_at 陷阱实证）、"7.14 积压 22 人误当今日"（updated_at 陷阱实证）→ 对应 SKILL.md 反模式"不要用 created_at/updated_at/目录名判定何时收到"。
- resolve_records.mjs 内"2026-07-29 target_dir 改 _暂定""455 条积压促生 --auto"等代码注释考古段已删（本文件 07-29/08-14 上午条目已有记录）。

## 2026-08-14 第一性原理+对抗式审查大修（慢/漏/一条命令/架构）

> 背景：用户反馈下载慢、有时漏。两位并行审查 agent + live 数据实证定位根因，全部修复并补回归测试。

**漏的三个实证根因（全部修复）：**
1. **通知关键词误杀候选人回信**（最严重）：`notifications.mjs` 的 NOTIF_RE 按 subject 匹配 `资料收集` 等词直接丢邮件——候选人回复 HR 的"【资料收集】公司名"主题邮件（带学历/薪酬附件）继承主题 → live 快照实证 ≥5 封真简历被静默丢弃且 manifest 零痕迹。修复：关键词降级为展示标签（🏷️），邮件是否相关由详情事实（附件/链接/正文提示）决定；fail-closed 原则的真正贯彻。
2. **verify_mails 每次全量重拉 853 封详情**（慢+限流雪崩面）：新增 `manifest.processed` 增量标记（message_id → 时间），已核查邮件（含"确认零附件"结论）跳过；详情拉取曾失败的（mail_detail blocked）保留重试且成功后自动自愈为 excluded（DETAIL_FETCH_RECOVERED）。`--force` 可全量重拉。
3. **301 条记录卡 verified 从未下载**（resolved 但没人跑 download+merge 两步）：download_attachment 新增 `--pending`；两个下载器批量尾部**自动合并**推进 archived（单进程串行无并发写风险，merge_results 降级为修复工具）；失效链接自动标 blocked(LINK_EXPIRED) 让闸门能发现缺失而非静默重试。

**一条命令：collect.mjs 编排器**：scan → verify(增量) → resolve --auto → 附件下载（今日 scope，--backlog 全量）→ 链接下载 → 自动合并 → redact --dir → verify_archive，每个受影响目录出闸门结果，汇总人工清单。下载默认只处理**今天** resolve 的记录（尊重"当天收简历别拖历史积压"的用户反馈），历史积压显式 `--backlog`。

**P0 数据丢失修复（_finalize_pending_dirs rmtree 删整批）**：旧版把 final_path 自身留在合并源列表——"已有 8.5_N份 + 同名重投 _暂定"场景下文件全被 skip 后 `rmtree(final_path)` 删光整批档案（8.05 实际丢过 4 个文件）。重写合并逻辑：合并源排除 final_path；同名冲突加 `_重投N` 后缀保留；源目录只在确已清空后删除；目录 rename 加 copytree+rmtree fallback（Windows 句柄锁）。补"同名重投不丢数据"回归测试。

**闸门诚实化与作用域**：`五重阻断` 实际只有三重（name_issues/blocked_files 是死变量，"姓名不符/文件阻断"分支不可达）——docstring 与判定对齐为"数量/薪酬/manifest 阻断 + 姓名/格式 warning"。manifest blocked 检查从全库收敛到**绑定本批次目录**（同父目录+同日期段，_暂定 与 rename 后的 _N份 同键），历史 blocked 不再劫持任意目录的闸门。数量闸门口径与 `_count_heads` 统一（旧版 collect 档位统计只认 os.listdir 全文件 → 备注.txt 虚增人头误 STOP）。单文件模式跳过数量闸门（旧版单文件必落"散落根目录"STOP，功能事实已死）。嵌套归档从静默跳过改为显式警告（防"外层zip里的嵌套zip藏薪酬"三层全放行）。

**重复造轮子清理（I5）**：getArg×5、isDirectRun×5、lark-cli 执行器×3 收敛到 `lib/cli_helpers.mjs`；retry.mjs 死函数 runLarkCliWithRetry 删除；file_identity.detectFileType 从整文件 readFileSync 改为只读 512 字节头（OOM 修复只修了一半）；`typeMatchesExtension` 不可达分支清理；batch_download_links 硬编码 playwright 路径收进 paths.mjs。⚠️ 教训：isDirectRun 收进共享模块时漏传 import.meta.url，首轮实测全脚本静默不执行（exit 0 零输出）——共享化必须当天实测。

**状态机补工具化入口**：`resolve_records --exclude <id> --code <原因码>`（历史 140 条 excluded 和 11 条非法 skipped 全是手改 JSON 的产物）；readManifest 读取时自动把非法状态（skipped）归一化为 excluded（LEGACY_SKIPPED_MIGRATED）。redact_salary --output 自动转绝对路径（相对路径落 CWD 坑）；--dir 模式新增 --report-json、列出批量范围外文件（图片简历提示 enhance_ocr_pdf）、rar→zip 成功后自动删原 rar（归档目录不留未脱敏源）。

**SKILL.md 重写为冷清单**（按 docs/skill-doc-standard.md）：时间戳/人名/教训故事全部移入本文件，执行路径只留 what/how。

**实测期追加修复（首轮 collect 全流程实测暴露）：**
- **collect.mjs python spawn 不能 shell:true**：中文路径含空格/全角括号时 cmd 按空格/括号重新拆参（"交互设计师（AI UGC平台）"被截断成"交互设计师（AI"→误报目录不存在）。改无 shell spawnSync('python')，ENOENT fallback `py -3`。
- **verify_archive 支持 target 是 `_暂定` 目录**：编排器把下载目录（_暂定）直接传给闸门时，旧逻辑只扫 target 内部找 _暂定 子目录、不处理自身 → 永远"未找到 _N份 批次"STOP。现在 target 为 _暂定 时先对父目录收敛（_finalize_pending_dirs），再重定向到收敛后的 _N份 校验。
- **同人同日重复投递 TARGET_CONFLICT 自动排除**：批量下载遇同名目标已存在+内容不同（候选人当天重发同份简历），删 conflict 副本（未脱敏内容不得残留在归档目录）+ 记录结构化排除（SAMEDAY_CONFLICT）。真新版简历可凭 message_id 回溯人工裁决。

### 移入的历史教训（原写在 SKILL.md 执行路径上）

- **8.05 游戏UI设计师误归 UIUE**：Agent 凭"意思接近"猜目录，长青"游戏ui设计师"归到坤灵"UIUE设计师"，两工作室不同岗位评估维度完全不同 → 产生 `--job` 路径前缀匹配（策略0）和 resolve 前归属核对铁律。
- **8.12 陈伟嘉方向打架**：邮件标题"UIUE设计师"但简历通篇网易/米哈游游戏动作经验，机械按标题归档 UIUE 判不推，挪 3D 动作岗重评变强推 → 产生"投递标题 vs 简历实际方向"核对铁律。
- **8.04 全量扫 27 条历史积压**：当天收 2 人排 28/29（含 1.8GB 文件），白等几小时 → 产生"当天用 --records 精准下"和编排器默认今日 scope。
- **7-14 积压 455 条漏一个月**：简历文件名本身是结构化信息（【岗位_城市_薪资】姓名_年限.ext），却要 AI 逐条人工转录 → 产生 resolve --auto（0→33 条自动解析）。

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

---

## 2026-08-17 漏件大修（第一性原理 + 对抗式审查）

**事故**：8.14 晚 20:41/23:45 贺丹、陈韵印两封 QQ 超大附件简历（链接在正文，无标准附件）漏掉，
用户质疑后全量对账发现 8 月共 7 条真实候选材料卡在管道（另含房越/王子谦/李俊龙/周光杰/陈伟嘉）。
三个并行审查 agent（扫描链/下载链/文档层）交叉验证，全部漏洞已修。

**根因链（贺丹/陈韵印漏件的完整闭环）**：
1. verify 建 link record（QQ 链接提取本身正常）→ needs_resolution
2. resolve --auto 硬编码只处理 mail_attachment，link 类永远跳过（P0）
3. collect 汇总按 JSON 插入序取前 20 条 = 永远显示最老的，贺丹排 114 条中第 113 位，不可见（P0）
4. 114 条存量 needs_resolution 使 collect 永远 exit 2 → 告警疲劳（P2）

**修复清单**（全部已修+测试）：
- `html_links.mjs`：无闭合 `<a>`（QQ 邮件 HTML 常见）、纯文本裸 URL、img src 三种提取盲区补齐；
  白名单加 quark/drive.google/lanhuapp；裸 URL 中文标点作边界防吞句号
- `verify_mails.mjs`：blocked/excluded 终态重复失败幂等（原会抛 INVALID_TRANSITION 炸整批
  → collect 静默继续 → 整批邮件零 record）；单封处理异常隔离；attachments 字段缺失时不写
  processed（原 fail-open 会永久锁死）；body_hint blocked 纳入重核查（修复后的提取器自愈
  历史误判）；关键词兜底扩到网盘/下载/链接/主页；record 落盘 subject；存量 subject 回填
- `collect.mjs`：汇总分「新到（近7天，完整列出+主题+时间）」vs「历史积压（仅报数）」，
  exit 2 只由新到触发；下载 scope = 指定日 ∪ 近7天 verified（治"处理日滞后滑进积压"）；
  resolve 退出码不再被忽略；scope 外近7天 verified 显式提示
- `resolve_records.mjs`：autoResolve 支持 link 类（邮件主题「姓名-岗位」提取，唯一匹配才
  落盘）；`--new-version` 重开 duplicate 终态（更新版简历正规入口，替代 unsafe-manual 旁路）；
  duplicate 转换不再丢 candidate_name；`--exclude-mail` 按邮件批量排除（背调材料一封
  13-25 附件逐条排没人做）
- `manifest.mjs`：duplicate 出边 needs_resolution（--new-version 唯一重开路）
- `scan_all.mjs`：增量边界命中后连续 2 页零新邮件才停（防服务端排序扰动把新邮件排进旧区）
- `download_attachment.mjs`：批量下载并发 3（CLI 冷启动 1.5s/份占 50-65%，串行慢 3 倍）；
  SAMEDAY_CONFLICT 批尾串行处理避免并发写 manifest
- `verify_archive.py`：文件名含「简历」的合体文件（XX_简历和作品集.pdf）按简历本体验证
  （原规则见「作品集」就跳过 → 55MB 合体 PDF 逃过姓名/薪酬检查）

**文档层**（单一真相源 + 渐进式披露）：
- CLAUDE.md「简历归档铁律」6 条中 5 条与 skill 重复 → 整节置换为指针；孤本规则
  （美术岗作品 ZIP 验证口径）搬入 archive-contract.md
- job-aliases.md 与磁盘脱节的全量表删除，降级为纯歧义规则（Unity 三岗/iOS SDK/UI特效动作撞名）
- AGENTS.md：脱敏铁律标题去"美术岗"限定（矩形/白色/边界适用于所有岗）；压缩包规则指针化
- link-attachments.md：阈值口径统一 150MB、CDP Proxy 标注仅 Claude Code 环境

**未修（记录在案）**：307 条 verified 历史积压清理需先做 target_dir 迁移（44 条反斜杠无
日期段、262 条指向已不存在的 8.13_暂定）；batch_download_links 下载后未按 magic bytes
校正扩展名（贺丹 PDF 被存成 .zip 后人工改名）。

## 2026-08-18 new-version 下载死锁修复（程远娟 bug）

**现象**：`--new-version` resolve 成功转 verified，但 `download_attachment --records` 的
批量前置自愈把同人同岗记录标 `excluded/DUPLICATE_ALREADY_ARCHIVED`（无出边终态），
再跑 `resolve --new-version` 返回 bad_record（excluded 不可重开）——补充版简历永久死锁。

**根因**：两层去重判断不对称——resolveRecord 的去重有 `!opts?.newVersion` 豁免，
download_attachment 的前置自愈没有；且 new-version resolve 成功时不落任何标记，
下载器无从豁免。excluded 终态无出边放大为死锁。

**修复**（三处）：
- `manifest.mjs`：`excluded` 出边 `['needs_resolution']`（受控重开路，与 duplicate 同构）
- `resolve_records.mjs`：--new-version 重开条件扩到"重复类排除码的 excluded"
  （DUPLICATE_ALREADY_ARCHIVED/SAMEDAY_CONFLICT/DUPLICATE_CANDIDATE 白名单，
  NOT_RESUME 等人工排除不放行）；重开时清掉残留 exclude_reason；
  new-version resolve 成功落 `new_version: true` 标记
- `download_attachment.mjs`：前置自愈去重循环 `if (rec.new_version) continue`

## 2026-08-18 对抗式审查 + 积压根治（用户质疑"机制有问题"触发）

**触发**：用户三问——①没几份简历为什么慢 ②08-14 的薪酬材料为什么 08-18 还在被当新到处理 ③历史积压每次都干扰，之前不是处理过吗。派独立审查 agent 对抗式审查 + 主会话数据实证交叉验证。

**被推翻的错误认知（曾写进记忆，已修正）**：
- "93 条待解析多为背调材料"——实为 49 条 6-7 月真简历僵尸（流程未跑起来的遗留）+ 44 条系统/流程邮件；08-17 旧版汇总只显最老 20 条，看到资料收集类就下了错误结论
- "39 条老链接失效"——实为 37 条面试邀约邮件误判（正文"简历/附件"字样撞 body_hint 正则）+ 真失效仅 3 条
- 08-17 大清理实际一条 needs_resolution 都没收敛（--exclude-mail 当天造好但没用过）；decisions.md 的"未修清单"也没记这批——被彻底遗忘

**修复**（6 项代码 + 1 次性数据收敛）：
- verify_mails：isNotification 豁免 body_hint（仅豁免"无附件且无链接"的兜底分支；带附件/链接的回信照常核查，不违反 08-14 静默丢简历教训）
- resolve_records：auto 失败缓存 `auto_failed_at`（失败过不再每日重试，`--force` 强制；成功清除标记）；failures 只列近 7 天；`--exclude-mail` 支持逗号分隔多 id
- scan_all：stdout 不再倾倒全量候选（913 封×每次=噪音），增量只打新增，全量写 `_candidates.txt`
- collect 汇总：新到标注"⏳已挂N天"（挂≥2天催落终态）；历史积压报数附构成分类（系统邮件/老投递/其他），黑盒总数曾掩盖 37/40 blocked 是误判的事实
- SKILL.md：新增"⛔当日收尾核对"铁律（新到清单必须清零落终态，决策停在对话里=明天原样再现）+ 系统/流程邮件当场排除口径
- 数据收敛（用户拍板）：132 条一次性排除（59 封系统邮件 NOT_RESUME + 50 封 6-7 月老投递/失效链接 STALE + 2 封测试稿），房越保留 blocked 等重发 → needs_resolution 93→0、blocked 40→1

**评估后未采纳**（对抗审查建议但不照单全收）：
- scan 结果缓存/无新邮件跳过 scan：边界 lookahead 2 页是 08-17 漏件事故后的防漏设计，为省几十秒引入漏件风险不值；resolve 空转已被失败缓存消除，verify 增量本来就快
- blocked 加 snooze/ack 新状态：真失效链接月均 1-2 条，为个位数引入状态机新态是过度设计；数据收敛+豁免已根治
- easyocr 无条件加载的假设被推翻：本就是惰性导入+图片型门槛（今天看到 torch 警告是谢文健加密 PDF 真需要 OCR 的必要成本）

## 2026-08-20 四条执行规则的固化（大文件分工 / merge 串行 / OCR 页数上限 / 手动归档入口）

**1. 大附件（≳50MB）默认用户手动下载，Agent 只归档**
- why：QQ 超大附件受服务器限速，Agent 的 Playwright 下载全程 10-20 分钟；用户浏览器/QQ 客户端有断点续传更快。且用户经常已在手动下载——Downloads 里看不到"正在下载中"的文件，Agent 无从感知，两边同时下载必有一遍白下（08-19 商连胜/邓世豪双下事故）。阈值从旧规则">150MB 建议人工"收紧为"≳50MB 默认不自动下"，因为分工模式变了：不是"谁快谁下"，而是"默认用户下，Agent 并行干别的"。
- 配套：手动归档三步入口（mv→result.json→merge）进 SKILL.md 单文件特殊场景。merge 的前置状态校验只认 verified/downloading/downloaded，blocked 记录要先 resolve --record 重推。

**2. 下载/合并任务不并行收尾**
- why：merge_results 是"读 manifest→改→原子写回"，无并发锁。两个下载任务几乎同时完成时各自 merge，后写的基于旧快照，把先写的状态覆盖回去（08-19 房越：sha256/扩展名校正都在但 status 被 covering 回 blocked，result 文件被消费，排查+修复花了 3 轮）。

**3. 图片型 PDF 的 OCR 只查前 3 页（`--ocr-pages` 可调，0=全页）**
- why：作品集式简历动辄几十页（吴雨坤 72 页），薪酬只出现在简历信息页（前几页个人信息行），后续全是作品图无薪酬字段。easyocr CPU 逐页跑 72 页 15 分钟跑不完，前 3 页 1-2 分钟。`redact_image_salary.py` 是同原则的独立快路径（jpg/单文件场景一条命令）。
- 坐标原则不变：只覆盖 OCR 命中块自身 bbox（含防锯齿小 padding），不做几何扩展——相邻字段（期望城市/求职意向）分属不同 OCR 块，天然不误删。

**4. `redact_image_salary.py` 诞生的 why**
- 08-19 邓世豪（7z 包内 jpg 简历）走了 6 步人工链（诊断嵌套阻断→解 jpg→转 PDF 绕路→easyocr 定位→视觉多轮确认→rects 涂白），其中 3 步是首次试错浪费。模式与吴雨坤（图片 PDF）同构 → 固化为脚本：复用 salary_pattern 正则判定命中、easyocr 定位、PIL/fitz 白块、输出验证图。easyocr 读不了中文路径（imageio 限制），脚本内先复制 ASCII 临时路径。fitz 的 apply_redactions 是 Page 级方法。
