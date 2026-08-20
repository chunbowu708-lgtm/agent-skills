---
name: candidate-entry
description: >
  候选人录入 skill。业务邀约后，一条命令把候选人录进飞书招聘（录人才 → 建投递 → 自校验）。
  触发词：录入候选人、把XX录进飞书招聘、建档、录ATS。
  不覆盖：每日对账（daily-recruit-report）、简历收集归档（collect-resumes）、保温（candidate-nurture）。
---

# 候选人录入

> 修改本文件前必读 `docs/skill-doc-standard.md`（冷清单铁律）：不加时间戳/人名/案例/戏剧性措辞；why 进 references/decisions.md，考古进 CHANGELOG.md。

## 最高原则

1. **录入是原子操作。** 一条命令完成 录人才 → 建投递 → 两段校验，全部内联进 `_hire.py`。
2. **ATS 是唯一事实源。** 录入结果以飞书招聘为准。
3. **该脚本的地方脚本。** 不另写解析/核对脚本，现造脚本 = 现造 bug。
4. **同人性优先。** 一个真人只允许一个 talent 档案；重复录入的唯一形态是档案分裂。
5. **不通过先问。** 判定录错岗 / 需 terminate 时先实查、报告用户。

> 规则背后的 why 见 [`references/decisions.md`](references/decisions.md)（维护者参考，日常执行不看）。

---

## 配置

| 项 | 值 |
|---|---|
| 录入脚本 | `notes/_hire.py`（录入+建投递+校验一条龙） |
| 换最新简历 | `<skill>/scripts/swap_resume.py`（存量 talent 投递绑旧版时，terminate+重建换绑） |
| 共享库 | `notes/_lark_shared.py` |
| 人才索引缓存 | `notes/_talents_cache.json`（TTL 24h，miss 时 fork 后台构建） |
| 简历落点 | `Downloads 目录`（先查），兜底 `data/在招岗位候选人管理/` |

`<skill>` = `…/candidate-entry`

全局常量（Base ID / 表 ID / 应用 ID / lark-cli 路径）统一在 AGENTS.md 关键路径表维护，本表不重复。

---

## 执行

### 单 / 同岗批量录入

```bash
python notes/_hire.py --by-name 白向庭,李毅 --job 海外游戏数据产品经理
# 录人才 → 建投递 → 两段闸门校验，一条命令全做完
```

- 闸门未过打印 `🔴 STOP` 并退出码 1 —— 不许继续后续约面。`--no-verify` 仅调试用。
- hire 域 API 走 `_lark_shared.hire_api` 直连，勿用 lark-cli subprocess 调 hire 接口。
- `--by-name` 三级级联查简历：① 本地归档库（zip 整包自动提取本体，幂等）② Downloads ③ 飞书群聊/私聊自动搜+下载。

### 跨岗位 / 批量清单录入

```bash
python notes/_hire.py --jobs 关键词        # 查 job_code
# 写 notes/_hire_list.txt（每行：简历路径|岗位编号|姓名）
python notes/_hire.py notes/_hire_list.txt --list
```

### 录入铁律（细节）

完整铁律 → [`references/entry-rules.md`](references/entry-rules.md)（岗位准入闸门 / job_code 自查 / 人才预热 / 同名岗位放宽 / 多人异岗拆分 / 解析质量防线 / 附件选择 / 同人性三层 / 预检零翻页 / 存量旧简历处理）。

要点速记：
- 岗位准入：目标岗必须「我创建 + 开放中」，脚本内置 `job_filter_ok()` 校验。
- job_code 自查：用 `--jobs 关键词`，绝不手撸 jobs API（page_size>20 返回空）。
- 多人异岗禁止共用一个 `--job`。
- 判断"录错岗"先实查 applications（`exists=1002206` 是正确岗位早存在，非录错）。
- 附件只传简历本体（pdf/docx/doc），禁整包；作品集 PDF/PPT 可传。
- talent_id 是对账主键；同人性预检三分支（新建 / 复用 / 歧义拦截）。
- 闸门校验（`verify_person` 人才对账+投递对账两段）已内联 `_hire.py`，录入时自动执行。

---

## 面评与终止

**接口/字段/错误码契约**：统一在 [`../lark-hire/SKILL.md`](../lark-hire/SKILL.md)（飞书招聘 OpenAPI 契约层）。

- **terminate 是唯一淘汰路径**（为什么见 [`references/decisions.md`](references/decisions.md)）。
- 不通过先问用户：conclusion=2 仍 active → 报告确认 → 确认后才 terminate。
- 改投递到新岗（录错岗需转移）：新岗建投递 + terminate 旧投递，talent 复用不重建。

---

## 反模式

- 不要另写解析/核对脚本 — `_hire.py` 已封装全流程
- 不要手撸 jobs API — 用 `--jobs`
- 不要 /talents?mobile= 去重 — 走 find_existing_talent
- 不要传附件指望自动解析 — combined_create/update 不解析，需 Document AI 映射
- 不要盲信 `_my_jobs.json` 是最新的 — 它是快照，新岗位需刷新

---

## 参考文档

- [招聘开发指南（官方）](https://open.feishu.cn/document/server-docs/hire-v1/recruitment-development-guide)
- [`../lark-hire/SKILL.md`](../lark-hire/SKILL.md) — 飞书招聘 OpenAPI 契约层
- [`references/entry-rules.md`](references/entry-rules.md) — 录入铁律细节
- [`references/decisions.md`](references/decisions.md) — 设计约束（why）
- [`CHANGELOG.md`](CHANGELOG.md) — 版本演进与踩坑历史（考古）
- [`notes/hire_record.md`](<PROJECT_ROOT>/notes/hire_record.md) — 录入手动排错手册
