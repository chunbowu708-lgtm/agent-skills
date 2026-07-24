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
| 归档根目录 | `F:/miniwanob/data/在招岗位候选人管理` |
| 用户下载目录 | **`F:/Users/wuchunbo/Downloads`**（F 盘，不是 C 盘） |
| lark-cli | `C:/Users/wuchunbo/AppData/Roaming/npm/lark-cli.cmd`，mail 域已授权 |
| node | 已装 v24，用于跑脚本 |
| **manifest 事实源** | **`F:/miniwanob/notes/collection_manifest.json`**（来源→候选人→岗位→目标路径→SHA-256 绑定） |

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
node "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/scan_all.mjs" [--date 2026-07-10]
```

- 穷尽分页到 `has_more=false`，按 `message_id` 去重。
- **任何异常（JSON 损坏、游标失效、CLI 错误）都不覆盖上一份完整快照**，部分结果写诊断文件，非零退出。
- 通知关键词只打 `is_notification` 标签，不删除邮件。

> ⚠️ 脚本在 **skill 目录**下，不在项目根。一律用绝对路径。

### 阶段2：核查附件 + 链接

```
node "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/verify_mails.mjs" [--date 2026-07-10]
```

- 对每封邮件严格解析详情（JSON 损坏 → blocked，不静默记"零附件"）。
- 用 HTML 解析器提取**全部** href（支持 `&amp;` 实体解码），覆盖 QQ/网易大附件、126、云盘、ArtStation、普通作品站。
- 附件和链接各生成 manifest record（稳定 ID：`sha256(message_id + attachment_id)`）。
- 正文提示有材料但无附件无可提取链接 → blocked。

### 阶段3：解析记录（绑定身份）

```
node "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/resolve_records.mjs" \
  --record <record_id> --name 张三 --job 特效设计师 --filename 张三_特效设计师_5年.pdf \
  [--manifest F:/miniwanob/notes/collection_manifest.json]
```

- 岗位目录从归档根**动态发现**（不再依赖手工别名表复制路径）。
- 歧义岗位（如 Unity 三岗）→ 保持 `needs_resolution`，不自动归档。
- 路径逃逸 → blocked。

### 阶段4：下载（record 驱动，事务式）

**附件类（lark-cli API 下载）：**

```
node "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/download_attachment.mjs" \
  --record <record_id> [--manifest F:/miniwanob/notes/collection_manifest.json]
```

- **只接受 `--record`**，MID/附件ID/目标路径全部从 manifest 派生（旧 `MID + OUT` 默认拒绝）。
- 下载到 `.part` → 校验 magic bytes + SHA-256 → 原子 rename 提交。
- **目标已存在绝不覆盖**：同哈希幂等，异哈希冲突阻断。
- 应急模式 `--unsafe-manual <MID> <Downloads内路径>` 只能写 Downloads 隔离目录，不进 manifest。

多封邮件 → 多个 Bash 并行发出。

**链接类（QQ/网易大附件，Playwright 批量下载）：**

```
# 从 manifest 读所有 verified 的 link 记录，一次性批量下载
node "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/batch_download_links.mjs" \
  --manifest F:/miniwanob/notes/collection_manifest.json

# 指定 record ID
node "…/scripts/batch_download_links.mjs" --records "sha256:xxx,sha256:yyy" --manifest <path>

# 直接传 URL（不走 manifest）
node "…/scripts/batch_download_links.mjs" --urls "https://wx.mail.qq.com/..." "https://..."
```

- Playwright headless 批量下载，自动检测链接失效。命令参数、fallback 策略详见 `references/link-attachments.md`。

### 阶段5：合并下载结果

```
node "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/merge_results.mjs"
```

单进程串行合并并行下载的独立结果 JSON 到 manifest，校验哈希一致后推进状态到 `archived`。

### 阶段6：归档闸门（fail-closed）

**先脱敏，再跑闸门。** verify_archive 扫到薪酬会 STOP，所以闸门前必须先脱敏。

```
# 预览：看哪些文件有薪酬命中（不改文件）
python "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/redact_salary.py" <简历.pdf|docx|zip|rar> --dry-run

# 脱敏单个文件：白色覆盖（禁止黑色，AGENTS.md 铁律）
python "…/scripts/redact_salary.py" <简历.pdf|docx>

# 脱敏 ZIP 包：自动解压→脱敏包内简历→重打包（一个命令搞定美术岗）
python "…/scripts/redact_salary.py" <作品集.zip>

# RAR 转 ZIP + 脱敏 + 规范命名（铁律：rar 必须转 zip 才能归档）
python "…/scripts/redact_salary.py" <黄东亮-场景原画.rar> --output "黄东亮_游戏场景原画_6年_简历加作品.zip"

# 闸门
python "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/verify_archive.py" \
  <简历目录> [--manifest F:/miniwanob/notes/collection_manifest.json] [--report-json <path>]
```

- 五重阻断校验（数量/姓名/薪酬/格式/manifest闭环）详见 `references/archive-contract.md`。

输出 `🟢 全过 — 可进评估` 才放行；任何无法验证的情况 `🔴 STOP`。

---

## 反模式（不要做）

- **不要把取 URL 和下载拆成两步** — auth code 有时效，原子完成
- **不要手动解压→脱敏→重打包美术岗 zip** — `redact_salary.py` 已支持 zip/rar 自动解压脱敏重打包，一个命令搞定
- **不要逐个手动浏览器下载 QQ 大附件** — 用 `batch_download_links.mjs` 批量下载
- **不要用 curl 下载链接类附件** — 只拿到 HTML 跳转页，必须用浏览器
- **不要 cp/mv 后不数文件** — `verify_archive.py` 数量闸门兜底
- **不要跳过 verify_archive 直接进评估** — 无法验证的文件必须阻断

---

## 脚本

> 所有脚本在 **skill 目录** `C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/`。

| 脚本 | 用途 |
|------|------|
| `…/scripts/scan_all.mjs` | 全量扫描邮箱，原子发布完整快照 |
| `…/scripts/verify_mails.mjs` | 严格核查附件+链接，生成 manifest records |
| `…/scripts/resolve_records.mjs` | 绑定候选人/岗位/目标路径到记录 |
| `…/scripts/download_attachment.mjs` | record 驱动事务式下载（`.part`→校验→原子提交） |
| `…/scripts/batch_download_links.mjs` | **链接类附件批量下载**（Playwright headless，QQ/网易大附件，自动检测失效） |
| `…/scripts/merge_results.mjs` | 合并并行下载结果到 manifest |
| `…/scripts/redact_salary.py` | 薪酬脱敏（白色覆盖，PDF/DOCX/**ZIP/RAR**，rar自动转zip，`--output`规范命名，闸门前必跑） |
| `…/scripts/verify_archive.py` | 归档闸门（数量/姓名/薪酬/格式/manifest 闭环） |
| `…/scripts/lib/manifest.mjs` | 稳定 ID/状态机/原子写入 |
| `…/scripts/lib/lark_mail.mjs` | 严格 lark-cli 响应解析 |
| `…/scripts/lib/html_links.mjs` | HTML 链接提取与分类 |
| `…/scripts/lib/file_identity.mjs` | SHA-256/类型检测/冲突保护 |
| `…/scripts/lib/paths.mjs` | 路径常量单一真相源 |
| `…/scripts/lib/notifications.mjs` | 通知邮件关键词/过滤 |
| `…/scripts/content_extractors.py` | PDF/OCR/DOCX 统一内容提取 |
| `…/scripts/archive_safety.py` | ZIP 安全检查（路径穿越/加密/嵌套/炸弹） |
| `…/scripts/salary_pattern.py` | 薪酬正则单一真相源（verify_archive + redact_salary 共用） |
| `…/scripts/paths.py` | Python 路径常量（SEVEN_ZIP/缓存目录） |

> `…/` = `C:/Users/wuchunbo/.agents/skills/collect-resumes`

## 参考文档

按需加载：
- `references/archive-contract.md` — 归档结构单一真相源
- `references/job-aliases.md` — 岗位别名和歧义规则（路径以磁盘为准）
- `references/archive-naming.md` — 美术岗打包规则
- `references/link-attachments.md` — 链接类附件下载策略
