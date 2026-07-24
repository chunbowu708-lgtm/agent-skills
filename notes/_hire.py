# -*- coding: utf-8 -*-
"""
一键录入候选人到飞书招聘（路径A全量版 v2）。
功能：单份/批量录入、进度提示、错误隔离、结果汇总、去重保护。

用法：
  # ⭐ 最快路径：按姓名录，简历自动定位、job_code 自动解析（不用写清单！）
  python notes/_hire.py --by-name 李毅,谭顺馨,吴欣圆 --job 游戏发行运营实习生
  python notes/_hire.py --by-name 李毅 --job A129248

  # ⭐ 群聊/私聊文件来源：--by-name 自动三级级联查找，本地没有时自动从飞书群聊下载
  #    归档库(data/) → Downloads → 飞书群聊(自动搜+下载)，不用管简历在哪
  python notes/_hire.py --by-name 白向庭 --job 海外游戏数据产品经理

  # 查岗位编号（不知道 job_code 时先查，关键词模糊匹配标题）
  python notes/_hire.py --jobs 发行
  python notes/_hire.py --jobs          # 不加关键词=列全部

  # 录单个
  python notes/_hire.py <简历.pdf> <岗位编号如A105045>

  # 批量录同一个岗位（传文件夹，自动扫描 pdf/docx）
  python notes/_hire.py <文件夹> <岗位编号> --batch

  # 批量录不同岗位（传清单文件，每行：简历路径|岗位编号|姓名(可选)）
  python notes/_hire.py <清单.txt> --list

示例：
  python notes/_hire.py --by-name 李毅,谭顺馨 --job 游戏发行   # ← 日常用这个
  python notes/_hire.py --from-chat 白向庭 --job 数据产品经理  # ← 群文件用这个
  python notes/_hire.py --jobs 发行     # → A129248 游戏发行运营实习生
"""
import json, re, sys, os, glob
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# hire API 封装 + 常量全部来自 _lark_shared（单一真相源，契约见 lark-hire skill）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or "notes")
from _lark_shared import (
    hire_get_job, hire_list_jobs, hire_get_talent, hire_get_application,
    hire_create_application, hire_combined_create, hire_combined_update,
    build_basic_info, upload_attachment_with_name, parse_resume,
    map_degree, map_gender, is_cn_mobile, guess_name_from_file, to_ms,
    WUBO_ID,
)

# 简历文件后缀
RESUME_EXT = ("*.pdf", "*.docx", "*.doc")

# 吴春波 open_id —— 岗位归属过滤用（只认我创建的岗，防误录到别人的岗/已关闭的岗）
MY_USER_ID = WUBO_ID  # 别名（语义：在 _hire.py 里它是"我的 user_id"用于岗位归属过滤）


def job_filter_ok(it):
    """岗位准入闸门：只认「我创建的 + 开放中」的岗位。
    active_status: 1=招聘中(开放) 2=暂停 3=已关闭。
    过滤掉暂停/已关闭/别人管理的岗，治本——防误录到废岗或非我岗。"""
    return (it.get("create_user_id") == MY_USER_ID
            and it.get("active_status") == 1)

# 缓存：job_code -> (job_id, title)，避免重复翻页
_JOB_CACHE = {}

def fmt_progress(idx, total, name, step, status=""):
    """进度提示：[2/5] 罗佳 - 上传附件..."""
    prefix = f"[{idx}/{total}] {name}"
    return f"{prefix} - {step}{status}"

# ========== API 层（薄 wrapper，调 _lark_shared 封装）==========
# 所有 hire 接口都走 _lark_shared（--as bot，契约见 lark-hire skill），_hire.py 不再裸调 requests。
# H 参数保留向后兼容（实际不再需要，封装内部管 token），调用方传 H=None 即可。

def _fetch_all_jobs_raw():
    """全量拉岗位（不过滤），返回 items 数组。内部用 _lark_shared 翻页。"""
    from _lark_shared import _page_all
    return _page_all("/open-apis/hire/v1/jobs")


def find_job_id(job_code, H=None):
    """按岗位编号查 (job_id, title)，带缓存。H 参数保留兼容（忽略）。"""
    if job_code in _JOB_CACHE:
        return _JOB_CACHE[job_code]
    items = _fetch_all_jobs_raw()
    for it in items:
        # 顺便缓存所有岗位，减少后续翻页
        _JOB_CACHE.setdefault(it.get("code"), (it.get("id"), it.get("title", "")))
        if it.get("code") == job_code:
            return _JOB_CACHE[job_code]
    return None, None


def search_jobs(keyword="", include_all=False, H=None):
    """全量拉岗位，按关键词模糊匹配（标题包含即命中）。返回 [(code,id,title)]。
    默认只返回「我创建的 + 开放中」的岗位（job_filter_ok 过滤），
    防误录到暂停/已关闭/别人的岗。include_all=True 时不过滤（排查用）。H 参数保留兼容（忽略）。"""
    items = _fetch_all_jobs_raw()
    out = []
    for it in items:
        if not include_all and not job_filter_ok(it):
            continue
        if not keyword or keyword in it.get("title", ""):
            out.append((it.get("code"), it.get("id"), it.get("title", "")))
    return out


def preload_jobs(job_codes, H=None):
    """批量开始前，主线程串行预查所有 job_code 填满 _JOB_CACHE。
    并行阶段 _JOB_CACHE 只读不写，消除竞态。返回 {code: (id, title)}，缺失的标 None。"""
    # 先一次性拉全量岗位填缓存（一次翻页代替 N 次单查）
    items = _fetch_all_jobs_raw()
    for it in items:
        _JOB_CACHE.setdefault(it.get("code"), (it.get("id"), it.get("title", "")))
    out = {}
    for code in set(job_codes):
        if code:
            jid_t = _JOB_CACHE.get(code)
            out[code] = jid_t if jid_t and jid_t[0] else (None, None)
            status = "✓" if jid_t and jid_t[0] else "✗ 找不到"
            print(f"  预查岗位 {code} -> {(jid_t[1] if jid_t else '') or status}")
    return out


def combined_create(resume, att_id, name_hint=None, mobile_override=None, H=None):
    """全量写入人才档案，返回 talent_id。
    mobile_override: 手动指定手机号（简历解析到的手机号）用，优先级高于解析结果。
    海外号不传（飞书 mobile_country_code 海外枚举未知，传了必失败），靠邮箱去重。
    H 参数保留兼容（忽略，封装内部管 token）。"""
    # 组装 basic_info（build_basic_info 自动处理手机三件套 + 海外号跳过）
    name = resume.get("name") or name_hint or "未知名"
    mobile = mobile_override or resume.get("mobile")
    birthday_ms = to_ms(resume.get("date_of_birth", ""))
    basic = build_basic_info(
        name=name,
        mobile=mobile,
        email=resume.get("email") or None,
        gender=resume.get("gender"),
        birthday_ms=int(birthday_ms) if birthday_ms else None,
    )

    # 组装 career_list
    # career_type 枚举（hire API 与 Document AI 一致）：1=实习经历 / 2=工作经历。
    # Document AI 返 c["type"]（1/2），直接透传；解析失败兜底 2(工作经历)——社招主流是全职工作。
    career_list = []
    for c in resume.get("careers", []):
        item = {"career_type": c.get("type") if c.get("type") in (1, 2) else 2}
        if c.get("company"): item["company"] = c["company"]
        if c.get("title"): item["title"] = c["title"]
        if c.get("job_description"): item["desc"] = c["job_description"]
        st = to_ms(c.get("start_date", ""))
        if st: item["start_time"] = str(st)  # 飞书 hire API 要 string，不能传 int
        et = to_ms(c.get("end_date", ""))
        if et: item["end_time"] = str(et)
        career_list.append(item)

    # 组装 education_list
    edu_list = []
    for e in resume.get("educations", []):
        item = {}
        if e.get("school"): item["school"] = e["school"]
        dg = map_degree(e.get("degree") or e.get("qualification"))
        if dg: item["degree"] = dg
        if e.get("major"): item["field_of_study"] = e["major"]
        st = to_ms(e.get("start_date", "")); et = to_ms(e.get("end_date", ""))
        if st: item["start_time"] = str(st)
        if et: item["end_time"] = str(et)
        edu_list.append(item)

    return hire_combined_create(basic, att_id, careers=career_list or None,
                                edus=edu_list or None,
                                self_eval=resume.get("self_evaluation", ""))

def check_stale_resume(talent_id, current_att_id, application_id=None, H=None):
    """检测 talent 是否有旧简历附件（存量 talent 复用场景）。
    飞书招聘 API 不支持删除/替换旧附件，只能提示用户去后台手动清理。
    两层检测：①talent 附件列表 ②投递挂的 attachment resume id（更可靠）。
    返回 list[dict]：旧附件信息，空列表=无旧附件。
    H 参数保留兼容（忽略）。"""
    stale = []
    try:
        # 层1：查 talent 的附件列表
        t = hire_get_talent(talent_id)
        atts = t.get("resume_attachment_list") or []
        if not atts and t.get("resume_attachment_id_list"):
            atts = [{"id": aid, "name": "?"} for aid in t["resume_attachment_id_list"]]
        stale = [{"id": a.get("id") or a.get("ID"),
                  "name": a.get("name") or a.get("Name", "?")} for a in atts
                 if str(a.get("id") or a.get("ID")) != str(current_att_id)]
    except Exception:
        pass
    # 层2：查投递挂的附件（更可靠——这个字段稳定返回）
    if not stale and application_id:
        try:
            app = hire_get_application(application_id)
            app_att = app.get("talent_attachment_resume_id")
            if app_att and str(app_att) != str(current_att_id):
                stale = [{"id": app_att, "name": "投递关联的旧附件（非本次上传）"}]
        except Exception:
            pass
    return stale

def create_application(talent_id, job_id, H=None):
    """建投递，返回 (status, msg, application_id)。status: 'ok'/'exists'/'fail'。
    H 参数保留兼容（忽略）。"""
    return hire_create_application(talent_id, job_id)

def _warn_stale(idx, total, name, talent_id, current_att_id, result, application_id=None, H=None):
    """检测存量 talent 是否有旧简历附件，有则告警 + 标记 result。
    飞书 API 不支持删旧附件，必须提示用户去后台手动清理。
    同时标记 result['reused']=True——has_stale_resume=True 说明 combined_create 命中了存量 talent 档案
    （即邮箱/手机去重命中老档），语义上属于"复用"而非"新建"。汇总计数靠这个信号区分新建/复用。"""
    stale = check_stale_resume(talent_id, current_att_id, application_id)
    if stale:
        result["has_stale_resume"] = True
        result["reused"] = True  # 复用现成 stale 检测作为 reused 信号，避免新增查重 API
        result["stale_attachments"] = [
            {"id": a.get("id"), "name": a.get("name", "?")} for a in stale]
        names = ", ".join(a.get("name", "?") for a in stale)
        print(fmt_progress(idx, total, name,
              f"⚠️ 检测到 {len(stale)} 份旧简历（{names}），含可能的过时/薪酬信息，"
              f"请到飞书招聘后台手动删除旧附件，保留本次上传的新版", " ⚠️"))


# ========== 核心录入流程 ==========

def hire_one(pdf_path, job_code, job_id, job_title, idx=1, total=1, name_hint=None, mobile_override=None, H=None):
    """录入单个候选人，返回结果 dict。
    job_id/job_title 由调用方预查传入（并行安全：hire_one 内不再触碰全局 _JOB_CACHE）。
    mobile_override 手动覆盖手机号。
    H 参数保留兼容（忽略，封装内部管 token）。"""
    name = name_hint or guess_name_from_file(pdf_path)
    result = {"name": name, "file": os.path.basename(pdf_path),
              "path": os.path.abspath(pdf_path),  # 完整路径供 track_after_hire.py 推断部门/职能
              "job_code": job_code, "ok": False}

    try:
        # [1] 解析
        print(fmt_progress(idx, total, name, "解析简历..."))
        resume = parse_resume(pdf_path)
        rname = resume.get("name", name)
        mobile = resume.get("mobile", "")
        print(fmt_progress(idx, total, name,
              f"姓名={rname} 手机={mobile or '?'} "
              f"经历{len(resume.get('careers',[]))}段 教育{len(resume.get('educations',[]))}段", " ✓"))

        # [2] 上传附件
        print(fmt_progress(idx, total, name, "上传附件..."))
        att_id = upload_attachment_with_name(pdf_path)
        print(fmt_progress(idx, total, name, f"att_id={att_id[:16]}...", " ✓"))

        # [3] combined_create 全量
        print(fmt_progress(idx, total, name, "写入人才档案(全量)..."))
        talent_id = combined_create(resume, att_id, name_hint, mobile_override)
        print(fmt_progress(idx, total, name, f"talent_id={talent_id}", " ✓"))

        # [4] 建投递
        print(fmt_progress(idx, total, name, f"建投递 {job_code}..."))
        status, msg, app_id = create_application(talent_id, job_id)
        if status == "fail":
            raise Exception(f"投递失败: {msg}")
        if status == "exists":
            # 已投递过：不是错，标记跳过（talent 已建，application 已存在）
            print(fmt_progress(idx, total, name, f"-> {job_title}（已投递，跳过）", " ⏭️\n"))
            result.update({"ok": True, "skipped": True, "talent_id": talent_id,
                           "name_parsed": rname, "job_title": job_title})
            _warn_stale(idx, total, name, talent_id, att_id, result, app_id)
            return result
        print(fmt_progress(idx, total, name, f"-> {job_title}", " ✓"))

        result.update({"ok": True, "talent_id": talent_id,
                       "name_parsed": rname, "job_title": job_title})
        _warn_stale(idx, total, name, talent_id, att_id, result, app_id)
        print(fmt_progress(idx, total, name, "完成", " 🎉\n"))
    except Exception as e:
        result["error"] = str(e)
        print(fmt_progress(idx, total, name, f"失败: {e}", " ❌\n"))
    return result

# ========== 批量入口 ==========

def collect_from_folder(folder):
    """从文件夹收集简历文件"""
    files = []
    for ext in RESUME_EXT:
        files.extend(glob.glob(os.path.join(folder, "**", ext), recursive=True))
    return sorted(files)

def parse_list_file(list_path):
    """解析清单文件，每行：简历路径|岗位编号|姓名(可选)|手机号(可选)"""
    tasks = []
    with open(list_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            path = parts[0].strip()
            code = parts[1].strip() if len(parts) > 1 else ""
            name = parts[2].strip() if len(parts) > 2 else None
            mobile = parts[3].strip() if len(parts) > 3 else None
            if path and code:
                tasks.append((path, code, name, mobile))
    return tasks


def find_resume_by_name(name, root="data/在招岗位候选人管理"):
    """按姓名在归档目录下定位简历（文件名以「姓名_」开头）。
    多个匹配时取最近修改的。返回相对路径或 None。"""
    if not os.path.isdir(root):
        return None
    matches = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.startswith(name + "_") and f.lower().endswith((".pdf", ".docx", ".doc")):
                matches.append(os.path.join(dirpath, f))
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0].replace("\\", "/")
    # 多个匹配：按修改时间取最新，并列出全部让用户确认
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    print(f"  ⚠️ '{name}' 匹配到 {len(matches)} 份简历，取最新：")
    for m in matches:
        print(f"     {'→ ' if m == matches[0] else '  '}{os.path.basename(m)}")
    return matches[0].replace("\\", "/")


def locate_resume_in_downloads(name):
    """在 Downloads 目录按姓名模糊匹配简历（文件名含姓名即可，不要求 姓名_ 前缀）。
    多个匹配取最新。返回绝对路径或 None。"""
    dl = "F:/Users/wuchunbo/Downloads"
    if not os.path.isdir(dl):
        return None
    matches = []
    for f in os.listdir(dl):
        if name in f and f.lower().endswith((".pdf", ".docx", ".doc")):
            matches.append(os.path.join(dl, f))
    if not matches:
        return None
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0].replace("\\", "/")


def download_from_chat(name):
    """从飞书群聊/私聊搜并下载简历文件，返回绝对路径或 None。"""
    import subprocess as _sp
    r = _sp.run([sys.executable, "notes/_download_chat_file.py", name],
                capture_output=True, text=True, timeout=200)
    dl_lines = [l for l in (r.stdout or "").splitlines() if l.startswith("DOWNLOADED ")]
    if not dl_lines:
        return None
    return dl_lines[-1][len("DOWNLOADED "):].strip()


def locate_resume(name, allow_chat=True):
    """三级级联查找简历（录入统一入口）：
    ① 本地归档库（data/在招岗位候选人管理，文件名以 姓名_ 开头）
    ② Downloads 目录（文件名含姓名即可）
    ③ 飞书群聊/私聊文件（自动搜+下载，allow_chat=False 时跳过）
    返回绝对/相对路径或 None。每级找不到自动降级，逐级报告。"""
    # ① 归档库
    p = find_resume_by_name(name)
    if p:
        print(f"  ✅ {name}: 归档库 → {os.path.basename(p)}")
        return p
    # ② Downloads
    p = locate_resume_in_downloads(name)
    if p:
        print(f"  ✅ {name}: Downloads → {os.path.basename(p)}")
        return p
    # ③ 飞书群聊
    if allow_chat:
        print(f"  ⏳ {name}: 本地没有，从飞书群聊搜并下载...", )
        p = download_from_chat(name)
        if p:
            print(f"  ✅ {name}: 群聊下载 → {os.path.basename(p)}")
            return p
    print(f"  ❌ {name}: 三级查找均未找到简历")
    return None


def resolve_job_code(job_keyword, H=None):
    """按岗位关键词查 job_code。传入 A开头编号或关键词模糊匹配标题。
    准入闸门：只认「我创建的 + 开放中」的岗位。即使是 A 编号也校验状态/归属，
    不通过就报错并给出我名下的开放同类岗建议，杜绝误录废岗/非我岗。
    H 参数保留兼容（忽略）。"""
    # ---- A 编号：校验状态 + 归属，不盲信 ----
    if job_keyword.upper().startswith("A") and job_keyword[1:].isdigit():
        jid, title = find_job_id(job_keyword)
        if not jid:
            print(f"❌ 编号 {job_keyword} 不存在")
            return None
        # 查详情拿 active_status + create_user_id
        it = hire_get_job(jid)
        if job_filter_ok(it):
            return job_keyword
        st = it.get("active_status")
        st_map = {1: "招聘中", 2: "⚠️暂停", 3: "❌已关闭"}
        owner = "我创建" if it.get("create_user_id") == MY_USER_ID else f"非我创建(create_user={it.get('create_user_id')})"
        print(f"❌ 编号 {job_keyword}（{title}）不可用：{st_map.get(st, st)} / {owner}")
        print("   不要录到暂停/已关闭/别人的岗。建议改用岗位关键词，脚本会自动从「我创建+开放」中匹配：")
        # 从标题关键词反查我的开放岗建议
        for code, _, t in search_jobs(""):
            if title and any(k in t for k in title.replace("（", "(").split("(")[0:1]):
                print(f"   → {code} | {t}")
        return None
    # ---- 关键词：默认只搜我的开放岗 ----
    hits = search_jobs(job_keyword)
    if not hits:
        # 给排查线索：是否有同名但已关闭/非我的岗
        all_hits = search_jobs(job_keyword, include_all=True)
        if all_hits:
            print(f"❌ 「{job_keyword}」没有「我创建+开放」的岗位。存在但被过滤的：")
            for code, jid, title in all_hits:
                it = hire_get_job(jid)
                st = it.get("active_status")
                st_map = {1: "招聘中", 2: "暂停", 3: "已关闭"}
                owner = "我" if it.get("create_user_id") == MY_USER_ID else "非我"
                print(f"   {code} | {title} | {st_map.get(st, st)} | {owner}")
            print("   如需录入请先在飞书招聘开放对应岗位，或确认岗位归属。")
        else:
            print(f"❌ 没找到含「{job_keyword}」的岗位")
        return None
    if len(hits) == 1:
        return hits[0][0]
    print(f"⚠️ 「{job_keyword}」在「我创建+开放」中匹配到多个，请指定 job_code：")
    for code, jid, title in hits:
        print(f"   {code} | {title}")
    return None

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    results = []  # hire_one 内部调 _lark_shared 封装管 token，无需全局 H

    if args and args[0] == "--jobs":
        # 查岗位编号：python notes/_hire.py --jobs 关键词
        kw = args[1] if len(args) > 1 else ""
        hits = search_jobs(kw)
        if not hits:
            print(f"没找到含「{kw}」的岗位" if kw else "没有岗位")
            sys.exit(1)
        print(f"共 {len(hits)} 个岗位" + (f" 含「{kw}」" if kw else "") + "：")
        for code, jid, title in hits:
            print(f"  {code} | {title}")
        sys.exit(0)

    elif "--from-chat" in args:
        # 群聊/私聊文件来源：自动搜群文件消息 → 下载 → 录入
        # 用法：python notes/_hire.py --from-chat 白向庭 --job 海外游戏数据产品经理
        #       python notes/_hire.py --from-chat 白向庭,李四 --job 数据产品经理  （多人逗号分隔）
        args = [a for a in args if a != "--from-chat"]
        names_str = args[0]
        job_kw = ""
        if "--job" in args:
            idx = args.index("--job")
            job_kw = args[idx + 1] if idx + 1 < len(args) else ""
            args = args[:idx] + args[idx+2:]
        if not job_kw:
            print("❌ --from-chat 必须配合 --job <岗位关键词或编号>"); sys.exit(1)
        job_code = resolve_job_code(job_kw)
        if not job_code:
            sys.exit(1)
        names = [n.strip() for n in names_str.split(",") if n.strip()]
        print(f"=== 从群聊下载 {len(names)} 份简历 -> {job_code} ===")
        # 下载阶段（调 _download_chat_file.py，逐人搜+下载）
        import subprocess as _sp
        tasks = []
        for name in names:
            print(f"【下载】{name} ...")
            r = _sp.run([sys.executable, "notes/_download_chat_file.py", name],
                        capture_output=True, text=True, timeout=200)
            # 最后一行 DOWNLOADED <path>
            dl_line = [l for l in (r.stdout or "").splitlines() if l.startswith("DOWNLOADED ")]
            if not dl_line:
                print(f"  ❌ {name} 下载失败: {(r.stderr or '')[:200]}"); continue
            path = dl_line[-1][len("DOWNLOADED "):].strip()
            print(f"  ✅ {name}: {os.path.basename(path)}")
            tasks.append((path, job_code, name, None))
        if not tasks:
            print("❌ 没有下载到任何简历"); sys.exit(1)
        print()
        # 预查岗位 + 并行录入（复用 --by-name 后半段逻辑）
        job_map = preload_jobs([job_code])
        jid, jtitle = job_map.get(job_code, (None, None))
        if not jid:
            print(f"❌ 找不到岗位 {job_code}"); sys.exit(1)
        with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as ex:
            futs = {}
            for i, (path, code, name, mobile) in enumerate(tasks, 1):
                futs[ex.submit(hire_one, path, code, jid, jtitle, i, len(tasks), name, mobile)] = i
            for fu in as_completed(futs):
                results.append(fu.result())

    elif "--by-name" in args:
        # 最快路径：按姓名直接录，简历自动定位、job_code 自动解析
        # 用法：python notes/_hire.py --by-name 李毅,谭顺馨,吴欣圆 --job 游戏发行运营实习生
        #       python notes/_hire.py --by-name 李毅,谭顺馨,吴欣圆 --job A129248
        args = [a for a in args if a != "--by-name"]
        names_str = args[0]
        job_kw = ""
        if "--job" in args:
            idx = args.index("--job")
            job_kw = args[idx + 1] if idx + 1 < len(args) else ""
            args = args[:idx] + args[idx+2:]
        if not job_kw:
            print("❌ --by-name 必须配合 --job <岗位关键词或编号>"); sys.exit(1)
        job_code = resolve_job_code(job_kw)
        if not job_code:
            sys.exit(1)
        names = [n.strip() for n in names_str.split(",") if n.strip()]
        print(f"=== 按姓名录入 {len(names)} 人 -> {job_code} ===")
        # 三级级联定位简历（归档库 → Downloads → 飞书群聊）
        print("【定位简历】")
        tasks = []
        for name in names:
            path = locate_resume(name)
            if not path:
                continue
            tasks.append((path, job_code, name, None))
        if not tasks:
            print("❌ 没有定位到任何简历"); sys.exit(1)
        print()
        # 预查岗位
        job_map = preload_jobs([job_code])
        jid, jtitle = job_map.get(job_code, (None, None))
        if not jid:
            print(f"❌ 找不到岗位 {job_code}"); sys.exit(1)
        # 并行录入（复用 --list 的并行逻辑）
        with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as ex:
            futs = {}
            for i, (path, code, name, mobile) in enumerate(tasks, 1):
                futs[ex.submit(hire_one, path, code, jid, jtitle, i, len(tasks), name, mobile)] = i
            for fu in as_completed(futs):
                results.append(fu.result())

    elif "--batch" in args:
        # 批量同岗位（并行）
        args = [a for a in args if a != "--batch"]
        folder, job_code = args[0], args[1]
        files = collect_from_folder(folder)
        if not files:
            print(f"文件夹 {folder} 没找到简历(pdf/docx)"); sys.exit(1)
        print(f"=== 批量录入：{len(files)}份简历 -> {job_code} ===")
        # 预查岗位（串行，避免并行写 _JOB_CACHE 竞态）
        print("【预查岗位】")
        job_map = preload_jobs([job_code])
        jid, jtitle = job_map.get(job_code, (None, None))
        if not jid:
            print(f"❌ 找不到岗位 {job_code}"); sys.exit(1)
        print()
        # 并行录入
        with ThreadPoolExecutor(max_workers=min(8, len(files))) as ex:
            futs = {ex.submit(hire_one, f, job_code, jid, jtitle, i, len(files)): i
                    for i, f in enumerate(files, 1)}
            for fu in as_completed(futs):
                results.append(fu.result())

    elif "--list" in args:
        # 批量不同岗位（并行）
        args = [a for a in args if a != "--list"]
        tasks = parse_list_file(args[0])
        if not tasks:
            print(f"清单 {args[0]} 没有有效行"); sys.exit(1)
        print(f"=== 批量录入：{len(tasks)}份简历（按清单）===")
        # 预查所有岗位
        print("【预查岗位】")
        job_map = preload_jobs([c for _, c, _, _ in tasks])
        missing = [c for c, (jid, _) in job_map.items() if not jid]
        if missing:
            print(f"❌ 这些岗位编号找不到: {missing}"); sys.exit(1)
        print()
        # 并行录入
        with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as ex:
            futs = {}
            for i, (path, code, name, mobile) in enumerate(tasks, 1):
                jid, jtitle = job_map[code]
                futs[ex.submit(hire_one, path, code, jid, jtitle, i, len(tasks), name, mobile)] = i
            for fu in as_completed(futs):
                results.append(fu.result())

    else:
        # 单个
        path, job_code = args[0], args[1]
        name = args[2] if len(args) > 2 else None
        jid, jtitle = find_job_id(job_code)
        if not jid:
            print(f"❌ 找不到岗位 {job_code}"); sys.exit(1)
        results.append(hire_one(path, job_code, jid, jtitle, 1, 1, name))

    # 汇总
    print("=" * 50)
    print("录入汇总")
    print("=" * 50)
    ok = sum(1 for r in results if r["ok"])
    new = sum(1 for r in results if r["ok"] and not r.get("skipped") and not r.get("reused"))
    reused = sum(1 for r in results if r.get("reused"))
    skipped = sum(1 for r in results if r.get("skipped"))
    print(f"成功 {ok}/{len(results)}（新建{new} 复用{reused} 跳过{skipped}）")
    for r in results:
        if r["ok"]:
            mark = "⏭️" if r.get("skipped") else ("↺" if r.get("reused") else "✅")
        else:
            mark = "❌"
        extra = f" -> {r.get('job_title', r['job_code'])}" if r["ok"] else f" {r.get('error','')}"
        print(f"  {mark} {r['name']}  ({r['file']}){extra}")

    # 旧简历告警（存量 talent 复用场景）
    stale_list = [r for r in results if r.get("has_stale_resume")]
    if stale_list:
        print(f"\n{'⚠️ 旧简历待清理 ' + '=' * 36}")
        print("以下候选人复用了存量 talent，有旧简历附件（可能含过时/薪酬信息）。")
        print("飞书 API 不支持删旧附件，请到飞书招聘后台手动删除：")
        for r in stale_list:
            atts = ", ".join(a["name"] for a in r.get("stale_attachments", []))
            print(f"  ⚠️ {r['name']}（talent_id={r.get('talent_id','?')}）: 旧附件 [{atts}]")
        print("=" * 50)

    # 保存结果
    with open("notes/_hire_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n明细已存 notes/_hire_result.json")

if __name__ == "__main__":
    main()
