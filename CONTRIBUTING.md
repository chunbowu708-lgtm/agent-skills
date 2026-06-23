# 贡献指南

欢迎提 Issue 和 PR。

## 提 Issue 前

先确认：

- 是 Bug 还是改进建议？标题写清楚 `[BUG]` / `[IMPROVEMENT]`。
- 复现步骤：用的哪个 Skill、哪个 Agent（Claude Code / Codex / OpenCode）、什么命令、报什么错。
- 贴日志前**先脱敏**——你的 App Secret、内部路径、真实候选人信息都别贴。

## 提 PR 前

### 1. 脱敏检查（硬性要求）

仓库里**不能出现任何真实凭证或内部信息**。提交前自查：

| 类型 | 示例 | 处理 |
|------|------|------|
| App ID / App Secret | `cli_xxxxxxxxxxxxxxxx`、长串密钥 | 用 `.env` + `process.env` 读取，`.env.example` 里放占位符 |
| 内部群 chat_id | `oc_xxxxxxxxxxxxxxx` | 用 `.env` 占位符 |
| 业务表 base/table ID | `KRAQxxxxxxxxx` / `tblxxxxxxxxx` | 用 `.env` 占位符 |
| 个人/公司绝对路径 | `C:\Users\zhangsan\...`、`/Users/lisi/...` | 用 `$HOME`、`os.path.expanduser("~")`、`process.env.HOME` 之类 |
| 真实人名 / 内部团队名 | `张三`、`XX事业部` | 改成 `Alice`、`Team A` 之类占位 |

```bash
# 提交前跑一遍，命中就回去改
grep -rnE "cli_[a-zA-Z0-9]{16}|oc_[a-f0-9]{20}|KRAQ[A-Za-z0-9]+|tbl[A-Za-z0-9]{14}" skills/
```

### 2. Skill 结构规范

- 顶层必须有 `SKILL.md`，YAML front matter 里写清 `name` 和 `description`（触发词 + 覆盖范围）
- 给 Agent 看的流程写 `SKILL.md`；给人看的维护文档写 `README.md`
- 大段参考资料放 `references/`，让 Agent 按需加载，不要全塞进 SKILL.md
- 可复用脚本放 `scripts/`

### 3. SKILL.md 风格

- **铁律用 ⚠️ 标注**，踩过的坑要写进"反模式"小节，防止下一个 Agent 重复踩
- 命令示例要可直接复制运行，不能是伪代码
- 用绝对时间（`2026-06-15`），不用相对时间（"今天"、"最近"）

### 4. 提交信息

```
<type>(<skill>): <subject>

type: feat / fix / docs / refactor / chore
```

示例：`feat(recruit-followup): 支持 combined_create 附件自动解析`

## License

提交即表示你同意以 [MIT](LICENSE) 协议发布你的贡献。
