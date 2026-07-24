# 决策记录（decisions）

> 本文件归档 collect-resumes 脚本的历史变更记录（"旧版怎么错 → 新版怎么改"）。
> 不在执行路径上——Agent 执行任务时无需阅读此文件。

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
