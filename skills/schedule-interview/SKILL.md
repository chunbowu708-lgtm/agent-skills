---
name: schedule-interview
description: >
  面试时间协调：批量查面试官空闲，和候选人给定的时间求交集，产出可约时段表 + 可直接转发给面试官的确认消息草稿。
  触发词：对时间、约面试、面试官空闲、协调面试、面试时间、看看XX有空没、匹配面试时间。
  只要用户提到"面试官时间/空闲/档期"+"候选人时间"，就使用这个 skill。
  覆盖：单/多面试官共同空闲查询、多候选人批量匹配、时段去重防撞档、自动生成确认草稿。
  不覆盖：替用户发消息给面试官（草稿拟好用户自发）、在飞书招聘建面试流程（无公开API，用户手点）、问候选人时间（用户自己沟通）、改跟踪表（见 recruit-followup）。
  依赖：lark-cli（calendar 域 + contact 域已授权）、interviewers.json 缓存。
---

# 面试时间协调

## 这个 skill 解决什么

面试协调本质是集合求交：`候选人候选时段 ∩ 面试官空闲`。
本 skill 把求交自动化——用户口头说"面试官 + 候选人时间"，产出**匹配好的时间表 + 可转发草稿**。

## 能力边界（重要）

| 环节 | 谁做 |
|------|------|
| ① 收集候选人时间 | **用户**（必须人沟通） |
| ② 查面试官空闲 | ✅ **本 skill 自动** |
| ③ 算交集、定时间集 | ✅ **本 skill 自动** |
| ④ 拟消息发面试官 | 🟡 **本 skill 拟草稿，用户自发** |
| ⑤ 飞书招聘建面试 | **用户手点**（无公开"安排面试"API） |

> ⚠️ 飞书招聘**没有公开的"安排面试"API**，只有"创建外部面试/面评"（用于导入外部系统数据），不是真正的排期+邀约。第⑤步只能用户在 HR 后台手动建。

## 配置

| 项 | 值 |
|---|---|
| 项目根 | `F:/miniwanob` |
| lark-cli | `C:/Users/wuchunbo/AppData/Roaming/npm/lark-cli.cmd`（subprocess 全路径，否则 WinError 2） |
| 面试官缓存 | `F:/miniwanob/notes/interviewers.json`（姓名/alias → open_id，首次用自动写回） |
| 输出文件 | `F:/miniwanob/notes/_schedule_match.txt`（结果写文件再 Read，避免 GBK 乱码） |
| 核心脚本 | `scripts/match_schedule.py` |

## 标准输入（用户这样跟我说即可）

```
面试官：张三（可多人，逗号分隔）
候选人时间：
  候选人A - 周四
  候选人B - 周四
  候选人C - 周五
（可选）面试时长：60分钟
```

AI 把自然语言转成命令行参数跑脚本。候选人时间支持的写法：
- `周四` / `周五` / `周三` — 模糊日（默认未来最近的那天）
- `周三下午` / `周四晚上` / `周五上午` — 日 + 时段偏好
- `周四 16:00` — 精确时刻（取该时刻及之后）
- `7-3` / `7月3日` — 绝对日期
- `今天` / `明天` — 相对日

## 标准命令

```bash
# 基础：1个面试官 + 多候选人各给候选日
python "C:/Users/wuchunbo/.agents/skills/schedule-interview/scripts/match_schedule.py" \
  --interviewer 张三 \
  --candidates "候选人A=周四,候选人B=周四,候选人C=周五" \
  --duration 60

# 多面试官（取共同空闲）
python "C:/Users/wuchunbo/.agents/skills/schedule-interview/scripts/match_schedule.py" \
  --interviewer "张三,李四" \
  --candidates "张三=周四" \
  --duration 60

# 单人单时段（草稿走"确认式"话术）
python "C:/Users/wuchunbo/.agents/skills/schedule-interview/scripts/match_schedule.py" \
  --interviewer 王五 \
  --candidates "候选人D=周三下午" \
  --duration 60

# 先 dry-run 验证解析（不查飞书，省 API）
python ".../match_schedule.py" --interviewer 张三 --candidates "..." --dry-run
```

**Windows 铁律**：脚本路径用绝对路径 + 正斜杠，在任何 CWD 下都对。candidates 参数含中文/空格，**必须用双引号包住整个 `--candidates` 的值**。

## 输出解读

脚本输出三段：

```
=== 匹配结果 ===
面试官：张三
  周四(7-2) 空闲: 18:30-19:30, 19:00-20:00        ← 面试官的真实空档
候选人可约时段：
  候选人A   周四(7-2) → ✅ 可约 18:30（建议 18:30）   ← 和候选人日期求交后
  候选人B 周四(7-2) → ❌ 已被候选人A占用              ← 同面试官不撞档

=== 可转发草稿（发给面试官）===
三哥，我和候选人沟通了一下，安排如下你看🆗不：
候选人A 周四 18:30

=== 不可约的（需重新和候选人协调）===
候选人B 周四(7-2) → ❌ ...
```

**AI 行为指导**：
- 把"匹配结果"和"草稿"都展示给用户，让用户扫一眼草稿能不能直接转发
- ❌ 的候选人要明确提示"需回去重新和候选人协调时间"，不能隐瞒
- 草稿是**建议**，用户可改。如果用户说"候选人B改到周五"，重跑脚本
- 多候选人时，脚本自动去重防撞档（同一面试官不会分给两人同一时段）

## 两种草稿话术（已固化）

| 场景 | 话术 | 来源 |
|------|------|------|
| 单人单时段 | `老王，这个候选人我沟通了一下，安排在 周三（7-1）15:00 面试🆗吗` | 用户案例2 |
| 多人多时段 | `三哥，我和候选人沟通了一下，安排如下你看🆗不：\n候选人A 周四 18:30\n候选人B ...` | 用户案例1 |

称谓从 `interviewers.json` 的 `alias` 字段取（如"三哥""老王"），没有 alias 就用姓名。

## 面试官缓存机制

`notes/interviewers.json` 结构（沿用 jobs_map.json 扁平风格）：
```json
{
  "ou_xxx": {
    "name": "张三",
    "dept": "XX团队",
    "email": "zhangsan@example.com",
    "alias": ["三哥"]
  }
}
```

- **首次用某面试官**：脚本自动调 `contact +search-user` 查 open_id 并写回缓存
- **后续用**：缓存命中，秒查（省一次 API）
- **新增 alias**：手动编辑 JSON，在 `alias` 数组加（如 `"alias": ["辉哥","老陆"]`）
- **缓存过期**（面试官离职/换部门）：删掉对应条目，下次自动重查

> 已预热：张三(ou_xxx)、李四(ou_yyy)、王五(ou_zzz)。

## 踩坑固化（脚本内部已处理，理解用）

1. **suggestion 查询区间 ≤ 7 天**（飞书 API 限制，超了报 190014 `interval too large`）。脚本以候选人最早日期为起点，最多查 7 天，超期会截断并告警。
2. **suggestion 返回有随机性**：同一查询每次返回的空闲块数和位置可能略不同（飞书侧算法），属正常。脚本以本次返回为准。
3. **suggestion 返回的空闲块本身就是可约时段**，不要再二次截断（曾用 work_end=18 砍晚上时段，导致晚上面试全丢——晚上面试也是有效的）。
4. **freebusy vs suggestion**：本 skill 统一用 suggestion（直接返回空闲块 + "完全空闲"判断），不用 freebusy（只返回忙碌时段，还得反推空闲，多一步易错）。
5. **星期计算用 datetime，绝不手算**（手算必错，特别是跨周边三计算）。
6. **多候选人去重**：同一面试官的同一时段不能分给两人，脚本用 `_has_conflict` 按 duration 区间重叠判断。
7. **subprocess 调 lark-cli 用全路径 .cmd**：Windows 下 `"lark-cli"` 报 WinError 2 找不到。
8. **午休自动排除**：脚本对每天 12:00-13:30 构造 `--exclude`，避免约到午休。

## 不做（显式边界，避免越界）

- ❌ 不替用户发 IM 给面试官（草稿拟好用户自发，措辞用户把关）
- ❌ 不碰飞书招聘 API（无"安排面试"公开接口，第⑤步用户手点）
- ❌ 不解析候选人简历（那是 collect-resumes 的活）
- ❌ 不建/改跟踪表（那是 recruit-followup 的活，本 skill 只读面试官日程）
- ❌ 不替用户问候选人时间（必须人沟通）

## 已验证可用的能力（2026-06-30 实测）

| 能力 | 命令 | 结果 |
|------|------|------|
| 搜姓名拿 open_id | `contact +search-user --query 姓名 --has-chatted` | ✅ |
| 查单人忙闲 | `calendar +freebusy --user-id ou_xxx` | ✅ |
| **算多人共同空闲** | `calendar +suggestion --attendee-ids A,B --duration-minutes 60` | ✅ 返回"完全空闲"判断 |
| 单/多候选人匹配 | `match_schedule.py` | ✅ 案例验证通过 |
