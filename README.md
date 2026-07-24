# agent-skills

个人自研的 AI Agent Skill 集合 —— 把日常工作流封装成可被 Claude Code、OpenAI Codex、OpenCode 等支持开放 Agent Skill 规范的工具直接调用的 Skill。

这些 Skill 都在真实工作里长期使用、反复打磨，不是 demo。

## 包含的 Skill

| Skill | 用途 | 语言/依赖 |
|-------|------|-----------|
| [**jd-craft**](skills/jd-craft) | JD 质量体检 + 需求澄清问题 + 专业版 JD + BOSS 个性化问候语生成 | 纯文档（prompt 驱动），配合 pandoc |
| [**collect-resumes**](skills/collect-resumes) | 从邮箱扫描简历邮件，按岗位归档到本地文件夹（附件下载 + 链接类附件抓取 + 多邮件合并 + 薪酬脱敏） | Node.js，配合飞书 mail API |
| [**analyze-resumes**](skills/analyze-resumes) | 对归档简历做 AI 4维度评估（方向/硬卡/含金量/风险+加分），产出强推/可推/待定/不推 + 业务推荐摘要，评估完自动发飞书文档 | Python + AI，配合飞书 document_ai |
| [**recruit-followup**](skills/recruit-followup) | 候选人跟进全流程：录入飞书招聘、跟踪表、面试流转、面评同步、每日对账、日报 | 飞书 hire/document_ai/im/base API |
| [**schedule-interview**](skills/schedule-interview) | 面试时间协调：面试官空闲 ∩ 候选人时间，产出可约时段 + 可转发草稿 | Python，配合飞书 calendar/contact API |
| [**interview-guide**](skills/interview-guide) | 面试考核维度问答：照5张评分表出4轮考察重点+定制问题（行为+情境+简历追问） | 纯文档（prompt 驱动） |
| [**candidate-nurture**](skills/candidate-nurture) | 候选人保温+面评催收：读对账预警→产出"今天该碰谁+话术"行动清单 | 纯文档，依赖 _daily_review.json |
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

> 飞书相关的 Skill（collect-resumes、recruit-followup 等）依赖 [lark-cli](https://www.npmjs.com/package/@larksuiteoapi/lark-cli) 或等价的飞书开放平台 API 封装。调 `/open-apis/hire/v1/*` 前先读 `lark-hire` skill；调 calendar/contact 前先读 `lark-calendar-contact` skill。

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

招聘相关 skill（collect-resumes、recruit-followup）需要配置环境变量。复制对应 skill 下的 `.env.example` 为 `.env` 并填入你自己的值：

```bash
cp skills/collect-resumes/.env.example skills/collect-resumes/.env
cp skills/recruit-followup/.env.example skills/recruit-followup/.env
# 然后编辑 .env 填入你的飞书应用凭证、归档路径等
```

**仓库里已经过脱敏处理**，所有 App ID / App Secret / 内部群 ID / 业务表 ID / 内部路径都已替换成占位符。fork 或使用前请填入你自己的值。

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
│   │   │   └── lib/          ← 共享库（manifest/lark_mail/html_links/file_identity...）
│   │   └── tests/
│   ├── analyze-resumes/      ← AI 简历评估（Python + 飞书 document_ai）
│   ├── recruit-followup/     ← 候选人跟进全流程（飞书 hire/im/base）
│   ├── schedule-interview/   ← 面试时间协调（Python + 飞书 calendar/contact）
│   ├── interview-guide/      ← 面试考核维度问答（纯文档）
│   ├── candidate-nurture/    ← 候选人保温+面评催收（纯文档）
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
