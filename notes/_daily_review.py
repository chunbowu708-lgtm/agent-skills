# -*- coding: utf-8 -*-
"""
_daily_review.py — 每日对账引擎（P0 重写：以 ATS 为中轴）

==================== 设计原则（第一性原理）====================
中轴：飞书招聘 ATS（applications + interviews + stage_time_list）是唯一事实源。
     跟踪表、日程、群/私聊消息都挂在 ATS 上校验，不再"表自己查自己"。

两层分工：
  - 脚本层（本文件，纯 Python 无 LLM）：拉全量数据 + 结构化计算（谁卡了几天、
    谁该推进、面评状态）+ 把消息原文全量导出（不截断、不分类）。
  - Agent 层（LLM 判读）：读本脚本导出的 raw_messages，用 LLM 判读每条消息
    意图（邀约/拒绝/讨论/决策），结合 structured 结果生成最终作战清单。

治根：ATS↔跟踪表对账优先用 talent_id 精确匹配；跟踪表若无 talent_id 列，
      降级姓名匹配并标注 ⚠️，不阻断对账。

用法：
    python notes/_daily_review.py            # 只读，出报告
    python notes/_daily_review.py --write    # 回写跟踪表（优先级/下一步/进展日期）
输出：notes/_daily_review.json
"""
import sys, os, json, re, datetime, argparse, time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lark_shared import (
    api, cli, extract_json, list_track_records, upsert_track_record,
    to_ms, from_ms, fmt_ms, fmt_ms_full, days_since,
    BASE_TOKEN, TRACK_TABLE, CLI, TZ, WUBO_ID,
    STAGE_INTERVIEW, STAGE_OFFER, STAGE_ONBOARD, STAGE_EMPLOYED,
)
sys.stdout.reconfigure(encoding="utf-8")

NOW = datetime.datetime.now(tz=TZ)
TODAY = NOW.date()
TODAY_STR = TODAY.isoformat()

# ============================================================
# 信息源配置
# ============================================================
# 已知的固定招聘群（种子，自动发现会在此基础上扩充）
CHAT_GROUPS_SEED = {
    "长青工作室招聘沟通群": "oc_b1d2f91abfd3b039b89cb0454a363b1d",
    "中台产品招聘": "oc_69e280f9546c3fef56f503a0837da61a",
    "山海工作室招聘沟通群": "oc_b8d76981f9d8d9740a85d7f4e6129481",
    "发行技术支持招聘沟通群": "oc_673bef44b7a8993550c3f80034c2e2b5",
    "可可爱爱招聘组": "oc_83c06afdb0fe44ff13e12b7a2fb9329c",
}
# 必扫的私聊 + 系统通知（邀约真正落地的地方）
CHAT_P2P_FIXED = {
    "钟波(Bruce)私聊": "oc_37218905e0782cfd5f239b5106162fe0",
    "飞书招聘通知": "oc_eceb45fc66a90be574089b72ffca7565",
}
# 消息窗口：7 天（旧版 24h 会系统性漏掉跨天邀约）
MSG_DAYS = 7
MSG_START_ISO = (NOW - datetime.timedelta(days=MSG_DAYS)).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _load_my_jobs():
    """加载我负责的岗位 job_id 集合（从 notes/_my_jobs.json）。

    飞书招聘 create_user_id=我 的岗位 = 我创建的 = 我负责的。
    作战清单只关注这些岗位的投递，别人的岗位过滤掉。
    文件不存在时返回 None（表示不过滤，全量展示）。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_my_jobs.json")
    if not os.path.exists(path):
        return None
    try:
        data = json.load(open(path, encoding="utf-8"))
        return set(j["id"] for j in data.get("mine", []))
    except Exception:
        return None

# "卡住"阈值（天）
STUCK_WARN = 2   # ≥2 天无进展 → 预警
STUCK_URGENT = 3
STUCK_CRITICAL = 5

# 招聘相关群名关键词（用于自动发现）
RECRUIT_KEYWORDS = ["招聘", "面试", "人才", "HR", "interview", "recruit"]


# ============================================================
# ① 拉数据源（6 路并行）
# ============================================================
def fetch_applications_all(max_pages=10):
    """全量拉投递列表（id 数组），再并行查每条详情。

    旧版只翻 3 页 60 条、详情只取 40 条 → 漏报主因。本版翻到 has_more=false 或 max_pages。
    返回 list[application 详情 dict]。
    """
    print("  · [ATS] 投递列表全量分页...")
    app_ids = []
    page_token = None
    for page in range(max_pages):
        params = {"page_size": 20}
        if page_token:
            params["page_token"] = page_token
        d = api("GET", "/open-apis/hire/v1/applications", "bot", params=params)
        if not d:
            break
        data = d.get("data", {})
        items = data.get("items", [])
        app_ids += items  # items 是 id 字符串数组
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    print(f"    投递ID {len(app_ids)} 条")

    # 并行查详情。注意：详情在 data.application（嵌套一层），with_job 不返回 job，
    # 岗位要另查 /jobs/{job_id}。stage_time_list 含完整流转历史。
    def fetch_one(aid):
        d = api("GET", f"/open-apis/hire/v1/applications/{aid}", "bot")
        if not d:
            return {}
        return d.get("data", {}).get("application", {}) or d.get("data", {})

    details = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for app in ex.map(fetch_one, app_ids):
            if app:
                details.append(app)
    print(f"    投递详情 {len(details)} 条")
    return details


def fetch_interviews_for(app_detail):
    """对单条 application 查面试列表，返回 interviews[]。

    只对在面试阶段(type=4)的查（省请求）；其他阶段返回 []。
    """
    stage = app_detail.get("stage", {})
    stage_type = stage.get("type")
    aid = app_detail.get("id")
    if stage_type != STAGE_INTERVIEW or not aid:
        return []
    d = api("GET", "/open-apis/hire/v1/interviews", "bot",
            params={"application_id": aid, "page_size": 10})
    if not d:
        return []
    items = d.get("data", {}).get("items", [])
    return items if isinstance(items, list) else []


def fetch_interviews_batch(app_details):
    """并行对所有"活跃且面试阶段"的 application 查 interviews。

    只查 active_status==1 的（不活跃的历史投递不需要跟进面试），省大量 API 请求。
    """
    interviewing = [a for a in app_details
                    if a.get("stage", {}).get("type") == STAGE_INTERVIEW
                    and a.get("active_status") == 1]
    print(f"  · [ATS] 活跃且面试阶段 {len(interviewing)} 人，并行查 interviews...")
    result = {}  # application_id -> interviews[]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_interviews_for, a): a.get("id") for a in interviewing}
        for fut, aid in futs.items():
            try:
                result[aid] = fut.result()
            except Exception:
                result[aid] = []
    return result


def fetch_calendar():
    """拉日程（今天~+7天），从 description 提 application_id（比 summary 子串可靠）。"""
    print("  · [日程] 今天~+7天...")
    ts_start = int(NOW.replace(hour=0, minute=0, second=0).timestamp())
    ts_end = int((NOW + datetime.timedelta(days=7)).timestamp())
    raw = cli(["calendar", "+agenda", "--start", str(ts_start), "--end", str(ts_end), "--as", "user"])
    d = extract_json(raw)
    events = d.get("data", []) if d else []
    if not isinstance(events, list):
        events = []
    out = []
    for e in events:
        desc = e.get("description", "") or ""
        m = re.search(r"application_id[=:](\d+)", desc.replace(" ", ""))
        # start/end 可能是 dict {'datetime': '2026-07-01T11:00...'} 或字符串，统一提取成 ISO 字符串
        def _extract_time(t):
            if not t:
                return ""
            if isinstance(t, dict):
                return t.get("datetime") or t.get("timestamp") or str(t)
            return str(t)
        start_str = _extract_time(e.get("start_time") or e.get("start"))
        out.append({
            "summary": e.get("summary", ""),
            "start": start_str,
            "end": _extract_time(e.get("end_time") or e.get("end")),
            "application_id": m.group(1) if m else None,
            "desc_has_appid": bool(m),
        })
    print(f"    日程 {len(out)} 条，含 app_id {sum(1 for x in out if x['application_id'])} 条")
    return out


def fetch_messages_one(chat_id, chat_name):
    """拉单个会话的近 N 天消息，返回 list[dict]（原文全量，不截断不分类）。"""
    raw = cli(["im", "+chat-messages-list", "--as", "user",
               "--chat-id", chat_id, "--start", MSG_START_ISO, "--page-size", "50"])
    d = extract_json(raw)
    if not d:
        return []
    # 消息列表在 data.messages（不是 data.items）
    items = d.get("data", {}).get("messages", [])
    if not isinstance(items, list):
        items = []
    out = []
    for msg in items:
        if not isinstance(msg, dict) or msg.get("deleted"):
            continue
        # text 消息的 content 是纯文本字符串（@吴春波 直接写文名）
        content = msg.get("content", "") or ""
        # 富文本消息 content 可能是 JSON，提取 text 字段
        if isinstance(content, str) and content.strip().startswith("{"):
            try:
                cj = json.loads(content)
                # 提取可能的文本字段
                content = (cj.get("text") or cj.get("content") or
                           cj.get("title", {}).get("text", "") if isinstance(cj, dict) else content)
                # post 类型嵌套 title + content
                if isinstance(cj, dict) and "title" in cj:
                    parts = [cj.get("title", {}).get("text", "")]
                    for lang in cj.values():
                        if isinstance(lang, dict):
                            for v in lang.values():
                                if isinstance(v, list):
                                    for blk in v:
                                        if isinstance(blk, dict):
                                            parts.append(blk.get("text", "") or blk.get("name", ""))
                    content = " ".join(p for p in parts if p)
            except Exception:
                pass
        # @吴春波：文本含"吴春波"（lark-cli 的 text 消息已把 @ 解析成文名）
        mentions_wubo = "吴春波" in content
        # 发送人
        sender = msg.get("sender", {})
        sender_name = sender.get("name", "") if isinstance(sender, dict) else str(sender)
        # 跳过自己发的（除非别人也@了——这里只跳纯自发）
        if isinstance(sender, dict) and sender.get("id") == WUBO_ID and not mentions_wubo:
            # 自己发的且没@自己，仍保留（可能含业务信息），但标记
            pass
        # file 类型消息 content 形如 <file key="..." name="柯晓东_3D角色_8年.pdf"/>，
        # 提取文件名让判读能直接看到候选人姓名/岗位（否则"这个约下"的引用对象无法识别）
        msg_type = msg.get("msg_type", "") or ""
        if msg_type == "file":
            import re as _re
            _m = _re.search(r'name="([^"]+)"', content)
            if _m:
                content = f"[文件] {_m.group(1)}"
        out.append({
            "source": chat_name,
            "sender": sender_name,
            "sender_is_wubo": isinstance(sender, dict) and sender.get("id") == WUBO_ID,
            "time": msg.get("create_time", "") or "",
            "content_full": content,   # 全量原文，不截断！
            "mentions_wubo": mentions_wubo,
            # 结构化字段（防指代判读失效）：reply_to=引用的父消息id，message_id=本条id
            # 缺这些字段时"这个/约下"的指代对象无法识别 → 邀约漏报
            "message_id": msg.get("message_id", "") or "",
            "reply_to": msg.get("reply_to", "") or "",
            "msg_type": msg_type,
        })
    return out


def discover_recruit_chats():
    """自动发现所有招聘相关群（含 3 人群、团队群、Interview for xxx 动态群）。

    避免硬编码遗漏：用户所在的群会增减，硬编码的种子列表永远不够。
    策略：拉用户全部群，按群名关键词匹配，再并入种子群和必扫私聊。
    排除外部社群群（external=true 的，如 HR 社区、AI招聘活动群，与本公司招聘无关）。
    """
    raw = cli(["im", "+chat-list", "--as", "user", "--types", "group", "--page-size", "100"])
    d = extract_json(raw)
    chats = d.get("data", {}).get("chats", []) if d else []
    discovered = {}
    skipped_external = []
    for c in chats:
        name = c.get("name", "")
        cid = c.get("chat_id", "")
        external = c.get("external", False)  # 外部群标志（跨公司）
        if not name or not cid:
            continue
        # 跳过外部社群群（HR社区、AI活动等，含"招聘"关键词但非本公司招聘）
        if external:
            skipped_external.append(name)
            continue
        if any(kw in name for kw in RECRUIT_KEYWORDS):
            discovered[name] = cid
        # Interview for xxx 动态群（飞书招聘按候选人自动创建）
        if "interview for" in name.lower():
            discovered[name] = cid
    if skipped_external:
        print(f"    （跳过 {len(skipped_external)} 个外部群: {', '.join(skipped_external[:3])}）")
    # 并入种子群（确保不漏）+ 必扫私聊
    discovered = {**CHAT_GROUPS_SEED, **discovered, **CHAT_P2P_FIXED}
    return discovered


def fetch_all_messages():
    """自动发现招聘群 + 必扫私聊，并行拉全部消息。"""
    all_chats = discover_recruit_chats()
    n_group = sum(1 for n, c in all_chats.items() if c not in CHAT_P2P_FIXED.values())
    n_p2p = len(all_chats) - n_group
    print(f"  · [消息] 自动发现 {n_group} 群 + {n_p2p} 私聊/bot，窗口 {MSG_DAYS} 天...")
    out = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(fetch_messages_one, cid, cname) for cname, cid in all_chats.items()]
        for fut in futs:
            try:
                out += fut.result()
            except Exception:
                pass
    print(f"    消息 {len(out)} 条")
    return out


def parse_hire_bot_cards(messages):
    """从飞书招聘 bot 通知卡片提取结构化事件（高价值，旧版全靠 Agent 手读）。

    卡片格式规整，正则提取。返回 list[dict]：
      {event, candidate, job, round, interviewer, conclusion, application_id, time, source_msg}
    event: 面试反馈/面试官接受/候选人未到场/面试官未到场/双方上线
    """
    events = []
    for m in messages:
        if m.get("source") != "飞书招聘通知":
            continue
        c = m.get("content_full", "")
        if "<card" not in c:
            continue
        def extract(field):
            # "候选人：XXX" 或 "投递职位：XXX"
            mm = re.search(rf"{field}[：:]\s*(.+)", c)
            return mm.group(1).strip().split("(")[0].strip() if mm else ""
        tm = re.search(r'title="([^"]+)"', c)
        title = tm.group(1) if tm else ""
        # 事件分类
        event = ""
        if "新反馈" in title:
            event = "面试反馈"
        elif "接受" in title:
            event = "面试官接受"
        elif "候选人未到场" in title:
            event = "候选人未到场"
        elif "面试官未到场" in title:
            event = "面试官未到场"
        elif "已上线" in title:
            event = "双方上线"
        else:
            event = title
        # application_id 从链接提取
        am = re.search(r"application_id=(\d+)", c)
        app_id = am.group(1) if am else ""
        events.append({
            "event": event,
            "candidate": extract("候选人"),
            "job": extract("投递职位") or extract("职位"),
            "round": extract("面试轮次"),
            "interviewer": extract("面试官"),
            "conclusion": extract("面试结论"),
            "application_id": app_id,
            "time": m.get("time", ""),
            "title": title,
        })
    return events


# ============================================================
# ② 结构化计算（以 ATS 为中轴）
# ============================================================
def _batch_get_names(talent_ids):
    """并行批量查 talent 姓名。返回 {talent_id: name}。

    200 人串行查要 3 分钟+，并行（8线程）10 秒内。
    """
    talent_ids = list({t for t in talent_ids if t})  # 去重去空
    cache = {}

    def one(tid):
        d = api("GET", f"/open-apis/hire/v1/talents/{tid}", "bot")
        bi = d.get("data", {}).get("talent", {}).get("basic_info", {}) if d else {}
        return tid, bi.get("name", "")

    with ThreadPoolExecutor(max_workers=8) as ex:
        for tid, name in ex.map(one, talent_ids):
            cache[tid] = name
    return cache


def _batch_get_jobs(job_ids):
    """并行批量查 job 岗位名+部门。返回 {job_id: (title, dept_name)}。

    job 数量远少于 application（同岗位共享 job_id），去重后通常 <20 个。
    """
    job_ids = list({j for j in job_ids if j})  # 去重去空
    cache = {}

    def one(jid):
        d = api("GET", f"/open-apis/hire/v1/jobs/{jid}", "bot")
        j = d.get("data", {}).get("job", {}) if d else {}
        title = j.get("title", "")
        dept = j.get("department", {})
        dept_name = dept.get("zh_name", "") if isinstance(dept, dict) else str(dept)
        return jid, (title, dept_name)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for jid, pair in ex.map(one, job_ids):
            cache[jid] = pair
    return cache


def parse_ats(app_details, interviews_by_app):
    """把 ATS 原始数据解析成标准化候选人视图。

    返回 list[dict]，每条：
      talent_id, name, job_title, dept, application_id,
      stage_type, stage_name, dwell_days (卡在当前阶段天数),
      interviews: [{round, round_type, begin_time, end_time,
                    feedback_submitted, conclusion, interviewers}],
      latest_conclusion (1通过/2不通过/None未提交),
      interview_over (是否已过 end_time)
    """
    # 预先并行批量查 talent 姓名 + job 信息（性能铁律：不许串行）
    name_cache = _batch_get_names([a.get("talent_id") for a in app_details])
    job_cache = _batch_get_jobs([a.get("job_id") for a in app_details])
    print(f"    talent姓名缓存 {len(name_cache)} | job缓存 {len(job_cache)}")

    out = []
    for app in app_details:
        tid = app.get("talent_id", "")
        aid = app.get("id", "")
        stage = app.get("stage", {})
        stage_type = stage.get("type")
        stage_name = stage.get("zh_name", "") or stage.get("id", "")

        # 卡在当前阶段天数：stage_time_list 里"无 exit_time"的那条 = 当前阶段
        # （实测顺序不固定，必须按 exit_time 缺失判断，不能取 [-1]）
        stl = app.get("stage_time_list", []) or []
        dwell_ms = None
        for st in stl:
            if not st.get("exit_time"):
                dwell_ms = st.get("enter_time")
                break
        if dwell_ms is None and stl:
            dwell_ms = stl[-1].get("enter_time")  # 兜底
        dwell_days = days_since(dwell_ms) if dwell_ms else None

        # 岗位/部门（从批量缓存取）
        job_title, dept_name = job_cache.get(app.get("job_id", ""), ("", ""))
        name = name_cache.get(tid, "")

        # 面试信息
        ivs_raw = interviews_by_app.get(aid, [])
        interviews = []
        latest_conclusion = None
        interview_over = False
        for iv in ivs_raw:
            begin = iv.get("begin_time")
            end = iv.get("end_time")
            fb = iv.get("feedback_submit_time")
            # 注：interview_round_summary 字段曾在 2026-07 前取过，但从未参与决策，已删（2026-07-22）
            # 如未来需要"是否出结论"信号，从 conclusion 字段（1通过/2不通过/None未提交）取，不用此字段
            irl = iv.get("interview_record_list", []) or []
            concls = [r.get("conclusion") for r in irl if r.get("conclusion") is not None]
            concl = concls[-1] if concls else None
            # 面试官：interview_record_list[].interviewer.name.zh_cn（多个取列表）
            iv_names = []
            for r in irl:
                iv_obj = r.get("interviewer", {})
                iv_name = iv_obj.get("name", {})
                if isinstance(iv_name, dict):
                    iv_names.append(iv_name.get("zh_cn", "") or iv_name.get("en_us", ""))
            rtype = iv.get("interview_round_type", {})
            rtype_name = rtype.get("name", {}).get("zh_cn", "") if isinstance(rtype, dict) else ""
            interviews.append({
                "round": iv.get("round"),
                "round_type": rtype_name,
                "begin_time": begin,
                "end_time": end,
                "feedback_submitted": fb,
                "conclusion": concl,
                "interviewers": iv_names,  # 面试官名字列表（催面评直用，不用 Agent 拼）
            })
            if concl is not None:
                latest_conclusion = concl
            if end and from_ms(end) and from_ms(end) < NOW:
                interview_over = True

        out.append({
            "talent_id": tid,
            "name": name,
            "job_title": job_title,
            "job_id": app.get("job_id", ""),
            "dept": dept_name,
            "application_id": aid,
            "stage_type": stage_type,
            "stage_name": stage_name,
            "dwell_ms": dwell_ms,
            "dwell_days": dwell_days,
            "termination_type": app.get("termination_type"),  # 1=已淘汰（active_status=2）
            "active_status": app.get("active_status"),
            "interviews": interviews,
            "latest_conclusion": latest_conclusion,
            "interview_over": interview_over,
            # 客观字段同步用（write_back 回写跟踪表）
            "stage_enter_ms": dwell_ms,  # 当前阶段进入时间（= dwell_ms）
            "latest_interview_begin_ms": max((iv["begin_time"] for iv in interviews if iv.get("begin_time")), default=None),
            "passed_rounds": sum(1 for iv in interviews if iv.get("conclusion") == 1),  # 已通过轮数
        })
    return out


def compute_battle_list(ats_people, track_recs, events):
    """核心计算：基于 ATS 中轴，产出结构化作战清单。

    所有判断都是确定性的（不靠关键词、不靠 LLM）：
      - stuck: 卡在当前阶段 ≥2 天
      - to_advance: 面评通过但还停在面试阶段
      - feedback_overdue: 面试已过但没交面评
      - track_vs_ats_gaps: 跟踪表状态落后于 ATS（talent_id 精确比对）
      - untracked_in_ats: ATS 有但跟踪表无
      - today_interviews: 今天有面试
    """
    # 跟踪表索引：talent_id → record（优先），降级 name → record
    track_by_tid = {r.get("talent_id"): r for r in track_recs if r.get("talent_id")}
    track_by_name = {r.get("候选人"): r for r in track_recs if r.get("候选人")}
    track_tids = set(track_by_tid.keys())
    track_names = set(track_by_name.keys())

    # 日程索引：application_id → event
    event_by_app = {}
    for e in events:
        if e.get("application_id"):
            event_by_app[e["application_id"]] = e

    stuck, to_advance, feedback_overdue = [], [], []
    track_vs_ats_gaps, untracked_in_ats, today_interviews = [], [], []

    for p in ats_people:
        aid = p["application_id"]
        tid = p["talent_id"]
        name = p["name"]
        stage_type = p["stage_type"]
        dwell = p["dwell_days"]

        # 1) 卡住：面试/offer 阶段 ≥2 天算卡（真正的瓶颈）；
        #    初筛阶段 ≥3 天才算卡（初筛本身需要时间，阈值更宽松）
        stuck_threshold = STUCK_WARN if stage_type in (STAGE_INTERVIEW, STAGE_OFFER, STAGE_ONBOARD) else 3
        if dwell is not None and dwell >= stuck_threshold:
            level = "🔴" if dwell >= STUCK_CRITICAL else ("🟠" if dwell >= STUCK_URGENT else "🟡")
            reason = ""
            if stage_type == STAGE_INTERVIEW:
                if not p["interviews"]:
                    # 暗坑检测：面试阶段但零面试记录（罗海贵类）
                    # 既不在 feedback_overdue（空列表不触发循环），也不在 to_advance（无 conclusion）
                    # ——两头不靠，最容易漏的人。dwell≥7天必须标出来人工核查。
                    reason = "⚠️面试阶段零记录，疑似漏安排/漏面评，需核查"
                    level = "🔴"  # 强制升级，确保被看见
                elif p["interview_over"] and p["latest_conclusion"] is None:
                    reason = "面试已过，等面评"
                elif p["latest_conclusion"] == 1:
                    reason = "面评通过，等推进下一轮"
                elif p["latest_conclusion"] == 2:
                    reason = "面评不通过，待终止决策"
                else:
                    reason = "面试阶段无进展"
            elif stage_type in (STAGE_OFFER, STAGE_ONBOARD):
                reason = f"{p['stage_name']}阶段，催HR/走流程"
            elif stage_type in (1, 2):
                reason = "初筛阶段无进展"
            else:
                reason = f"{p['stage_name']}阶段无进展"
            stuck.append({
                "name": name, "talent_id": tid, "stage": p["stage_name"],
                "dwell_days": dwell, "level": level, "reason": reason,
            })

        # 2) 待推进下一轮（面评通过 + 还在面试阶段）
        if stage_type == STAGE_INTERVIEW and p["latest_conclusion"] == 1:
            passed_round = max((iv["round"] for iv in p["interviews"] if iv["conclusion"] == 1), default=0)
            to_advance.append({
                "name": name, "talent_id": tid, "passed_round": passed_round,
                "stage": p["stage_name"], "dwell_days": dwell,
            })

        # 3) 催面评（只看时间最新的那场面试，不追溯旧场）
        #    旧逻辑遍历所有面试导致误报：骆航4场里早期某场没交面评，但最新场已通过
        #    ——流程早该推进了，报旧场欠面评反而掩盖真实问题。
        #    新逻辑：只看最新一场。最新场已交（无论通过与否）= 流程已往前走，不报欠面评。
        if p["interviews"]:
            latest_iv = max(p["interviews"], key=lambda x: x.get("begin_time") or 0)
            iv_end = from_ms(latest_iv.get("end_time"))
            if iv_end and iv_end < NOW:  # 面试已过
                if not latest_iv.get("feedback_submitted") or latest_iv.get("conclusion") is None:
                    overdue_days = (NOW - iv_end).days
                    if overdue_days >= 14:
                        urgency = "🔴严重"  # >14天：候选人快凉了，必须今天催
                    elif overdue_days >= 4:
                        urgency = "🟠常规"   # 4-14天：该催
                    else:
                        urgency = "🟡可缓"   # <4天：正常等待，别催太急
                    feedback_overdue.append({
                        "name": name, "talent_id": tid,
                        "interview_time": fmt_ms_full(latest_iv.get("begin_time")),
                        "round": latest_iv.get("round"), "round_type": latest_iv.get("round_type"),
                        "interviewers": latest_iv.get("interviewers", []),  # 面试官直用
                        "overdue_days": overdue_days, "urgency": urgency,
                    })

        # 4) 跟踪表 vs ATS 差异（talent_id 精确，降级姓名）
        rec = track_by_tid.get(tid) or track_by_name.get(name)
        match_method = "talent_id" if (rec and rec.get("talent_id") == tid) else ("姓名" if rec else None)
        if rec:
            track_status = rec.get("状态", "")
            # 表状态 vs ATS 阶段矛盾（覆盖所有组合，不只旧版 2 种）
            mismatch = None
            if stage_type in (STAGE_OFFER, STAGE_ONBOARD) and track_status not in ("通过", "已完成"):
                mismatch = f"ATS={p['stage_name']} 但表={track_status}"
            elif stage_type == STAGE_INTERVIEW and track_status in ("已发简历", "等测题", "终止", "未通过"):
                # 待安排/待约面 对面试阶段是合法的（刚进面试还没排时间）
                # 只有明显旧状态（初筛阶段的状态）才算表落后
                mismatch = f"ATS=面试 但表={track_status}（表落后）"
            elif stage_type in (1, 2) and track_status in ("已排期",):
                mismatch = f"ATS={p['stage_name']} 但表={track_status}"
            if mismatch:
                track_vs_ats_gaps.append({
                    "name": name, "talent_id": tid, "match": match_method,
                    "track_status": track_status, "ats_stage": p["stage_name"],
                    "mismatch": mismatch, "_rid": rec.get("_rid"),
                })
        else:
            # ATS 有但表无
            untracked_in_ats.append({
                "name": name, "talent_id": tid, "stage": p["stage_name"],
                "job": p["job_title"], "dwell_days": dwell,
            })

        # 5) 今日面试（从日程 + application_id 精确匹配）
        ev = event_by_app.get(aid)
        if ev:
            ev_start = ev.get("start")
            if ev_start and TODAY_STR in str(ev_start)[:10]:
                today_interviews.append({
                    "name": name, "talent_id": tid,
                    "time": ev.get("start"), "summary": ev.get("summary"),
                    "in_track": bool(rec),
                })

    return {
        "stuck": stuck,
        "to_advance": to_advance,
        "feedback_overdue": feedback_overdue,
        "track_vs_ats_gaps": track_vs_ats_gaps,
        "untracked_in_ats": untracked_in_ats,
        "today_interviews": today_interviews,
    }


# ============================================================
# ③ 输出
# ============================================================
def build_report(ats_people, battle, messages, track_recs):
    """组装最终 JSON 报告（structured + raw_messages 分离）。"""
    # 解析飞书招聘 bot 卡片为结构化事件（高价值，旧版全靠 Agent 手读）
    hire_events = parse_hire_bot_cards(messages)
    report = {
        "date": TODAY_STR,
        "generated_at": NOW.strftime("%Y-%m-%d %H:%M"),
        "summary": {
            "ATS总投递": len(ats_people),
            "跟踪表总数": len(track_recs),
            "今日面试": len(battle["today_interviews"]),
            "卡住(≥2天)": len(battle["stuck"]),
            "待推进下一轮": len(battle["to_advance"]),
            "催面评": len(battle["feedback_overdue"]),
            "表落后ATS": len(battle["track_vs_ats_gaps"]),
            "ATS有表无(漏建行)": len(battle["untracked_in_ats"]),
            "消息总数": len(messages),
            "招聘通知事件": len(hire_events),
        },
        "structured": {
            "ats": [{
                "name": p["name"], "talent_id": p["talent_id"],
                "stage": p["stage_name"], "job": p["job_title"], "dept": p["dept"],
                "dwell_days": p["dwell_days"],
                "latest_conclusion": p["latest_conclusion"],
                "interview_count": len(p["interviews"]),
            } for p in ats_people],
            "today_interviews": battle["today_interviews"],
            "stuck": battle["stuck"],
            "to_advance": battle["to_advance"],
            "feedback_overdue": battle["feedback_overdue"],
            "track_vs_ats_gaps": battle["track_vs_ats_gaps"],
            "untracked_in_ats": battle["untracked_in_ats"],
            "hire_bot_events": hire_events,  # 飞书招聘通知卡片已结构化
        },
        "raw_messages": messages,  # 全量原文，交给 Agent LLM 判读
    }
    return report


def print_report(report):
    """stdout 打印作战清单摘要（中文安全，控制长度）。"""
    b = report["structured"]
    s = report["summary"]
    print(f"\n{'='*60}")
    print(f"📊 对账结果 [ATS中轴] [{report['date']}]")
    print(f"{'='*60}")
    print(f"ATS {s['ATS总投递']}人 | 跟踪表 {s['跟踪表总数']}人 | 消息 {s['消息总数']}条")
    print(f"今日面试 {s['今日面试']} | 卡住≥2天 {s['卡住(≥2天)']} | 待推进 {s['待推进下一轮']} | 催面评 {s['催面评']}")
    print(f"表落后ATS {s['表落后ATS']} | 漏建行 {s['ATS有表无(漏建行)']}")

    if b["today_interviews"]:
        print(f"\n🔴 今日面试（必须落地）—— {len(b['today_interviews'])} 人")
        for x in b["today_interviews"]:
            print(f"   • {x['name']:<6} {str(x.get('time',''))[:16]} | 表内:{x['in_track']}")

    if b["stuck"]:
        # 分级展示：近期（2-4天）单列，积压（5天+）折叠，让 Agent 优先关注近期
        recent = [x for x in b["stuck"] if (x["dwell_days"] or 0) < STUCK_CRITICAL]
        backlog = [x for x in b["stuck"] if (x["dwell_days"] or 0) >= STUCK_CRITICAL]
        if recent:
            print(f"\n🟠 卡住 2-4天（近期，需处理）—— {len(recent)} 人")
            for x in sorted(recent, key=lambda r: (r["dwell_days"] or 0)):
                print(f"   • {x['name']:<6} {x['stage']} | 停{x['dwell_days']}天 | {x['reason']}")
        if backlog:
            # 积压按原因聚类统计，不逐条列（30+ 条逐条列会淹没重点）
            from collections import Counter
            reason_cnt = Counter(x["reason"] for x in backlog)
            print(f"\n🔴 卡住 ≥5天（历史积压）—— {len(backlog)} 人（详情见 JSON）")
            for reason, cnt in reason_cnt.most_common():
                print(f"   • {cnt}人: {reason}")

    if b["to_advance"]:
        print(f"\n🟢 待推进下一轮（面评通过）—— {len(b['to_advance'])} 人")
        for x in b["to_advance"]:
            print(f"   • {x['name']:<6} 通过第{x['passed_round']}轮 | 停{x.get('dwell_days','?')}天")

    if b["feedback_overdue"]:
        # 按紧急度排序：严重→常规→可缓
        urgency_order = {"🔴严重": 0, "🟠常规": 1, "🟡可缓": 2}
        fo_sorted = sorted(b["feedback_overdue"],
                           key=lambda x: urgency_order.get(x.get("urgency", ""), 9))
        print(f"\n⏰ 催面评（面试已过没交）—— {len(fo_sorted)} 人")
        for x in fo_sorted:
            ivs = "/".join(x.get("interviewers", [])) or "未知"
            urg = x.get("urgency", "")
            od = x.get("overdue_days", 0)
            print(f"   • {x['name']:<6} {x['interview_time']} {x.get('round_type','')}"
                  f" | {urg}欠{od}天 | 面试官:{ivs}")

    if b["track_vs_ats_gaps"]:
        print(f"\n⚠️ 表落后 ATS —— {len(b['track_vs_ats_gaps'])} 人")
        for x in b["track_vs_ats_gaps"]:
            print(f"   • {x['name']:<6} {x['mismatch']} | 匹配:{x['match']}")

    if b["untracked_in_ats"]:
        print(f"\n📋 ATS 有但表无（漏建行）—— {len(b['untracked_in_ats'])} 人")
        for x in b["untracked_in_ats"][:15]:
            print(f"   • {x['name']:<6} {x['stage']} | {x['job']} | 停{x.get('dwell_days','?')}天")
        if len(b["untracked_in_ats"]) > 15:
            print(f"   ... 还有 {len(b['untracked_in_ats'])-15} 人")

    # 飞书招聘 bot 通知事件（面试反馈/接受/未到场，已结构化）
    hire_events = b.get("hire_bot_events", [])
    if hire_events:
        from collections import Counter
        evt_cnt = Counter(e["event"] for e in hire_events)
        print(f"\n🔔 飞书招聘通知事件 —— {len(hire_events)} 条")
        for evt, cnt in evt_cnt.most_common():
            # 列出该事件的候选人（反馈类带结论的优先）
            samples = [e for e in hire_events if e["event"] == evt][:3]
            names = " ".join(f"{e['candidate']}({e.get('conclusion','')})" for e in samples if e.get("candidate"))
            print(f"   • {cnt}条 {evt}: {names}")

    print(f"\n💬 raw_messages {len(report['raw_messages'])} 条已导出（交给 Agent LLM 判读意图）")
    print(f"✅ 报告已存: notes/_daily_review.json")
    print(f"💡 加 --write 可回写跟踪表（优先级/下一步动作/进展日期）")

    # 判读启动钩子：检查 _signals.json 是否当天，提示 Agent 接手判读
    import datetime as _dt
    sig_path = os.path.join(os.path.dirname(__file__), "_signals.json")
    sig_stale = True
    sig_count_hint = ""
    if os.path.exists(sig_path):
        try:
            _sig = json.load(open(sig_path, encoding="utf-8"))
            _sig_date = _sig.get("date", "")
            _today = _dt.date.today().isoformat()
            if _sig_date == _today:
                sig_stale = False
                sig_count_hint = f"（_signals.json 已是当天，{len(_sig.get('signals', []))} 条信号）"
        except Exception:
            pass
    if sig_stale and len(report.get("raw_messages", [])) > 0:
        print(f"\n{'='*60}")
        print(f"⚠️  判读待完成：{len(report['raw_messages'])} 条消息已导出，但 _signals.json 非当天。")
        print(f"   → 说「判读」让 Agent 读 raw_messages 做 LLM 意图判读，更新 _signals.json。")
        print(f"   （判读是 Agent 的活，脚本只导出原文——这是设计约定，见 review-contract.md）")
        print(f"{'='*60}")
    elif not sig_stale:
        print(f"   ✅ 信号已判读{sig_count_hint}")


# ============================================================
# ④ --write 回写
# ============================================================
def _map_ats_to_track_fields(p):
    """ATS 阶段 → 跟踪表客观字段（状态/当前轮次）映射。

    跟踪表选项（精确字符串，写错会被静默忽略）：
      状态: 待安排/已排期/已完成/通过/未通过/终止/等测题
      当前轮次: 已发简历/待约面/一面(技术面)/二面(业务负责人)/三面(HR面)/四面(部门负责人)
    """
    st = p.get("stage_type")
    passed = p.get("passed_rounds", 0)
    concl = p.get("latest_conclusion")
    result = {}

    # 状态映射
    if st in (1, 2):          # 初筛/简历评估
        result["状态"] = "等测题"
        result["当前轮次"] = "已发简历"
    elif st == STAGE_INTERVIEW:  # 面试
        # has_iv 判据用"是否有任何面试记录"（而非 latest_interview_begin_ms）。
        # latest_interview_begin_ms 是历史最大值，passed=0 但已不通过的人该字段非空，
        # 用它会把"已凉的人"误标为"正在面"。用 interviews 非空判断更准确。
        interviews = p.get("interviews") or []
        has_iv_record = len(interviews) > 0
        has_iv_time = bool(p.get("latest_interview_begin_ms"))
        result["状态"] = "已排期" if has_iv_time else "待安排"
        # 当前轮次 = 当前所在轮次（不是"已通过轮数"），按 passed 推：
        #   passed=0 无面试记录 → 待约面（还没开始面）
        #   passed=0 有面试记录 → 一面(技术面)（已进一轮：在面中/等结论/未通过待终止）
        #   passed=1 → 已过一面，在二面(业务负责人)
        #   passed=2 → 已过二面，在三面(HR面)
        #   passed≥3 → 已过三面及以上，在四面(部门负责人)
        # （与 interview-guide 4 轮模型 + review-contract.md 对齐）
        if passed == 0:
            result["当前轮次"] = "待约面" if not has_iv_record else "一面(技术面)"
        else:
            round_map = {1: "二面(业务负责人)", 2: "三面(HR面)", 3: "四面(部门负责人)"}
            result["当前轮次"] = round_map.get(passed, "四面(部门负责人)")
    elif st == STAGE_OFFER:     # Offer沟通
        result["状态"] = "通过"
        result["当前轮次"] = "四面(部门负责人)"
    elif st == STAGE_ONBOARD:   # 待入职
        result["状态"] = "通过"
        result["当前轮次"] = "四面(部门负责人)"

    # 面试时间（最近一场面试的开始时间）
    iv_begin = p.get("latest_interview_begin_ms")
    if iv_begin:
        result["面试时间"] = iv_begin

    # 进入阶段日期
    stage_enter = p.get("stage_enter_ms")
    if stage_enter:
        result["进入阶段日期"] = stage_enter

    return result


def write_back(report, track_recs, ats_people=None):
    """回写跟踪表（分层共管：客观字段ATS同步 + 主观字段人填）。

    客观层（ATS拥有，脚本全量同步）：状态/当前轮次/面试时间/进入阶段日期
    主观层（人拥有，脚本只填空槽）：优先级/下一步动作
    主键层：talent_id（由 backfill/track_after_hire 维护，本函数不动）

    先读再写：客观字段若与 ATS 不一致，以 ATS 为准覆盖并报告。
    失败行写入 notes/_write_failures.json（供 post_bash hook 捕获）。
    """
    rid_by_tid = {r.get("talent_id"): r.get("_rid") for r in track_recs if r.get("talent_id") and r.get("_rid")}
    rid_by_name = {r.get("候选人"): r.get("_rid") for r in track_recs if r.get("候选人") and r.get("_rid")}
    # 跟踪表现值索引（先读再写保护用）
    track_by_rid = {r.get("_rid"): r for r in track_recs if r.get("_rid")}

    # ats_people 索引（含完整字段，用于客观字段同步）
    ats_by_tid = {}
    ats_by_name = {}
    if ats_people:
        for p in ats_people:
            ats_by_tid[p["talent_id"]] = p
            ats_by_name[p["name"]] = p

    # === 客观字段同步（ATS → 表）===
    objective_overrides = []  # 记录覆盖了哪些人工值
    obj_actions = {}  # rid -> 客观字段
    for p in ats_people or []:
        rid = rid_by_tid.get(p["talent_id"]) or rid_by_name.get(p["name"])
        if not rid:
            continue  # 表无此行，客观字段无处可写（漏建行问题由 track_after_hire 解决）
        obj_fields = _map_ats_to_track_fields(p)
        if not obj_fields:
            continue
        # 先读再写：检查现值是否与 ATS 一致，不一致才标记覆盖
        existing = track_by_rid.get(rid, {})
        for fname, fval in obj_fields.items():
            cur = existing.get(fname)
            if cur and str(cur) != str(fval):
                objective_overrides.append(f"{p['name']}.{fname}: 表={cur} → ATS={fval}")
        obj_actions[rid] = obj_fields

    # === 主观字段（人拥有，只填空槽，不覆盖已有值）===
    subj_actions = {}  # rid -> 主观字段
    for x in report["structured"]["today_interviews"]:
        rid = rid_by_tid.get(x["talent_id"]) or rid_by_name.get(x["name"])
        if rid:
            subj_actions.setdefault(rid, {}).update({"优先级": "🔴紧急", "下一步动作": f"今天面试{x.get('time','')}"})
    for x in report["structured"]["stuck"]:
        rid = rid_by_tid.get(x.get("talent_id")) or rid_by_name.get(x.get("name"))
        if rid:
            pri = "🔴紧急" if x["dwell_days"] >= STUCK_CRITICAL else ("高" if x["dwell_days"] >= STUCK_URGENT else "中")
            subj_actions.setdefault(rid, {}).update({"优先级": pri, "下一步动作": f"卡{x['dwell_days']}天:{x['reason']}"})
    for x in report["structured"]["to_advance"]:
        rid = rid_by_tid.get(x.get("talent_id")) or rid_by_name.get(x.get("name"))
        if rid:
            subj_actions.setdefault(rid, {}).update({"优先级": "高", "下一步动作": f"通过{x['passed_round']}轮,推进下一轮"})

    # 主观字段：只填空槽（尊重人工已填值，不覆盖）
    to_del = []
    for rid, sfields in subj_actions.items():
        existing = track_by_rid.get(rid, {})
        kept = {}
        for fname, fval in sfields.items():
            if not existing.get(fname):  # 空槽才填
                kept[fname] = fval
        if kept:
            subj_actions[rid] = kept
        else:
            to_del.append(rid)  # 全被人工填了，跳过
    for rid in to_del:
        del subj_actions[rid]

    # === 合并执行 ===
    all_rids = set(obj_actions.keys()) | set(subj_actions.keys())
    now_ms = to_ms(f"{TODAY_STR}T09:00:00")
    ok_cnt = 0
    failures = []
    for rid in all_rids:
        fields = {}
        fields.update(obj_actions.get(rid, {}))   # 客观字段
        fields.update(subj_actions.get(rid, {}))  # 主观字段
        fields["最近进展日期"] = now_ms
        if upsert_track_record(fields, record_id=rid):
            ok_cnt += 1
        else:
            name = track_by_rid.get(rid, {}).get("候选人", rid)
            failures.append({"rid": rid, "name": name, "fields": list(fields.keys())})
        time.sleep(0.05)

    print(f"\n✏️ 回写跟踪表 {ok_cnt}/{len(all_rids)} 条"
          f"（客观同步 {len(obj_actions)} / 主观补填 {len(subj_actions)}）")
    if objective_overrides:
        print(f"   ⚠️ 客观字段覆盖了 {len(objective_overrides)} 处人工旧值（以ATS为准）：")
        for o in objective_overrides[:10]:
            print(f"      {o}")
        if len(objective_overrides) > 10:
            print(f"      ... 共 {len(objective_overrides)} 处")

    # 失败行落盘（供 post_bash hook 捕获）
    fail_path = "notes/_write_failures.json"
    if failures:
        with open(fail_path, "w", encoding="utf-8") as f:
            json.dump({"date": TODAY_STR, "failures": failures}, f, ensure_ascii=False, indent=2)
        print(f"   ❌ 失败 {len(failures)} 条，详见 {fail_path}")
    elif os.path.exists(fail_path):
        os.remove(fail_path)  # 清理上次残留的失败记录


# ============================================================
# 主流程
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="回写跟踪表")
    args = ap.parse_args()

    print(f"⏳ [{NOW.strftime('%H:%M:%S')}] 拉取数据源（6路并行）...")

    # 6 路并行拉取（独立数据源必须并行，串行浪费时间）
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut_apps = ex.submit(fetch_applications_all)
        fut_track = ex.submit(list_track_records)
        fut_cal = ex.submit(fetch_calendar)
        fut_msgs = ex.submit(fetch_all_messages)
        app_details = fut_apps.result()
        track_recs = fut_track.result()
        events = fut_cal.result()
        messages = fut_msgs.result()

    # ATS interviews 依赖 app_details（第二轮并行）
    interviews_by_app = fetch_interviews_batch(app_details)

    print(f"\n⏳ 结构化计算（ATS中轴）...")
    # 过滤：① 只看我负责的岗位（create_user_id=我 的 job_id）
    #       ② 排除已淘汰（termination_type=1，ATS 已标记终止，不用再关注）
    my_job_ids = _load_my_jobs()
    scope_apps = app_details
    if my_job_ids is not None:
        before = len(scope_apps)
        scope_apps = [a for a in scope_apps if a.get("job_id") in my_job_ids]
        print(f"  我负责的岗位投递 {len(scope_apps)}/{before} 条（其余非我职责已筛除）")
    # 已淘汰的过滤掉（termination_type=1 = ATS 已终止，不再跟进）
    # 已入职的也过滤掉（stage.type=7 = 终态，入职后归 HR，不进作战清单/看板/保温）
    active_apps = [a for a in scope_apps
                   if not a.get("termination_type")
                   and a.get("stage", {}).get("type") != STAGE_EMPLOYED]
    terminated = sum(1 for a in scope_apps if a.get("termination_type"))
    employed = sum(1 for a in scope_apps if a.get("stage", {}).get("type") == STAGE_EMPLOYED)
    removed = len(scope_apps) - len(active_apps)
    if removed:
        print(f"  活跃 {len(active_apps)} 条（{terminated} 条已淘汰 + {employed} 条已入职筛除）")
    ats_people = parse_ats(active_apps, interviews_by_app)
    battle = compute_battle_list(ats_people, track_recs, events)

    report = build_report(ats_people, battle, messages, track_recs)

    out_path = "notes/_daily_review.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print_report(report)

    if args.write:
        write_back(report, track_recs, ats_people)


if __name__ == "__main__":
    main()
