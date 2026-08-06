# -*- coding: utf-8 -*-
"""
_lark_shared.py — 飞书招聘共享库

收口所有飞书 API 调用（lark-cli 封装 + hire 域封装）+ JSON 抽取 + 跟踪表 + 时间转换。
新建/重写的脚本统一 import 本库，消灭 copy-paste。

用法：
    from _lark_shared import api, cli, hire_get_talent, hire_list_jobs, ...
    from _lark_shared import STAGE_TYPE, map_degree, to_ms, days_since

依赖：lark-cli（subprocess 全路径）、requests（仅 upload_attachment_with_name 用）。
hire API 契约见 ~/.agents/skills/lark-hire/。
"""
import subprocess, json, re, datetime, os
import requests

# ============================================================
# 配置（环境变量优先，回退默认值）
# ============================================================
CLI = os.environ.get(
    "LARK_CLI_PATH",
    "lark-cli",
)
HIRE_APP = os.environ.get("HIRE_APP_ID", "")  # 飞书招聘应用
# ⚠️ secret 走环境变量（2026-07-22 治理）：旧版曾硬编码在源码 + docs + memory 共 6 处
# 用法：export HIRE_APP_SECRET=<你的值>（或 setx / .bashrc 持久化）
# 没配置时此值为 None，upload_attachment_with_name 会报错提示——不让 import 阶段炸，但用到时立刻报
HIRE_APP_SECRET = os.environ.get("HIRE_APP_SECRET")
FEISHU_BASE = "https://open.feishu.cn/open-apis"
BASE_TOKEN = os.environ.get("TRACKING_BASE_TOKEN", "")
TRACK_TABLE = os.environ.get("TRACKING_TABLE_ID", "")
# 吴春波 open_id（飞书用户标识，用于岗位归属过滤 + 私聊抓取）
WUBO_ID = os.environ.get("WUBO_OPEN_ID", "")
TZ = datetime.timezone(datetime.timedelta(hours=8))  # +08:00

# 招聘流程阶段枚举（2026-07-20 job_processes 实测权威定义，校招/社招一致）
# 完整表见 ~/.agents/skills/lark-hire/references/hire-stages.md
STAGE_SCREEN = 1       # 简历初筛
STAGE_EVALUATE = 2     # 简历评估
STAGE_INTERVIEW = 4    # 面试
STAGE_OFFER = 5        # Offer沟通
STAGE_ONBOARD = 6      # 待入职（要跟：催HR走流程）
STAGE_EMPLOYED = 7     # 已入职（终态：不进作战清单/看板/保温）

STAGE_TYPE = {
    STAGE_SCREEN: "简历初筛",
    STAGE_EVALUATE: "简历评估",
    STAGE_INTERVIEW: "面试",
    STAGE_OFFER: "Offer沟通",
    STAGE_ONBOARD: "待入职",
    STAGE_EMPLOYED: "已入职",
}

# 学历枚举（hire-fields.md 权威）
DEGREE_MAP = {"大专": 5, "专科": 5, "中专": 4, "高中": 3,
              "本科": 6, "学士": 6, "硕士": 7, "研究生": 7, "博士": 8}


def map_degree(s):
    """学历文本 → hire degree 枚举（3高中/4中专/5大专/6本科/7硕士/8博士），未匹配返 None"""
    if not s:
        return None
    s = str(s)
    for k, v in DEGREE_MAP.items():
        if k in s:
            return v
    return None


def map_gender(g):
    """性别 → hire 枚举（1男/2女），未知返 None（不传 0）"""
    return {1: 1, 2: 2}.get(g)


def is_cn_mobile(s):
    """国内号：11位且1开头。海外号不传，否则报 invalid mobile country code。"""
    if not s:
        return False
    s = re.sub(r"\D", "", str(s))
    return len(s) == 11 and s[0] == "1"


def guess_name_from_file(path):
    """从文件名提取姓名：'罗佳_Spine动作_简历.pdf' -> '罗佳'"""
    base = os.path.basename(path)
    base = re.sub(r"\.(pdf|docx?|PDF|DOCX?)$", "", base)
    name = re.split(r"[_\-\s（(]", base)[0]
    return name.strip() if name.strip() else "未知名"


# ============================================================
# lark-cli 调用封装
# ============================================================
def cli(args, timeout=120):
    """跑 lark-cli 子命令（base/im/calendar 域），返回 stdout+stderr 合并文本。

    错误信息常在 stderr，故合并。中文安全（caller 写文件再读）。
    """
    # git-bash (MSYS2) 会把 /open-apis/... 前导斜杠自动转换成 C:/Program Files/Git/open-apis/...
    # 导致所有 lark-cli api POST/GET 请求路径变成 Windows 绝对路径 → 404。
    # 设 MSYS_NO_PATHCONV=1 禁用路径转换（不影响真正需要路径转换的场景，lark-cli 参数不走文件路径）。
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    # Windows 下 lark-cli 输出是 utf-8，但 text=True 默认用 cp936 解码会抛 UnicodeDecodeError。
    # 显式指定 utf-8 + errors=replace，根治中文输出解码炸裂（2026-07-13 补建行踩坑）。
    r = subprocess.run([CLI] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, env=env)
    return (r.stdout or "") + (r.stderr or "")


def api(method, path, identity="bot", params=None, data=None, data_file=None, timeout=120):
    """调 lark-cli api（hire/document_ai 域）。

    method: GET/POST
    path:   /open-apis/...
    identity: 'bot'（hire 写接口必须）或 'user'（base/im/calendar 读）
    params: dict → --params（query string，JSON）
    data:   dict → --data（POST body，JSON）；与 data_file 二选一
    data_file: 文件路径 → --data @file（cwd 相对路径，传中文/大 body 时用）
    返回：解析后的 dict；失败返回 None
    """
    args = ["api", method, path, "--as", identity]
    if params:
        args += ["--params", json.dumps(params, ensure_ascii=False)]
    if data_file:
        args += ["--data", f"@{data_file}"]
    elif data is not None:
        args += ["--data", json.dumps(data, ensure_ascii=False)]
    raw = cli(args, timeout=timeout)
    d = extract_json(raw)
    return d


def extract_json(raw):
    """从混了 tip/日志/notice 的 lark-cli 输出里抠出第一个 JSON 对象。

    lark-cli stdout 可能带 _notice 更新提示、彩色日志，正则取最外层 {}。
    """
    m = re.search(r'\{[\s\S]*\}', raw)
    return json.loads(m.group(0)) if m else None


# ============================================================
# 时间转换
# ============================================================
def to_ms(dt_str):
    """ISO 时间字符串 '2026-07-02 10:00' / '2026-07-02T10:00:00' → 毫秒时间戳（+08:00）。
    空串/None → 返回 ''（飞书 API 对空日期字段接受空串，不报错）。"""
    if not dt_str or not dt_str.strip():
        return ""
    s = dt_str.replace(" ", "T") if " " in dt_str and "T" not in dt_str else dt_str
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return int(dt.timestamp() * 1000)


def from_ms(ms):
    """毫秒时间戳（int 或 str）→ 本地 datetime（+08:00）。"""
    if ms is None or ms == "":
        return None
    return datetime.datetime.fromtimestamp(int(ms) / 1000, tz=TZ)


def fmt_ms(ms):
    """毫秒时间戳 → 'YYYY-MM-DD' 字符串。"""
    dt = from_ms(ms)
    return dt.strftime("%Y-%m-%d") if dt else ""


def fmt_ms_full(ms):
    """毫秒时间戳 → 'YYYY-MM-DD HH:MM' 字符串。"""
    dt = from_ms(ms)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def days_since(d):
    """日期字符串 'YYYY-MM-DD' / ISO / 毫秒时间戳 → 距今天数（int），无法解析返回 None。

    今天=0，昨天=1。用于"卡了几天"计算。
    """
    if d is None or d == "":
        return None
    try:
        if isinstance(d, (int, float)) or (isinstance(d, str) and d.isdigit()):
            dt = from_ms(d)
        else:
            s = str(d).replace(" ", "T") if " " in str(d) and "T" not in str(d) else str(d)
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
        if dt is None:
            return None
        today = datetime.datetime.now(tz=TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        dt0 = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return (today - dt0).days
    except Exception:
        return None


# ============================================================
# 跟踪表（多维表格）读取
# ============================================================
# 跟踪表字段 id（fieldlist_track.json 实测锁定，避免每次查）
FLD = {
    "候选人": "fldsTlIZsk",
    "状态": "fldr5lHqwK",
    "当前轮次": "fldZ9sJpcA",
    "面试时间": "fld3RoTQvQ",
    "面试官": "fldmw2kMEl",
    "部门": "fldKh4NPLQ",
    "岗位": "fldfi6hRY6",
    "职能类别": "fldTQUJs9j",
    "进入阶段日期": "fldX2OIZEY",
    "最近进展日期": "fldFl9dVV5",
    "优先级": "fldnrbv4u3",
    "下一步动作": "fldH5Ac9we",
    # talent_id 列 field_id（2026-07-01 用 +field-create 创建，动态发现兜底）
    "talent_id": "fldSTYVNJ2",
}


def cell(v):
    """多维表格单元格 → 字符串。

    cell 可能是 None/str/单选(dict)/多选(list)/日期(ms)。
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        # 多选 = [str] 或 [{text}]；取拼接
        parts = []
        for x in v:
            if isinstance(x, str):
                parts.append(x)
            elif isinstance(x, dict):
                parts.append(x.get("text") or x.get("name") or "")
        return "".join(parts)
    if isinstance(v, dict):
        # 单选 {name} 或 富文本 {text}
        return v.get("text") or v.get("name") or ""
    return str(v)


def _resolve_talent_id_field():
    """动态查找 talent_id 列的 field_id（用户手动加的列，id 未知）。

    跟踪表目前没有此列，返回 None。用户加列后自动生效。
    """
    if FLD["talent_id"] is not None:
        return FLD["talent_id"]
    d = api("GET", "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
            .replace("{app_token}", BASE_TOKEN).replace("{table_id}", TRACK_TABLE),
            identity="user") or {}
    # lark-cli base +field-list 更可靠，这里用 cli 走 base 子命令
    raw = cli(["base", "+field-list", "--base-token", BASE_TOKEN,
               "--table-id", TRACK_TABLE, "--as", "user"])
    fd = extract_json(raw)
    if not fd:
        return None
    for f in fd.get("data", {}).get("fields", []):
        nm = f.get("name", "")
        if nm.lower().replace("-", "").replace("_", "") == "talentid" or nm == "talent_id":
            FLD["talent_id"] = f.get("id")
            return FLD["talent_id"]
    return None


def list_track_records(extra_fields=None):
    """拉跟踪表全部记录，返回 list[dict]。

    每条 dict 含 FLD 中的字段（候选人/状态/当前轮次/面试时间/...）+ '_rid'(record_id)
    + 'talent_id'（若列存在）。

    ⚠️ --field-id 投影固定列顺序（矩阵默认列顺序不稳定，2026-06-26 实测）。
    ⚠️ --limit 500 会返回空 stdout，用 200 并分页兜底。
    extra_fields: 额外要投影的字段名列表（如 ['talent_id']）。
    """
    wanted = list(FLD.keys())
    if extra_fields:
        wanted += [f for f in extra_fields if f not in wanted]

    # 去掉无 field_id 的（如 talent_id 未建列）
    field_ids = []
    name_order = []
    for nm in wanted:
        fid = FLD.get(nm)
        if fid:
            field_ids += ["--field-id", nm]
            name_order.append(nm)
        elif nm == "talent_id":
            fid = _resolve_talent_id_field()
            if fid:
                field_ids += ["--field-id", nm]
                name_order.append(nm)

    recs = []
    for lim in (200, 100):
        raw = cli(["base", "+record-list", "--base-token", BASE_TOKEN,
                   "--table-id", TRACK_TABLE] + field_ids +
                  ["--format", "json", "--as", "user", "--limit", str(lim)])
        d = extract_json(raw)
        if not d:
            continue
        data = d.get("data", {})
        rows = data.get("data", [])
        rids = data.get("record_id_list", [])
        for i, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            rec = {"_rid": rids[i] if i < len(rids) else None}
            for j, nm in enumerate(name_order):
                rec[nm] = cell(row[j] if j < len(row) else None)
            recs.append(rec)
        if recs:
            break  # 拿到就够（200 条一般覆盖全表）
    return recs


def upsert_track_record(fields, record_id=None, dry_run=False):
    """建/更新跟踪表一行。

    fields: dict 字段名→值（datetime 字段传毫秒 int，单选传命中选项的字符串）
    record_id: 有则 update（upsert --record-id），无则 create
    返回 True/False。
    """
    j = json.dumps(fields, ensure_ascii=False)
    if dry_run:
        print(f"  [DRY] {'UPDATE' if record_id else 'CREATE'}: {j[:120]}")
        return True
    args = ["base", "+record-upsert", "--base-token", BASE_TOKEN,
            "--table-id", TRACK_TABLE, "--json", j, "--as", "user"]
    if record_id:
        # ⚠️ lark-cli 没有 +record-update（unknown subcommand），更新也走 upsert --record-id
        args += ["--record-id", record_id]
    raw = cli(args)
    ok = extract_json(raw)
    return bool(ok and ok.get("ok"))


# ============================================================
# hire 域封装（飞书招聘 OpenAPI）
# 契约见 ~/.agents/skills/lark-hire/
# 全部默认 --as bot（写接口必须 bot，user 返 99991668）
# ============================================================
_HIRE_TOKEN = None  # 进程内缓存 tenant_access_token（仅 upload_attachment_with_name 用）


def get_hire_token():
    """换飞书招聘 tenant_access_token（带进程缓存）。
    仅 upload_attachment_with_name 用——lark-cli --file 传不了 file_name，必须 requests 直传。
    其他 hire 接口走 api()（lark-cli --as bot 内部管 token）。"""
    global _HIRE_TOKEN
    if _HIRE_TOKEN:
        return _HIRE_TOKEN
    if not HIRE_APP_SECRET:
        raise RuntimeError(
            "HIRE_APP_SECRET 未配置。请设置环境变量：\n"
            "  Windows:  setx HIRE_APP_SECRET <你的飞书招聘应用 secret>\n"
            "  git-bash: export HIRE_APP_SECRET=<你的飞书招聘应用 secret>\n"
            "  或写入 .bashrc / 系统环境变量持久化\n"
            "（secret 不再硬编码在源码；如需查值，看 .lark-cli/config.json）"
        )
    r = requests.post(f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": HIRE_APP, "app_secret": HIRE_APP_SECRET},
                      timeout=30).json()
    if "tenant_access_token" not in r:
        raise Exception(f"换 token 失败: {r}")
    _HIRE_TOKEN = r["tenant_access_token"]
    return _HIRE_TOKEN


# ---- hire 工具函数（内部）----
def _hire_check(d, where=""):
    """校验 lark-cli api 返回的 hire 响应。code 非 0 抛异常。"""
    if d is None:
        raise Exception(f"{where}: lark-cli 无 JSON 返回")
    # lark-cli 失败时包成 {ok:false, error:{code,message}}，成功时包成 {ok:true, data:{code,...}}
    if d.get("ok") is False:
        err = d.get("error", {}) or {}
        raise Exception(f"{where}: code={err.get('code')} msg={err.get('message')}")
    # lark-cli 输出包了一层 {ok, data}，hire 接口的 code 在 data.code
    code = d.get("code") if "code" in d else d.get("data", {}).get("code", 0)
    if code != 0:
        msg = d.get("msg") or d.get("data", {}).get("msg", "")
        raise Exception(f"{where}: code={code} msg={msg}")
    return d.get("data", {})


def _page_all(path, params=None, data_key="items"):
    """翻页拉 hire 列表接口（page_size≤20）。返回 items 数组。"""
    params = dict(params or {})
    params["page_size"] = 20
    out, pt = [], None
    while True:
        if pt:
            params["page_token"] = pt
        d = api("GET", path, "bot", params=params)
        data = _hire_check(d, path)
        out.extend(data.get(data_key, []) or [])
        pt = data.get("page_token") or ""
        if not data.get("has_more"):
            break
    return out


# ---- Talent（人才档案）----
def hire_get_talent(talent_id, user_id_type="open_id"):
    """查人才详情。返回 talent dict（含 resume_attachment_list 等）。"""
    d = api("GET", f"/open-apis/hire/v1/talents/{talent_id}", "bot",
            params={"user_id_type": user_id_type})
    data = _hire_check(d, "hire_get_talent")
    return data.get("talent") or data


def hire_combined_create(basic_info, att_id, careers=None, edus=None, self_eval=None):
    """全量建人才，返回 talent_id。
    basic_info: dict（name/mobile/mobile_code/mobile_country_code/email/gender/birthday）
    att_id: 简历附件 ID（upload_attachment_with_name 的返回）
    careers: list[dict]（company/title/start_time/end_time/career_type/desc）
    edus: list[dict]（school/degree/field_of_study/start_time/end_time）
    self_eval: str（自我评价）"""
    body = {"basic_info": basic_info, "resume_attachment_id": att_id}
    if careers:
        body["career_list"] = careers
    if edus:
        body["education_list"] = edus
    if self_eval and self_eval.strip():
        body["self_evaluation"] = {"contents": [{"text": self_eval.strip()}]}
    d = api("POST", "/open-apis/hire/v1/talents/combined_create", "bot", data=body)
    data = _hire_check(d, "hire_combined_create")
    return data.get("talent_id") or data.get("talent", {}).get("id")


def hire_combined_update(talent_id, basic_info, att_id=None, careers=None, edus=None):
    """更新人才。即使只改一个字段也必带 basic_info + talent_id（否则报 basic_info is required）。
    沿用原 basic_info 值即可。"""
    body = {"talent_id": talent_id, "basic_info": basic_info}
    if att_id:
        body["resume_attachment_id"] = att_id
    if careers:
        body["career_list"] = careers
    if edus:
        body["education_list"] = edus
    d = api("POST", "/open-apis/hire/v1/talents/combined_update", "bot", data=body)
    return _hire_check(d, "hire_combined_update")


def build_basic_info(name, mobile=None, email=None, gender=None, birthday_ms=None):
    """构造 basic_info dict。手机三件套自动配齐（CN_1/86）。
    海外号自动跳过（mobile_country_code 枚举未知，传了必失败，靠邮箱去重）。"""
    basic = {"name": name}
    if is_cn_mobile(mobile):
        basic["mobile"] = re.sub(r"\D", "", str(mobile))
        basic["mobile_code"] = "86"
        basic["mobile_country_code"] = "CN_1"
    if email:
        basic["email"] = email
    g = map_gender(gender)
    if g:
        basic["gender"] = g
    if birthday_ms:
        basic["birthday"] = str(birthday_ms)
    return basic


# ---- Application（投递）----
def hire_create_application(talent_id, job_id):
    """建投递，返回 (status, msg, application_id)。
    status: 'ok' / 'exists'(已投递,code 1002206) / 'fail'。"""
    d = api("POST", "/open-apis/hire/v1/applications", "bot",
            data={"talent_id": talent_id, "job_id": job_id})
    # 1002206 是业务码不是失败，单独处理
    code = d.get("data", {}).get("code", d.get("code")) if d else None
    if d and d.get("data", {}).get("code", d.get("code")) == 1002206:
        app_id = d.get("data", {}).get("application_id") or d.get("data", {}).get("data", {}).get("application_id")
        return "exists", "已投递过", app_id
    try:
        data = _hire_check(d, "hire_create_application")
        return "ok", "成功", data.get("application_id")
    except Exception as e:
        return "fail", str(e), None


def hire_get_application(app_id):
    """查投递详情。返回 application dict。"""
    d = api("GET", f"/open-apis/hire/v1/applications/{app_id}", "bot")
    data = _hire_check(d, "hire_get_application")
    return data.get("application") or data


def hire_list_applications(job_id=None, talent_id=None):
    """列投递。返回 application_id **字符串数组**(不是对象数组)。

    ⚠️ /hire/v1/applications 的 items 是 id 字符串数组(非 application 对象),
    要拿详情必须逐条调 hire_get_application(id)。
    例:
        for aid in hire_list_applications(talent_id="xxx"):
            app = hire_get_application(aid)  # 拿详情
    """
    params = {}
    if job_id:
        params["job_id"] = job_id
    if talent_id:
        params["talent_id"] = talent_id
    return _page_all("/open-apis/hire/v1/applications", params)


def hire_transfer_stage(app_id, stage_id):
    """流转阶段。stage_id 从 hire_get_job_processes 取（不是 stage.type）。"""
    d = api("POST", f"/open-apis/hire/v1/applications/{app_id}/transfer_stage", "bot",
            data={"stage_id": stage_id})
    return _hire_check(d, "hire_transfer_stage")


def hire_terminate(app_id, termination_type):
    """终止/淘汰。调后 active_status 变 2，对账自动过滤。"""
    d = api("POST", f"/open-apis/hire/v1/applications/{app_id}/terminate", "bot",
            data={"termination_type": termination_type})
    return _hire_check(d, "hire_terminate")


# ---- Job（岗位）----
def hire_get_job(job_id):
    """查岗位详情。返回 job dict（title/code/department/create_user_id/active_status）。"""
    d = api("GET", f"/open-apis/hire/v1/jobs/{job_id}", "bot")
    data = _hire_check(d, "hire_get_job")
    return data.get("job") or data


def hire_list_jobs(keyword="", include_all=False, my_user_id=None):
    """全量拉岗位，返回 [(code, id, title)]。
    include_all=False 时过滤 active_status==1（开放中）；可选过滤 my_user_id（只看我的）。
    keyword 非空时按 title 包含模糊匹配。"""
    items = _page_all("/open-apis/hire/v1/jobs")
    out = []
    for it in items:
        if not include_all and it.get("active_status") != 1:
            continue
        if my_user_id and it.get("create_user_id") != my_user_id:
            continue
        if keyword and keyword not in it.get("title", ""):
            continue
        out.append((it.get("code"), it.get("id"), it.get("title", "")))
    return out


def hire_get_job_processes():
    """查招聘流程（含所有流程的 stage_list）。返回 job_process items 数组。"""
    return _page_all("/open-apis/hire/v1/job_processes")


# ---- Interview / 面评 ----
def hire_list_interviews(application_id):
    """列面试（含面评结论）。返回 items 数组。"""
    return _page_all("/open-apis/hire/v1/interviews",
                     params={"application_id": application_id})


def hire_get_interview_record(record_id):
    """查面评全文记录（v2 接口，路径含 v2）。返回 record dict。"""
    d = api("GET", f"/open-apis/hire/v2/interview_records/{record_id}", "bot")
    data = _hire_check(d, "hire_get_interview_record")
    return data.get("interview_record") or data


def hire_get_feedback_pdf(application_id, record_id):
    """查面评 PDF 附件。返回 data dict（含 url，30 分钟有效）。"""
    d = api("GET", "/open-apis/hire/v1/interview_records/attachments", "bot",
            params={"application_id": application_id, "interview_record_id": record_id})
    return _hire_check(d, "hire_get_feedback_pdf")


# ---- Attachment（附件上传，唯一保留 requests 直连）----
def upload_attachment_with_name(file_path, file_name=None):
    """上传附件（带正确文件名），返回 attachment_id。
    ⚠️ lark-cli --file 传不了 file_name 表单字段，必须 requests multipart 直传。
    ⚠️ 飞书招聘无删除附件 API，传错的 unknown-file 删不掉，第一次就要传对文件名。"""
    file_name = file_name or os.path.basename(file_path)
    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    ftype = "pdf" if ext == "pdf" else "doc" if ext in ("doc", "docx") else "other"
    mime = {"pdf": "application/pdf"}.get(ext, "application/octet-stream")
    token = get_hire_token()
    with open(file_path, "rb") as f:
        files = {"content": (file_name, f, mime)}
        data = {"file_name": file_name, "file_type": ftype}
        r = requests.post(f"{FEISHU_BASE}/hire/v1/attachments",
                          headers={"Authorization": f"Bearer {token}"},
                          files=files, data=data, timeout=120)
    rj = r.json()
    if rj.get("code") != 0:
        raise Exception(f"附件上传失败: {rj.get('msg', rj)}")
    return rj["data"]["id"]


# ---- Document AI（简历解析）----
def parse_resume(pdf_path):
    """Document AI 解析简历，返回 resume dict。
    大文件（>10MB）自动抽前几页（简历正文）解析，避免超限报 param is invalid。"""
    MAX_DIRECT = 10 * 1024 * 1024
    ext = os.path.splitext(pdf_path)[1].lower().lstrip(".")
    parse_path = pdf_path
    if os.path.getsize(pdf_path) > MAX_DIRECT and ext == "pdf":
        try:
            import fitz  # PyMuPDF
            tmp = pdf_path + ".__resume_only__.pdf"
            doc = fitz.open(pdf_path)
            n = min(4, doc.page_count)
            new = fitz.open()
            new.insert_pdf(doc, from_page=0, to_page=n - 1)
            new.save(tmp)
            new.close(); doc.close()
            parse_path = tmp
        except Exception as e:
            print(f"    (大文件抽页失败 {e}，整文件解析)")
    mime = {"pdf": "application/pdf"}.get(ext, "application/octet-stream")
    token = get_hire_token()
    try:
        with open(parse_path, "rb") as f:
            files = {"file": (os.path.basename(pdf_path), f, mime)}
            r = requests.post(f"{FEISHU_BASE}/document_ai/v1/resume/parse",
                              headers={"Authorization": f"Bearer {token}"}, files=files, timeout=120)
        rj = r.json()
        if rj.get("code") != 0:
            raise Exception(f"解析失败: {rj.get('msg', rj)}")
        return rj["data"]["resumes"][0]
    finally:
        if parse_path != pdf_path and os.path.exists(parse_path):
            os.remove(parse_path)


if __name__ == "__main__":
    # 自检：拉跟踪表打印前 3 条
    recs = list_track_records()
    print(f"跟踪表 {len(recs)} 条，前 3 条：")
    for r in recs[:3]:
        print({k: r.get(k) for k in ("候选人", "状态", "当前轮次", "talent_id", "_rid")})
