# agent-skills

个人自研的 AI Agent Skill 集合 —— 把日常工作流封装成可被 Claude Code、OpenAI Codex、OpenCode 等支持开放 Agent Skill 规范的工具直接调用的 Skill。

这些 Skill 都在真实工作里长期使用、反复打磨，不是 demo。

## 包含的 Skill

| Skill | 用途 | 语言/依赖 |
|-------|------|-----------|
| [**jd-craft**](skills/jd-craft) | JD 质量体检 + 需求澄清问题 + 专业版 JD + BOSS 个性化问候语生成 | 纯文档（prompt 驱动），配合 pandoc |
| [**collect-resumes**](skills/collect-resumes) | 从邮箱扫描简历邮件，按岗位归档到本地文件夹（附件下载 + 链接类附件抓取 + 多邮件合并 + 薪酬脱敏） | Node.js，配合飞书 mail API |
| [**analyze-resumes**](skills/analyze-resumes) | 对归档简历做 AI 4维度评估（方向/硬卡/含金量/风险+加分），产出强推/可推/待定/不推四档判定 + 业务推荐摘要 | Python + AI，配合飞书 document_ai |
| [**daily-recruit-report**](skills/daily-recruit-report) | 每日招聘晨报：每日对账→LLM判读→作战清单→决策落盘，接棒 candidate-nurture | 飞书 hire/im/base API |
| [**candidate-entry**](skills/candidate-entry) | 候选人录入飞书招聘：一条命令录人才→建投递→闸门自校验 | 飞书 hire API（notes/_hire.py） |
| [**schedule-interview**](skills/schedule-interview) | 面试时间协调：批量查面试官空闲，和候选人给定时间求交集，产出可约时段 + 可转发给面试官的确认草稿 | Python，配合飞书 calendar/contact API |
| [**interview-guide**](skills/interview-guide) | 面试考核维度问答：照公司5张评分表出4轮考察重点+定制问题（行为+情境+简历薄弱点追问） | 纯文档（prompt 驱动） |
| [**candidate-nurture**](skills/candidate-nurture) | 候选人保温+面评催收：读对账预警+信号判读+保温状态→产出"今天该碰谁+话术+升级标注"行动清单，触达状态跨天延续（话术自动升级/去重/终止提示） | Python 脚本（`nurture_state.py`），依赖 `_daily_review.json` + `_signals.json` |
| [**talent-profile**](skills/talent-profile) | 候选人匹配覆盖图：JD要求×候选人矩阵，✅/⚠️/❌覆盖度（不打分），横向对比谁缺哪块 | Python，输出 HTML |
| [**pipeline-dashboard**](skills/pipeline-dashboard) | 招聘管道看板：岗位×阶段漏斗+停滞预警+转化率，HTML可视化（多维表格当DB） | Python，输出 HTML |

### 飞书 API 契约层 Skill

招聘主线 skill 按需调用这些契约层 skill，不重复造 API 封装：

| Skill | 用途 |
|-------|------|
| [**lark-hire**](skills/lark-hire) | 飞书招聘 `/open-apis/hire/v1/*` API 契约（接口/字段/枚举/错误码权威源） |
| [**lark-calendar-contact**](skills/lark-calendar-contact) | 飞书日历 + 通讯录 API 契约（freebusy 反推空闲） |
| [**lark-shared**](skills/lark-shared) | 飞书鉴权 + lark-cli 封装契约（鉴权/CLI/易错点） |
| [**lark-mail**](skills/lark-mail) | 飞书邮箱 API 契约 |

> 飞书相关的 Skill（collect-resumes、daily-recruit-report、candidate-entry 等）依赖 [lark-cli](https://www.npmjs.com/package/@larksuiteoapi/lark-cli) 或等价的飞书开放平台 API 封装。调 `/open-apis/hire/v1/*` 前先读 `lark-hire` skill；调 calendar/contact 前先读 `lark-calendar-contact` skill。

### 通用工具 Skill

| Skill | 用途 | 语言/依赖 |
|-------|------|-----------|
| [**neat-freak**](skills/neat-freak) | 会话收尾时对项目文档和 Agent 记忆做"洁癖级"审查与同步，跨平台（Claude Code / Codex / OpenCode / OpenClaw） | 纯文档，无依赖 |
| [**storage-analyzer**](skills/storage-analyzer) | 只读扫描磁盘占用，生成交互式 HTML 报告，支持网页上一键清理（移废纸篓/直接删），macOS + Windows | Python 3 标准库，零第三方依赖 |

## 设计理念

- **Agent 驱动，脚本兜底**：机械活（分页、下载、去重）用现成脚本一次跑完；判断活（岗位匹配、分级决策）交给 AI。不重复造轮子。
- **铁律写在 SKILL.md 里**：每个 Skill 都把"不要做什么"（踩过的坑）明确列出来，防止下次 Agent 重复踩。
- **安全优先**：storage-analyzer 全程只读，删除命令只展示不执行；涉及密钥一律走环境变量，代码里不硬编码。

## 安装

### 方式一：Claude Code / Codex / OpenCode（开放 Agent Skill 规范）

把对应 skill 目录整个复制（或软链）到你 agent 的 skills 目录：

```bash
# Claude Code
git clone https://github.com/chunbowu708-lgtm/agent-skills.git
cp -r agent-skills/skills/<skill-name> ~/.claude/skills/

# Codex / 其他遵循 AGENTS.md 规范的工具
cp -r agent-skills/skills/<skill-name> ~/.agents/skills/
```

复制后，Agent 会根据 SKILL.md 里的 `description` 自动在合适的场景调用。

### 方式二：仅参考流程

每个 skill 的 `SKILL.md` 都是自洽的流程文档。即使不用 Agent，照着里面写的步骤手动操作也能完成对应任务。

## 配置

**凭证一律走环境变量，代码里不硬编码。** 使用前按各 skill 的 `SKILL.md` 说明设置：

- `LARK_CLI_PATH`：lark-cli 可执行文件路径（Windows 下是 `.../lark-cli.cmd`），不设则用 PATH 里的 `lark-cli`
- `PROJECT_ROOT`：你的项目根目录（`notes/`、`data/` 的上级），不设则用当前工作目录
- 招聘表 ID（`TRACKING_BASE_TOKEN` / `TRACKING_TABLE_ID` / `CANDIDATE_TABLE_ID` 等）：见 `daily-recruit-report/references/lark-cli-base-commands.md`

**仓库里已经过脱敏处理**，所有 App ID / App Secret / 内部群 ID / 业务表 ID / 内部路径都已替换成占位符或环境变量读法。fork 或使用前请填入你自己的值。

## 目录结构

```
agent-skills/
├── README.md                 ← 本文件
├── LICENSE                   ← MIT
├── CONTRIBUTING.md           ← 贡献指南
├── .gitignore
├── skills/                   ← Skill 集合
│   ├── collect-resumes/      ← 收简历+归档（Node.js + 飞书 mail）
│   │   ├── SKILL.md
│   │   ├── references/
│   │   ├── scripts/          ← scan_all/verify_mails/download/redact_salary/verify_archive...
│   │   │   └── lib/          ← 共享模块（路径常量、重试、manifest 等）
│   │   └── tests/
│   ├── analyze-resumes/      ← AI 简历评估（Python + 飞书 document_ai）
│   ├── daily-recruit-report/   ← 每日招聘晨报（飞书 hire/im/base）
│   ├── candidate-entry/        ← 候选人录入（飞书 hire）
│   ├── schedule-interview/   ← 面试时间协调（Python + 飞书 calendar/contact）
│   ├── jd-craft/             ← JD 质量体检（纯文档）
│   ├── interview-guide/      ← 面试考核维度问答（纯文档）
│   ├── candidate-nurture/    ← 候选人保温+面评催收（Python 保温状态机）
│   ├── talent-profile/       ← 候选人匹配覆盖图（Python）
│   ├── pipeline-dashboard/   ← 招聘管道看板（Python）
│   ├── lark-hire/            ← 飞书招聘 API 契约层
│   ├── lark-calendar-contact/← 飞书日历+通讯录 API 契约层
│   ├── lark-shared/          ← 飞书鉴权+CLI 契约层
│   ├── lark-mail/            ← 飞书邮箱 API 契约层
│   ├── neat-freak/           ← 文档/记忆洁癖审查（纯文档）
│   └── storage-analyzer/     ← 磁盘占用分析（Python 标准库）
└── notes/                    ← 招聘主线 skill 共享的核心脚本（数据文件已 gitignore）
    ├── _lark_shared.py       ← 飞书鉴权 + lark-cli 封装（被所有脚本依赖）
    ├── _hire.py              ← 候选人录入飞书招聘
    ├── _daily_review.py      ← 每日对账（ATS→跟踪表同步）
    ├── _download_chat_file.py← 群聊文件下载
    ├── _sync_tables.py       ← 表格定时同步
    ├── refresh_my_jobs.py    ← 岗位范围刷新
    └── setup_daily_sync.ps1  ← 定时任务安装
```

## Skill 规范

每个 Skill 遵循开放 Agent Skill 规范：

- 顶层必须有 `SKILL.md`，文件头是 YAML front matter（`name` + `description`），`description` 里写清楚触发词和覆盖范围，Agent 靠这个判断何时调用
- `references/` 放按需加载的详细参考（Agent 不会预读，需要时才查）
- `scripts/` 放可重复执行的辅助脚本
- 人类维护文档放 `README.md`，和 `SKILL.md`（给 Agent 看）分开

## License

[MIT](LICENSE) — 随便用，欢迎 PR。
