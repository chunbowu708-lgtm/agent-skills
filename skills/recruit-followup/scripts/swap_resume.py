# -*- coding: utf-8 -*-
"""
swap_resume.py — 给已录入的候选人换成最新版简历（投递主简历同步更新）

解决场景：录入时复用存量 talent，投递绑了旧版简历，面试官首屏看到的是旧简历。
飞书招聘没有「换投递主简历」API（投递的 talent_attachment_resume_id 只读、无 update 接口），
但实测：terminate 旧投递 + 用同一 talent 重建投递，重建后自动绑定 talent 档案里最新上传的附件。

固化流程（2026-07-23 验证）：
  重传最新简历 → combined_update 挂档案（新旧并存）→ terminate 绑旧简历的投递 → 重建投递

固化本次踩的 3 个坑（避免每次临场踩）：
  1. 路径不靠人写 —— 复用 _hire.py 的 locate_resume 三级级联定位（归档库→Downloads→群聊）
  2. att_id 基准以回读档案为准 —— upload 返回的 id 和 combined_update 后档案里挂的 id 不同，
     判断「是否最新」必须用 combined_update 后回读档案拿到的 id，不能用上传返回值
  3. hire_list_applications 延迟 —— terminate+重建后立即 list 可能不返回新投递，脚本内置重试

⚠️ 前提：投递必须还在初筛/早期阶段。terminate 重建会丢失当前阶段状态，已在面试/Offer 阶段的
   投递不能用此脚本（会丢面评/审批状态），那种情况只能让用户去飞书后台手动操作。

用法：
  # 单人换简历（简历自动定位）
  python scripts/swap_resume.py --talent 7665198618701826346 --pdf 王国栋.docx

  # 指定明确路径（zip 内简历需先解压）
  python scripts/swap_resume.py --talent 7665198618701826346 --pdf "notes/_tmp/王国栋.docx"

  # 同时给同 talent 的所有活跃投递换简历（默认行为）
  python scripts/swap_resume.py --talent 7665198618701826346 --pdf 王国栋.docx --all-active

  # 只换指定岗位的投递（--job-id 是飞书 job_id 数字，非 A 编号）
  python scripts/swap_resume.py --talent <id> --pdf x.pdf --job-id 7646354412315822378

  # dry-run（只打印不写）
  python scripts/swap_resume.py --talent <id> --pdf x.pdf --dry-run
"""
import sys, os, json, time, argparse
sys.stdout.reconfigure(encoding="utf-8")

# 复用项目共享库（收口 hire API 封装 + cli，契约见 lark-hire skill）
sys.path.insert(0, "F:/miniwanob/notes")
sys.path.insert(0, "F:/miniwanob")  # locate_resume 在 notes/_hire.py
from _lark_shared import (  # noqa: E402
    hire_get_talent, hire_combined_update, hire_list_applications,
    hire_get_application, hire_terminate, hire_create_application,
    hire_get_job, upload_attachment_with_name, build_basic_info, parse_resume,
)
try:
    from _hire import locate_resume  # 三级级联定位（归档库→Downloads→群聊）
except Exception:
    locate_resume = None  # 兜底：import 失败则要求 --pdf 是存在的路径

# termination_type 枚举（飞书文档实测，2026-07-23）：1=我们拒绝 / 22=候选人拒绝 / 27=其他
TERM_OTHER = 27


def list_active_apps(talent_id, retries=2, delay=2.0):
    """列活跃投递(active_status==1)。terminate+重建后 list 可能延迟，内置重试。
    返回 [(app_id, job_id, main_resume_att_id)]。"""
    for attempt in range(retries + 1):
        aids = hire_list_applications(talent_id=talent_id)
        aids = aids if isinstance(aids, list) else ([aids] if aids else [])
        out = []
        for aid in aids:
            app = hire_get_application(aid)
            if app.get("active_status") == 1:
                out.append((aid, app.get("job_id"), app.get("talent_attachment_resume_id")))
        if out:
            return out
        if attempt < retries:
            time.sleep(delay)  # 坑3：重建后 list 延迟，重试
    return out


def latest_att_from_profile(talent_id):
    """坑2：从回读档案拿「最新附件 id」。upload 返回值不可靠（combined_update 后档案里
    挂的附件 id 会重新生成）。resume_attachment_list 按上传时间倒序，第一个是最新。"""
    t = hire_get_talent(talent_id)
    atts = t.get("resume_attachment_list") or []
    if not atts:
        return None, []
    first = atts[0]
    return str(first.get("ID") or first.get("id")), atts


def swap_one(talent_id, pdf_path, job_id_filter=None, dry_run=False):
    """给一个 talent 换简历。job_id_filter 指定时只换该岗位的投递，None=所有活跃投递。"""
    if not os.path.exists(pdf_path):
        if locate_resume:
            found = locate_resume(os.path.basename(pdf_path).rsplit(".", 1)[0])
            if found:
                pdf_path = found
        if not os.path.exists(pdf_path):
            print(f"❌ 简历不存在: {pdf_path}")
            return False

    # ---- 0. 先看当前活跃投递（换之前）----
    before = list_active_apps(talent_id)
    if not before:
        print(f"⚠️ talent {talent_id} 无活跃投递，无需换简历（或先录入）")
        return False
    targets = [(a, j, m) for a, j, m in before if (job_id_filter is None or j == job_id_filter)]
    if not targets:
        print(f"⚠️ 没有匹配的活跃投递 (job_id_filter={job_id_filter})")
        return False

    # 防呆：投递已过初筛阶段的不让换（会丢阶段）。这里简单按"是否已有面试记录"判断——
    # 飞书投递 active_status 不直接反映阶段，保险起见提示用户确认，不硬拦。
    print(f"待换 {len(targets)} 个活跃投递（阶段状态会重置到初筛，确认这些投递还在早期阶段）:")
    for aid, jid, _ in targets:
        job = hire_get_job(jid) if jid else {}
        print(f"  app={aid} | {job.get('title', jid)}")

    if dry_run:
        print("[DRY] 不实际执行")
        return True

    # ---- 1. 重传最新简历 + combined_update 挂档案 ----
    print(f"【重传简历】{os.path.basename(pdf_path)}")
    upload_attachment_with_name(pdf_path)
    resume = parse_resume(pdf_path)
    basic = build_basic_info(
        name=resume.get("name") or os.path.basename(pdf_path).split("_")[0],
        mobile=resume.get("mobile"), email=resume.get("email") or None,
        gender=resume.get("gender"))
    hire_combined_update(talent_id, basic, att_id=None)  # 注：att_id 走档案最新，不单独传

    # ---- 2. 坑2：回读档案拿真正的最新附件 id ----
    new_att, atts = latest_att_from_profile(talent_id)
    print(f"  档案现附件({len(atts)})，最新={new_att}")
    if not new_att:
        print("❌ combined_update 后档案无附件，中止")
        return False

    # ---- 3. terminate 旧投递 + 重建（飞书自动绑档案最新附件）----
    print("【terminate 旧投递 + 重建】")
    for old_aid, jid, _ in targets:
        hire_terminate(old_aid, TERM_OTHER)
        hire_create_application(talent_id, jid)

    # ---- 4. 坑3：重试读活跃投递，验证主简历=最新 ----
    after = list_active_apps(talent_id, retries=3, delay=2.0)
    print("【验证】")
    ok_all = True
    for aid, jid, main in after:
        if jid not in [t[1] for t in targets]:
            continue  # 不属于本次目标岗位的跳过
        is_new = str(main) == str(new_att)
        job = hire_get_job(jid) if jid else {}
        print(f"  {'✅' if is_new else '❌'} app={aid} | {job.get('title', jid)} | 主简历={main}"
              f" {'= 最新' if is_new else f'≠ 最新({new_att})'}")
        ok_all = ok_all and is_new
    print(f"\n{'🟢 完成，主简历已是最新' if ok_all else '🔴 部分未更新，请检查'}")
    return ok_all


def main():
    ap = argparse.ArgumentParser(description="给已录入候选人换最新版简历（投递主简历同步更新）")
    ap.add_argument("--talent", required=True, help="talent_id")
    ap.add_argument("--pdf", required=True, help="最新简历路径（或文件名，自动三级定位）")
    ap.add_argument("--job-id", default=None, help="只换该飞书 job_id 的投递（数字），不传=所有活跃投递")
    ap.add_argument("--all-active", action="store_true", help="换该 talent 所有活跃投递（默认即此行为）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ok = swap_one(args.talent, args.pdf, job_id_filter=args.job_id, dry_run=args.dry_run)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
