# -*- coding: utf-8 -*-
"""
generate_profile.py — 候选人匹配覆盖图 HTML 生成器

把 AI 在对话中完成的覆盖判断结果，渲染成离线可看的 HTML 表格。
覆盖判断（✅/⚠️/❌）由 AI 完成，本脚本只负责渲染。

用法：
  # 基础：传入 AI 判断结果的 JSON（无 --output 时 HTML 打印到 stdout）
  python generate_profile.py --data notes/_profile_data.json --output "<岗位文件夹>/talent-profile.html"

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

# HTML 模板从 assets 读取（单一真相源，改样式只改模板文件不改代码）
_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "profile-template.html")


def _load_template():
    """读取 assets/profile-template.html（str.format 占位符：{position}/{date}/{count}/{headers}/{rows}）。"""
    with open(_TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


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

    html = _load_template().format(
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
