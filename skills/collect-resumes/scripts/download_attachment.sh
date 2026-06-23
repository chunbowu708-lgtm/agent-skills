#!/bin/bash
# 从飞书邮件下载附件，一条命令串完，中间不断。
# 用法: download_attachment.sh <MESSAGE_ID> <OUTPUT_PATH>
# 为什么 pipeline：分步跑容易中间态丢失附件 ID 或下载 URL，串起来保证原子性。
#
# 依赖 lark-cli 在 PATH 里，或设环境变量 LARK_CLI_PATH 指向它的路径。

set -euo pipefail

MID="${1:?用法: download_attachment.sh <MESSAGE_ID> <OUTPUT_PATH>}"
OUT="${2:?缺少输出路径}"

# lark-cli 路径：优先用环境变量，否则假定在 PATH
LARK="${LARK_CLI_PATH:-lark-cli}"

# 如果输出路径的目录不存在，自动创建
mkdir -p "$(dirname "$OUT")"

ATT_ID=$("$LARK" mail +message --message-id "$MID" --as user -q '.data.attachments[0].id' 2>/dev/null) \
  && URL=$("$LARK" mail user_mailbox.message.attachments download_url --as user \
      --params "{\"user_mailbox_id\":\"me\",\"message_id\":\"$MID\",\"attachment_ids\":\"$ATT_ID\"}" \
      -q '.data.download_urls[0].download_url' 2>/dev/null) \
  && curl -sL -o "$OUT" "$URL" \
  && echo "OK: $OUT"

# 多附件版本（第2个附件）:
# ATT_ID=$("$LARK" mail +message --message-id "$MID" --as user -q '.data.attachments[1].id' 2>/dev/null)
