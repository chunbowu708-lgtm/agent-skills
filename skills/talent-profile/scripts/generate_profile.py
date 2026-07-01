# -*- coding: utf-8 -*-
"""
generate_profile.py — 候选人匹配覆盖图 HTML 生成器

把 AI 在对话中完成的覆盖判断结果，渲染成离线可看的 HTML 表格。
覆盖判断（✅/⚠️/❌）由 AI 完成，本脚本只负责渲染。

用法：
  # 基础：传入 AI 判断结果的 JSON
  python generate_profile.py --data notes/_profile_data.json --position "AI产品经理（UGC）"

  # data JSON 结构：
  # {
  #   "position": "AI产品经理（UGC游戏平台）",
  #   "requirements": ["AI产品经验", "独立负责模块", "英文可工作", ...],
  #   "candidates": [
  #     {"name":"张三", "verdict":"🟢强推", "cells":["✅","✅","⚠️"], "risk":"英文未验证"},
  #     ...
  #   ]
  # }

输出：HTML 写 stdout（AI 负责重定向到文件），或 --output 指定路径
"""
import json, sys, os, argparse, datetime
sys.stdout.reconfigure(encoding="utf-8")

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>匹配覆盖图 · {position}</title>
<style>
  body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; margin: 20px; background: #f8f9fa; }}
  h1 {{ font-size: 20px; color: #1a1a1a; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; font-size: 14px; }}
  th, td {{ border: 1px solid #e0e0e0; padding: 8px 10px; text-align: center; }}
  th {{ background: #f0f0f0; font-weight: 600; white-space: nowrap; }}
  td.name {{ text-align: left; font-weight: 500; white-space: nowrap; }}
  td.risk {{ text-align: left; font-size: 13px; color: #c0392b; max-width: 200px; }}
  .verdict {{ font-weight: 600; white-space: nowrap; }}
  .v-strong {{ color: #27ae60; }}
  .v-ok {{ color: #f39c12; }}
  .cell-yes {{ background: #d4edda; color: #155724; font-size: 16px; }}
  .cell-part {{ background: #fff3cd; color: #856404; font-size: 16px; }}
  .cell-no {{ background: #f8d7da; color: #721c24; font-size: 16px; }}
  .cell-na {{ background: #e9ecef; color: #999; }}
  .legend {{ margin: 12px 0; font-size: 13px; }}
  .legend span {{ margin-right: 16px; }}
</style>
</head>
<body>
<h1>匹配覆盖图 · {position}</h1>
<div class="meta">生成时间 {date} · 共 {count} 人</div>
<div class="legend">
  <span class="cell-yes">✅ 满足</span>
  <span class="cell-part">⚠️ 部分/模糊</span>
  <span class="cell-no">❌ 缺失</span>
  <span class="cell-na">— 不适用</span>
</div>
<table>
  <tr>
    <th>候选人</th><th>判定</th>{headers}<th>风险点</th>
  </tr>
{rows}
</table>
</body>
</html>"""

CELL_CLASS = {"✅": "cell-yes", "⚠️": "cell-part", "❌": "cell-no", "—": "cell-na"}


def render(data):
    position = data.get("position", "未命名岗位")
    reqs = data.get("requirements", [])
    candidates = data.get("candidates", [])

    headers = "".join(f"<th>{r}</th>" for r in reqs)

    rows = []
    for c in candidates:
        name = c.get("name", "")
        verdict = c.get("verdict", "")
        cells = c.get("cells", [])
        risk = c.get("risk", "")

        v_class = "v-strong" if "强推" in verdict else "v-ok"
        cells_html = ""
        for cell in cells:
            cls = CELL_CLASS.get(cell, "cell-na")
            cells_html += f'<td class="{cls}">{cell}</td>'

        rows.append(
            f'<tr><td class="name">{name}</td>'
            f'<td class="verdict {v_class}">{verdict}</td>'
            f'{cells_html}'
            f'<td class="risk">{risk}</td></tr>'
        )

    html = TEMPLATE.format(
        position=position,
        date=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        count=len(candidates),
        headers=headers,
        rows="\n".join(rows),
    )
    return html


def main():
    ap = argparse.ArgumentParser(description="生成匹配覆盖图 HTML")
    ap.add_argument("--data", required=True, help="AI 判断结果的 JSON 文件路径")
    ap.add_argument("--output", help="输出 HTML 路径（默认 stdout）")
    args = ap.parse_args()

    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)

    html = render(data)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[✅] HTML 已生成: {args.output}")
    else:
        print(html)


if __name__ == "__main__":
    main()
