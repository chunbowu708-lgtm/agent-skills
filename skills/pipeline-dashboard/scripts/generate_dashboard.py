# -*- coding: utf-8 -*-
"""
generate_dashboard.py — 招聘管道看板 HTML 生成器

读 _daily_report.json → 按岗位×阶段聚合 → 输出 HTML 看板。
纯数据聚合 + 渲染，不调 AI。

用法：
  python generate_dashboard.py --report notes/_daily_report.json --output notes/pipeline-dashboard.html
"""
import json, sys, os, argparse, datetime
from collections import defaultdict, OrderedDict
sys.stdout.reconfigure(encoding="utf-8")

# 阶段排序（初筛 → ... → 入职）
STAGE_ORDER = ["初筛", "简历评估", "待约面", "一面", "二面", "三面", "四面", "Offer", "入职"]
STUCK_WARN = 3      # ≥3 天标橙
STUCK_CRIT = 5      # ≥5 天标红


def load_report(path):
    if not os.path.exists(path):
        print(f"[❌] 报告不存在: {path}，请先跑 python notes/_daily_review.py")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def extract_people(report):
    """从报告提取人员列表，返回 [{name, job, stage, dwell_days, conclusion}]"""
    people = []
    # 兼容多种报告结构
    ats = report.get("ats_people") or report.get("structured", {}).get("ats_people") or []
    for p in ats:
        if not isinstance(p, dict):
            continue
        people.append({
            "name": p.get("name", ""),
            "job": p.get("job_title", p.get("job", "")),
            "stage": p.get("stage_name", p.get("stage", "")),
            "stage_type": p.get("stage_type", 0),
            "dwell_days": p.get("dwell_days"),
            "conclusion": p.get("latest_conclusion"),
        })
    # 如果 ats_people 为空，尝试从 battle_list 提取
    if not people:
        bl = report.get("structured", report.get("battle_list", {}))
        for item in bl.get("stuck", []):
            people.append({
                "name": item.get("name", ""),
                "job": "",
                "stage": item.get("stage", ""),
                "dwell_days": item.get("dwell_days"),
                "conclusion": None,
            })
    return people


def aggregate_funnel(people):
    """全局漏斗：按阶段聚人数"""
    counts = defaultdict(int)
    for p in people:
        stage = _normalize_stage(p["stage"])
        counts[stage] += 1
    # 按 STAGE_ORDER 排序，保留有人的阶段
    ordered = OrderedDict()
    for s in STAGE_ORDER:
        if counts.get(s, 0) > 0 or s in counts:
            ordered[s] = counts.get(s, 0)
    # 没匹配到标准阶段的归到"其他"
    for s, c in counts.items():
        if s not in ordered:
            ordered["其他"] = ordered.get("其他", 0) + c
    return ordered


def _normalize_stage(stage_name):
    """把各种阶段名归一到 STAGE_ORDER"""
    if not stage_name:
        return "初筛"
    s = str(stage_name)
    for std in STAGE_ORDER:
        if std in s:
            return std
    return s


def aggregate_matrix(people):
    """分岗位矩阵：{job: {stage: count}} + 各岗位停滞数"""
    matrix = defaultdict(lambda: defaultdict(int))
    stuck_count = defaultdict(int)
    for p in people:
        job = p["job"] or "未分类"
        stage = _normalize_stage(p["stage"])
        matrix[job][stage] += 1
        if p["dwell_days"] and p["dwell_days"] >= STUCK_WARN:
            stuck_count[job] += 1
    return matrix, stuck_count


def render_funnel(funnel):
    """渲染漏斗 HTML"""
    stages = list(funnel.items())
    if not stages:
        return "<p>（无数据）</p>"
    max_count = max(funnel.values())

    parts = []
    for i, (stage, count) in enumerate(stages):
        is_bottleneck = count == max_count and max_count > 3
        cls = "funnel-stage bottleneck" if is_bottleneck else "funnel-stage"
        parts.append(f'<div class="{cls}"><div class="count">{count}</div><div class="label">{stage}</div></div>')
        if i < len(stages) - 1:
            next_count = stages[i + 1][1]
            rate = int(next_count / count * 100) if count > 0 else 0
            parts.append(f'<div class="funnel-arrow">→<div class="funnel-rate">{rate}%</div></div>')
    return "".join(parts)


def render_matrix(matrix, stuck_count, funnel_stages):
    """渲染分岗位矩阵表"""
    stages = [s for s in STAGE_ORDER if any(s in row for row in matrix.values())] or list(funnel_stages.keys())
    headers = "<tr><th>岗位</th>" + "".join(f"<th>{s}</th>" for s in stages) + "<th>停滞数</th></tr>"

    rows = []
    for job in sorted(matrix.keys(), key=lambda j: -(sum(matrix[j].values()))):
        cells = ""
        for s in stages:
            cnt = matrix[job].get(s, 0)
            cells += f"<td>{cnt if cnt else ''}</td>"
        sc = stuck_count.get(job, 0)
        sc_cls = "stuck-crit" if sc >= 3 else ("stuck-warn" if sc >= 1 else "stuck-0")
        rows.append(f'<tr><td class="pos">{job}</td>{cells}<td class="{sc_cls}">{sc}</td></tr>')
    return headers, "\n".join(rows)


def render_warn_wall(people):
    """渲染停滞预警墙"""
    crit, warn = [], []
    for p in people:
        d = p["dwell_days"]
        if d is None:
            continue
        line = f'{p["name"]} - {p["job"] or ""} - 卡在"{p["stage"]}" {d}天'
        if d >= STUCK_CRIT:
            crit.append(line)
        elif d >= STUCK_WARN:
            warn.append(line)

    parts = []
    if crit:
        parts.append('<div class="warn-crit">🔴 停滞≥5天（高危）</div>')
        parts.extend(f'<div class="warn-item warn-crit">{c}</div>' for c in crit)
    if warn:
        parts.append('<div class="warn-warn">🟠 停滞 3-4天（需关注）</div>')
        parts.extend(f'<div class="warn-item warn-warn">{w}</div>' for w in warn)
    if not crit and not warn:
        parts.append('<div class="warn-item">✅ 暂无停滞预警</div>')
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser(description="生成招聘管道看板 HTML")
    ap.add_argument("--report", default="notes/_daily_report.json", help="对账报告 JSON 路径")
    ap.add_argument("--output", default="notes/pipeline-dashboard.html", help="输出 HTML 路径")
    args = ap.parse_args()

    report = load_report(args.report)
    people = extract_people(report)

    if not people:
        print("[⚠️] 报告里没有候选人数据，看板为空")
        # 仍然生成空看板（让用户知道数据源有问题）

    funnel = aggregate_funnel(people)
    matrix, stuck_count = aggregate_matrix(people)
    funnel_html = render_funnel(funnel)
    headers, matrix_rows = render_matrix(matrix, stuck_count, funnel)
    warn_html = render_warn_wall(people)

    # 读模板渲染
    tpl_path = os.path.join(os.path.dirname(__file__), "..", "assets", "dashboard-template.html")
    if os.path.exists(tpl_path):
        with open(tpl_path, encoding="utf-8") as f:
            html = f.read()
        html = html.replace("{{DATE}}", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        html = html.replace("{{SOURCE}}", args.report)
        html = html.replace("{{FUNNEL}}", funnel_html)
        html = html.replace("{{MATRIX_HEADERS}}", headers)
        html = html.replace("{{MATRIX_ROWS}}", matrix_rows)
        html = html.replace("{{WARN_WALL}}", warn_html)
    else:
        # 无模板时用内嵌简化版
        html = f"<html><body><h1>管道看板</h1>{funnel_html}{warn_html}</body></html>"

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[✅] 看板已生成: {args.output}（{len(people)}人）")


if __name__ == "__main__":
    main()
