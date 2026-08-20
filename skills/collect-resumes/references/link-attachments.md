# 链接类附件下载策略

邮件 API attachments 为空但 body 里有下载链接 → 链接类附件。
链接附件不能走 lark-cli pipeline，也不能直接 curl 原始 URL（只拿到 HTML 跳转页或报错）。

## 常见链接类型

| URL 特征 | 来源 | 示例 |
|---------|------|------|
| `wx.mail.qq.com/ftn/download` | QQ邮箱超大附件 | `?func=3&key=xxx&code=xxx` |
| `mail.163.com/large-attachment` | 网易邮箱超大附件 | 文件名通常在链接前的文字里 |
| `pan.baidu.com` | 百度网盘 | 可能有提取码 |
| `aliyundrive` | 阿里云盘 | |

## 下载策略（按优先级）

### 首选：batch_download_links.mjs 批量下载（或直接跑 collect.mjs，编排器自动调）

**三种模式**（选哪个看场景）：

```bash
# 【当天收简历】按 record ID 精准下载（只下今天的，不碰历史积压）
node "…/scripts/batch_download_links.mjs" \
  --records "sha256:xxx,sha256:yyy" --manifest <PROJECT_ROOT>/notes/collection_manifest.json

# 【清历史积压】从 manifest 读所有 verified 的 link 记录全量下载
node "…/scripts/batch_download_links.mjs" --manifest <path>

# 【不走 manifest】直接传 URL
node "…/scripts/batch_download_links.mjs" --urls "https://wx.mail.qq.com/..." "https://..."
```

- Playwright headless 打开链接 → 检测"失效"跳过 → 点下载 → 轮询等待完成 → 移文件到 target_dir + 写 result.json + **自动合并推进 archived**（无需再跑 merge_results）
- 确认失效的链接自动推进 `blocked`（`LINK_EXPIRED`）——失效是永久性的，留在 verified 每次白试；blocked 让闸门能发现"该人材料从未归档"。处理：联系候选人重发，或 `resolve_records --exclude`
- 快速失败：download 事件等 15 秒（需登录的页面按钮无效→跳过），传输超时 5 分钟
- 依赖 `playwright` npm 包（装在 `<PROJECT_ROOT>/node_modules`，脚本自动 fallback 加载）

### Fallback：Playwright MCP 手动操作

脚本失败时（如页面改版按钮失效），用 MCP 逐个打开链接点下载：

```
browser_navigate → 打开链接
browser_snapshot → 找下载按钮
browser_click → 点击
轮询 Downloads 目录（每2秒 ls）→ 文件大小连续3次不变 = 下载完成
ls -lh 确认大小合理 → cp 归档
```


MCP 也失败 → 切 CDP Proxy（localhost:3456，仅 Claude Code 环境有 web-access skill；ZCode 环境直接走"用户手动下载"兜底）。

### 最后兜底：让用户手动下载

浏览器下载也失败 → 把链接和文件名发给用户：
```
需手动下载：张三_特效设计师(260MB): https://wx.mail.qq.com/ftn/...
```
用户下载到 Downloads 后说"好了"，从 Downloads 归档（手写 result.json 必须写到 `notes/_download_results/`，否则 merge 静默不认）。

## 运行时注意

- QQ 超大附件链接通常 30 天有效；报 `fileid error` 先让用户确认是否真过期再放弃
- 网易超大附件链接可能被 HTML 实体编码（`&amp;`）——脚本已统一 decode，手提链接时注意
- 大文件（>150MB）下载后 `ls -lh` 确认大小合理，不要只看文件是否存在
- 全量模式（不传 `--records`）会把**所有** verified 历史链接串行拖出来下（可能几 GB）——当天收简历务必用 `--records` 或直接跑 `collect.mjs`（默认只下今天的）
