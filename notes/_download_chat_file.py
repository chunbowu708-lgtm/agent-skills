"""从飞书群聊/私聊下载简历文件，供 _hire.py 录入。

用法：
  python notes/_download_chat_file.py 白向庭
  python notes/_download_chat_file.py 白向庭 --output "F:/Users/wuchunbo/Downloads"

原理（复用 lark-cli 现有 IM 命令，不造轮子）：
  1. im +messages-search --query "<姓名>" --include-attachment-type file  跨群/私聊搜文件消息
  2. 从命中消息提 message_id + file_key
  3. im +messages-resources-download 下载到本地（自动保留原文件名）

输出（stdout 最后一行，供 _hire.py 解析）：
  DOWNLOADED <绝对路径>
"""
import sys
import os
import re
import json
import argparse
import subprocess

sys.path.insert(0, os.path.dirname(__file__))
from _lark_shared import extract_json, CLI  # 复用共享库的 CLI 常量（含 LARK_CLI_PATH 环境变量覆盖）

DOWNLOADS = "F:/Users/wuchunbo/Downloads"


def _run_lark(args, cwd=None, timeout=180):
    """跑 lark-cli 子命令，返回合并 stdout+stderr 文本。cwd 决定文件下载落点。

    ⚠️ 不复用 _lark_shared.cli() 是因为本脚本需要 cwd 参数控制下载落点(cli() 不支持)。
    encoding="utf-8" 必须显式设——Windows Python text=True 默认用 cp936 解码,
    lark-cli 的中文输出(chat_name、文件名)会 UnicodeDecodeError。
    """
    r = subprocess.run([CLI] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd)
    return (r.stdout or "") + (r.stderr or "")


def search_file_messages(keyword):
    """跨群/私聊搜含关键词的文件消息，返回 [{message_id, file_key, name, chat, time}]。"""
    raw = _run_lark([
        "im", "+messages-search",
        "--query", keyword,
        "--include-attachment-type", "file",
        "--page-size", "20",
        "--as", "user",
    ])
    d = extract_json(raw)
    if not d:
        return []
    msgs = d.get("data", {}).get("messages", [])
    out = []
    for m in msgs:
        if m.get("msg_type") != "file":
            continue
        content = m.get("content", "")
        # content 形如 <file key="file_v3_xxx" name="数据产品 白向庭.pdf"/>
        key_m = re.search(r'key="([^"]+)"', content)
        name_m = re.search(r'name="([^"]+)"', content)
        if not key_m:
            continue
        out.append({
            "message_id": m.get("message_id"),
            "file_key": key_m.group(1),
            "name": name_m.group(1) if name_m else f"{key_m.group(1)}.bin",
            "chat": m.get("chat_name") or m.get("chat_partner", {}).get("name") or m.get("chat_type", ""),
            "time": m.get("create_time", ""),
        })
    return out


def download(message_id, file_key, out_dir):
    """下载文件到 out_dir（cwd=out_dir 决定落点），返回实际落地的绝对路径。"""
    os.makedirs(out_dir, exist_ok=True)
    raw = _run_lark([
        "im", "+messages-resources-download",
        "--message-id", message_id,
        "--file-key", file_key,
        "--type", "file",
        "--as", "user",
    ], cwd=out_dir, timeout=180)
    d = extract_json(raw)
    if not d or d.get("ok") is False:
        print(f"❌ 下载失败: {raw[:300]}", file=sys.stderr)
        return None
    saved = d.get("data", {}).get("saved_path") if isinstance(d.get("data"), dict) else None
    return saved


def main():
    ap = argparse.ArgumentParser(description="从飞书聊天下载简历文件")
    ap.add_argument("keyword", help="候选人姓名或文件名关键词")
    ap.add_argument("--output", default=DOWNLOADS, help=f"下载目录（默认 {DOWNLOADS}）")
    args = ap.parse_args()

    print(f"【搜文件消息】关键词: {args.keyword}", file=sys.stderr)
    hits = search_file_messages(args.keyword)
    if not hits:
        print(f"❌ 没搜到含「{args.keyword}」的文件消息", file=sys.stderr)
        sys.exit(1)

    print(f"  命中 {len(hits)} 条:", file=sys.stderr)
    for i, h in enumerate(hits):
        print(f"    [{i}] {h['name']} | {h['chat']} | {h['time']} | msg={h['message_id']}", file=sys.stderr)

    # 默认取最新一条（search 默认按时间倒序）
    pick = hits[0]
    print(f"【下载】{pick['name']} (file_key={pick['file_key'][:24]}...)", file=sys.stderr)

    path = download(pick["message_id"], pick["file_key"], args.output)
    if not path:
        sys.exit(2)

    # 输出绝对路径供 _hire.py 解析（stdout 最后一行）
    abs_path = os.path.abspath(path)
    print(f"✅ 下载完成: {abs_path}", file=sys.stderr)
    print(f"DOWNLOADED {abs_path}")


if __name__ == "__main__":
    main()
