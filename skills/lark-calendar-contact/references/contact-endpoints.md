# contact 接口全量表

> 飞书通讯录 API 完整契约。优先调 `schedule-interview/scripts/match_schedule.py` 的 `resolve_interviewers()`，裸调时查本表。

所有命令身份默认 `--as user`（**contact 域 user 是必需的，bot 几乎都不支持**）。

## contact +search-user

按姓名/email/open_id 搜用户。

**三种用法**（互斥）：

```bash
# 1. 按关键词（姓名/email）搜——最常用
lark-cli contact +search-user --query 谢坤 --as user

# 2. 批量关键词搜（≤20 个，并行）
lark-cli contact +search-user --queries 谢坤,潘腾飞,陆江辉 --as user

# 3. 按 open_id 查（≤100 个）
lark-cli contact +search-user --user-ids ou_a,ou_b,ou_c --as user
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `--query` | 三选一 | 单个关键词（≤50 字符） |
| `--queries` | 三选一 | CSV 多关键词（≤20，并行搜），输出带 `matched_query` 归属 |
| `--user-ids` | 三选一 | CSV open_id（≤100，`me`=自己） |
| `--page-size` | ❌ | 1-30，默认 20 |
| `--as` | ✅ | **必须 `user`**，bot 不支持 |
| `--has-chatted` | ❌ | **❌ 别加**——过滤陌生人面试官（schedule-interview 踩过坑） |
| `--exclude-external-users` | ❌ | 排除跨租户用户 |
| `--has-enterprise-email` | ❌ | 只返有企业邮箱的 |
| `--left-organization` | ❌ | 只返已离职的 |
| `--lang` | ❌ | 本地化（`zh_cn`/`en_us`） |

**返回**（`data.users[]`，平铺）：

| 字段 | 说明 |
|---|---|
| `open_id` | `ou_` 前缀，稳定标识（永远非空） |
| `localized_name` | 显示名（永远非空，fallback 到 open_id） |
| `email` / `enterprise_email` | 可能空 |
| `department` | 部门名（可子串匹配，可能空） |
| `is_cross_tenant` | 是否跨租户 |
| `p2p_chat_id` | `oc_` 前缀（可能空） |
| `has_chatted` | 是否聊过（衍生字段） |

`--queries` 模式额外返 `matched_query`（每个用户匹配的关键词）+ `queries[]` sidecar。

```bash
# schedule-interview 实际用法（resolve_interviewer）
lark-cli contact +search-user --query 谢坤 --as user --format json
```

## contact +get-user

按 open_id 查用户详情。

**唯一接受 `--as bot` 的 contact 命令**——bot 也能查（用于自动化场景）。

```bash
lark-cli contact +get-user --user-id ou_xxx              # 默认 --as user
lark-cli contact +get-user --user-id ou_xxx --as bot     # bot 也可
lark-cli contact +get-user                               # 不传 user-id=查自己
```

## contact user_profiles batch_query（typed command）

批量查 profile（带个人状态/签名等扩展字段）。

```bash
lark-cli contact user_profiles batch_query \
  --params '{"user_id_type":"open_id"}' \
  --data '{"user_ids":["ou_a","ou_b"],"query_option":{"include_personal_status":true,"include_description":true}}' \
  --as user
```

| 参数 | 说明 |
|---|---|
| `--params` | JSON，含 `user_id_type`（`user_id`/`union_id`/`open_id`，默认 open_id） |
| `--data.user_ids` | ✅ open_id 数组 |
| `--data.query_option` | `include_personal_status`/`include_description` |

身份：**仅 `--as user`**。

## 错误码

| 错误 | 含义 | 处理 |
|---|---|---|
| 空 users[] | 用了 bot 身份 | 改 `--as user` |
| 空 users[] | 加了 `--has-chatted` 过滤掉陌生人 | 去掉该参数 |
| 找不到人 | 关键词太短/重名 | 加部门/邮箱限定，或用 `--queries` 批量 |

## 反模式

- ❌ `+search-user --as bot`（bot 不支持）
- ❌ `+search-user --has-chatted`（漏陌生人面试官，schedule-interview 踩坑）
- ❌ `+search-user` 三种参数混用（`--query`/`--queries`/`--user-ids` 互斥）
