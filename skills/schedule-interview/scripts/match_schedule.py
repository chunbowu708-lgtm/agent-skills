# -*- coding: utf-8 -*-
"""
match_schedule.py — 面试时间协调核心脚本

做两件事：
  1. 调 lark-cli calendar +suggestion 拿面试官共同空闲块
  2. 本地把"候选人给的候选日"和空闲块求交，产出可约时段 + 可转发草稿

设计要点（踩坑固化）：
  - subprocess 调 lark-cli 用全路径 .cmd（Windows 下 "lark-cli" 报 WinError 2）
  - 星期推断用 datetime，绝不手算（手算必错）
  - 工作时间默认 09:00-18:00，午休 12:00-13:30 自动排除
  - 候选人时间可能是模糊的（"周四"=当天全天）或精确的（"周四 16:00"）
  - 输出写文件再让 AI Read，避免 Windows GBK 乱码
  - suggestion 返回的空闲块用"完全空闲"判断过滤冲突方案

用法：
  # 基础：1个面试官 + 多候选人各给候选日
  python match_schedule.py --interviewer 谢坤 --candidates "罗艺=周四,陈思宇=周四,刘涵辰=周五" --duration 60

  # 多面试官（取共同空闲）
  python match_schedule.py --interviewer 谢坤,潘腾飞 --candidates "张三=周三" --duration 60

  # 候选人给精确时段
  python match_schedule.py --interviewer 谢坤 --candidates "罗艺=周四 16:00" --duration 60

  # 自定义工作时间范围
  python match_schedule.py --interviewer 谢坤 --candidates "罗艺=周四" --duration 45 --work-start 10:00 --work-end 19:00
"""
import subprocess, json, sys, os, datetime, argparse, re
sys.stdout.reconfigure(encoding="utf-8")

CLI = r"C:\Users\wuchunbo\AppData\Roaming\npm\lark-cli.cmd"
CACHE = "notes/interviewers.json"
OUTPUT = "notes/_schedule_match.txt"

# 星期映射（周一=0）
WEEKDAY_MAP = {
    "周一": 0, "星期一": 0, "礼拜一": 0,
    "周二": 1, "星期二": 1, "礼拜二": 1,
    "周三": 2, "星期三": 2, "礼拜三": 2,
    "周四": 3, "星期四": 3, "礼拜四": 3,
    "周五": 4, "星期五": 4, "礼拜五": 4,
    "周六": 5, "星期六": 5, "礼拜六": 5,
    "周日": 6, "星期天": 6, "星期日": 6,
}

TZ = datetime.timezone(datetime.timedelta(hours=8))  # Asia/Shanghai


def run_lark(args):
    """调 lark-cli，返回解析后的 JSON。stdout+stderr 合并看（错误常在 stderr）。"""
    cmd = [CLI] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        data = json.loads(out)
        if not data.get("ok", True):
            print(f"[⚠️ lark-cli 返回 ok=false] {data.get('error',{})}")
        return data
    except json.JSONDecodeError:
        raise RuntimeError(f"lark-cli 返回非 JSON: {out[:500]}")
    except Exception as e:
        raise RuntimeError(f"调 lark-cli 失败: {e}")


def load_cache():
    """加载面试官缓存。按 open_id 存，但查找要按 name/alias。"""
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def find_in_cache(cache, name):
    """在缓存里按 name 或 alias 找面试官，返回 (open_id, info) 或 None。"""
    for oid, info in cache.items():
        if info.get("name") == name or name in info.get("alias", []):
            return oid, info
    return None


def search_interviewer(name):
    """缓存未命中时，调 contact +search-user 查 open_id 并写回缓存。"""
    print(f"[缓存未命中] 正在搜索面试官: {name}")
    data = run_lark([
        "contact", "+search-user",
        "--query", name,
        "--has-chatted",
        "--as", "user",
        "--format", "json",
    ])
    users = data.get("data", {}).get("users", [])
    if not users:
        return None
    # 取第一个匹配（has_chatted=true 已过滤掉陌生人）
    u = users[0]
    oid = u["open_id"]
    info = {
        "name": u.get("localized_name", name),
        "dept": u.get("department", ""),
        "email": u.get("email", ""),
        "alias": [],
    }
    return oid, info


def resolve_interviewers(names):
    """把姓名列表解析成 open_id 列表，缓存未命中的自动查询并写回。"""
    cache = load_cache()
    oids = []
    labels = []  # 用于输出展示
    changed = False
    for n in names:
        hit = find_in_cache(cache, n)
        if hit:
            oid, info = hit
            print(f"[缓存命中] {n} → {oid[:12]}... ({info.get('dept','')})")
        else:
            result = search_interviewer(n)
            if not result:
                print(f"[⚠️ 未找到] 面试官 '{n}' 搜不到，跳过")
                continue
            oid, info = result
            cache[oid] = info
            changed = True
            print(f"[已缓存] {n} → {oid[:12]}... ({info.get('dept','')})")
        oids.append(oid)
        labels.append(info.get("name", n))
    if changed:
        save_cache(cache)
    return oids, labels


def parse_candidate_time(raw, ref_date=None):
    """
    解析候选人给的时间，返回 (date_list, time_hint)。
      - "周四"          → ([绝对日期], None)
      - "周四 16:00"     → ([绝对日期], "16:00")
      - "周四下午"       → ([绝对日期], "下午")  → 后续映射到 14:00-18:00
      - "7-3" / "7月3日" → ([绝对日期], None)
    """
    ref = ref_date or datetime.date.today()
    raw = raw.strip()

    # 提取时间部分（如果有）
    time_hint = None
    time_match = re.search(r"(\d{1,2}[:：]\d{2}|上午|下午|早上|晚上)", raw)
    if time_match:
        time_hint = time_match.group(1).replace("：", ":")
        date_raw = raw.replace(time_match.group(0), "").strip()
    else:
        date_raw = raw

    # 解析日期
    dates = []
    # 星期
    for wk, idx in WEEKDAY_MAP.items():
        if wk in date_raw:
            today = ref
            delta = (idx - today.weekday()) % 7
            if delta == 0:
                delta = 7  # "本周四"如果今天就是周四，默认下周四（避免过去）
            d = today + datetime.timedelta(days=delta)
            dates.append(d)
            break
    # 月日（7-3 / 7月3日 / 07-03）
    if not dates:
        md = re.search(r"(\d{1,2})[-月](\d{1,2})", date_raw)
        if md:
            month, day = int(md.group(1)), int(md.group(2))
            year = ref.year
            try:
                dates.append(datetime.date(year, month, day))
            except ValueError:
                pass
    # "今天"/"明天"/"后天"
    if not dates:
        if "今天" in date_raw:
            dates.append(ref)
        elif "明天" in date_raw:
            dates.append(ref + datetime.timedelta(days=1))
        elif "后天" in date_raw:
            dates.append(ref + datetime.timedelta(days=2))

    if not dates:
        print(f"[⚠️ 解析失败] 无法识别日期: {raw}")
    return dates, time_hint


def get_freebusy_blocks(oids, start_iso, end_iso):
    """
    调 +suggestion 拿多面试官的共同空闲块。
    返回 [{"start": datetime, "end": datetime, "reason": str}, ...]，只保留"完全空闲"的。
    """
    attendee_ids = ",".join(oids)
    # 构造排除项：每天午休 12:00-13:30
    start_dt = datetime.datetime.fromisoformat(start_iso.replace("+08:00", "+08:00"))
    end_dt = datetime.datetime.fromisoformat(end_iso.replace("+08:00", "+08:00"))
    excludes = []
    cur = start_dt.date()
    while cur <= end_dt.date():
        excludes.append(f"{cur}T12:00:00+08:00~{cur}T13:30:00+08:00")
        cur += datetime.timedelta(days=1)
    exclude_str = ",".join(excludes)

    args = [
        "calendar", "+suggestion",
        "--start", start_iso,
        "--end", end_iso,
        "--attendee-ids", attendee_ids,
        "--duration-minutes", "60",
        "--exclude", exclude_str,
        "--format", "json",
    ]
    print(f"[查询中] 调用 suggestion 查 {len(oids)} 位面试官共同空闲，范围 {start_iso} ~ {end_iso}")
    data = run_lark(args)

    suggestions = data.get("data", {}).get("suggestions", [])
    blocks = []
    for s in suggestions:
        reason = s.get("recommend_reason", "")
        start = datetime.datetime.fromisoformat(s["event_start_time"])
        end = datetime.datetime.fromisoformat(s["event_end_time"])
        blocks.append({"start": start, "end": end, "reason": reason, "fully_free": "完全空闲" in reason})
    return blocks


def intersect(candidate_date, time_hint, blocks, duration_min, work_start=9, work_end=18):
    """
    把候选人的候选日（+可选时段偏好）和空闲块求交，输出可约的具体时刻列表。
    返回 [{"time": "HH:MM", "label": "..."}, ...]
    """
    avail_slots = []
    for b in blocks:
        if not b["fully_free"]:
            continue
        # 限定在候选人指定的那一天
        if b["start"].date() != candidate_date:
            continue
        # suggestion 返回的空闲块本身就是飞书算好的可约时段，直接用其起止切档
        # （不再用 work_start/work_end 二次截断——晚上面试也是有效的）
        slot_start = b["start"]
        slot_end = b["end"]
        if slot_end <= slot_start:
            continue
        # 切档（每 30 分钟一档，找够 duration_min 的）
        cur = slot_start
        while cur + datetime.timedelta(minutes=duration_min) <= slot_end:
            avail_slots.append(cur.strftime("%H:%M"))
            cur += datetime.timedelta(minutes=30)

    # 应用 time_hint 过滤
    if time_hint:
        if ":" in time_hint:
            # 精确时刻：找最接近的
            target_h, target_m = [int(x) for x in time_hint.split(":")]
            avail_slots = [s for s in avail_slots if int(s[:2]) >= target_h]
        elif time_hint in ("下午", "晚上"):
            avail_slots = [s for s in avail_slots if int(s[:2]) >= 13]
        elif time_hint == "上午":
            avail_slots = [s for s in avail_slots if int(s[:2]) < 12]
        elif time_hint == "早上":
            avail_slots = [s for s in avail_slots if int(s[:2]) < 10]

    return avail_slots


def _has_conflict(date, time_str, duration_min, occupied):
    """检查 (date, time_str) 这段 duration_min 的面试是否和已占用时段重叠。"""
    h, m = [int(x) for x in time_str.split(":")]
    new_start = datetime.datetime(date.year, date.month, date.day, h, m)
    new_end = new_start + datetime.timedelta(minutes=duration_min)
    for od, ot in occupied:
        oh, om = [int(x) for x in ot.split(":")]
        o_start = datetime.datetime(od.year, od.month, od.day, oh, om)
        o_end = o_start + datetime.timedelta(minutes=duration_min)
        # 区间重叠判断
        if new_start < o_end and o_start < new_end:
            return True
    return False


def weekday_cn(d):
    return "周" + "一二三四五六日"[d.weekday()]


def main():
    ap = argparse.ArgumentParser(description="面试时间协调")
    ap.add_argument("--interviewer", required=True, help="面试官姓名，逗号分隔（如 谢坤,潘腾飞）")
    ap.add_argument("--candidates", required=True, help='候选人及时间，逗号分隔（如 "罗艺=周四,陈思宇=周四 16:00"）')
    ap.add_argument("--duration", type=int, default=60, help="面试时长（分钟），默认 60")
    ap.add_argument("--days", type=int, default=7, help="查询未来几天，默认 7")
    ap.add_argument("--work-start", default="9", help="工作开始小时，默认 9")
    ap.add_argument("--work-end", default="18", help="工作结束小时，默认 18")
    ap.add_argument("--dry-run", action="store_true", help="只解析不查飞书")
    args = ap.parse_args()

    today = datetime.date.today()
    work_start = int(args.work_start.split(":")[0])
    work_end = int(args.work_end.split(":")[0])

    # ① 解析面试官
    interviewer_names = [n.strip() for n in args.interviewer.split(",")]
    oids, labels = resolve_interviewers(interviewer_names)
    if not oids:
        print("[❌ 没有有效的面试官，终止]")
        return

    # ② 解析候选人时间
    candidates = []
    for pair in args.candidates.split(","):
        if "=" not in pair:
            print(f"[⚠️ 跳过] 候选人格式错误（需 姓名=时间）: {pair}")
            continue
        name, raw_time = pair.split("=", 1)
        dates, time_hint = parse_candidate_time(raw_time.strip(), today)
        candidates.append({"name": name.strip(), "dates": dates, "time_hint": time_hint, "raw": raw_time.strip()})

    if args.dry_run:
        print("\n=== [dry-run] 解析结果 ===")
        print(f"面试官: {labels}")
        for c in candidates:
            print(f"  {c['name']}: 日期={[weekday_cn(d)+str(d) for d in c['dates']]}, 时段偏好={c['time_hint']}")
        return

    # ③ 查面试官共同空闲（覆盖所有候选人提到的日期 + 未来 N 天）
    all_dates = set()
    for c in candidates:
        for d in c["dates"]:
            all_dates.add(d)
    if not all_dates:
        print("[❌ 没有有效的候选人日期，终止]")
        return
    # suggestion 查询区间不能超过 7 天（飞书 API 限制，超了报 190014）
    # 策略：以候选人最早日期为起点，最多查 7 天（确保覆盖所有候选人日期）
    earliest = min(all_dates | {today})
    latest_candidate = max(all_dates)
    latest = min(latest_candidate, earliest + datetime.timedelta(days=6))  # 7天区间=起止差6天
    # 如果候选人日期跨度超过7天（罕见），分批查（这里先单批，超期则截断并告警）
    if latest_candidate > earliest + datetime.timedelta(days=6):
        print(f"[⚠️ 候选人日期跨度超过7天，本次只查 {earliest} ~ {latest}")
    start_iso = f"{earliest}T{work_start:02d}:00:00+08:00"
    end_iso = f"{latest}T{work_end:02d}:00:00+08:00"

    blocks = get_freebusy_blocks(oids, start_iso, end_iso)
    fully_free = [b for b in blocks if b["fully_free"]]
    print(f"[查询完成] 共 {len(blocks)} 个空闲块，其中 {len(fully_free)} 个完全空闲")

    # ④ 对每个候选人求交（同一面试官的时段不能重复分给多人）
    lines_result = []
    lines_draft_candidates = []  # 用于草稿
    occupied = set()  # 已分配给前面候选人的 (date, time)，避免同一面试官撞档
    for c in candidates:
        for d in c["dates"]:
            slots = intersect(d, c["time_hint"], blocks, args.duration, work_start, work_end)
            # 排除已被其他候选人占用的时段（按 30 分钟对齐，duration 内不能重叠）
            avail = [s for s in slots if not _has_conflict(d, s, args.duration, occupied)]
            wd = weekday_cn(d)
            date_str = f"{d.month}-{d.day}"
            if avail:
                # 建议策略：无偏好取最早的（让面试官先面完），有偏好取符合偏好的最早
                suggest = avail[0]
                occupied.add((d, suggest))
                # 展示用原始 slots（让用户看到全部可选项），标注建议
                show = avail if len(avail) <= 6 else avail[:6]
                lines_result.append(f"  {c['name']:<6} {wd}({date_str}) → ✅ 可约 {'/'.join(show)}（建议 {suggest}）")
                lines_draft_candidates.append((c["name"], wd, date_str, suggest))
            else:
                lines_result.append(f"  {c['name']:<6} {wd}({date_str}) → ❌ 该时段面试官无空档或已被其他候选人占用（{c['raw']}）")

    # ⑤ 生成草稿
    # 面试官称谓：缓存里 alias[0] 或姓名
    cache = load_cache()
    iv_label = labels[0]  # 多面试官取第一个的称谓（通常主面）
    # 从缓存拿 alias
    iv_alias = ""
    for oid, info in cache.items():
        if info.get("name") == interviewer_names[0] and info.get("alias"):
            iv_alias = info["alias"][0]
            break
    salutation = iv_alias or interviewer_names[0]

    if len(lines_draft_candidates) == 1:
        # 单人单时段：用你案例2的话术风格
        name, wd, ds, t = lines_draft_candidates[0]
        draft = f"{salutation}，这个候选人我沟通了一下，安排在 {wd}（{ds}）{t} 面试🆗吗"
    else:
        # 多人：用你案例1的话术风格
        body = "\n".join([f"{name}：\n{wd}：{t}" if False else f"{name} {wd} {t}" for name, wd, ds, t in lines_draft_candidates])
        draft = f"{salutation}，我和候选人沟通了一下，安排如下你看🆗不：\n{body}"

    # ⑥ 输出写文件（避免 GBK 乱码）
    out = []
    out.append("=== 匹配结果 ===")
    out.append(f"面试官：{ '、'.join(labels) }")
    # 面试官空闲摘要
    free_by_date = {}
    for b in fully_free:
        k = b["start"].date()
        free_by_date.setdefault(k, []).append(b)
    for d in sorted(free_by_date.keys()):
        slots_d = free_by_date[d]
        ranges = []
        for b in slots_d:
            ranges.append(f"{b['start'].strftime('%H:%M')}-{b['end'].strftime('%H:%M')}")
        out.append(f"  {weekday_cn(d)}({d.month}-{d.day}) 空闲: {', '.join(ranges)}")
    out.append("")
    out.append("候选人可约时段：")
    out.extend(lines_result)
    out.append("")
    out.append("=== 可转发草稿（发给面试官）===")
    out.append(draft)
    out.append("")
    out.append("=== 不可约的（如有，需重新和候选人协调）===")
    no_go = [l for l in lines_result if "❌" in l]
    out.extend(no_go if no_go else ["（无）"])

    text = "\n".join(out)
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\n[✅ 完成] 结果已写入 {OUTPUT}")
    print("\n" + text)


if __name__ == "__main__":
    main()
