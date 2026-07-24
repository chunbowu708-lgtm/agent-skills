# -*- coding: utf-8 -*-
"""
match_schedule.py — 面试时间协调核心脚本

做三件事（输出透明四段，每个时间都可追溯到 freebusy 实查）：
  1. 调 lark-cli calendar +freebusy 拿面试官真实忙碌段（ground truth）
  2. 反推空闲段（全天-会议-午休），多面试官取交集
  3. 空闲 ∩ 候选人日期求交，按黄金时段选建议，产出可转发草稿

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

# 复用项目共享库（收口 cli/extract_json，含 MSYS_NO_PATHCONV + utf-8 encoding + timeout）
PROJECT_ROOT = "F:/miniwanob"
sys.path.insert(0, f"{PROJECT_ROOT}/notes")
from _lark_shared import cli as _lark_cli, extract_json  # noqa: E402

CLI = r"C:\Users\wuchunbo\AppData\Roaming\npm\lark-cli.cmd"
CACHE = f"{PROJECT_ROOT}/notes/interviewers.json"
ATS_FILE = f"{PROJECT_ROOT}/notes/_daily_review.json"  # 岗位/进展/轮次的数据源（recruit-followup 产出）

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
    """调 lark-cli，返回解析后的 JSON dict。走 _lark_shared.cli（已设 MSYS_NO_PATHCONV + timeout + encoding）。
    ok=false 时区分 token 类错误（99991663/99991664）明确提示，避免和"无数据"混淆。"""
    raw = _lark_cli(args)  # stdout+stderr 合并文本（_lark_shared.cli 已含 env/encoding/timeout）
    data = extract_json(raw)
    if data is None:
        raise RuntimeError(f"lark-cli 返回非 JSON: {raw[:500]}")
    if not data.get("ok", True):
        err = data.get("error", {}) or {}
        code = err.get("code") or data.get("code")
        # token 类错误码（飞书通用）：99991663 token 过期 / 99991664 token 无效 / 99991661 缺权限
        if str(code) in ("99991663", "99991664", "99991661"):
            print(f"[❌ 鉴权失败 code={code}] {err.get('message','') or err}")
            print("    可能是 tenant_access_token 过期（2小时有效）。建议重跑触发 token 刷新，或检查 lark-cli 登录状态。")
        else:
            print(f"[⚠️ lark-cli 返回 ok=false code={code}] {err}")
    return data


def load_cache():
    """加载面试官缓存。按 open_id 存，但查找要按 name/alias。
    文件损坏（并发写坏/手动编辑坏）时降级返回 {}，不阻塞主流程。"""
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[⚠️ 缓存损坏] {CACHE}: {e}，降级用空缓存（首次跑会重建）")
            return {}
    return {}


def save_cache(cache):
    """原子写：先写 .tmp 再 os.replace，避免并发写一半被另一进程读到坏文件。
    无文件锁——多 agent 并发写仍可能后写覆盖先写（丢失新条目），但不会留下损坏的 JSON。"""
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CACHE)  # 原子替换（Windows/POSIX 都支持）


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
        "--as", "user",
        "--format", "json",
    ])
    users = data.get("data", {}).get("users", [])
    if not users:
        return None
    # 取第一个匹配（故意不带 --has-chatted，避免陌生人面试官被过滤——见 SKILL.md 踩坑固化第10条）
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


# ============================================================
# ATS 数据消费（吃 _daily_review.json，自动补草稿五要素）
# ============================================================
def load_ats_data(ats_file=None):
    """读 _daily_review.json，返回 (by_tid, by_name) 两个索引。
    by_tid: {talent_id: ats_record}（精确匹配，AI 传 --talent-ids 时用）
    by_name: {name: [ats_record, ...]}（按姓名兜底，同名可能多条）
    """
    path = ats_file or ATS_FILE
    if not os.path.exists(path):
        return {}, {}
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"[⚠️ ATS 读取失败] {path}: {e}")
        return {}, {}
    ats = d.get("structured", {}).get("ats", []) or []
    by_tid = {a["talent_id"]: a for a in ats if a.get("talent_id")}
    by_name = {}
    for a in ats:
        nm = a.get("name")
        if nm:
            by_name.setdefault(nm, []).append(a)
    return by_tid, by_name


def match_ats(name, talent_id, by_tid, by_name):
    """为候选人匹配 ATS 记录。优先 talent_id 精确匹配，回退姓名。
    返回 ats_record 或 None。同名歧义时返回 None 并打印告警（让 AI 介入）。
    """
    if talent_id and talent_id in by_tid:
        return by_tid[talent_id]
    hits = by_name.get(name, [])
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        print(f"[⚠️ 同名歧义] '{name}' 在 ATS 有 {len(hits)} 条，请传 --talent-ids 精确指定")
        return None
    return None


# 面试轮次角色映射（interview_count → 这轮叫什么面）
# 写死规则，不再靠 AI 临场编。可按团队实际流程微调。
_ROUND_ROLE = {
    1: "初面",
    2: "复试",
    3: "三面",
    4: "四面",
    5: "五面",
}


def progress_text(stage, interview_count, latest_conclusion):
    """把 ATS 字段转成草稿话术。
    返回 (progress_str, role_str)：
      progress_str: "2面已通过，安排3面" / "初面"
      role_str: 这轮角色（"初面"/"复试"/"三面"/...）
    缺字段时返 (None, None)——调用方据此标【需问用户】。
    """
    # 非"面试"阶段不算面试轮次（如简历评估/Offer 阶段），返 None 让 AI 处理
    if stage != "面试":
        return None, None
    n = int(interview_count or 0)
    if n == 0:
        return None, None
    role = _ROUND_ROLE.get(n, f"第{n}面")
    # latest_conclusion: 1=通过, 2=不通过, null=未提交面评
    if n == 1:
        # 还没面过，这是初面
        return f"安排{role}", role
    # N>=2：上轮(N-1)已通过，安排这轮
    prev_role = _ROUND_ROLE.get(n - 1, f"第{n-1}面")
    if latest_conclusion == 1:
        return f"{prev_role}已通过，安排{role}", role
    elif latest_conclusion == 2:
        return f"{prev_role}（⚠️ 上轮conclusion=不通过，请人工核实）", role
    else:
        # 未提交面评，按流程推进
        return f"{prev_role}已面，安排{role}", role


def build_header(ats_records, form, team_override=None):
    """构造草稿标题行：{团队}{岗位}{形式}面试
    多个候选人岗位/团队不同时，取第一个有效的；都缺则返 None 让 AI 补。
    """
    for a in ats_records:
        if not a:
            continue
        dept = team_override or a.get("dept", "")
        job = a.get("job", "")
        if dept or job:
            return f"{dept}{job}{'视频' if form == '视频' else '现场'}面试"
    return None  # 全空


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
    # 优先匹配精确时刻 HH:MM / 时段词 / 时间范围（如"9-11点"）
    time_match = re.search(r"(\d{1,2}[:：]\d{2}|上午|下午|早上|晚上)", raw)
    if time_match:
        time_hint = time_match.group(1).replace("：", ":")
        date_raw = raw.replace(time_match.group(0), "").strip()
    else:
        # 时间范围如"9-11点"——取起始小时做 time_hint，避免被月日正则误匹配成"9月11日"
        range_match = re.search(r"(\d{1,2})[-~～到](\d{1,2})\s*点", raw)
        if range_match:
            start_h = int(range_match.group(1))
            time_hint = f"{start_h:02d}:00"
            date_raw = raw.replace(range_match.group(0), "").strip()
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


def _parse_iso(s):
    return datetime.datetime.fromisoformat(s)


def _subtract_busy(window_start, window_end, busy_list):
    """从 [window_start, window_end] 区间里减去 busy_list（多个忙碌段），返回空闲段列表。
    输入输出都是 (start_dt, end_dt) 元组。"""
    free = [(window_start, window_end)]
    for bs, be in busy_list:
        # be >= window_start 且 bs <= window_end 才有交集
        if be <= window_start or bs >= window_end:
            continue
        new_free = []
        for fs, fe in free:
            # 拆分：[fs,fe] 减去 [bs,be]
            if be <= fs or bs >= fe:
                new_free.append((fs, fe))  # 无交集
            else:
                if bs > fs:
                    new_free.append((fs, min(bs, fe)))
                if be < fe:
                    new_free.append((max(be, fs), fe))
        free = new_free
    return free


def get_freebusy_blocks(oids, start_iso, end_iso, labels=None):
    """
    用 freebusy 拿每个人真实忙碌，反推空闲段，多人取交集。
    返回 (blocks, busy_by_date)：
      - blocks: [{"start","end","reason","fully_free":True}] 反推空闲段（已扣除午休）
      - busy_by_date: {date: [(start_dt, end_dt, who_or_reason), ...]} 真实会议段
                      （只含 freebusy 返回的会议，不含午休；午休单独标"午休"）

    注意：suggestion 接口只返回飞书"挑出来的建议"不是完整空闲列表（实测漏报严重：
    人越空漏得越多，曾导致全天空闲的人显示为"无空档"）。所以用 freebusy 反推。
    """
    window_start = _parse_iso(start_iso)
    window_end = _parse_iso(end_iso)

    # 构造午休忙碌段（每天 12:00-13:30，作为"必须排除"加入 busy）
    lunch_busy = []
    cur = window_start.date()
    while cur <= window_end.date():
        lunch_busy.append((
            datetime.datetime(cur.year, cur.month, cur.day, 12, 0, tzinfo=TZ),
            datetime.datetime(cur.year, cur.month, cur.day, 13, 30, tzinfo=TZ),
        ))
        cur += datetime.timedelta(days=1)

    labels = labels or [str(i) for i in range(len(oids))]
    print(f"[查询中] 调用 freebusy 查 {len(oids)} 位面试官真实忙碌，范围 {start_iso} ~ {end_iso}")

    # 每个人反推空闲，存每人的空闲段集合；同时收集真实忙碌段（带姓名归属）
    per_person_free = []
    real_busy = []  # [(start, end, who_label)] 真实会议，不含午休
    for oid, label in zip(oids, labels):
        data = run_lark([
            "calendar", "+freebusy",
            "--user-id", oid,
            "--start", start_iso,
            "--end", end_iso,
            "--format", "json",
        ])
        # freebusy 返回平铺数组（非 data 下），兼容两种结构
        items = data.get("data", [])
        if items is None:
            items = []
        if isinstance(items, dict):
            items = items.get("items", []) or items.get("freebusy_list", [])
        # 区分"面试官真没会" vs "API 失败返空（token 过期/open_id 失效）"
        if not data.get("ok", True) and not items:
            print(f"  [⚠️ 空结果可能异常] {label}({oid[:12]}...) freebusy 返回空且 ok=false——"
                  f"可能是 token 过期或 open_id 失效，反推出的'全空闲'不一定真实。建议核对飞书日历。")
        busy = []
        for it in items:
            bs = _parse_iso(it["start_time"])
            be = _parse_iso(it["end_time"])
            busy.append((bs, be))
            real_busy.append((bs, be, label))
        busy.extend(lunch_busy)  # 午休当忙碌排除（但只在反推用，不进 real_busy）
        free = _subtract_busy(window_start, window_end, busy)
        per_person_free.append(free)
        print(f"  {label}({oid[:12]}...) 忙碌 {len(items)} 段，反推空闲 {len(free)} 段")

    # 多人取交集（单人直接用）
    if len(per_person_free) == 1:
        common_free = per_person_free[0]
    else:
        common_free = per_person_free[0]
        for other in per_person_free[1:]:
            merged = []
            for (s1, e1) in common_free:
                for (s2, e2) in other:
                    s, e = max(s1, s2), min(e1, e2)
                    if s < e:
                        merged.append((s, e))
            common_free = merged

    blocks = []
    for s, e in common_free:
        # 跨天的空闲段按自然天拆分（避免显示 "15:30-11:00" 这种倒序段）
        day = s.date()
        while day <= e.date():
            day_start = datetime.datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
            day_end = day_start + datetime.timedelta(days=1)
            seg_s, seg_e = max(s, day_start), min(e, day_end)
            if seg_s < seg_e:
                blocks.append({"start": seg_s, "end": seg_e, "reason": "freebusy反推空闲", "fully_free": True})
            day += datetime.timedelta(days=1)

    # 真实会议按天聚合（同一时段多人有会，合并归属）
    busy_by_date = {}
    for bs, be, who in real_busy:
        busy_by_date.setdefault(bs.date(), []).append((bs, be, who))
    return blocks, busy_by_date, lunch_busy


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
        # freebusy 反推的空闲段是全天，必须按工作时间截断
        # 默认 09:00-18:00（晚上面试有效时用户传 --work-end 21 等）
        day = candidate_date
        win_start = datetime.datetime(day.year, day.month, day.day, work_start, 0, tzinfo=TZ)
        win_end = datetime.datetime(day.year, day.month, day.day, work_end, 0, tzinfo=TZ)
        slot_start = max(b["start"], win_start)
        slot_end = min(b["end"], win_end)
        if slot_end <= slot_start:
            continue
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


def _slot_priority(time_str):
    """面试时间优先级评分（越小越优先）。面试黄金时段：11:00、15:00-18:00。
    09:00 过早、12:00-13:00 午休前后、18:00+ 偏晚都靠后。"""
    h, m = int(time_str[:2]), int(time_str[3:5])
    minutes = h * 60 + m
    # 偏好锚点：11:00(660)、15:00(900)、16:00(960)、17:00(1020) 取最近距离
    anchors = [660, 900, 960, 1020]
    return min(abs(minutes - a) for a in anchors)


def best_slot(avail, time_hint=None):
    """从可选时段里挑最佳。
    - time_hint 是精确时刻（HH:MM）→ 优先选离该时刻最近的（尊重候选人明确给的时间点）
    - 否则 → 面试黄金时段优先（11点/下午3-6点）
    """
    if not avail:
        return None
    if time_hint and ":" in time_hint:
        target_h, target_m = [int(x) for x in time_hint.split(":")]
        target_minutes = target_h * 60 + target_m
        return min(avail, key=lambda s: abs(int(s[:2]) * 60 + int(s[3:5]) - target_minutes))
    return min(avail, key=_slot_priority)


def time_cn(time_str):
    """'HH:MM' → 中文时间习惯：'上午11点' / '下午4点' / '下午3点半'。
    12点前=上午，12-18=下午，18+=晚上。整点不带'分'，半点用'半'。"""
    h, m = int(time_str[:2]), int(time_str[3:5])
    if h < 12:
        period = "上午"
        h12 = h
    elif h < 18:
        period = "下午"
        h12 = h - 12 if h > 12 else 12
    else:
        period = "晚上"
        h12 = h - 12
    if m == 0:
        return f"{period}{h12}点"
    elif m == 30:
        return f"{period}{h12}点半"
    else:
        return f"{period}{h12}点{m:02d}分"


def weekday_cn(d):
    return "周" + "一二三四五六日"[d.weekday()]


def main():
    ap = argparse.ArgumentParser(description="面试时间协调（吃 ATS 出完整草稿）")
    ap.add_argument("--interviewer", required=True, help="面试官姓名，逗号分隔（如 谢坤,潘腾飞）")
    ap.add_argument("--candidates", required=True, help='候选人及时间，逗号分隔（如 "罗艺=周四,陈思宇=周四 16:00"）')
    ap.add_argument("--talent-ids", default="", help="候选人 talent_id，逗号分隔，与 --candidates 顺序对齐（可选，用于精确匹配 ATS）")
    ap.add_argument("--form", default="视频", choices=["视频", "现场"], help="面试形式，默认 视频")
    ap.add_argument("--team", default="", help="团队名覆盖（草稿标题用，默认取 ATS dept）")
    ap.add_argument("--ats-file", default=ATS_FILE, help=f"ATS 数据源 JSON 路径（默认 {ATS_FILE}）")
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

    # ② 解析候选人时间 + 匹配 ATS
    # 支持格式：姓名=时间   或   姓名=时间@岗位方向（@后是岗位简称，用于草稿展示）
    talent_ids = [t.strip() for t in args.talent_ids.split(",") if t.strip()] if args.talent_ids else []
    by_tid, by_name = load_ats_data(args.ats_file)
    if by_tid:
        print(f"[ATS] 加载 {len(by_tid)} 条候选人记录（{args.ats_file}）")
    else:
        print(f"[ATS] 无数据或读取失败（{args.ats_file}），草稿岗位/进展将标【需问用户】")

    candidates = []
    for idx, pair in enumerate(args.candidates.split(",")):
        if "=" not in pair:
            print(f"[⚠️ 跳过] 候选人格式错误（需 姓名=时间 或 姓名=时间@岗位）: {pair}")
            continue
        name, raw_time = pair.split("=", 1)
        name = name.strip()
        # 拆出 @岗位方向（可选，草稿展示用）
        role = ""
        if "@" in raw_time:
            raw_time, role = raw_time.rsplit("@", 1)
            role = role.strip()
        raw_time = raw_time.strip()
        dates, time_hint = parse_candidate_time(raw_time, today)
        # 周末告警（不强制排除，业务上偶尔有周末面试）：让用户看到"这是周末"再决定
        for d in dates:
            if d.weekday() >= 5:  # 5=周六, 6=周日
                wd_cn = "周六" if d.weekday() == 5 else "周日"
                print(f"[⚠️ 周末] {name} 给的 {d.isoformat()} 是{wd_cn}，公司通常不上班——草稿若生成周末时段请人工确认")
        # 匹配 ATS（优先 talent_id，回退姓名）
        tid = talent_ids[idx] if idx < len(talent_ids) else ""
        ats = match_ats(name, tid, by_tid, by_name)
        candidates.append({
            "name": name, "dates": dates, "time_hint": time_hint, "raw": raw_time,
            "role": role,  # @后的岗位方向（草稿展示用，可选）
            "talent_id": tid, "ats": ats,
        })

    if args.dry_run:
        print("\n=== [dry-run] 解析结果 ===")
        print(f"面试官: {labels}")
        print(f"形式: {args.form}")
        for c in candidates:
            role_tag = f" [{c['role']}]" if c["role"] else ""
            ats = c.get("ats") or {}
            ats_str = f" ATS={ats.get('job','?')}/{ats.get('stage','?')}/面{ats.get('interview_count','?')}" if ats else " ATS=【缺】"
            tid_str = f" tid={c['talent_id']}" if c["talent_id"] else ""
            print(f"  {c['name']}{role_tag}{tid_str}{ats_str}: 日期={[weekday_cn(d)+str(d) for d in c['dates']]}, 时段偏好={c['time_hint']}")
        return

    # ③ 查面试官共同空闲（覆盖所有候选人提到的日期 + 未来 N 天）
    all_dates = set()
    for c in candidates:
        for d in c["dates"]:
            all_dates.add(d)
    if not all_dates:
        print("[❌ 没有有效的候选人日期，终止]")
        return
    # freebusy 无 7 天限制（只有 suggestion 才报 190014，本项目已弃用 suggestion）。
    # 查询区间 = earliest ~ earliest + args.days 天，覆盖 --days 参数。候选人日期超出此窗口会被截断告警。
    earliest = min(all_dates | {today})
    latest_candidate = max(all_dates)
    latest = min(latest_candidate, earliest + datetime.timedelta(days=args.days - 1))
    if latest_candidate > earliest + datetime.timedelta(days=args.days - 1):
        print(f"[⚠️ 候选人日期跨度超过 --days={args.days} 天，本次只查 {earliest} ~ {latest}，"
              f"超期未查（{latest_candidate} 等）。可加大 --days 扩大窗口。")
    start_iso = f"{earliest}T{work_start:02d}:00:00+08:00"
    end_iso = f"{latest}T{work_end:02d}:00:00+08:00"

    blocks, busy_by_date, lunch_busy = get_freebusy_blocks(oids, start_iso, end_iso, labels)
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
                # 建议策略：候选人有精确时刻→贴他给的时间点；否则→面试黄金时段（11点/下午3-6点）
                suggest = best_slot(avail, c["time_hint"])
                occupied.add((d, suggest))
                # 展示按时段优先级排序（黄金时段靠前），最多6个
                avail_sorted = sorted(avail, key=_slot_priority)
                show = avail_sorted if len(avail_sorted) <= 6 else avail_sorted[:6]
                lines_result.append(f"  {c['name']:<6} {wd}({date_str}) → ✅ 可约 {'/'.join(show)}（建议 {suggest}）")
                lines_draft_candidates.append((c["name"], wd, date_str, suggest, c["raw"], c.get("role", "")))
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

    # ⑤ 生成完整草稿（吃 ATS，五要素齐全）
    # 标题行：{团队}{岗位}{形式}面试（取第一个 ATS 命中的）
    ats_records = [c.get("ats") for c in candidates if c.get("name") in [dc[0] for dc in lines_draft_candidates]]
    # 用 lines_draft_candidates 的顺序对齐 ATS（它们都按候选人循环顺序）
    # 重新从 candidates 找 ATS（lines_draft_candidates 没存 ats 引用）
    name_to_ats = {c["name"]: c.get("ats") for c in candidates}

    header = build_header(
        [name_to_ats.get(n) for n, *_ in lines_draft_candidates],
        args.form, args.team,
    )
    header_line = header or f"【需问用户：团队+岗位】{args.form}面试"

    cache = load_cache()
    iv_label = labels[0]
    iv_alias = ""
    for oid, info in cache.items():
        if info.get("name") == interviewer_names[0] and info.get("alias"):
            iv_alias = info["alias"][0]
            break
    salutation = iv_alias or interviewer_names[0]

    def _candidate_block(name, role_hint, raw, wd, t, ats):
        """单人块：姓名（角色）：\n面试可以时间：原话\n拟安排：周X时间
        ats 命中时 role/progress 自动从 ATS 推；未命中标【需问用户】。
        role_hint 是 @后的岗位方向（可选，ATS 缺时用它兜底）。
        """
        # 角色括号：优先 ATS 推的轮次角色，其次 @岗位方向，都没有就不带括号
        ats = ats or {}
        progress, round_role = progress_text(
            ats.get("stage"), ats.get("interview_count"), ats.get("latest_conclusion"),
        )
        paren = round_role or role_hint
        head = f"{name}（{paren}）：" if paren else f"{name}："
        return f"{head}\n面试可以时间：{raw}\n拟安排：{wd}{time_cn(t)}"

    if len(lines_draft_candidates) == 1:
        name, wd, ds, t, raw, role = lines_draft_candidates[0]
        ats = name_to_ats.get(name)
        progress, _ = progress_text(
            (ats or {}).get("stage"), (ats or {}).get("interview_count"), (ats or {}).get("latest_conclusion"),
        )
        # 单人版：标题行下直接块，进展融入块内或单独行
        block = _candidate_block(name, role, raw, wd, t, ats)
        progress_line = ""
        if progress:
            progress_line = f"\n{progress}，{salutation}你这边" if "安排" in progress else f"\n{progress}"
        elif not ats:
            progress_line = "\n【需问用户：上轮进展+这轮角色】"
        draft = (
            f"{header_line}\n"
            f"{block}{progress_line}\n\n"
            f"安排这个时间可以吗（已避开日程忙碌时间段）"
        )
    else:
        # 多人：逐个候选人块状，开头加标题+过渡句
        blocks = []
        for name, wd, ds, t, raw, role in lines_draft_candidates:
            ats = name_to_ats.get(name)
            blocks.append(_candidate_block(name, role, raw, wd, t, ats))
        body = "\n\n".join(blocks)
        draft = (
            f"{header_line}\n"
            f"我沟通了一下候选人\n\n"
            f"{body}\n\n"
            f"安排这几个时间点可以吗（已避开日程忙碌时间段）"
        )

    # ⑥ stdout 直出（删 _schedule_match.txt——git-bash UTF-8 不乱码，AGENTS.md 已确认）
    out = []
    out.append("=== 面试官日程（freebusy 实查）===")
    out.append(f"面试官：{'、'.join(labels)}")
    out.append(f"形式：{args.form}｜查询区间：{earliest.month}-{earliest.day} ~ {latest.month}-{latest.day}｜工作时段 {work_start:02d}:00-{work_end:02d}:00｜每天 12:00-13:30 午休已排除")
    out.append("")

    # —— 忙碌时间段
    out.append("【忙碌时间段】（真实会议）")
    all_dates_in_range = sorted(set(list(busy_by_date.keys()) + [b["start"].date() for b in fully_free]))
    multi = len(oids) > 1
    for d in all_dates_in_range:
        segs = sorted(busy_by_date.get(d, []), key=lambda x: x[0])
        if segs:
            if multi:
                parts = [f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}({who})" for s, e, who in segs]
            else:
                parts = [f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e, who in segs]
            out.append(f"  {weekday_cn(d)}({d.month}-{d.day}): {', '.join(parts)}")
        else:
            out.append(f"  {weekday_cn(d)}({d.month}-{d.day}): 无会议")
    out.append("")

    # —— 空闲时间段
    out.append("【空闲时间段】（反推空闲 ∩ 工作时段，已扣上述会议和午休）")
    free_by_date = {}
    for b in fully_free:
        free_by_date.setdefault(b["start"].date(), []).append(b)
    for d in all_dates_in_range:
        ranges = []
        win_s = datetime.datetime(d.year, d.month, d.day, work_start, 0, tzinfo=TZ)
        win_e = datetime.datetime(d.year, d.month, d.day, work_end, 0, tzinfo=TZ)
        for b in free_by_date.get(d, []):
            s, e = max(b["start"], win_s), min(b["end"], win_e)
            if s < e:
                ranges.append(f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}")
        out.append(f"  {weekday_cn(d)}({d.month}-{d.day}): {', '.join(ranges) if ranges else '无空闲档'}")
    out.append("")

    # —— ATS 数据匹配（新增段）
    out.append("【ATS 数据匹配】（_daily_review.json 的 structured.ats）")
    for c in candidates:
        ats = c.get("ats") or {}
        if ats:
            tid = c.get("talent_id") or ats.get("talent_id", "?")
            out.append(f"  ✅ {c['name']} (tid={tid[:12]}...): 岗位={ats.get('job','?')} 团队={ats.get('dept','?')} 阶段={ats.get('stage','?')} 已面{ats.get('interview_count','?')}轮 上轮结论={ats.get('latest_conclusion')}")
        else:
            out.append(f"  ❌ {c['name']}: ATS 未匹配（{'talent_id=' + c.get('talent_id','?') + ' 不在 ATS' if c.get('talent_id') else '未传 talent_id 且姓名无匹配/同名歧义'}）→ 草稿该候选人岗位/进展标【需问用户】")
    out.append("")

    # —— 候选人匹配
    out.append("【候选人匹配】（空闲 ∩ 候选人日期，黄金时段优先）")
    out.extend(lines_result)
    out.append("")
    out.append("  选择规则：11:00、15:00-18:00 黄金时段优先；避开 09:00 过早 / 12:00-13:30 午休 / 18:00+ 偏晚；同一面试官多候选人自动防撞档。")
    out.append("")

    out.append("=== 可转发草稿（完整版，AI 只在【需问用户】处介入）===")
    out.append(draft)
    out.append("")
    out.append("=== 不可约的（如有，需重新和候选人协调）===")
    no_go = [l for l in lines_result if "❌" in l]
    out.extend(no_go if no_go else ["（无）"])

    text = "\n".join(out)
    print("\n" + text)
    print(f"\n[✅ 完成]（stdout 直出，不再写文件）")


if __name__ == "__main__":
    main()
