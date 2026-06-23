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

## 下载策略

### 第1级：Playwright MCP 浏览器点击

```
browser_navigate → 打开链接
browser_snapshot → 找下载按钮
browser_click → 点击
等15秒 → 检查 Downloads 目录有无新文件
落盘了 → 等下载完成（大文件60-90秒）→ cp 归档
```

Playwright 失败 → 切 CDP Proxy（localhost:3456），操作方式相同。

### 第2级：让用户手动下载

浏览器下载也失败 → 把链接和文件名发给用户：
```
需手动下载：张三_特效设计师(260MB): https://wx.mail.qq.com/ftn/...
```
用户下载到 Downloads 后说"好了"，从 Downloads 归档。

## 踩过的坑

- QQ 超大附件链接有时效，但通常30天有效。如果报 `fileid error`，先让用户确认是否真的过期再放弃
- 网易超大附件在 body_html 里，下载链接可能被 HTML 实体编码（`&amp;` → `&`），提取时需 decode
- 大文件（>100MB）下载后务必 `ls -lh` 确认大小合理，不要只看文件是否存在
