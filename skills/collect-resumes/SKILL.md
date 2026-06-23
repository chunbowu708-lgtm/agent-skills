---
name: collect-resumes
description: >
  从邮箱扫描简历邮件，按岗位归档到本地文件夹。
  触发词：收简历、整理简历、处理简历、下载简历、分类简历。
  只要用户提到简历、邮箱、候选人、作品集、归档，就使用这个skill。
  覆盖：标准附件下载、链接类附件（QQ超大附件/云盘）、美术岗作品集打包、多附件合并。
  不覆盖：Bitable写入（按需执行）、招聘平台打招呼。
  依赖：lark-cli（mail 域已授权，权限已存本地，不查 auth）、Playwright MCP（链接类附件下载，CDP Proxy 做补充）。
---

# 简历收集与归档

## 配置

所有路径和凭证走环境变量，见 [`.env.example`](.env.example)。下文用 `$LARK_CLI_PATH` / `$ARCHIVE_ROOT` / `$DOWNLOAD_DIR` 指代。

| 项 | 来源 |
|---|---|
| 归档根目录 | `$ARCHIVE_ROOT`（如 `~/Documents/resumes`） |
| 用户下载目录 | `$DOWNLOAD_DIR`（留空则 `~/Downloads`） |
| lark-cli | `$LARK_CLI_PATH`，mail 域已授权，不查 auth |
| node | 已装 v20+，用于跑脚本（bash 在 Windows cmd 不可用，统一用 node） |

---

## 阶段1：扫描（只读）

### 1. 拉取邮件 — 穷尽式，不要抽样

**用现成脚本一次性翻完，不要手动 max 200 翻一页就以为扫完了：**

```
node scripts/scan_all.mjs
```

脚本会分页直到 `has_more=false`（全量，不靠 subject 猜），排除通知关键词，把所有候选邮件列出来。输出存 `notes/_scan_all.json`（路径可由 `$SCAN_OUTPUT` 覆盖）。

> 为什么用脚本：分页 + 排除 + 去重是机械活，每次手敲 max 值会漏。

筛选今天日期的邮件。排除关键词（招聘通知类）：员工关爱、面试邀约、日程提醒、系统通知、奋斗食代（按你公司的实际通知类邮件调整 `scan_all.mjs` 里的 `notif` 正则）。

招聘平台邮件 subject 格式示例：`{姓名} | {经验}，应聘 {岗位} | {城市}{薪资}【招聘平台名】`
- 从 subject 提取姓名、工作年限、岗位名
- 直投邮件（非招聘平台）subject 不规则，姓名和岗位需手动判断

### 2. 逐封核查附件 + body 链接（每封都要查，不管附件数是几）

**用现成脚本，不要手动一封封敲：**

```
node scripts/verify_mails.mjs
```

脚本对每封邮件同时查 `attachments`（标准附件）和 `body_html`（链接类附件），输出清单。

> ⚠️ 铁律：**每封都查 body_html，不管 attachments 数量是几。**
> attachments 和 body 链接是**并存**的，不是二选一。常见踩坑：某封 attachments=1（简历docx），就以为没作品了，结果作品是 QQ 超大附件，藏在 body_html 链接里。

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
- 招聘平台简历 + 直投作品 → 合并
- 招聘平台简历 + QQ/163 作品集 → 合并
- 展示时附件类型全部列出

### 5. 对账本地

```bash
find <归档根目录> -name "*姓名*" -type f
```

### 6. 输出给用户确认

**固定格式，按岗位分组：**

```
【产品经理实习生】15 份
  候选人A 28届 平台投递 1PDF     [未归档]
  候选人B 27届 平台投递 1PDF     [未归档]
  ...

【特效设计师】2 人
  候选人C 3年 平台+QQ 1PDF+作品rar(260MB)  [未归档]
  候选人D 直投 163 作品zip(148MB)           [待确认岗位]

【服务端开发】2 份
  候选人E 5年 平台投递 1PDF  [未归档]
  候选人F 8年 平台投递 1PDF  [未归档]
```

每行：`姓名 | 工作年限/届 | 来源 | 附件情况 | 状态`

---

## 阶段2：下载与归档

用户确认后执行。

### 标准附件 — 原子下载（取URL+下载必须在一次操作里）

**用现成脚本（bash 在 Windows cmd 不可用，统一用 node）：**

```
node scripts/download_attachment.mjs <MID> <输出路径> [附件序号，默认0]
```

脚本内部：取 ATT_ID → 取 download_url → https.get 下载，**一个进程内完成**。

> ⚠️ 铁律：**取 URL 和下载必须原子完成，不能分两步。**
> 邮件 download_url 的 auth code 有时效（几十秒），先取 URL 存下来、过会儿再 curl，会得到 0 字节空文件。

多封邮件 → 多个 Bash 并行发出，不串行等。

### 链接类附件

见 `references/link-attachments.md`。Playwright MCP 优先，CDP Proxy 补充。

### 归档检查点

1. 先 `ls` 确认目标路径存在，不猜
2. 归档后更新文件夹份数标注：`find "$DIR" -maxdepth 1 -type f | wc -l`
3. 美术岗规范 zip 直接用，不拆包
4. 简历文件名不得含薪资信息
5. Windows 中文编码：lark-cli 输出写文件再 Read，不在 stdout 硬扛

---

## 反模式（不要做）

- **不要信任 triage 的 attachment_count** — 永远是 0，必须用 +message 查
- **不要只查 attachments 就下结论** — attachments 和 body 链接并存，每封都要查 body_html
- **不要把取 URL 和下载拆成两步** — auth code 有时效，原子完成
- **不要假设下载目录** — 读 `$DOWNLOAD_DIR`（或系统默认 `~/Downloads`），不要硬编码
- **不要现场写一次性脚本** — 扫描/下载/核查都用 `scripts/` 里现成的，AI 负责判断不负责重复造轮子
- **不要写规则脚本做岗位匹配** — AI 直接判断比规则可靠
- **不要串行下载** — 能并行就并行
- **不要用 curl 下载链接类附件** — 只拿到 HTML 跳转页，必须用浏览器
- **不要在 stdout 输出中文** — Windows GBK 会乱码，写文件再 Read

---

## 脚本

| 脚本 | 用途 |
|------|------|
| `scripts/download_attachment.sh` | 单封邮件附件下载（bash 版，Linux/mac） |
| `scripts/download_attachment.mjs` | 单封邮件附件下载（**node 版，Windows 用这个**，原子操作） |
| `scripts/scan_all.mjs` | 全量扫描邮箱，分页到 has_more=false，排除通知 |
| `scripts/verify_mails.mjs` | 逐封核查附件数 + body 链接，防止漏作品 |

脚本从 `.env` 读取 `LARK_CLI_PATH` / `ARCHIVE_ROOT` 等，不读不进 git 的本地值。

## 参考文档

按需加载，不要预读：
- `references/job-aliases.md` — 岗位别名映射表（含团队路径），按你公司实际情况改
- `references/archive-naming.md` — 目录结构、文件命名规则
- `references/link-attachments.md` — 链接类附件下载策略
