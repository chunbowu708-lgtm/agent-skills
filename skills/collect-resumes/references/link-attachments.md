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

### 首选：batch_download_links.mjs 批量下载

```
node "…/scripts/batch_download_links.mjs" --manifest F:/miniwanob/notes/collection_manifest.json
```

Playwright headless 浏览器自动打开链接 → 检测"失效"跳过 → 点下载 → 轮询等待完成 → 存到 Downloads。
- 自动处理 QQ/网易大附件的 SPA 动态渲染 + 失效检测
- 串行处理（浏览器并行下载易冲突）
- 快速失败：download 事件等 15 秒（需登录的页面按钮无效→跳过），传输超时 5 分钟
- 依赖 `playwright` npm 包（装在 `F:/miniwanob/node_modules`）

### Fallback：Playwright MCP 手动操作

脚本失败时，用 MCP 逐个打开链接点下载：

```
browser_navigate → 打开链接
browser_snapshot → 找下载按钮
browser_click → 点击
轮询 Downloads 目录（每2秒 ls）→ 文件大小连续3次不变 = 下载完成
ls -lh 确认大小合理 → cp 归档
```

> 判断下载完成：目标文件大小连续 3 次轮询不变（间隔 2s），且 > 0。

MCP 也失败 → 切 CDP Proxy（localhost:3456），操作方式相同。

### 最后兜底：让用户手动下载

浏览器下载也失败 → 把链接和文件名发给用户：
```
需手动下载：张三_特效设计师(260MB): https://wx.mail.qq.com/ftn/...
```
用户下载到 Downloads 后说"好了"，从 Downloads 归档。

## 踩过的坑

- QQ 超大附件链接有时效，但通常30天有效。如果报 `fileid error`，先让用户确认是否真的过期再放弃
- 网易超大附件在 body_html 里，下载链接可能被 HTML 实体编码（`&amp;` → `&`），提取时需 decode
- 大文件（>100MB）下载后务必 `ls -lh` 确认大小合理，不要只看文件是否存在
