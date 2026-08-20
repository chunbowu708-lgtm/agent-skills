# -*- coding: utf-8 -*-
"""
generate_dashboard.py — 招聘管道看板 HTML 生成器（v2 重写 2026-08-04）

读 _daily_review.json → 按工作室分组 → 岗位漏斗色块 + 推送提醒 + 停滞预警 → 输出 HTML 看板。
纯数据聚合 + 渲染，不调 AI。
数据契约见 daily-recruit-report/references/review-contract.md：人员主数据在 structured.ats。

v2 变更（对抗式审查结论）：
  - 按 dept（工作室）分组卡片，不再一张扁平表
  - 用 interview_count + latest_conclusion 推断轮次（初试/复试/终试/Offer），不再全归"其他"
  - 新增推送提醒区（在途少/高危停滞/面评阻塞），带原因和动作
  - 漏斗用 CSS 色块条，不用 emoji

用法：
  python generate_dashboard.py --report notes/_daily_review.json --output notes/pipeline-dashboard.html
"""
import json, sys, os, argparse
from collections import defaultdict, OrderedDict
sys.stdout.reconfigure(encoding="utf-8")


# ============================================================
# 数据加载
# ============================================================
def load_report(path):
    if not os.path.exists(path):
        print(f"[❌] 报告不存在: {path}，请先跑 python notes/_daily_review.py")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def extract_people(report):
    """从 structured.ats 提取人员列表，补上推断的轮次阶段"""
    ats = report.get("structured", {}).get("ats", [])
    people = []
    for p in ats:
        if not isinstance(p, dict):
            continue
        person = {
            "name": p.get("name", ""),
            "talent_id": p.get("talent_id", ""),
            "job": p.get("job", ""),
            "dept": p.get("dept", ""),
            "stage": p.get("stage", ""),
            "dwell_days": p.get("dwell_days"),
            "interview_count": p.get("interview_count", 0),
            "conclusion": p.get("latest_conclusion"),  # 1=通过 2=不通过 None=未出
        }
        person["funnel_stage"] = _infer_funnel_stage(person)
        people.append(person)
    return people


def _infer_funnel_stage(p):
    """
    推断候选人所在漏斗阶段（用于矩阵/色块条）。
    判据优先级：不通过 > Offer阶段 > 按面试轮次 > 初筛
    """
    if p["conclusion"] == 2:
        return "不通过"
    stage = p.get("stage", "")
    if "Offer" in stage or "offer" in stage:
        return "Offer"
    if "入职" in stage:
        return "入职"
    if "初筛" in stage or "评估" in stage:
        return "初筛"
    # 面试阶段：按 interview_count 推断轮次
    ic = p.get("interview_count", 0)
    if ic == 0:
        return "待约面"
    if ic == 1:
        return "初试"
    if ic == 2:
        return "复试"
    if ic == 3:
        return "终试"
    return "HR/Offer"  # ic>=4


# 漏斗阶段排序 + 颜色映射
FUNNEL_ORDER = ["初筛", "待约面", "初试", "复试", "终试", "HR/Offer", "Offer", "入职"]
FUNNEL_COLORS = {
    "初筛":     "#adb5bd",  # 灰
    "待约面":   "#74c0fc",  # 浅蓝
    "初试":     "#4dabf7",  # 蓝
    "复试":     "#ffd43b",  # 黄
    "终试":     "#ff922b",  # 橙
    "HR/Offer": "#69db7c",  # 浅绿
    "Offer":    "#37b24d",  # 绿
    "入职":     "#2f9e44",  # 深绿
    "不通过":   "#ff6b6b",  # 红
}


# ============================================================
# 聚合
# ============================================================
def group_by_dept(people):
    """
    按工作室(dept) → 岗位(job) → 候选人分组。
    返回 OrderedDict: {dept: {job: [person, ...]}}
    工作室按人数降序，岗位按人数降序。
    """
    dept_job = defaultdict(lambda: defaultdict(list))
    for p in people:
        dept = p["dept"] or "未分类"
        job = p["job"] or "未分类"
        dept_job[dept][job].append(p)

    # 排序：工作室按总人数降序，岗位按人数降序
    result = OrderedDict()
    for dept in sorted(dept_job.keys(), key=lambda d: -sum(len(v) for v in dept_job[d].values())):
        result[dept] = OrderedDict()
        for job in sorted(dept_job[dept].keys(), key=lambda j: -len(dept_job[dept][j])):
            result[dept][job] = sorted(dept_job[dept][job], key=lambda p: -p.get("interview_count", 0))
    return result


def count_funnel(people):
    """全局漏斗人数统计"""
    counts = defaultdict(int)
    for p in people:
        counts[p["funnel_stage"]] += 1
    ordered = OrderedDict()
    for s in FUNNEL_ORDER:
        if counts.get(s, 0) > 0:
            ordered[s] = counts[s]
    return ordered


# ============================================================
# 预警分析
# ============================================================
def analyze_alerts(people, report):
    """
    分析三类预警：
    1. 急需补人（在途人少）
    2. 高危停滞（≥15天）
    3. 面评阻塞（已面完等评）
    """
    s = report.get("structured", {})

    # 面评阻塞：从 feedback_overdue 取
    feedback_overdue = s.get("feedback_overdue", [])
    blocked = []
    for f in feedback_overdue:
        blocked.append({
            "name": f.get("name", ""),
            "round": f.get("round_type", ""),
            "interviewer": "、".join(f.get("interviewers", [])),
            "overdue": f.get("overdue_days", 0),
            "urgency": f.get("urgency", ""),
        })

    # 高危停滞：dwell >= 15
    critical = []
    for p in people:
        d = p.get("dwell_days")
        if d and d >= 15:
            critical.append({
                "name": p["name"],
                "job": p["job"],
                "dept": p["dept"],
                "dwell": d,
                "stage": p["funnel_stage"],
                "conclusion": p["conclusion"],
            })
    critical.sort(key=lambda x: -x["dwell"])

    # 急需补人：按岗位统计在途人数，少于等于2的标出
    job_counts = defaultdict(int)
    for p in people:
        job_counts[(p["dept"], p["job"])] += 1
    shortage = []
    for (dept, job), cnt in sorted(job_counts.items(), key=lambda x: x[1]):
        if cnt <= 2:
            shortage.append({"dept": dept, "job": job, "count": cnt})

    return {"blocked": blocked, "critical": critical, "shortage": shortage}


# ============================================================
# HTML 渲染
# ============================================================
CSS = """
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif; margin: 0; background: #f0f2f5; color: #1a1a1a; }
  .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
  h1 { font-size: 24px; margin: 0 0 4px; }
  h2 { font-size: 18px; margin: 32px 0 12px; padding-bottom: 8px; border-bottom: 2px solid #dee2e6; }
  .meta { color: #868e96; font-size: 13px; margin-bottom: 24px; }

  /* 全局漏斗 */
  .funnel-bar { display: flex; align-items: flex-end; gap: 0; margin: 16px 0; height: 60px; }
  .funnel-cell { display: flex; flex-direction: column; align-items: center; justify-content: flex-end; min-width: 90px; padding: 8px 12px; border-radius: 8px 8px 0 0; margin: 0 3px; transition: transform 0.15s; }
  .funnel-cell:hover { transform: translateY(-3px); }
  .funnel-cell .num { font-size: 22px; font-weight: 700; color: #fff; }
  .funnel-cell .label { font-size: 12px; color: #fff; opacity: 0.9; margin-top: 2px; }
  .funnel-arrow { display: flex; align-items: flex-end; color: #ced4da; font-size: 16px; height: 40px; padding-bottom: 10px; }

  /* 工作室卡片 */
  .dept-card { background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .dept-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
  .dept-title { font-size: 17px; font-weight: 600; }
  .dept-count { font-size: 13px; color: #868e96; background: #f1f3f5; padding: 2px 10px; border-radius: 12px; }

  /* 岗位行 */
  .job-row { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid #f1f3f5; }
  .job-row:last-child { border-bottom: none; }
  .job-name { min-width: 180px; font-size: 14px; font-weight: 500; }
  .job-pipe { flex: 1; display: flex; gap: 3px; align-items: center; flex-wrap: wrap; }
  .pipe-dot { width: 22px; height: 22px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 11px; color: #fff; cursor: default; position: relative; }
  .pipe-dot:hover .dot-tip { display: block; }
  .dot-tip { display: none; position: absolute; bottom: 28px; left: 50%; transform: translateX(-50%); background: #333; color: #fff; padding: 4px 8px; border-radius: 4px; font-size: 12px; white-space: nowrap; z-index: 10; }
  .job-meta { font-size: 12px; color: #868e96; min-width: 160px; text-align: right; }
  .job-meta .tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; margin-left: 4px; }
  .tag-red { background: #fff5f5; color: #e03131; }
  .tag-orange { background: #fff4e6; color: #e8590c; }
  .tag-green { background: #ebfbee; color: #2f9e44; }

  /* 预警区 */
  .alert-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
  .alert-card { background: #fff; border-radius: 12px; padding: 16px 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .alert-card.full { grid-column: 1 / -1; }
  .alert-title { font-size: 15px; font-weight: 600; margin-bottom: 12px; display: flex; align-items: center; gap: 6px; }
  .alert-item { padding: 8px 0; border-bottom: 1px solid #f8f9fa; font-size: 13px; line-height: 1.6; }
  .alert-item:last-child { border-bottom: none; }
  .alert-item .person { font-weight: 600; }
  .alert-item .reason { color: #495057; }
  .alert-item .action { color: #1971c2; font-weight: 500; }
  .alert-item .badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; margin-left: 4px; font-weight: 600; }
  .badge-crit { background: #fff5f5; color: #e03131; }
  .badge-warn { background: #fff9db; color: #e67700; }
  .badge-block { background: #f3f0ff; color: #6741d9; }
"""


def render_funnel_bar(funnel):
    """渲染全局漏斗色块条（横向）"""
    if not funnel:
        return "<p>（无数据）</p>"
    parts = ['<div class="funnel-bar">']
    stages = list(funnel.items())
    for i, (stage, count) in enumerate(stages):
        color = FUNNEL_COLORS.get(stage, "#adb5bd")
        parts.append(f'<div class="funnel-cell" style="background:{color}">')
        parts.append(f'<div class="num">{count}</div>')
        parts.append(f'<div class="label">{stage}</div>')
        parts.append('</div>')
        if i < len(stages) - 1:
            parts.append('<div class="funnel-arrow">→</div>')
    parts.append('</div>')
    return "".join(parts)


def render_dept_card(dept, jobs):
    """渲染单个工作室卡片"""
    total = sum(len(cands) for cands in jobs.values())
    parts = [
        f'<div class="dept-card">',
        f'<div class="dept-header">',
        f'<span class="dept-title">🔹 {dept}</span>',
        f'<span class="dept-count">{total} 人在途</span>',
        f'</div>',
    ]

    for job, cands in jobs.items():
        # 渲染漏斗色块点（每人一个圆点，颜色=所在阶段）
        dots = []
        for c in cands:
            color = FUNNEL_COLORS.get(c["funnel_stage"], "#adb5bd")
            dwell = c.get("dwell_days", 0)
            tip = f'{c["name"]} · {c["funnel_stage"]} · 停{dwell}天'
            if c["conclusion"] == 2:
                tip += ' · 不通过'
            dots.append(f'<span class="pipe-dot" style="background:{color}"><span class="dot-tip">{tip}</span></span>')

        # 状态标签
        tags = []
        crit_dwell = [c for c in cands if c.get("dwell_days") and c["dwell_days"] >= 30]
        warn_dwell = [c for c in cands if c.get("dwell_days") and 15 <= c["dwell_days"] < 30]
        if crit_dwell:
            names = "、".join(c["name"] for c in crit_dwell)
            days = crit_dwell[0]["dwell_days"]
            tags.append(f'<span class="tag tag-red">{names}停{days}天🔴</span>')
        if warn_dwell:
            names = "、".join(c["name"] for c in warn_dwell)
            tags.append(f'<span class="tag tag-orange">{names}停{warn_dwell[0]["dwell_days"]}天🟠</span>')
        notpass = [c for c in cands if c.get("conclusion") == 2]
        if notpass:
            names = "、".join(c["name"] for c in notpass)
            tags.append(f'<span class="tag tag-red">{names}不通过</span>')
        # 人少标记
        if len(cands) <= 1:
            tags.append(f'<span class="tag tag-red">仅{len(cands)}人！</span>')

        parts.append(f'<div class="job-row">')
        parts.append(f'<span class="job-name">{job}</span>')
        parts.append(f'<span class="job-pipe">{"".join(dots)}</span>')
        parts.append(f'<span class="job-meta">{"".join(tags) or f"{len(cands)}人"}</span>')
        parts.append(f'</div>')

    parts.append('</div>')
    return "".join(parts)


def render_alerts(alerts):
    """渲染三类预警"""
    parts = ['<div class="alert-grid">']

    # 1. 高危停滞
    crit = alerts["critical"]
    parts.append('<div class="alert-card">')
    parts.append('<div class="alert-title">🔴 高危停滞（≥15天，流失风险）</div>')
    if crit:
        for c in crit:
            badge = "badge-crit" if c["dwell"] >= 30 else "badge-warn"
            parts.append(
                f'<div class="alert-item"><span class="person">{c["name"]}</span>'
                f' · {c["job"]}({c["dept"]})'
                f' <span class="badge {badge}">停{c["dwell"]}天</span>'
                f'<br><span class="reason">当前：{c["stage"]}阶段'
                f'{"，4轮全通过等offer" if c["conclusion"]==1 and c["dwell"]>=15 else ""}</span>'
                f'<br><span class="action">➜ 今日必须追问进度</span></div>'
            )
    else:
        parts.append('<div class="alert-item">✅ 暂无高危停滞</div>')
    parts.append('</div>')

    # 2. 面评阻塞
    blocked = alerts["blocked"]
    parts.append('<div class="alert-card">')
    parts.append('<div class="alert-title">🟣 面评阻塞（已面完等评，卡住流转）</div>')
    if blocked:
        for b in blocked:
            urgency_cls = "badge-crit" if "严重" in b.get("urgency", "") else ("badge-warn" if "常规" in b.get("urgency", "") else "badge-block")
            parts.append(
                f'<div class="alert-item"><span class="person">{b["name"]}</span>'
                f' · {b["round"]}等评'
                f' <span class="badge {urgency_cls}">欠{b["overdue"]}天</span>'
                f'<br><span class="reason">面试官：{b["interviewer"]}</span>'
                f'<br><span class="action">➜ 催{b["interviewer"].split("、")[0]}交面评</span></div>'
            )
    else:
        parts.append('<div class="alert-item">✅ 暂无面评阻塞</div>')
    parts.append('</div>')

    # 3. 急需补人
    shortage = [s for s in alerts["shortage"] if s["count"] <= 2]
    parts.append('<div class="alert-card full">')
    parts.append('<div class="alert-title">⚡ 急需补人（在途≤2人岗位）</div>')
    if shortage:
        for s in shortage:
            parts.append(
                f'<div class="alert-item"><span class="person">{s["job"]}</span>'
                f'({s["dept"]})'
                f' <span class="badge badge-crit">仅{s["count"]}人</span>'
                f'<br><span class="action">➜ 今日重点推简历</span></div>'
            )
    else:
        parts.append('<div class="alert-item">✅ 各岗位在途充裕</div>')
    parts.append('</div>')

    parts.append('</div>')
    return "".join(parts)


# ============================================================
# 图例
# ============================================================
def render_legend():
    parts = ['<div style="margin:12px 0;font-size:13px;color:#495057">']
    parts.append('<b>阶段色块：</b> ')
    for stage in FUNNEL_ORDER:
        color = FUNNEL_COLORS.get(stage, "#adb5bd")
        parts.append(f'<span style="display:inline-block;width:14px;height:14px;border-radius:50%;background:{color};vertical-align:middle;margin:0 2px"></span>{stage} ')
    parts.append(f'<span style="display:inline-block;width:14px;height:14px;border-radius:50%;background:{FUNNEL_COLORS["不通过"]};vertical-align:middle;margin:0 2px"></span>不通过')
    parts.append('</div>')
    return "".join(parts)


# ============================================================
# 主流程
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="生成招聘管道看板 HTML（v2）")
    ap.add_argument("--report", default="notes/_daily_review.json", help="对账报告 JSON 路径")
    ap.add_argument("--output", default="notes/pipeline-dashboard.html", help="输出 HTML 路径")
    args = ap.parse_args()

    report = load_report(args.report)
    people = extract_people(report)

    if not people:
        print("[⚠️] 报告里没有候选人数据，看板为空")

    # 聚合
    funnel = count_funnel(people)
    dept_groups = group_by_dept(people)
    alerts = analyze_alerts(people, report)
    report_date = report.get("date", "")
    report_weekday = report.get("weekday", "")
    generated = report.get("generated_at", "")

    # 渲染
    funnel_html = render_funnel_bar(funnel)
    legend_html = render_legend()
    dept_html = "".join(render_dept_card(dept, jobs) for dept, jobs in dept_groups.items())
    alert_html = render_alerts(alerts)

    total = len(people)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>招聘管道看板 · {report_date}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
<h1>📊 招聘管道看板</h1>
<div class="meta">{report_date}（{report_weekday}）· {total} 人在途 · 数据源 {args.report} · 生成于 {generated}</div>

<h2>全局漏斗</h2>
{funnel_html}
{legend_html}

<h2>分工作室管道分布</h2>
<p style="font-size:13px;color:#868e96;margin:-8px 0 12px">每个圆点 = 1位候选人，颜色 = 所在阶段。鼠标悬停看详情。</p>
{dept_html}

<h2>预警与推送提醒</h2>
{alert_html}

</div>
</body>
</html>"""

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[✅] 看板已生成: {args.output}（{total}人）")


if __name__ == "__main__":
    main()
