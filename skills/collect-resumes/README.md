# collect-resumes

从邮箱扫描简历邮件，按岗位归档到本地文件夹。

## 依赖

| 依赖 | 用途 |
|------|------|
| [lark-cli](https://www.npmjs.com/package/@larksuiteoapi/lark-cli) 或等价的邮件 API 封装 | 邮件 API（mail 域已授权） |
| Playwright MCP | 链接类附件下载主力（Claude 插件 / 或直接调 Playwright） |
| CDP Proxy | Playwright 不可用时的补充（默认 `localhost:3456`） |
| Node.js v20+ | 跑 `scripts/*.mjs` |
| Bash（可选） | Git Bash / MSYS2，仅 `download_attachment.sh` 用 |

## 配置

复制 `.env.example` 为 `.env`，填入你的值：

```bash
cp .env.example .env
```

关键配置项：

- `LARK_CLI_PATH` — lark-cli 可执行文件路径
- `ARCHIVE_ROOT` — 简历归档根目录
- `DOWNLOAD_DIR` — 浏览器默认下载目录（留空则用 `~/Downloads`）

## 目录结构

```
collect-resumes/
├── SKILL.md                  ← Agent 入口：流程 + 检查点
├── README.md                 ← 本文件：人类维护文档
├── .env.example              ← 配置模板
├── references/
│   ├── job-aliases.md        ← 岗位别名映射表（改成你公司的）
│   ├── archive-naming.md     ← 目录结构、文件命名规则
│   └── link-attachments.md   ← 链接类附件下载策略
└── scripts/
    ├── scan_all.mjs          ← 全量扫描邮箱（node）
    ├── verify_mails.mjs      ← 逐封核查附件 + body 链接（node）
    ├── download_attachment.mjs ← 单封附件下载（node，原子操作）
    └── download_attachment.sh  ← 单封附件下载（bash，Linux/mac）
```

## 归档目录组织

在 `$ARCHIVE_ROOT` 下按 `{项目团队}/{岗位名}/已收集简历/{M.DD}_{N}份/` 组织。
详细规则见 `references/archive-naming.md`。

## 岗位别名维护

新增岗位时，编辑 `references/job-aliases.md` 加一行。

## 作者

chunbowu708-lgtm
