---
name: collect-resumes
description: >
  从飞书邮箱扫描简历邮件，按岗位归档到本地文件夹。
  触发词：收简历、整理简历、处理简历、下载简历、分类简历。
  只要用户提到简历、邮箱、候选人、作品集、归档，就使用这个skill。
  覆盖：标准附件下载、链接类附件（QQ超大附件/云盘）、美术岗作品集打包、多附件合并。
  不覆盖：Bitable写入（按需执行）、BOSS直聘打招呼（见boss-recruit skill）。
  依赖：lark-cli（mail 域已授权，权限已存本地，不查 auth）、Playwright MCP（链接类附件下载，CDP Proxy 做补充）。
---

# 简历收集与归档

## 配置

| 项 | 值 |
|---|---|
| 归档根目录 | `F:/miniwanob/data/在招岗位候选人管理` |
| 用户下载目录 | **`F:/Users/wuchunbo/Downloads`**（真实下载盘，不是 C 盘！USERPROFILE 是 C 盘但 Downloads 被重定向到 F 盘） |
| lark-cli | `C:/Users/wuchunbo/AppData/Roaming/npm/lark-cli.cmd`，mail 域已授权，不查 auth |
| node | 已装 v24，用于跑脚本（bash 在 cmd 不可用，统一用 node） |

---

## 阶段1：扫描（只读）

### 1. 拉取邮件 — 穷尽式，不要抽样

**用现成脚本一次性翻完，不要手动 max 200 翻一页就以为扫完了：**

```
node "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/scan_all.mjs"
```

> ⚠️ 脚本在 **skill 目录**下，不在项目根。项目根 `F:/miniwanob/scripts/` 不存在，写 `scripts/scan_all.mjs` 会报 `Cannot find module`。一律用上面这条绝对路径，在任何 CWD 下都对。

脚本会分页直到 `has_more=false`（全量，不靠 subject 猜），排除通知关键词，把所有候选邮件列出来。输出存 `notes/_scan_all.json`。

> 为什么用脚本：分页 + 排除 + 去重是机械活，每次手敲 max 值会漏。今天就是 max 200 翻一页就停，漏了候选人。

筛选今天日期的邮件。排除关键词：员工关爱、面试邀约、日程提醒、系统通知、奋斗食代。

BOSS 直聘邮件 subject 格式：`{姓名} | {经验}，应聘 {岗位} | {城市}{薪资}【BOSS直聘】`
- 从 subject 提取姓名、工作年限、岗位名
- 直投邮件（非 BOSS）subject 不规则，姓名和岗位需手动判断

### 2. 逐封核查附件 + body 链接（每封都要查，不管附件数是几）

**用现成脚本，不要手动一封封敲：**

```
node "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/verify_mails.mjs"
```

脚本对每封邮件**一次** `mail +message` 同时取 `attachments`（标准附件）和 `body_html`（链接类附件），输出清单，并把附件 id 存到 `notes/_verified.json` 供 download 复用（省 N 次 +message）。

> ⚠️ 铁律：**每封都查 body_html，不管 attachments 数量是几。**
> 踩过的坑：卢苇那封 attachments=1（简历docx），就以为没作品了，结果作品是 QQ 超大附件，藏在 body_html 链接里。attachments 和 body 链接是**并存**的，不是二选一。

链接类附件关键词：
- `wx.mail.qq.com/ftn` → QQ超大附件
- `mail.163.com/large` / `126.com` → 网易超大附件
- `pan.baidu.com` / `aliyundrive` → 云盘
- `作品` / `portfolio` / `artstation` → 作品集链接

### 3. 岗位匹配 + 归档路径

从 subject 提取岗位名，对照 `references/job-aliases.md` 找到文件夹名和团队路径。
AI 直接判断，不写规则脚本。匹配不上 → 标"待确认"。

### 4. 同一人多邮件合并

按姓名去重，同一人的多封邮件合并为一行：
- BOSS 简历 + 直投作品 → 合并
- BOSS 简历 + QQ/163 作品集 → 合并
- 展示时附件类型全部列出

### 5. 对账本地

```bash
find <归档根目录> -name "*姓名*" -type f
```

### 6. 输出给用户确认

**固定格式，按岗位分组：**

```
【产品经理实习生】15 份
  黎高岚 28届 BOSS 1PDF     [未归档]
  李女士 27届 BOSS 1PDF     [未归档]
  ...

【特效设计师】2 人
  陈伟龙 3年 BOSS+QQ 1PDF+作品rar(260MB)  [未归档]
  刘莹 直投 163 作品zip(148MB)             [待确认岗位]

【AI Native游戏服务端】2 份
  钟明羿 5年 BOSS 1PDF  [未归档]
  梁梓健 8年 BOSS 1PDF  [未归档]
```

每行：`姓名 | 工作年限/届 | 来源 | 附件情况 | 状态`

---

## 阶段2：下载与归档

用户确认后执行。

### 标准附件 — 原子下载（取URL+下载必须在一次操作里）

**用现成脚本（bash 在 Windows cmd 不可用，统一用 node）：**

```
node "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/download_attachment.mjs" <MID> <输出路径> [附件序号，默认0]
```

脚本内部：复用 `_verified.json` 的 ATT_ID（没跑过 verify_mails 才实时查）→ 取 download_url → https.get 下载 → **校验 magic bytes**（%PDF/PK 才算 OK，0字节/HTML错误页 exit 4/5/6），**一个进程内完成**。

> ⚠️ 铁律：**取 URL 和下载必须原子完成，不能分两步。**
> 飞书 download_url 的 auth code 有时效（几十秒），先取 URL 存下来、过会儿再 curl，会得到 0 字节空文件。
> 下载完会校验文件头：返回 0 字节（exit 4）/ HTML 错误页（exit 5）/ 非 PDF-ZIP（exit 6）都算失败，要重试。

多封邮件 → 多个 Bash 并行发出，不串行等。

### 链接类附件

见 `references/link-attachments.md`。Playwright MCP 优先，CDP Proxy 补充。

### 归档检查点（最后必跑闸门，不过不许进评估）

1. 先 `ls` 确认目标路径存在，不猜
2. 归档后更新文件夹份数标注：`find "$DIR" -maxdepth 1 -type f | wc -l`
3. **美术岗 zip 不许直接用原包**：凡归档压缩包（zip/rar），必须先解压查包内简历 → 脱敏包内简历薪酬 → 重新打包（zip 等价替代，7z 不能建 RAR）→ 再归档。原包原样归档会让薪酬泄漏（os.walk 进不去 zip，闸门扫不到包内）。
4. 简历文件名不得含薪资信息
5. Windows 中文编码：lark-cli 输出写文件再 Read，不在 stdout 硬扛
6. **必跑闸门 `verify_archive.py`（read-only，绝不写盘/删文件，只检测不修复）**：

```
python "C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/verify_archive.py" <简历目录或单个pdf/zip> [--no-cache]
```

一道命令跑三重校验（zip 会被解压到临时目录扫描包内简历 PDF，扫完即删，read-only 契约不破）：
- **数量闸门**：`_N份` 目录标注数 == 实际人头数（按姓名去重，zip 整体算 1 份，排除 temp 暂存目录）。挡住"cp/mv 后静默丢文件"
- **姓名闸门**：按命名规则解析姓名（实习生取第 3 段、正职取第 1 段，剥离【岗位】/简历/作品后缀），token 相等匹配正文署名。挡住"手填 MID 串行错位"
- **薪酬闸门**：归档后不得残留薪酬段。⚠️ 检出薪酬即 🔴 STOP —— 本脚本**只检测不脱敏**，必须由你用 PyMuPDF redact 脱敏后重跑

**增量缓存**：通过的文件（姓名 pass + 无薪酬）会缓存到 `notes/.verified_manifest/`，下次未变动则跳过解析（大目录省一半时间）。安全约束：任何 STOP/⚠️/图片型都不缓存，确保问题文件每次重扫、脱敏后重跑不被旧缓存放行。强制全量扫描加 `--no-cache`。

输出 `🟢 全过 — 可进评估` 才放行；出 `🔴 STOP` 立即修，**不许带错进评估**。图片型 PDF（无文本）或姓名模糊（2 字名疑似他人子串）标 ⚠️ 走人工确认分支，不算失败。

---

## 反模式（不要做）

- **不要信任 triage 的 attachment_count** — 永远是 0，必须用 +message 查
- **不要只查 attachments 就下结论** — attachments 和 body 链接并存，每封都要查 body_html
- **不要把取 URL 和下载拆成两步** — auth code 有时效，原子完成
- **不要假设下载目录** — 真实是 `F:/Users/wuchunbo/Downloads`（F 盘），不是 C 盘的 USERPROFILE
- **不要现场写一次性脚本** — 扫描/下载/核查都用 `scripts/` 里现成的，AI 负责判断不负责重复造轮子
- **不要写规则脚本做岗位匹配** — AI 直接判断比规则可靠
- **不要串行下载** — 能并行就并行
- **不要用 curl 下载链接类附件** — 只拿到 HTML 跳转页，必须用浏览器
- **不要在 stdout 输出中文** — Windows GBK 会乱码，写文件再 Read
- **不要手填 MID 下载** — MID↔文件名的对应必须由脚本数据绑定，不靠肉眼"读 JSON 再手填命令"。错配根因就在这一跳，已用 `verify_archive.py` 姓名闸门兜底：下载后文件名姓名 ∉ 正文即 STOP。
- **不要 cp/mv 后不数文件** — 曾两次静默丢简历都因操作后没对账。`verify_archive.py` 数量闸门已固化此检查：`_N份` 标注 ≠ 实际数即 STOP。
- **姓名对不上先自查下载环节** — 邮件 MID↔投递姓名↔简历内容通常自洽，姓名不符基本是下载填串，不要急着甩锅"投递者填错名"。

---

## 脚本

> 所有脚本都在 **skill 目录** `C:/Users/wuchunbo/.agents/skills/collect-resumes/scripts/` 下，**不在项目根 `F:/miniwanob/scripts/`**（那个目录不存在）。调用时一律用绝对路径，不要写 `scripts/xxx` 相对路径，否则报 `Cannot find module`。

| 脚本 | 用途 |
|------|------|
| `…/scripts/download_attachment.sh` | 单封邮件附件下载（bash 版，Linux/mac） |
| `…/scripts/download_attachment.mjs` | 单封邮件附件下载（**node 版，Windows 用这个**，原子操作） |
| `…/scripts/scan_all.mjs` | 全量扫描邮箱，分页到 has_more=false，排除通知 |
| `…/scripts/verify_mails.mjs` | 逐封核查附件数 + body 链接，防止漏作品 |
| `…/scripts/verify_archive.py` | **归档闸门**（read-only）：数量/姓名/薪酬三重校验，挡 MID 填串 + 静默丢文件 + 薪酬残留 |

> `…/` = `C:/Users/wuchunbo/.agents/skills/collect-resumes`

## 参考文档

按需加载，不要预读：
- `references/job-aliases.md` — 岗位别名映射表（含团队路径）
- `references/archive-naming.md` — 目录结构、文件命名规则
- `references/link-attachments.md` — 链接类附件下载策略
