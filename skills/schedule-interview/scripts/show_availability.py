# -*- coding: utf-8 -*-
"""
展示面试官本周（或指定区间）可约时段。

输出三种形式（skill 标准展示）：
  1. markdown 表格：日期 | 空闲时段 | 备注（自动生成"最宽裕/时段零碎/周末"等备注）
  2. 可排档位列表（默认 45 分钟/场切档，只列未来档位）
  3. --svg <path>：生成时间轴色段图（SVG），空闲绿/会议红/午休灰/已过与周末半透明

用法：
  python show_availability.py --interviewer 古振兴
  python show_availability.py --interviewer 谢坤,潘腾飞 --duration 60     # 多人：每人 + 共同档位
  python show_availability.py --interviewer 古振兴 --svg out.svg        # 同时生成色段图
  python show_availability.py --interviewer 古振兴 --start 2026-08-05 --days 3
  python show_availability.py --interviewer 古振兴 --work-start 10:00 --work-end 20:00  # 晚上面试
  python show_availability.py --interviewer 古振兴 --past               # 连已过档位一起列

与 match_schedule.py 共用 freebusy 反推逻辑（同一数据源），只展示不排候选人。
"""
import argparse
import datetime
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from match_schedule import resolve_interviewers, get_freebusy_blocks, TZ


def parse_hhmm(s):
    h, m = s.split(":")
    return int(h), int(m)


def fmt(dt):
    return dt.strftime("%H:%M")


def merge_segs(segs):
    segs = sorted(segs)
    merged = []
    for s, e in segs:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def build_table(days_info, duration, ws_str, we_str):
    """生成 markdown 表格：日期 | 空闲时段 | 备注"""
    lines = ["| 日期 | 空闲时段 | 备注 |", "|---|---|---|"]
    for d in days_info:
        label, seg_txt, remark, _ = d
        lines.append(f"| {label} | {seg_txt or '—'} | {remark} |")
    return "\n".join(lines)


def remark_for(day, today, segs, weekend_note, coverage):
    """备注列自动生成"""
    if day < today:
        return "已过去"
    wd = day.weekday()
    if wd >= 5:
        return "周末·一般不上班"
    if not segs:
        return "❌ 无可约时段"
    if coverage >= 6.5:
        return "最宽裕，几乎整天"
    if coverage >= 4:
        return "较宽裕"
    return "时段零碎"


def build_svg(title, subtitle, days_info, ws_h, we_h, today, now, duration, out_path):
    """生成时间轴色段图 SVG。
    days_info: [(label, seg_txt, remark, day_data)] 其中 day_data =
      {"date", "free": [(s,e),...], "busy": [(s,e),...], "weekend": bool, "past": bool}
    """
    X0, X1 = 120, 630
    W = X1 - X0
    ROW_H, GAP = 30, 10
    y0 = 84  # 首行 y
    n = len(days_info)
    rows_bottom = y0 + n * (ROW_H + GAP) - GAP
    AXIS_Y = rows_bottom + 10
    LABEL_Y = AXIS_Y + 22
    H = LABEL_Y + 26

    px_per_h = W / (we_h - ws_h)

    def tx(hhmm):
        h, m = int(hhmm[:2]), int(hhmm[3:5])
        return X0 + (h + m / 60 - ws_h) * px_per_h

    parts = []
    parts.append(f'<svg viewBox="0 0 680 {H}" width="100%" xmlns="http://www.w3.org/2000/svg" role="img" font-family="var(--font-sans)">')
    parts.append(f"<title>{title} 可约时段</title>")
    parts.append("<desc>面试官可约时段色段图：绿=空闲可约，红=已有会议，灰=午休，半透明=已过/周末</desc>")
    parts.append(f'<text x="40" y="24" font-size="15" font-weight="500" fill="#2C2C2A">{title}</text>')
    parts.append(f'<text x="40" y="42" font-size="12" fill="#5F5E5A">{subtitle}</text>')
    # 图例
    ly = 54
    parts.append(f'<rect x="120" y="{ly}" width="14" height="14" rx="2" fill="#9FE1CB"/>')
    parts.append(f'<text x="140" y="{ly+11}" font-size="12" fill="#444441">空闲可约</text>')
    parts.append(f'<rect x="225" y="{ly}" width="14" height="14" rx="2" fill="#F7C1C1"/>')
    parts.append(f'<text x="245" y="{ly+11}" font-size="12" fill="#444441">已有会议</text>')
    parts.append(f'<rect x="330" y="{ly}" width="14" height="14" rx="2" fill="#D3D1C7"/>')
    parts.append(f'<text x="350" y="{ly+11}" font-size="12" fill="#444441">午休</text>')
    if today.weekday() < 5:
        now_x = tx(now.strftime("%H:%M"))
        if X0 < now_x < X1:
            parts.append(f'<line x1="{now_x}" y1="58" x2="{now_x+18}" y2="{ly+11}" stroke="#5F5E5A" stroke-width="1" stroke-dasharray="3,2"/>')
            parts.append(f'<text x="{now_x+24}" y="{ly+11}" font-size="12" fill="#444441">现在 {now.strftime("%H:%M")}</text>')

    for i, d in enumerate(days_info):
        label, _, _, data = d
        y = y0 + i * (ROW_H + GAP)
        day = data["date"]
        wd = "一二三四五六日"[day.weekday()]
        past = day < today
        weekend = day.weekday() >= 5

        def blk_op(s, e):
            # 已过整日/周末整行半透明；今天则按块是否已过（块结束 <= now → 半透明）
            if past or weekend:
                return "0.45"
            if day == today and e <= now:
                return "0.45"
            return "1"
        # 行背景
        parts.append(f'<rect x="{X0}" y="{y}" width="{W}" height="{ROW_H}" rx="3" fill="#F1EFE8"/>')
        parts.append(f'<text x="112" y="{y+15}" font-size="13" font-weight="500" fill="#2C2C2A" text-anchor="end">{label}</text>')
        # 工作时段截断显示
        day_s = datetime.datetime(day.year, day.month, day.day, ws_h, 0, tzinfo=TZ)
        day_e = datetime.datetime(day.year, day.month, day.day, we_h, 0, tzinfo=TZ)
        # 午休灰块（12:00-13:30 且在窗口内）
        lunch_s = datetime.datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
        lunch_e = datetime.datetime(day.year, day.month, day.day, 13, 30, tzinfo=TZ)
        ls, le = max(lunch_s, day_s), min(lunch_e, day_e)
        if le > ls:
            parts.append(f'<rect x="{tx(ls.strftime("%H:%M"))}" y="{y}" width="{max(2, tx(le.strftime("%H:%M"))-tx(ls.strftime("%H:%M")))}" height="{ROW_H}" rx="3" fill="#D3D1C7" opacity="{blk_op(lunch_s, lunch_e)}"/>')
        # 空闲绿块
        for s, e in data.get("free", []):
            ss, ee = max(s, day_s), min(e, day_e)
            if ee > ss:
                parts.append(f'<rect x="{tx(ss.strftime("%H:%M"))}" y="{y}" width="{max(2, tx(ee.strftime("%H:%M"))-tx(ss.strftime("%H:%M")))}" height="{ROW_H}" rx="3" fill="#9FE1CB" opacity="{blk_op(ss, ee)}"/>')
        # 忙碌红块
        for s, e in data.get("busy", []):
            ss, ee = max(s, day_s), min(e, day_e)
            if ee > ss:
                parts.append(f'<rect x="{tx(ss.strftime("%H:%M"))}" y="{y}" width="{max(2, tx(ee.strftime("%H:%M"))-tx(ss.strftime("%H:%M")))}" height="{ROW_H}" rx="3" fill="#F7C1C1" opacity="{blk_op(ss, ee)}"/>')
        # 今天当前时刻竖线
        if day == today:
            now_x = tx(now.strftime("%H:%M"))
            if X0 < now_x < X1:
                parts.append(f'<line x1="{now_x}" y1="{y}" x2="{now_x}" y2="{y+ROW_H}" stroke="#5F5E5A" stroke-width="1.2" stroke-dasharray="4,3"/>')
        # 右侧标记
        if past:
            parts.append(f'<text x="636" y="{y+15}" font-size="11" fill="#A32D2D" text-anchor="end">已过</text>')
        elif weekend:
            parts.append(f'<text x="636" y="{y+15}" font-size="11" fill="#888780" text-anchor="end">周末</text>')

    # 时间轴
    parts.append(f'<line x1="{X0}" y1="{AXIS_Y}" x2="{X1}" y2="{AXIS_Y}" stroke="#D3D1C7" stroke-width="0.5"/>')
    for h in range(ws_h, we_h + 1):
        x = X0 + (h - ws_h) * px_per_h
        parts.append(f'<text x="{x}" y="{LABEL_Y}" font-size="11" fill="#888780" text-anchor="middle">{h}:00</text>')
    parts.append("</svg>")
    svg = "\n".join(parts)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="展示面试官可约时段（表格 + 档位 + 色段图）")
    ap.add_argument("--interviewer", required=True, help="面试官姓名，逗号分隔")
    ap.add_argument("--duration", type=int, default=45, help="每场时长（分钟），默认 45")
    ap.add_argument("--start", default="", help="起始日期 YYYY-MM-DD，默认本周一")
    ap.add_argument("--days", type=int, default=7, help="展示天数，默认 7")
    ap.add_argument("--work-start", default="09:00", help="工作开始，默认 09:00")
    ap.add_argument("--work-end", default="18:00", help="工作结束，默认 18:00")
    ap.add_argument("--past", action="store_true", help="连已过档位一起列出")
    ap.add_argument("--svg", default="", help="生成色段图 SVG 文件路径（如 availability.svg）")
    args = ap.parse_args()

    ws_h, ws_m = parse_hhmm(args.work_start)
    we_h, we_m = parse_hhmm(args.work_end)
    today = datetime.date.today()
    if args.start:
        start = datetime.date.fromisoformat(args.start)
    else:
        start = today - datetime.timedelta(days=today.weekday())
    end = start + datetime.timedelta(days=args.days - 1)

    interviewer_names = [n.strip() for n in args.interviewer.split(",")]
    oids, labels = resolve_interviewers(interviewer_names)
    if not oids:
        print("[❌] 面试官解析失败，终止")
        return

    start_iso = f"{start}T00:00:00+08:00"
    end_iso = f"{end}T23:59:59+08:00"
    blocks, busy_by_date, _ = get_freebusy_blocks(oids, start_iso, end_iso, labels)

    now = datetime.datetime.now(TZ)
    wd_cn = "一二三四五六日"

    print(f"\n=== {'、'.join(labels)} 可约时段（{args.duration}分钟/场）===")
    print(f"查询区间：{start.month}/{start.day}（周{wd_cn[start.weekday()]}）~ "
          f"{end.month}/{end.day}（周{wd_cn[end.weekday()]}）｜"
          f"工作时段 {args.work_start}-{args.work_end}｜午休 12:00-13:30 已排除"
          + ("" if args.past else "｜仅列未来档位"))

    # 按天聚合：空闲段（工作时段截断）+ 忙碌段
    free_by_day = {}
    busy_by_day = {}
    for b in blocks:
        if not b.get("fully_free"):
            continue
        day = b["start"].date()
        win_s = datetime.datetime(day.year, day.month, day.day, ws_h, ws_m, tzinfo=TZ)
        win_e = datetime.datetime(day.year, day.month, day.day, we_h, we_m, tzinfo=TZ)
        s, e = max(b["start"], win_s), min(b["end"], win_e)
        if e > s:
            free_by_day.setdefault(day, []).append((s, e))
    for d, lst in busy_by_date.items():
        busy_by_day[d] = lst

    def gen_slots(day, segs):
        out = []
        for s, e in segs:
            cur = s
            while cur + datetime.timedelta(minutes=args.duration) <= e:
                if args.past or cur >= now:
                    out.append(cur.strftime("%H:%M"))
                cur += datetime.timedelta(minutes=args.duration)
        return out

    days_info = []
    print()
    for d in (start + datetime.timedelta(days=i) for i in range(args.days)):
        label = f"{d.month}/{d.day}（周{wd_cn[d.weekday()]}）"
        segs = merge_segs(free_by_day.get(d, []))
        busy_segs = [(s, e) for s, e, _ in busy_by_day.get(d, [])]
        coverage = sum((e - s).total_seconds() / 3600 for s, e in segs)
        seg_txt = "、".join(f"{fmt(s)}-{fmt(e)}" for s, e in segs)
        remark = remark_for(d, today, segs, True, coverage)
        days_info.append((label, seg_txt, remark, {
            "date": d, "free": segs, "busy": busy_segs,
            "weekend": d.weekday() >= 5, "past": d < today,
        }))
        if d < today:
            print(f"{label}: 已过去，跳过")
            continue
        if not segs:
            print(f"{label}: ❌ 无可约时段")
            continue
        slots = gen_slots(d, segs)
        if not slots:
            print(f"{label}: 空闲 {seg_txt}，但今日已过时段，无可排未来档位")
            continue
        am = [x for x in slots if int(x[:2]) < 12]
        pm = [x for x in slots if int(x[:2]) >= 13]
        parts = []
        if am:
            parts.append("上午: " + " ".join(am))
        if pm:
            parts.append("下午: " + " ".join(pm))
        print(f"{label}: 空闲 {seg_txt}")
        print(f"  可排档位（{'/'.join(parts)}）")

    # markdown 表格（skill 标准展示形式）
    print("\n=== 表格（复制用）===")
    print(build_table(days_info, args.duration, args.work_start, args.work_end))

    # 多人共同档位
    if len(oids) > 1:
        print("\n=== 共同可排档位 ===")
        for d in (start + datetime.timedelta(days=i) for i in range(args.days)):
            if d < today:
                continue
            segs = merge_segs(free_by_day.get(d, []))
            slots = gen_slots(d, segs)
            if slots:
                print(f"{d.month}/{d.day}（周{wd_cn[d.weekday()]}）: " + " ".join(slots))
        print("\n（说明：上方为空闲段反推交集，真实会议见飞书日历；档位为建议起点，需按会议时长复查）")

    # SVG 色段图
    if args.svg:
        title = f"{'、'.join(labels)} 可约时段"
        subtitle = (f"工作时段 {args.work_start}-{args.work_end} · 已排除午休 12:00-13:30 · "
                    f"半透明色块 = 已过时段 / 周末 · {args.duration}分钟/场切档")
        out = build_svg(title, subtitle, days_info, ws_h, we_h, today, now, args.duration, args.svg)
        print(f"\n[✅] 色段图已生成: {out}")


if __name__ == "__main__":
    main()
