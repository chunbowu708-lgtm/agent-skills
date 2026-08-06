# lark-cli base 子命令参数速查（实测锁定，不靠记忆）

> **铁律**：base 子命令的参数名容易记错（`--app-token` 还是 `--base-token`？`--filter` 还是 `--filter-json`？）。
> 每次调 base API 前先查这张表，**不许凭印象猜参数名，猜错了就是盲试，盲试就是浪费时间**。
> 参数如有疑问，唯一合法验证动作是 `+<cmd> --help`，不是发请求。

## 共用参数

| 参数 | 值 |
|---|---|
| `--base-token` | `YOUR_BASE_TOKEN`（候选人管理表 base） |
| `--table-id` | 跟踪表 `YOUR_TRACKING_TABLE_ID` / 候选人主库 `YOUR_CANDIDATE_TABLE_ID` |
| `--as user` | 身份（跟踪表用 user；hire 写接口用 bot） |
| `--format json` | 输出 JSON（默认 markdown） |

⚠️ **是 `--base-token` 不是 `--app-token`**（lark-cli 报 unknown flag，hint 会提示）。

---

## +record-list（列记录）

**用途**：拉全表记录，查重/查 record_id。

```bash
lark-cli base +record-list \
  --base-token YOUR_BASE_TOKEN \
  --table-id YOUR_TRACKING_TABLE_ID \
  --field-id 候选人 --field-id talent_id \
  --limit 200 --offset 0 \
  --format json --as user
```

**返回结构（矩阵模式！）**：
```json
{
  "data": {
    "data": [["赵朝魁", "7646746387257854259"], ...],   // 行矩阵
    "field_id_list": ["fldsTlIZsk", "fldSTYVNJ2"],        // 列顺序对应 data
    "fields": ["候选人", "talent_id"],                     // 列名
    "record_id_list": ["recABC", "recDEF"],                // ⭐ record_id 在这里，和 data 平行
    "has_more": true,
    "query_context": {...}
  }
}
```

⚠️ **不是 `data.items`**（那是 record-search 的格式）。record-list 返回矩阵：`data.data[i]` 是第 i 行，`data.record_id_list[i]` 是对应 record_id。

⚠️ **`--limit 500` 会返回空**，用 200 翻页。

⚠️ **投影用 `--field-id`**（可重复），不指定则返回全部列。

---

## +record-search（搜记录）

**用途**：按关键词/过滤条件搜记录。

```bash
# 基础关键词搜索（--keyword 必填）
lark-cli base +record-search \
  --base-token YOUR_BASE_TOKEN \
  --table-id YOUR_TRACKING_TABLE_ID \
  --keyword 龙双峰 --search-field 候选人 \
  --field-id 候选人 --field-id talent_id --field-id 岗位 \
  --format json
```

**过滤搜索**（用 `--json` 传完整请求体，keyword + filter 都能带）：
```bash
lark-cli base +record-search \
  --base-token YOUR_BASE_TOKEN \
  --table-id YOUR_TRACKING_TABLE_ID \
  --json '{"keyword":"龙双峰","search_fields":["候选人"],"select_fields":["候选人","talent_id","岗位"],"limit":10}' \
  --format json
```

**纯 filter（无关键词）**——必须用 `--json`（`--keyword` 是必填项，不传报错）：
```bash
lark-cli base +record-search \
  --base-token ... --table-id ... \
  --json '{"filter":{"logic":"or","conditions":[["talent_id","==","ID1"],["talent_id","==","ID2"]]},"select_fields":["talent_id"],"limit":10}' \
  --format json
```

⚠️ **`--keyword` 是必填**（除非用 `--json`）。空 keyword 报错 `800010701 Request validation failed`。

⚠️ **过滤参数是 `--filter-json` 不是 `--filter`**（单独用时）。但更推荐直接 `--json` 传完整体。

⚠️ **search-field 必须是表里真实存在的字段**，传"姓名"会报 not_found（跟踪表的列叫"候选人"）。不确定字段名先 `+field-list`。

---

## +record-upsert（建/更新记录）

**用途**：建新行（不带 --record-id）或更新已有行（带 --record-id）。

```bash
# 建新行
lark-cli base +record-upsert \
  --base-token YOUR_BASE_TOKEN \
  --table-id YOUR_TRACKING_TABLE_ID \
  --json '{"候选人":"张三","talent_id":"123","岗位":"研发"}' \
  --as user

# 更新已有行（--record-id 指定要改的行）
lark-cli base +record-upsert \
  --base-token YOUR_BASE_TOKEN \
  --table-id YOUR_TRACKING_TABLE_ID \
  --record-id recABC123 \
  --json '{"岗位":"研发","部门":"山海弹珠项目"}' \
  --as user
```

⚠️ **`--json` 的值是顶层层段 map**（`{"岗位":"研发"}`），**不要包在 `fields` 里**（`{"fields":{"岗位":"研发"}}` 是错的）。

⚠️ **没有 `+record-update` 子命令**！更新也用 `+record-upsert --record-id`（upsert 带指定 id = 覆盖更新）。

⚠️ **单选字段值必须是已有选项**（如"岗位"选"研发"但表里选项叫"Unity客户端(AI-Native)"就填不上，会被静默忽略）。先 `+field-list` 确认选项。

⚠️ **`--json` 支持 `@file`**：复杂 JSON 写文件再 `--json @notes/_body.json`，避免命令行转义地狱。

**单选字段值的 CellValue 写法**：
- text → `"字符串"`
- select → `"选项名"`（单选）
- multi-select → `["标签A","标签B"]`
- datetime → `"2026-03-24 10:00:00"` 或毫秒整数
- checkbox → `true`/`false`

---

## +field-list（列字段定义）

**用途**：查表有哪些字段、字段 id、单选选项。

```bash
lark-cli base +field-list \
  --base-token YOUR_BASE_TOKEN \
  --table-id YOUR_TRACKING_TABLE_ID \
  --as user
```

**返回结构**：
```json
{
  "data": {
    "fields": [
      {"id": "fldsTlIZsk", "name": "候选人", "type": 1, ...},
      {"id": "fldfi6hRY6", "name": "岗位", "type": 3, "options": [{"name": "研发"}, {"name": "美术"}]},
      ...
    ]
  }
}
```

⚠️ **是 `+field-list` 不是 `+table-fields`**。

⚠️ 单选字段的选项在 `options[].name`，更新前核对选项名。

**跟踪表字段 id（实测锁定）**：

| field_id | 字段名 | type |
|---|---|---|
| fldsTlIZsk | 候选人 | text |
| fldfi6hRY6 | 岗位 | select |
| fldKh4NPLQ | 部门 | select |
| fldTQUJs9j | 职能类别 | select |
| fldSTYVNJ2 | talent_id | text |
| fldnrbv4u3 | 优先级 | select |
| fldmSODE6c | ID | auto_number |
| fldZ9sJpcA | 当前轮次 | select |
| fldr5lHqwK | 状态 | select |
| fld3RoTQvQ | 面试时间 | datetime |
| fldFl9dVV5 | 最近进展日期 | datetime |
| fldX2OIZEY | 进入阶段日期 | datetime |
| fldmw2kMEl | 面试官 | text |
| fldH5Ac9we | 下一步动作 | text |

---

## +record-get（按 id 查单条）

```bash
lark-cli base +record-get \
  --base-token YOUR_BASE_TOKEN \
  --table-id YOUR_TRACKING_TABLE_ID \
  --record-id recABC123 \
  --format json
```

---

## 在 Python 里调用（subprocess）

⚠️ **必须设 `MSYS_NO_PATHCONV=1`**，否则 git-bash 把 API 路径的 `/open-apis/...` 吞成 `C:/Program Files/Git/...`（404）。
⚠️ **必须 `text=True, encoding="utf-8"`**，否则 stdout 是 bytes，中文 JSON 解析失败。
⚠️ **CLI 用全路径 `.cmd`**，Windows 下 `"lark-cli"` 报 WinError 2。

```python
import subprocess, json, os
os.environ["MSYS_NO_PATHCONV"] = "1"
CLI = os.environ.get("LARK_CLI_PATH", "lark-cli")
r = subprocess.run([CLI, "base", "+record-upsert",
                    "--base-token", "KRAQ...", "--table-id", "tbl...",
                    "--record-id", "rec...", "--json", "@notes/_body.json", "--as", "user"],
                   capture_output=True, text=True, encoding="utf-8")
d = json.loads(r.stdout)
```

⚠️ **复杂 JSON 写文件再 `@file`**，不要内联在命令行（中文+引号+空格的转义在 git-bash 里是噩梦）。

---

## 易错对照表（踩过的坑）

| 错误写法 | 正确写法 | 报错 |
|---|---|---|
| `--app-token` | `--base-token` | unknown flag |
| `+table-fields` | `+field-list` | unknown subcommand |
| `+record-update` | `+record-upsert --record-id` | unknown subcommand |
| `--filter` | `--filter-json` 或 `--json` | unknown flag |
| `--fields-json` | `--json`（顶层 map） | unknown flag |
| `--json {"fields":{...}}` | `--json {"岗位":"研发"}`（顶层层段） | 写入字段不生效 |
| 空字符串 keyword | `--json '{"keyword":"x",...}'` | 800010701 |
| 不带 encoding 的 subprocess | `text=True, encoding="utf-8"` | JSONDecodeError |
| subprocess 不设 MSYS_NO_PATHCONV | `os.environ["MSYS_NO_PATHCONV"]="1"` | api 路径 404 |
| 内联中文 JSON | `--json @notes/_body.json` | 转义错误 |
