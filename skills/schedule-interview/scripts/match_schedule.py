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
  - 草稿绝对日期由候选人匹配结果（hits 的 wd/date_str）直接生成，不靠文本替换

用法：
  # 基础：1个面试官 + 多候选人各给候选日
  python match_schedule.py --interviewer 谢坤 --candidates "罗艺=周四,陈思宇=周四,刘涵辰=周五" --duration 60

  # 多面试官（取共同空闲）
  python match_schedule.py --interviewer 谢坤,潘腾飞 --candidates "张三=周三" --duration 60

  # 候选人给精确时段
  python match_schedule.py --interviewer 谢坤 --candidates "罗艺=周四 16:00" --duration 60

  # per-candidate 形式覆盖（2026-08-12 新增）：同面试官跨形式合并跑
  python match_schedule.py --interviewer 金海 --candidates "范亚军=8-14上午#线下,谢大文=8-13 20:00#视频" --work-end 21

  # 自定义工作时间范围
  python match_schedule.py --interviewer 谢坤 --candidates "罗艺=周四" --duration 45 --work-start 10:00 --work-end 19:00
"""

import os

# === 自动内联的 lark-cli 封装（由 sync_to_opensource.py 从 _lark_shared 抽出，开源版自包含）===
import subprocess, json, re as _re, os as _os, shutil
# 凭证走环境变量（见 .env.example），不硬编码
# Windows 下 subprocess 不走 PATHEXT，裸 "lark-cli" 会 WinError 2，探测 .cmd 扩展
_cli_env = _os.environ.get("LARK_CLI_PATH")
if _cli_env:
    CLI = _cli_env
elif shutil.which("lark-cli"):
    CLI = shutil.which("lark-cli")
else:
    # Windows 最后兜底：npm 全局装的话在 AppData/Roaming/npm/
    _win_guess = _os.path.expanduser("~/AppData/Roaming/npm/lark-cli.cmd")
    CLI = _win_guess if _os.path.exists(_win_guess) else "lark-cli"

def cli(args, timeout=120):
    """跑 lark-cli 子命令，返回 stdout+stderr 合并文本。"""
    env = dict(_os.environ, MSYS_NO_PATHCONV="1")  # 防 git-bash 吃掉 /open-apis 前导斜杠
    r = subprocess.run([CLI] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, env=env)
    return (r.stdout or "") + (r.stderr or "")

def extract_json(raw):
    """从混了 tip/日志的 lark-cli 输出里抠出第一个 JSON 对象。"""
    m = _re.search(r'\{[\s\S]*\}', raw)
    return json.loads(m.group(0)) if m else None

# token 过期/无效错误码（lark-cli 错误响应里常见）
_TOKEN_ERR_CODES = {99991661, 99991663, 99991664, 99991668, 99991679}

def api(method, path, identity="bot", params=None, data=None, timeout=120):
    """调 lark-cli api（hire/document_ai 域）。返回解析后的 dict；失败 None。
    token 过期会抛 RuntimeError 提示重跑授权（避免静默失败让下游崩在 .get() 上）。"""
    args = ["api", method, path, "--as", identity]
    if params:
        args += ["--params", json.dumps(params, ensure_ascii=False)]
    if data is not None:
        args += ["--data", json.dumps(data, ensure_ascii=False)]
    raw = cli(args, timeout=timeout)
    d = extract_json(raw)
    if d is None:
        raise RuntimeError(f"lark-cli 无 JSON 返回，原始输出前 200 字: {raw[:200]}")
    # lark-cli 失败包成 {ok:false, error:{code,message}}；成功包成 {ok:true, data:{...}}
    if d.get("ok") is False:
        err = d.get("error", {}) or {}
        code = err.get("code", 0)
        if code in _TOKEN_ERR_CODES:
            raise RuntimeError(f"token 过期/无效 (code={code})，请重跑 lark-cli auth login")
        raise RuntimeError(f"lark-cli 调用失败 code={code}: {err.get('message', '')[:200]}")
    return d
# === 内联封装结束 ===


import json, sys, os, datetime, argparse, re
sys.stdout.reconfigure(encoding="utf-8")

# 复用项目共享库（收口 cli/extract_json，含 MSYS_NO_PATHCONV + utf-8 encoding + timeout）
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.getcwd())
CACHE = os.path.join(PROJECT_ROOT, "notes", "interviewers.json")
ATS_FILE = os.path.join(PROJECT_ROOT, "notes", "_daily_review.json")  # 岗位/进展/轮次的数据源（daily-recruit-report 产出）

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
    """调 lark-cli，返回解析后的 JSON dict。走 cli（已设 MSYS_NO_PATHCONV + timeout + encoding）。
    ok=false 时区分 token 类错误（99991663/99991664）明确提示，避免和"无数据"混淆。"""
    raw = cli(args)  # stdout+stderr 合并文本（cli 已含 env/encoding/timeout）
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
      progress_str: "初试已通过，安排复试" / "安排初面"
      role_str: 这轮角色（"初面"/"复试"/"三面"/...）——即当前要安排的这一轮
    缺字段时返 (None, None)——调用方据此标【需问用户】。

    核心逻辑：interview_count=已面完轮数，latest_conclusion=最近一轮结论。
      已面0轮           → 当前是初面（安排初面）
      已面N轮且最近通过 → 当前是第N+1轮（上轮已通过，安排这轮）
    """
    # 非"面试"阶段不算面试轮次（如简历评估/Offer 阶段），返 None 让 AI 处理
    if stage != "面试":
        return None, None
    done = int(interview_count or 0)  # 已面完的轮数
    if done == 0:
        # 还没面过，这是初面
        return "安排初面", "初面"
    # 已面 done 轮，当前要安排第 done+1 轮
    cur = done + 1
    cur_role = _ROUND_ROLE.get(cur, f"第{cur}面")
    prev_role = _ROUND_ROLE.get(done, f"第{done}面")
    # latest_conclusion: 1=通过, 2=不通过, null=未提交面评
    if latest_conclusion == 1:
        return f"{prev_role}已通过，安排{cur_role}", cur_role
    elif latest_conclusion == 2:
        return f"{prev_role}（⚠️ 上轮conclusion=不通过，请人工核实）", cur_role
    else:
        # 未提交面评，按流程推进
        return f"{prev_role}已面，安排{cur_role}", cur_role


def build_header(ats_records, form, team_override=None):
    """构造草稿标题行（job_brief 同源）：{团队}——{岗位}
    多人岗位相同时合并写一个岗位；岗位不同时标题只写团队（岗位跟人名走，见候选人块）。
    团队岗位用"——"间隔（2026-08-17 用户定稿后又退回：标题——更自然，块内才用 ｜）。
    形式（视频/线下）不放标题，下沉到候选人块的"拟安排"行（2026-08-04 起）。
    """
    depts = set()
    jobs = set()
    for a in ats_records:
        if not a:
            continue
        depts.add(team_override or a.get("dept", ""))
        jobs.add((a.get("job") or "").strip())
    depts.discard("")
    jobs.discard("")
    if not depts and not jobs:
        return None
    team_part = "、".join(sorted(depts)) if depts else ""
    job_part = ""
    if len(jobs) == 1:
        job_part = "——" + next(iter(jobs)) if jobs else ""
    elif len(jobs) > 1:
        # 岗位不同：标题不列具体岗位，岗位跟人名走（候选人块里"姓名｜岗位·轮次"）
        job_part = ""
    return f"{team_part}{job_part}"


def _job_brief(ats_records, team_override=None):
    """草稿开头用的精简描述：{团队}——{岗位}（多人岗位相同合并；岗位不同只写团队，岗位跟人名走）。
    团队岗位用"——"间隔（2026-08-17 标题退回旧版）。形式（视频/线下）下沉到候选人块"拟安排"行，
    标题不再带形式后缀。
    """
    depts = set()
    jobs = set()
    for a in ats_records:
        if not a:
            continue
        depts.add(team_override or a.get("dept", ""))
        jobs.add((a.get("job") or "").strip())
    depts.discard("")
    jobs.discard("")
    if not depts and not jobs:
        return None  # 全空
    team_part = "、".join(sorted(depts)) if depts else ""
    job_part = ""
    if len(jobs) == 1:
        job_part = "——" + next(iter(jobs)) if jobs else ""
    return f"{team_part}{job_part}"


def parse_candidate_time(raw, ref_date=None):
    """
    解析候选人给的时间，返回 (date_list, day_hints)。
    - day_hints: {datetime.date: hint_str}  —— 按天的时段偏好，支持"某天带时段"精确表达，
      不再把单天的时段塌缩成全局（修复 spillover bug）。
    - 支持多个日期：用 `|` 分隔（逗号已被 --candidates 用于分隔候选人）
      "周一周二" → 内部等价 "周一|周二"；"8-3,8-4" 在 --candidates 里会被拆成两个候选人，
      所以多日期必须用 `|`：candidates="张三=8-3|8-4"
      - "周四"                 → ([绝对日期], {})
      - "周四 16:00"            → ([绝对日期], {绝对日期: "16:00"})
      - "周四下午"              → ([绝对日期], {绝对日期: "下午"})
      - "8-20下午|8-21|8-24"    → ([8-20,8-21,8-24], {8-20: "下午"})  ← 只有 8-20 是下午，其余全天
      - "7-3" / "7月3日"        → ([绝对日期], {})
      - "周一周二"              → ([周一, 周二], {周一: None, 周二: None})  ← 连续星期词自动拆
    """
    ref = ref_date or datetime.date.today()
    raw = raw.strip()

    # 把"周一周二""周一到周三"这类连续星期词拆成用 | 分隔（AI 或用户不传 | 时兜底）
    wk_chain = re.findall(r"[下这本]?(?:周[一二三四五六日天]|星期[一二三四五六日天]|礼拜[一二三四五六日天])", raw)
    if len(wk_chain) >= 2:
        # 例："周一周二" → "周一|周二"；"周一到周三" → "周一|周二|周三"
        # 先按原始顺序取出所有星期词，其余文本按第一个星期词前的内容保留
        first_idx = raw.find(wk_chain[0])
        prefix = raw[:first_idx]
        # 拆连续星期词：逐个解析
        parts = []
        for w in wk_chain:
            parts.append(parse_candidate_time(w, ref)[0])
        # 展开所有星期词对应的日期
        all_dates = []
        for p in parts:
            all_dates.extend(p)
        # 若有时间提示（如"周一周二下午"），取第一个星期词后剩余文本里的提示
        time_hint = None
        tm = re.search(r"(\d{1,2}[:：]\d{2}|下午|晚上|上午|早上)", raw)
        if tm:
            time_hint = tm.group(1).replace("：", ":")
        # 按天挂时段偏好（连续星期词共享同一时段词）
        day_hints = {}
        for d in all_dates:
            day_hints[d] = time_hint
        return list(dict.fromkeys(all_dates)), day_hints

    # 单日期解析：支持 | 分隔多日期（"8-3|8-4"）
    if "|" in raw:
        subs = [s.strip() for s in raw.split("|")]
        all_dates = []
        day_hints = {}
        for sub in subs:
            d_list, h_dict = parse_candidate_time(sub, ref)
            all_dates.extend(d_list)
            day_hints.update(h_dict)
        return list(dict.fromkeys(all_dates)), day_hints

    # 提取时间部分（如果有）
    time_hint = None
    has_pm = ("下午" in raw) or ("晚上" in raw)
    # 先找时间区间 "HH:MM-HH:MM"（如 "14:00-15:00"，AI 由原话"下午2点到3点"转写）——
    # 必须在单时刻匹配之前，否则只截到起始时刻、区间尾丢失（2026-08-20 新增）
    rng_match = re.search(r"(\d{1,2}[:：]\d{2})\s*[-~～到]\s*(\d{1,2}[:：]\d{2})", raw)
    # 再找 "X点(半)?"（精确时刻，如 "4点"/"4点半"）—— 必须在时段词之前，
    # 否则"今天下午4点"会先命中"下午"导致整点时刻丢失、time_hint 变成时段词
    hm_match = None if rng_match else re.search(r"(\d{1,2})点(半)?", raw)
    if rng_match:
        time_hint = rng_match.group(1).replace("：", ":") + "-" + rng_match.group(2).replace("：", ":")
        date_raw = raw.replace(rng_match.group(0), "").strip()
    elif hm_match:
        hh = int(hm_match.group(1))
        mm = 30 if hm_match.group(2) else 0
        if has_pm and hh < 12:
            hh += 12
        time_hint = f"{hh:02d}:{mm:02d}"
        date_raw = raw.replace(hm_match.group(0), "").strip()
    else:
        # 再找精确 HH:MM
        mm_match = re.search(r"(\d{1,2}[:：]\d{2})", raw)
        if mm_match:
            time_hint = mm_match.group(1).replace("：", ":")
            date_raw = raw.replace(mm_match.group(0), "").strip()
        else:
            # 纯时段词（上午/下午/早上/晚上）
            pw_match = re.search(r"(上午|下午|早上|晚上)", raw)
            if pw_match:
                time_hint = pw_match.group(1)
                date_raw = raw.replace(pw_match.group(0), "").strip()
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
    # 返回按天的时段偏好字典（单日期场景：最多 1 个日期）。
    # ⚠️ 无时段词的天也要入字典（值为 None=全天），否则多日期聚合后这些天没有 date 对象，
    #    后续「相邻全天合并」等按天逻辑会因 d_obj=None 而失效。
    day_hints = {}
    if dates:
        day_hints[dates[0]] = time_hint
    return dates, day_hints


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

    def _fetch_one(oid, label):
        """单人 freebusy 查询（2026-08-12 并行化：多面试官时逐人 subprocess 改为并发）"""
        data = run_lark([
            "calendar", "+freebusy",
            "--user-id", oid,
            "--start", start_iso,
            "--end", end_iso,
            "--format", "json",
        ])
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
        return label, oid, busy, len(items)

    # 并行查多面试官 freebusy（2026-08-12：串行 → ThreadPool 并发）
    if len(oids) == 1:
        fetched = [_fetch_one(oids[0], labels[0])]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(6, len(oids))) as ex:
            fetched = list(ex.map(lambda args_: _fetch_one(*args_), zip(oids, labels)))

    for label, oid, busy_items, n_items in fetched:
        for bs, be in busy_items:
            real_busy.append((bs, be, label))
        busy = busy_items + list(lunch_busy)  # 午休当忙碌排除（但只在反推用，不进 real_busy）
        free = _subtract_busy(window_start, window_end, busy)
        per_person_free.append(free)
        print(f"  {label}({oid[:12]}...) 忙碌 {n_items} 段，反推空闲 {len(free)} 段")

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


def intersect(candidate_date, time_hint, blocks, duration_min, work_start=9, work_end=18, now=None):
    """
    把候选人的候选日（+可选时段偏好）和空闲块求交，输出可约的具体时刻列表。
    返回 [{"time": "HH:MM", "label": "..."}, ...]

    now: 当前时间（含时区）。当 candidate_date == now.date()（即今天）时，
    会把已经过去的时段剔除，避免把"今天上午11点"这种过期时点排成面试建议。
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
        # 2026-08-19 修：今天已过的时段直接剔除。
        # 对齐到下一个 30 分钟边界，避免建议"现在4分钟后"这种非整点时点。
        if now is not None and candidate_date == now.date():
            floor_min = ((now.hour * 60 + now.minute) // 30 + 1) * 30
            if floor_min >= 24 * 60:
                # 今天已无剩余 30 分钟边界（接近午夜），整段跳过
                continue
            floor = now.replace(hour=floor_min // 60, minute=floor_min % 60, second=0, microsecond=0)
            slot_start = max(slot_start, floor)
        if slot_end <= slot_start:
            continue
        # 切档（每 30 分钟一档，找够 duration_min 的）
        cur = slot_start
        while cur + datetime.timedelta(minutes=duration_min) <= slot_end:
            avail_slots.append(cur.strftime("%H:%M"))
            cur += datetime.timedelta(minutes=30)
        # 精确时刻偏好（2026-08-18 新增）：若候选人给的整点时刻（如 16:00）落在本空闲段内（够 duration），
        # 直接纳入候选——避免紧贴会尾时只能给 4:15 而非候选人要的 4:00
        # （区间窗口 "14:00-15:00" 不走这里：起点已在 30 分钟网格上，且过滤段已保证整段落在窗口内）
        if time_hint and ":" in time_hint and "-" not in time_hint:
            th, tm = map(int, time_hint.split(":"))
            target = datetime.datetime(day.year, day.month, day.day, th, tm, tzinfo=TZ)
            # 今天给的精确时刻若已过期，不纳入候选
            expired = now is not None and candidate_date == now.date() and target < now
            if not expired and slot_start <= target and target + datetime.timedelta(minutes=duration_min) <= slot_end:
                ts = target.strftime("%H:%M")
                if ts not in avail_slots:
                    avail_slots.append(ts)

    # 应用 time_hint 过滤
    if time_hint:
        if "-" in time_hint and ":" in time_hint:
            # 时间区间窗口（如 "14:00-15:00"，候选人原话"下午2点到3点"）：档位必须完整落在窗口内
            # —— slot >= 起点 且 slot + 时长 <= 终点，杜绝建议跑出候选人窗口（2026-08-20 新增）
            m = re.match(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", time_hint)
            if m:
                s_min = int(m.group(1)) * 60 + int(m.group(2))
                e_min = int(m.group(3)) * 60 + int(m.group(4))
                avail_slots = [s for s in avail_slots
                               if int(s[:2]) * 60 + int(s[3:5]) >= s_min
                               and int(s[:2]) * 60 + int(s[3:5]) + duration_min <= e_min]
        elif ":" in time_hint:
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
    - time_hint 是区间窗口（HH:MM-HH:MM）→ 取窗口起点做偏好时刻（2026-08-20 新增）
    - 否则 → 面试黄金时段优先（11点/下午3-6点）
    """
    if not avail:
        return None
    if time_hint and ":" in time_hint:
        # 区间窗口取起点（"14:00-15:00"→14:00）；纯时刻直接用
        target = time_hint.split("-")[0]
        target_h, target_m = [int(x) for x in target.split(":")]
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


def time_bare(time_str):
    """'HH:MM' → 去时段词的中文时刻：'4点' / '4点15分' / '11点' / '3点半'。
    用于近时间相对词已带「下午/上午」时，避免「今天下午下午4点」这种重复。"""
    h, m = int(time_str[:2]), int(time_str[3:5])
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    if m == 0:
        return f"{h12}点"
    elif m == 30:
        return f"{h12}点半"
    else:
        return f"{h12}点{m:02d}分"


def _window_label(hint, work_start=9, work_end=18):
    """把候选人的时段偏好词映射成草稿「可面时间」里可见的具体窗口文字。
    - None        → 全天（工作时段，如 09:00-18:00）
    - "下午"       → 下午（13:00-{work_end}）   ← 与 intersect 的下午过滤（>=13:00）对齐
    - "晚上"       → 晚上（17:00-{work_end}）
    - "上午"       → 上午（09:00-12:00）
    - "早上"       → 早上（09:00-10:00）
    - "HH:MM"      → 约HH:MM（候选人给了精确时刻）
    - "HH:MM-HH:MM" → 原样展示（候选人给了时间区间窗口，如"下午2点到3点"）
    这样「可面时间」每行都写明具体时段，不再靠一个全局时段词含糊带过。"""
    if not hint:
        return f"全天（{work_start:02d}:00-{work_end:02d}:00）"
    if hint == "下午":
        return f"下午（13:00-{work_end:02d}:00）"
    if hint == "晚上":
        return f"晚上（17:00-{work_end:02d}:00）"
    if hint == "上午":
        return "上午（09:00-12:00）"
    if hint == "早上":
        return "早上（09:00-10:00）"
    if "-" in hint and ":" in hint:
        return hint
    if ":" in hint:
        return f"约{hint}"
    return hint


def extract_rel_term(raw, today):
    """从候选时间原文提取近时间相对词（今天/明天/后天 + 上午/下午/早上/晚上）。
    返回 (rel_term, rel_date)：如 ('今天下午', date) / ('明天', tomorrow) / ('', None)。
    仅近时间（今天/明天/后天）保留；周X/绝对日期不保留（按旧规则转绝对日期，避免歧义）。"""
    day_word, rel_date = "", None
    if "今天" in raw:
        day_word, rel_date = "今天", today
    elif "明天" in raw:
        day_word, rel_date = "明天", today + datetime.timedelta(days=1)
    elif "后天" in raw:
        day_word, rel_date = "后天", today + datetime.timedelta(days=2)
    if not day_word:
        return "", None
    period = next((pw for pw in ("上午", "下午", "早上", "晚上") if pw in raw), "")
    return day_word + period, rel_date


def auto_rel_term(d, today):
    """日期==运行日/次日时返回'今天'/'明天'，其余返回 ''。
    2026-08-20 用户定稿：原话没写相对词（AI 传的是绝对日期）也按日期自动补标注——
    面试官当天读草稿不用心算"周四是哪天"；绝对日期仍保留在前面，延迟阅读不误导。"""
    if d is None or today is None:
        return ""
    delta = (d - today).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    return ""


def _ds_to_date(ds, today):
    """hits 里的日期串 'M-D'（无年份）→ date 对象。年份取运行年，12月查1月时 +1。
    2026-08-20 修：拟安排行的日期比较曾用 (yy,mm,dd)=map(int,'8-20') 的错误拆法
    （year=8、month=20 直接 ValueError 被吞，相对词贴拟安排从未生效），统一走本函数。"""
    try:
        mm, dd = map(int, ds.split("-"))
        year = today.year + (1 if (today.month == 12 and mm == 1) else 0)
        return datetime.date(year, mm, dd)
    except Exception:
        return None


def weekday_cn(d):
    return "周" + "一二三四五六日"[d.weekday()]


_ROOMS_CACHE = None  # 进程内缓存全量会议室列表（vc/v1/rooms 枚举结果）


def list_all_rooms(force_refresh=False):
    """枚举企业全量会议室（vc/v1/rooms，tenant token）。
    ⚠️ room-find 只返回推荐子集（实测全天仅 6/7 间、单时段漏 5 间），不可靠；
    正确做法 = 枚举全量 + 逐间查忙闲。需要应用已申请 vc:room（或 readonly）权限。
    返回 [{room_id, name, capacity}, ...]；失败返回 []。
    """
    global _ROOMS_CACHE
    if _ROOMS_CACHE is not None and not force_refresh:
        return _ROOMS_CACHE
    try:


        rooms = []
        page_token = ""
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            d = api("GET", "/open-apis/vc/v1/rooms", params=params)
            data = d.get("data", {}) or {}
            for rm in data.get("rooms", []) or []:
                rooms.append({
                    "room_id": rm.get("room_id", ""),
                    "room_name": rm.get("name", ""),
                    "capacity": rm.get("capacity", 0),
                })
            if data.get("has_more"):
                page_token = data.get("page_token", "")
            else:
                break
        _ROOMS_CACHE = rooms
        print(f"[会议室] 枚举全量 {len(rooms)} 间")
        return rooms
    except Exception as e:
        print(f"[⚠️ 枚举会议室异常] {e}")
        return []


def room_busy(start_iso, end_iso, room_id):
    """查单间会议室忙闲（calendar/v4/freebusy/list，room_id 参数）。
    返回 (busy, failed)：
      busy   = True=忙（有占用），False=空闲
      failed = True=查询失败（已重试 1 次仍失败）——调用方须按忙处理（fail-closed）
    **2026-08-07 大修（挖出假校验）**：
      ① fail-open→fail-closed：原"查询失败按空闲"把失败房当可用（会议室校验是幻觉）
      ② token 换源：原用 get_hire_token()（招聘应用 tenant token）调 calendar/v4/freebusy/list
         → 400 code=99991672 无 calendar:free_busy 权限 → 永远失败 → fail-open 掩盖成"全可用"。
         改用 lark-cli api(identity='user')（用户授权，日历域已开通，面试官 freebusy 同源）才是真校验。
    """
    for _ in (1, 2):  # 重试 1 次
        try:
            d = _lark_api("POST", "/open-apis/calendar/v4/freebusy/list", identity="user",
                          data={"time_min": start_iso, "time_max": end_iso, "room_id": room_id}, timeout=30)
            # ⚠️ lark-cli 封装返回 {ok: bool, data/error}，没有原生 code 字段！
            # 用 d.get("code") != 0 判断会永远判失败（None != 0）→ 全房 failed（2026-08-07 实测踩坑）
            if d is None or d.get("ok") is not True:
                continue
            busy = d.get("data", {}).get("freebusy_list", []) or []
            return len(busy) > 0, False
        except Exception:
            continue
    return True, True  # 两次失败 → fail-closed：按忙处理并上报 failed


def find_rooms(start_iso, end_iso, min_capacity=0, room_name="", building="", floor="", as_user=True):
    """查指定时间段可用的会议室（视频/现场面试的场地硬约束）。
    **升级版（2026-08-03）**：弃用 room-find 推荐子集，改为：
      ① vc/v1/rooms 枚举全量会议室（进程内缓存）
      ② calendar/v4/freebusy/list 逐间查忙闲（room_id 参数）
      ③ 空闲 ∩ 容量/名称/楼栋筛选 → 全部返回（不再只给推荐 3 间）
    返回 (ok, rooms, hint)：
      ok    = 是否有可用会议室
      rooms = [{room_id, room_name, capacity}, ...]（全部可用，最多展示前 5）
      hint  = 无可用时的原因
    """
    all_rooms = list_all_rooms()
    if not all_rooms:
        # 枚举失败（权限未开）→ 回退 room-find 推荐，并明确提示
        print("[⚠️ 回退] 全量枚举不可用，改用 room-find 推荐子集（结果可能不全）")
        args = ["calendar", "+room-find", "--as", "user" if as_user else "bot",
                "--slot", f"{start_iso}~{end_iso}", "--format", "json"]
        if min_capacity and min_capacity > 0:
            args += ["--min-capacity", str(min_capacity)]
        if room_name:
            args += ["--room-name", room_name]
        if building:
            args += ["--building", building]
        if floor:
            args += ["--floor", floor]
        data = run_lark(args)
        try:
            slots = (data.get("data") or {}).get("time_slots", []) or []
            if not slots:
                return False, [], "无返回"
            ts = slots[0]
            rooms = ts.get("meeting_rooms", []) or []
            hint = ts.get("hint", "")
            return (len(rooms) > 0), rooms[:3], hint
        except Exception as e:
            return False, [], f"解析失败: {e}"

    # 主路径：全量枚举 + 并发查忙闲（2026-08-07：串行→ThreadPool 并发 max_workers=5，
    # 失败重试1次 + fail-closed；token 走 lark-cli user 身份，见 room_busy 注释）
    filtered = []
    for rm in all_rooms:
        # 容量筛选
        if min_capacity and min_capacity > 0 and (rm.get("capacity") or 0) < min_capacity:
            continue
        # 名称筛选
        if room_name and room_name not in (rm.get("room_name") or ""):
            continue
        # 楼栋/楼层筛选（room_name 形如"恒昌大厦西座7楼-迷你玩-A区-星灵"）
        if building and building not in (rm.get("room_name") or ""):
            continue
        if floor and floor not in (rm.get("room_name") or ""):
            continue
        filtered.append(rm)

    avail = []
    fail_n = 0
    from concurrent.futures import ThreadPoolExecutor

    def _check(rm):
        busy, failed = room_busy(start_iso, end_iso, rm["room_id"])
        return rm, busy, failed

    with ThreadPoolExecutor(max_workers=5) as ex:
        for rm, busy, failed in ex.map(_check, filtered):
            if failed:
                fail_n += 1
                continue  # fail-closed：查询失败按忙，不报可用
            if busy:
                continue
            avail.append(rm)
    if fail_n:
        print(f"[⚠️ 会议室] {fail_n} 间查询失败已按忙处理（fail-closed，宁缺毋滥）")

    if avail:
        return True, avail, ""  # 全量返回（不再截断——草稿要如实展示所有可用会议室，2026-08-04 修：曾截到 5 间再截 2 间，草稿永远只显示前两间）
    return False, [], "该时段全量会议室均被占用"


def build_emoji_chart(labels, dates, busy_by_date, work_start, work_end, today, now):
    """生成面试官可约时段 emoji 色块图（纯文本，30分钟/格，09:00-18:00 共 18 格）。

    2026-08-07 用户定稿：对话内图示化用纯文本色块，**不用 SVG 也不用 HTML**——
    SVG 在部分渲染端降级成文本树（色块全丢只剩文字）、HTML 用户明确不要；
    纯文本 emoji 不依赖任何渲染器，且能直接贴在草稿文字前。

    字符：🟩 空闲可约 ｜ 🟥 已有会议 ｜ ⬜ 午休 ｜ ◽ 已过/周末
    返回字符串列表（含图例），main 里插在草稿段之前。
    """
    CELL = datetime.timedelta(minutes=30)
    ws_t = datetime.time(work_start, 0)
    we_t = datetime.time(work_end, 0)
    n = int((datetime.timedelta(hours=we_t.hour - ws_t.hour + (we_t.minute - ws_t.minute) / 60)) / CELL)
    wd = "一二三四五六日"
    lunch_s, lunch_e = datetime.time(12, 0), datetime.time(13, 30)
    lines = [f"=== {'、'.join(labels)} 可约时段图示（{ws_t.strftime('%H:%M')}-{we_t.strftime('%H:%M')}，每格30分钟）===",
             "图例：🟩 空闲可约 ｜ 🟥 已有会议 ｜ ⬜ 午休 ｜ ◽ 已过/周末"]
    for d in dates:
        past = d < today
        weekend = d.weekday() >= 5
        busy_segs = busy_by_date.get(d, [])
        chars = []
        for i in range(n):
            t0 = datetime.datetime(d.year, d.month, d.day, ws_t.hour, ws_t.minute, tzinfo=TZ) + CELL * i
            t1 = t0 + CELL
            if lunch_s <= t0.time() < lunch_e:
                ch = "⬜"
            elif past or weekend or (d == today and t1 <= now):
                ch = "◽"
            else:
                busy = any(s < t1 and t0 < e for s, e, *_ in busy_segs)
                ch = "🟥" if busy else "🟩"
            chars.append(ch)
        tail = " 已过" if past else (" 周末" if weekend else "")
        lines.append(f"{d.month}/{d.day}（周{wd[d.weekday()]}）{''.join(chars)}{tail}")
    return lines


def main():
    ap = argparse.ArgumentParser(description="面试时间协调（吃 ATS 出完整草稿）")
    ap.add_argument("--interviewer", required=True, help="面试官姓名，逗号分隔（如 谢坤,潘腾飞）")
    ap.add_argument("--candidates", required=True, help='候选人及时间，逗号分隔（如 "罗艺=周四,陈思宇=周四 16:00"）')
    ap.add_argument("--talent-ids", default="", help="候选人 talent_id，逗号分隔，与 --candidates 顺序对齐（可选，用于精确匹配 ATS）")
    ap.add_argument("--form", default="视频", choices=["视频", "线下", "现场"], help="面试形式，默认 视频（'现场'已废弃→自动归一化为'线下'，保留仅为兼容旧用法）")
    ap.add_argument("--team", default="", help="团队名覆盖（草稿标题用，默认取 ATS dept）")
    ap.add_argument("--ats-file", default=ATS_FILE, help=f"ATS 数据源 JSON 路径（默认 {ATS_FILE}）")
    ap.add_argument("--duration", type=int, default=45, help="面试时长（分钟），默认 45（2026-08-04 用户定稿；需要 60 分钟显式传）")
    ap.add_argument("--days", type=int, default=7, help="查询未来几天，默认 7")
    ap.add_argument("--max-per-day", type=int, default=0, help="每天最多安排几场面试（0=不限，如 2 表示每天≤2 场——2026-08-04 用户'别排太密'偏好落地）")
    ap.add_argument("--min-capacity", type=int, default=0, help="会议室最小容量（默认 0=不限制）")
    ap.add_argument("--no-room", action="store_true", help="不校验会议室（默认所有形式都查——视频面试面试官也需要会议室）")
    ap.add_argument("--room-name", default="", help="会议室名称约束（如 01,02，可选）")
    ap.add_argument("--building", default="", help="会议室楼栋约束（可选）")
    ap.add_argument("--floor", default="", help="会议室楼层约束，如 F7（可选）")
    ap.add_argument("--work-start", default="9", help="工作开始小时，默认 9")
    ap.add_argument("--work-end", default="18", help="工作结束小时，默认 18")
    ap.add_argument("--dry-run", action="store_true", help="只解析不查飞书")
    args = ap.parse_args()
    # 归一化：'现场' → '线下'（2026-08-04 用户定稿：'线下面试'更贴切，'现场'保留仅为兼容旧用法）
    if args.form == "现场":
        args.form = "线下"

    today = datetime.date.today()
    now = datetime.datetime.now(TZ)  # 2026-08-19 修：供 intersect 过滤"今天已过时段的建议"
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
            print(f"[⚠️ 跳过] 候选人格式错误（需 姓名=时间 或 姓名=时间@岗位 或 姓名=时间#线下）: {pair}")
            continue
        name, raw_time = pair.split("=", 1)
        name = name.strip()
        # 拆出 #形式覆盖（可选，2026-08-12 新增：同面试官跨形式合并跑）
        # 格式：姓名=时间#线下 或 姓名=时间@岗位#线下
        form_override = ""
        if "#" in raw_time:
            raw_time, form_override = raw_time.rsplit("#", 1)
            form_override = form_override.strip()
            if form_override == "现场":
                form_override = "线下"
            if form_override not in ("视频", "线下"):
                print(f"[⚠️ 形式覆盖] '{form_override}' 不支持，用全局 --form {args.form}")
                form_override = ""
        # 拆出 @岗位方向（可选，草稿展示用）
        role = ""
        if "@" in raw_time:
            raw_time, role = raw_time.rsplit("@", 1)
            role = role.strip()
        raw_time = raw_time.strip()
        dates, day_hints = parse_candidate_time(raw_time, today)
        # 近时间相对词提取（今天/明天/后天+上午/下午）→ 草稿保留"今天下午"等措辞（2026-08-18 新增）
        rel_term, rel_date = extract_rel_term(raw_time, today)
        # 周末告警（不强制排除，业务上偶尔有周末面试）：让用户看到"这是周末"再决定
        for d in dates:
            if d.weekday() >= 5:  # 5=周六, 6=周日
                wd_cn = "周六" if d.weekday() == 5 else "周日"
                print(f"[⚠️ 周末] {name} 给的 {d.isoformat()} 是{wd_cn}，公司通常不上班——草稿若生成周末时段请人工确认")
        # 匹配 ATS（优先 talent_id，回退姓名）
        tid = talent_ids[idx] if idx < len(talent_ids) else ""
        ats = match_ats(name, tid, by_tid, by_name)
        candidates.append({
            "name": name, "dates": dates, "day_hints": day_hints, "raw": raw_time,
            "role": role,  # @后的岗位方向（草稿展示用，可选）
            "form": form_override or args.form,  # #后的形式覆盖（可选，无则用全局 --form）
            "talent_id": tid, "ats": ats,
            "rel_term": rel_term, "rel_date": rel_date,  # 近时间相对词（草稿保留用，2026-08-18 新增）
        })

    if args.dry_run:
        print("\n=== [dry-run] 解析结果 ===")
        print(f"面试官: {labels}")
        print(f"形式: {args.form}（全局）")
        for c in candidates:
            role_tag = f" [{c['role']}]" if c["role"] else ""
            form_tag = f" #{c['form']}" if c.get("form") and c["form"] != args.form else ""
            ats = c.get("ats") or {}
            ats_str = f" ATS={ats.get('job','?')}/{ats.get('stage','?')}/面{ats.get('interview_count','?')}" if ats else " ATS=【缺】"
            tid_str = f" tid={c['talent_id']}" if c["talent_id"] else ""
            print(f"  {c['name']}{role_tag}{form_tag}{tid_str}{ats_str}: 日期={[weekday_cn(d)+str(d) for d in c['dates']]}, 时段偏好={c['day_hints']}")
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
    # 按候选人聚合：{name: {raw, role, hits: [(wd, date_str, suggest, room_line), ...]}}
    cand_matches = {}
    # name -> {date: 时段偏好}（按天，不再全局塌缩）
    # 2026-08-20 修：同名多条目必须合并而非覆盖——dict comprehension 后条目把前条目整表冲掉，
    # "8-20 14:00" 的 hint 被 "8-21" 的 {8-21:None} 覆盖 → 草稿窗口把 8-20 显示成全天
    c_day_hints = {}
    for c in candidates:
        c_day_hints.setdefault(c["name"], {}).update(c["day_hints"])
    occupied = set()  # 已分配给前面候选人的 (date, time)，避免同一面试官撞档
    per_day_count = {}  # date -> 已分配场数（--max-per-day 密度约束：用户"别排太密"偏好）
    assigned_once = {}  # name -> bool：每候选人只算拟安排（首次分配）的日期计入密度；同名条目的兜底备选日期不算
    for c in candidates:
        cand_matches.setdefault(c["name"], {"raw": c["raw"], "role": c.get("role", ""), "hits": []})
        for d in c["dates"]:
            # 每天容量上限：该日期已满 → 跳过（候选人顺延到后续日期，草稿窗口仍完整展示可约日期）
            if args.max_per_day and per_day_count.get(d, 0) >= args.max_per_day:
                continue
            slots = intersect(d, c["day_hints"].get(d), blocks, args.duration, work_start, work_end, now)
            # 排除已被其他候选人占用的时段（按 30 分钟对齐，duration 内不能重叠）
            avail = [s for s in slots if not _has_conflict(d, s, args.duration, occupied)]
            wd = weekday_cn(d)
            date_str = f"{d.month}-{d.day}"
            if avail:
                # 建议策略：候选人有精确时刻→贴他给的时间点；否则→面试黄金时段（11点/下午3-6点）
                suggest = best_slot(avail, c["day_hints"].get(d))
                room_line = ""
                if not args.no_room:
                    # 会议室是面试官侧的场地硬约束：视频面试面试官也在公司会议室里，同样要订会议室。
                    # 黄金时段没会议室 → 顺延找有会议室的时段。--no-room 可关闭（纯远程/不需要会议室时）。
                    # ⚠️ suggest 是 best_slot 结果（已含候选人精确时刻偏好/黄金时段），必须放最前先查会议室——
                    #    否则按黄金时段重排会把候选人的精确偏好（如 10:00/14:00）挤到队尾，
                    #    会议室顺延循环先命中 17:00 就停，偏好时段被跳过（2026-08-04 修：传 8-6 10:00 却建议 17:00）。
                    ordered = [suggest] + sorted((s for s in avail if s != suggest), key=_slot_priority)
                    chosen = None
                    for s in ordered:
                        h, m = int(s[:2]), int(s[3:5])
                        s_dt = datetime.datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
                        e_dt = s_dt + datetime.timedelta(minutes=args.duration)
                        ok, rooms, hint = find_rooms(
                            s_dt.isoformat(), e_dt.isoformat(),
                            args.min_capacity, args.room_name, args.building, args.floor,
                        )
                        if ok:
                            chosen = s
                            # 会议室是"校验器"不是"展示器"：成功时草稿不输出任何会议室信息
                            # （2026-08-04 用户定稿：只在无可用会议室时才告警提醒换时段，有会议室就别提）
                            room_line = ""
                            break
                    if chosen is None:
                        # 所有可约时段都没会议室 → 该日期整体标记，建议换日期
                        lines_result.append(
                            f"  {c['name']:<6} {wd}({date_str}) → ❌ 面试官有空但全时段无可用会议室（{c['raw']}），需换日期"
                        )
                        continue
                    if chosen != suggest:
                        suggest = chosen
                occupied.add((d, suggest))
                # 密度计数：每候选人只在"拟安排（首次分配）"的日期计 1 场，兜底备选日期不计
                if not assigned_once.get(c["name"]):
                    assigned_once[c["name"]] = True
                    per_day_count[d] = per_day_count.get(d, 0) + 1
                # 展示按时段优先级排序（黄金时段靠前），最多6个
                avail_sorted = sorted(avail, key=_slot_priority)
                show = avail_sorted if len(avail_sorted) <= 6 else avail_sorted[:6]
                room_tag = f" 会议室✅" if room_line else ""
                lines_result.append(f"  {c['name']:<6} {wd}({date_str}) → ✅ 可约 {'/'.join(show)}（建议 {suggest}）{room_tag}")
                cand_matches[c["name"]]["hits"].append((wd, date_str, suggest, room_line))
            else:
                lines_result.append(f"  {c['name']:<6} {wd}({date_str}) → ❌ 该时段面试官无空档或已被其他候选人占用（{c['raw']}）")

    # ⑤ 生成草稿
    # 守卫：会议室校验开启时全部被卡死 → 不出草稿，明确提示换日期
    matched = [n for n, m in cand_matches.items() if m["hits"]]
    if not matched:
        out = []
        out.append("=== 协调结果 ===")
        out.append(f"面试官：{'、'.join(labels)}（{args.form}面试）")
        out.append("")
        out.append("【候选人匹配】")
        out.extend(lines_result)
        out.append("")
        out.append("=== ⚠️ 无可用安排：所有候选人的可约时段均无可用会议室，需与候选人重新协调日期（或 --no-room 跳过会议室校验） ===")
        print("\n" + "\n".join(out))
        print(f"\n[✅ 完成]（无草稿产出——会议室硬约束未满足）")
        return

    # ⑤ 生成完整草稿（吃 ATS，五要素齐全）
    name_to_ats = {c["name"]: c.get("ats") for c in candidates}

    header = build_header(
        [name_to_ats.get(n) for n in matched],
        args.form, args.team,
    )
    header_line = header or f"【需问用户：团队+岗位】"

    # 面试官称谓：缓存里 alias[0] 或姓名（草稿抬头用，alias 如 Sava=古振兴）
    iv_alias = ""
    for oid, info in load_cache().items():
        if info.get("name") == interviewer_names[0] and info.get("alias"):
            iv_alias = info["alias"][0]
            break
    salutation = iv_alias or interviewer_names[0]

    def _rel_of(name):
        """取候选人近时间相对词（今天/明天/后天+上午/下午）及对应绝对日期，找不到返回 ('', None)。"""
        for c in candidates:
            if c["name"] == name:
                return c.get("rel_term", ""), c.get("rel_date")
        return "", None

    def _candidate_block(name, role_hint, raw, hits, ats, form_cn):
        """单人块（多日期聚合版），2026-08-17 模板定稿 + 2026-08-20 排版定稿：
        - 三行各自独立成行（姓名行 / 可面时间行 / 拟安排行，不再挤成一段）
        - 可面时间、拟安排两行缩进一个全角空格（≈两格，2026-08-20 用户定稿"中间缩进两格"：层次更清晰）
        - 近时间相对词保留进草稿（如「今天下午」）；原话没写时日期==运行日/次日自动补「今天/明天」
        姓名｜岗位·轮次
        　可面时间：8/18（周二）今天下午（16:00）
        　拟安排：8/18（周二）今天下午4点（线下）
        hits: [(wd, date_str, suggest, room_line), ...] 按优先级排序，第一个是最优
        """
        ats = ats or {}
        progress, round_role = progress_text(
            ats.get("stage"), ats.get("interview_count"), ats.get("latest_conclusion"),
        )
        # 括号内容：岗位·轮次（岗位从 ATS 取，轮次从 ATS 推；ATS 缺时用 role_hint 兜底）
        job = (ats.get("job") or "").strip()
        role_tag = round_role or role_hint
        if job and role_tag:
            paren = f"{job}·{role_tag}"
        elif job:
            paren = job
        elif role_tag:
            paren = role_tag
        else:
            paren = ""
        head = f"{name}｜{paren}" if paren else f"{name}"
        # 近时间相对词：仅贴到对应绝对日期（今天/明天/后天 各指一天，避免错贴到其它日期）
        rel_term, rel_date = _rel_of(name)

        # 完整可约窗口：按天展示具体时段（修复 spillover bug）；
        # B 方案呈现：相邻「全天」合并成一段（~ 连接），跨「工作日↔周末」边界时断开；
        # 带具体时段（下午/上午/精确时刻）的单独成行，不并入全天合并。
        day_hints_for_name = c_day_hints.get(name, {})
        hint_by_ds = {f"{dd.month}-{dd.day}": (dd, h) for dd, h in day_hints_for_name.items()}

        # 构造有序 item 列表（hits 已按优先级排序）
        items = []
        for wd, ds, t, room_line in hits:
            d_obj, hint = hint_by_ds.get(ds, (None, None))
            rel_applied = False
            if rel_term and rel_date is not None and d_obj is not None:
                try:
                    if (d_obj.year, d_obj.month, d_obj.day) == (rel_date.year, rel_date.month, rel_date.day):
                        rel_applied = True
                except Exception:
                    pass
            # 原话没带相对词（AI 传绝对日期）时，日期==运行日/次日自动补「今天/明天」（2026-08-20 定稿）
            rel_show = rel_term if rel_applied else auto_rel_term(d_obj, today)
            items.append({"ds": ds, "wd": wd, "d": d_obj, "hint": hint, "rel": rel_show})

        def _is_we(d):
            return bool(d) and d.weekday() >= 5

        def _consec(a, b):
            return bool(a) and bool(b) and (b - a).days == 1

        parts = []
        i, n = 0, len(items)
        while i < n:
            it = items[i]
            if it["hint"]:
                # 带具体时段（下午/上午/精确时刻）→ 单独一行
                d_str = it["ds"].replace("-", "/") + "（" + it["wd"] + "）" + it["rel"]
                parts.append(d_str + _window_label(it["hint"], work_start, work_end))
                i += 1
            else:
                # 全天：贪心合并连续日历日，且跨「工作日↔周末」边界时断开
                run = [it]
                j = i + 1
                while j < n and (not items[j]["hint"]) and _consec(run[-1]["d"], items[j]["d"]) \
                        and _is_we(run[-1]["d"]) == _is_we(items[j]["d"]):
                    run.append(items[j])
                    j += 1
                if len(run) == 1:
                    r = run[0]
                    d_str = r["ds"].replace("-", "/") + "（" + r["wd"] + "）" + r["rel"]
                    parts.append(d_str + _window_label(None, work_start, work_end))
                else:
                    a, b = run[0], run[-1]
                    d_str = (a["ds"].replace("-", "/") + "（" + a["wd"] + "）~ "
                             + b["ds"].replace("-", "/") + "（" + b["wd"] + "）")
                    parts.append(d_str + _window_label(None, work_start, work_end))
                i = j
        window = "、".join(parts)
        # 拟安排：第一个命中（优先级最高），标形式（视频/现场）+ 相对词
        wd0, ds0, t0, room_line0 = hits[0]
        d0 = _ds_to_date(ds0, today)
        show_rel = ""
        if rel_term and rel_date is not None and d0 is not None and d0 == rel_date:
            show_rel = rel_term
        # 原话没带相对词时，拟安排日期==运行日/次日自动补「今天/明天」（2026-08-20 定稿）
        if not show_rel:
            show_rel = auto_rel_term(d0, today)
        # 原话相对词已带时段（如「今天下午」）→ 时刻去时段词（time_bare），避免「今天下午下午4点」；
        # 自动补的「今天/明天」不带时段 → 保留 time_cn 的上午/下午，避免「今天3点」歧义
        time_part = (time_bare(t0) if any(p in show_rel for p in ("上午", "下午", "早上", "晚上"))
                     else time_cn(t0)) if show_rel else time_cn(t0)
        abs_suggest = ds0.replace("-", "/") + "（" + wd0 + "）" + show_rel + time_part + f"（{form_cn}）" + room_line0
        # 布局：三行各自独立；可面时间/拟安排缩进一个全角空格（≈两格，2026-08-20 用户定稿"中间缩进两格"）
        return f"{head}\n　可面时间：{window}\n　拟安排：{abs_suggest}"

    def _summary_phrase(names, name_to_ats):
        """从匹配成功的候选人列表提炼'是谁、推进几面'的概述。
        同轮次合并：'蒋新斌、林盛烁推进三面'；轮次不同分开：'A推进复试，B推进三面'。
        ATS 缺失标【需问用户】。
        """
        segs = []  # [(轮次, [姓名])]
        for name in names:
            ats = name_to_ats.get(name) or {}
            _, round_role = progress_text(
                ats.get("stage"), ats.get("interview_count"), ats.get("latest_conclusion"),
            )
            role = round_role or "【需问用户：轮次】"
            # 合并同轮次
            if segs and segs[-1][0] == role:
                segs[-1][1].append(name)
            else:
                segs.append((role, [name]))
        parts = []
        for role, names_grp in segs:
            parts.append(f"{'、'.join(names_grp)}推进{role}")
        return "，".join(parts)

    if len(matched) == 1:
        name = matched[0]
        m = cand_matches[name]
        ats = name_to_ats.get(name)
        progress, _ = progress_text(
            (ats or {}).get("stage"), (ats or {}).get("interview_count"), (ats or {}).get("latest_conclusion"),
        )
        c_form = next((c.get("form", args.form) for c in candidates if c["name"] == name), args.form)
        form_cn = "视频" if c_form == "视频" else "线下"
        block = _candidate_block(name, m["role"], m["raw"], m["hits"], ats, form_cn)
        need_user = ""
        if not ats or not progress:
            need_user = "\n【需问用户：上轮进展+这轮角色】"
        summary = _summary_phrase(matched, name_to_ats)
        job_brief = _job_brief([name_to_ats.get(n) for n in matched], args.team) or header_line
        # 会议室信息不展示在草稿尾部（2026-08-04 用户定稿：有会议室就别提，只在无可用时另行告警）
        room_tail = ""
        draft = (
            f"{salutation}，{job_brief}，{summary}，我和候选人沟通了一下面试时间：\n\n"
            f"{block}{need_user}\n\n"
            f"时间OK的话我直接和候选人敲定～"
        )
    else:
        # 多人：称谓 → 这是谁/推进几面 → 我沟通了时间 → 具体安排
        # 2026-08-12 修：按拟安排时间排序（时间早的在前），面试官一眼看到排期顺序
        def _sort_key(name):
            m = cand_matches.get(name, {})
            hits = m.get("hits", [])
            if not hits:
                return (9999, 99, 99, 99)
            wd, ds, t, _ = hits[0]
            # ds = "8-14", t = "11:15"
            try:
                mm, dd = ds.split("-")
                hh, mi = t.split(":")
                return (int(mm), int(dd), int(hh), int(mi))
            except Exception:
                return (9999, 99, 99, 99)
        matched_sorted = sorted(matched, key=_sort_key)
        blocks = []
        for name in matched_sorted:
            m = cand_matches[name]
            c_form = next((c.get("form", args.form) for c in candidates if c["name"] == name), args.form)
            form_cn = "视频" if c_form == "视频" else "线下"
            blocks.append(_candidate_block(name, m["role"], m["raw"], m["hits"], name_to_ats.get(name), form_cn))
        body = "\n\n".join(blocks)
        summary = _summary_phrase(matched_sorted, name_to_ats)
        job_brief = _job_brief([name_to_ats.get(n) for n in matched_sorted], args.team) or header_line
        # 会议室信息不展示在草稿尾部（2026-08-04 用户定稿：有会议室就别提，只在无可用时另行告警）
        room_tail = ""
        draft = (
            f"{salutation}，{job_brief}，{summary}，我和候选人沟通了一下面试时间：\n\n"
            f"{body}\n\n"
            f"时间OK的话我直接和候选人敲定～"
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

    # —— emoji 色块图示（纯文本，2026-08-07 起：对话内图示化主载体，贴在草稿前；不依赖渲染器）
    now = datetime.datetime.now(TZ)
    out.extend(build_emoji_chart(labels, all_dates_in_range, busy_by_date, work_start, work_end, today, now))
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
