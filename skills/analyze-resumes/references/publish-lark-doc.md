# 阶段7：发布飞书文档（评估后必做）

> **第一性原则**：报告躺在 `notes/` 里不算完成——只有到达 HR 的屏幕（飞书）才算闭环。阶段6 分发完本地档位后，**自动**进入阶段7 把报告发到飞书。

> **✅ 实测验证**（2026-07-20）：用 7.20 报告（19KB / 238 行）跑通整条链路——预处理剔除本地噪音 → `drive +import` 秒级返回 `data.url` → `im +messages-send --text url` 成功送达吴春波飞书 IM。表格/emoji/标题层级渲染正常（飞书服务端 GFM 转换器）。

## 流程概览

```
notes/简历评估_{M.DD}.md
   ↓ 预处理（剔除本地噪音）
notes/_发布版_简历评估_{M.DD}.md
   ↓ drive +import
飞书 Docx（云空间根目录）
   ↓ im +messages-send --user-id
吴春波飞书 IM 收到链接
```

---

## 步骤1：发布前预处理（生成"飞书发布版"）

现有 `notes/简历评估_{M.DD}.md` 是为本地阅读优化的，直接 import 会带本地噪音（绝对路径、归档说明、内部执行信息）。派生一份"飞书发布版"：

**输入**：`notes/简历评估_{M.DD}.md`
**输出**：`notes/_发布版_简历评估_{M.DD}.md`（`_` 前缀表示临时派生文件，不进入评估档案）

**4 项必做处理**：

| # | 处理 | 理由 |
|---|------|------|
| 1 | **删除「## 归档说明」整节**（从 `## 归档说明` 到文件尾的下一个 `## ` 或 EOF） | 本地执行信息（manifest 状态、脱敏记录、闸门阻断），飞书读者无意义 |
| 2 | **删除「## 需人工看原文件」里的本地路径**，只保留姓名 + issue 描述 | 飞书用户打不开 `F:/miniwanob/...`；需要看原件的，让 HR 找 Agent 要 |
| 3 | **检查待定Action清单/正文里有无本地路径**（grep `F:/` `C:/`），有就删除整行或改写 | 同上 |
| 4 | **转义破坏 GFM 表格渲染的字符**：表格 cell 里的 `|` → `\|`；行内代码外的 `<`/`>` 视情况改写 | 飞书 import 用 GFM 解析，裸 `|` 会被当列分隔符 |

**预处理示例**（Python 脚本片段，主会话直接跑或手动处理都可）：

```python
import re
with open('notes/简历评估_7.20.md', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 删除「## 归档说明」整节
content = re.sub(r'## 归档说明.*?(?=\n## |\Z)', '', content, flags=re.DOTALL)

# 2. 删除本地路径行
content = re.sub(r'.*路径：`?[A-Z]:/[^`\s]*`?.*\n?', '', content)

# 3. 转义表格 cell 里的 |
# （简单做法：只处理以 | 开头的表格行，cell 内的 | 转义）
# 注意不要破坏表格结构本身

with open('notes/_发布版_简历评估_7.20.md', 'w', encoding='utf-8') as f:
    f.write(content)
```

> 预处理用主会话直接做（Read 原文 → 派生新文件），**不写一次性脚本**——和 skill 一贯风格一致。

---

## 步骤2：鉴权检查

发文档需要 `drive:drive`（导入）+ `im:message`（发消息）权限。先查状态：

```bash
LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 \
  lark-cli auth status --json --verify
```

看 `identities.user.scope` 里是否包含 `drive:drive`、`im:message` 等关键字。
> **scope 字段是空格分隔的字符串**（不是数组），Python 直接 `if 'drive' in scopes_str` 判断即可，不要 `for s in scopes`（会把字符串拆成字符）。

如果缺权限，按 lark-shared 的 domain 模式授权：

```bash
# 缺权限就走 split-flow（AGENTS.md 铁律：不在同一轮阻塞）
lark-cli auth login --domain drive --domain im --no-wait --json
```

拿到 `verification_url` 后**展示给用户**（`lark-cli auth qrcode <url> --output qrcode.png` 生成二维码），告诉用户"授权完成后回来告诉我"。用户确认后再继续。

---

## 步骤3：导入飞书 Docx（核心命令）

> ⚠️ **lark-shared 铁律**：`--file` **只接受相对路径**。必须先 `cd notes/` 或把命令的 cwd 设到 notes/。

```bash
cd F:/miniwanob/notes && \
LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 \
  MSYS_NO_PATHCONV=1 \
  lark-cli drive +import \
  --file "./_发布版_简历评估_{M.DD}.md" \
  --type docx \
  --name "简历评估 {M.DD} · {N}人"
```

**返回字段处理**（2026-07-20 实测验证）：

成功时返回结构（不需要轮询续查，19KB 文件秒级完成）：
```json
{
  "ok": true,
  "identity": "user",
  "data": {
    "job_status": 0,
    "job_status_label": "success",
    "ready": true,
    "ticket": "7664570168756079575",
    "token": "OTyldmSyeoFYFOxhMekce3SUnnh",
    "type": "docx",
    "url": "https://mini1.feishu.cn/docx/OTyldmSyeoFYFOxhMekce3SUnnh"
  }
}
```

**取 url 字段**：`data.url`（不是 `result.url`，实测字段直接在 `data` 下）。

| 返回情况 | 字段 | 处理 |
|---------|------|------|
| 成功 | `data.ready=true` + `data.url` + `data.token` | 直接拿 `data.url` 进步骤4 |
| 超时但任务在跑 | `data.ready=false` + `data.timed_out=true` + `data.ticket` + `next_command` | 复制 `next_command` 执行续查：`lark-cli drive +task_result --scenario import --ticket <TICKET>` |
| 失败 | `data.job_status≠0` + `data.job_error_msg` | 走兜底（步骤5） |

**两个必加的环境变量**（AGENTS.md Shell 铁律）：
- `MSYS_NO_PATHCONV=1`：git-bash 会吞 `/` 前导路径（把 `./` 之类误转），不加会 404
- `LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1`：抑制 `_notice` 干扰 JSON 解析

**import 是写操作但不是高风险**（不会触发 exit 10 门禁，不需要 `--yes`）——用户触发 analyze-resumes 已表达意图。

---

## 步骤4：发 IM 消息给吴春波

```bash
LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 \
  MSYS_NO_PATHCONV=1 \
  lark-cli im +messages-send \
  --user-id "ou_18a4ddb7512560aea7d7a30cf824a516" \
  --text "{doc_url}"
```

**关键点**（AGENTS.md 飞书铁律第1条）：
- **用 `--text` 不用 `--markdown`**：纯 URL 在 `--text` 里飞书会自动渲染成文档卡片（可预览标题）；`--markdown` 会把 URL 当文本、且对 GFM 不完整支持
- **链接单独一条消息**：URL 前后有任何文字都会降级为纯文本，失去卡片预览
- `ou_18a4ddb7512560aea7d7a30cf824a516` 是吴春波 open_id（AGENTS.md 关键路径表已固化）

发完想补一句说明？**发完纯链接后再单独发一条文字消息**（两条 IM，不要合并）。

**返回字段**（2026-07-20 实测验证）：
```json
{
  "ok": true,
  "identity": "user",
  "data": {
    "chat_id": "oc_fa200f44f78267a3ac4a1327d102a8a3",   // P2P 会话 ID
    "create_time": "2026-07-20 19:27:02",
    "message_id": "om_x100b6ad42eab2ca0b1ff30ab4a6144d"  // 消息 ID
  }
}
```
判断成功：`ok=true` 即发送成功。

---

## 步骤5：兜底（失败降级）

| 失败点 | 兜底动作 |
|--------|---------|
| import 失败（格式/大小/权限） | 用 `im +messages-send` 发文字："飞书发布失败（原因：{err}），本地报告：F:/miniwanob/notes/简历评估_{M.DD}.md" |
| task_result 续查超过 3 次仍未 ready | 同上，发本地路径 |
| im 发消息失败 | 终端打印 url 让用户手动复制；继续完成 skill 的其他收尾（不阻塞） |

**不重试 import 超过 2 次**——失败大概率是格式问题，重试无用，走兜底。

---

## 完成后告诉用户

终端最后一行必须包含：
1. **飞书文档链接**（已发到你 IM，点开可看）
2. **本地 md 路径**（兜底/完整版，含本地执行细节）
3. **发布版 md 路径**（已 import 的那份，便于复跑）

示例输出：
```
✅ 飞书文档已发布：https://xxx.feishu.cn/docx/XXXXXX
📩 链接已发到你飞书 IM
📁 本地完整报告：notes/简历评估_7.20.md
📁 发布版（已 import）：notes/_发布版_简历评估_7.20.md
```

---

## 反模式

- **不要直接 import 原始 md** — 必须先预处理剔除本地路径和归档说明，否则飞书文档里会出现 `F:/miniwanob/...` 这种用户打不开的噪音
- **不要用 `docs +create --doc-format markdown`** — 表格渲染不如 `drive +import` 稳（飞书服务端转换器更可靠）
- **不要用 `lark-markdown`** — 它建的是 Drive 里的 .md 文件，不是可渲染表格的 Docx
- **不要把链接和文字合并发** — 飞书只在消息体纯 URL 时渲染卡片，混文字会降级纯文本（AGENTS.md 飞书铁律第1条）
- **不要跳过鉴权检查直接调** — 缺 drive 域会报 permission_violations，浪费一轮调用
- **不要写一次性脚本封装 lark-cli** — 主会话直接调命令即可，和 skill 一贯风格一致

---

## 参考

- [`../lark-shared/SKILL.md`](../../lark-shared/SKILL.md) — 认证、路径相对性约束、高风险审批协议
- [`../lark-drive/SKILL.md`](../../lark-drive/SKILL.md) + [`lark-drive-import.md`](../../lark-drive/references/lark-drive-import.md) — import 命令细节、返回字段、续查
- [`../lark-im/SKILL.md`](../../lark-im/SKILL.md) + `+messages-send` — IM 发消息
- AGENTS.md「飞书操作铁律」— 纯链接单独发、`--text` 不用 `--markdown`、`MSYS_NO_PATHCONV=1`
