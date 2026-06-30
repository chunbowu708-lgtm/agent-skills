# agent-skills

个人自研的 AI Agent Skill 集合 —— 把日常工作流封装成可被 Claude Code、OpenAI Codex、OpenCode 等支持开放 Agent Skill 规范的工具直接调用的 Skill。

这些 Skill 都在真实工作里长期使用、反复打磨，不是 demo。

## 包含的 Skill

| Skill | 用途 | 语言/依赖 |
|-------|------|-----------|
| [**collect-resumes**](skills/collect-resumes) | 从邮箱扫描简历邮件，按岗位归档到本地文件夹（附件下载 + 链接类附件抓取 + 多邮件合并 + 薪酬脱敏） | Node.js，配合飞书 mail API |
| [**analyze-resumes**](skills/analyze-resumes) | 对归档简历做 AI 5维评估，产出强推/可推/待定/不推四档判定 + 业务推荐摘要 | Python + AI，配合飞书 document_ai |
| [**recruit-followup**](skills/recruit-followup) | 招聘跟进全流程：候选人录入飞书招聘、邀约信号扫描、面评同步、跟踪表自动更新、每日对账 | 飞书 hire/document_ai/im/base API |
| [**schedule-interview**](skills/schedule-interview) | 面试时间协调：批量查面试官空闲，和候选人给定时间求交集，产出可约时段 + 可转发给面试官的确认草稿 | Python，配合飞书 calendar/contact API |
| [**neat-freak**](skills/neat-freak) | 会话收尾时对项目文档和 Agent 记忆做"洁癖级"审查与同步，跨平台（Claude Code / Codex / OpenCode / OpenClaw） | 纯文档，无依赖 |
| [**storage-analyzer**](skills/storage-analyzer) | 只读扫描磁盘占用，生成交互式 HTML 报告，支持网页上一键清理（移废纸篓/直接删），macOS + Windows | Python 3 标准库，零第三方依赖 |

> 飞书相关的 Skill（collect-resumes、recruit-followup）依赖 [lark-cli](https://www.npmjs.com/package/@larksuiteoapi/lark-cli) 或等价的飞书开放平台 API 封装。

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
└── skills/
    ├── collect-resumes/      ← 收简历+归档（Node.js + 飞书 mail）
    │   ├── SKILL.md
    │   ├── README.md
    │   ├── .env.example
    │   ├── references/
    │   └── scripts/
    ├── analyze-resumes/      ← AI 简历评估（Python + 飞书 document_ai）
    │   ├── SKILL.md
    │   ├── .env.example
    │   ├── references/
    │   └── scripts/
    ├── recruit-followup/     ← 候选人跟进全流程（飞书 hire/im/base）
    │   ├── SKILL.md
    │   ├── .env.example
    │   └── scripts/
    ├── schedule-interview/   ← 面试时间协调（Python + 飞书 calendar/contact）
    │   ├── SKILL.md
    │   └── scripts/
    ├── neat-freak/           ← 文档/记忆洁癖审查（纯文档）
    │   ├── SKILL.md
    │   └── references/
    └── storage-analyzer/     ← 磁盘占用分析（Python 标准库）
        ├── SKILL.md
        ├── references/
        ├── scripts/
        └── assets/
```

## Skill 规范

每个 Skill 遵循开放 Agent Skill 规范：

- 顶层必须有 `SKILL.md`，文件头是 YAML front matter（`name` + `description`），`description` 里写清楚触发词和覆盖范围，Agent 靠这个判断何时调用
- `references/` 放按需加载的详细参考（Agent 不会预读，需要时才查）
- `scripts/` 放可重复执行的辅助脚本
- 人类维护文档放 `README.md`，和 `SKILL.md`（给 Agent 看）分开

## License

[MIT](LICENSE) — 随便用，欢迎 PR。
