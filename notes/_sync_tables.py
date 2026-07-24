#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
表格自动同步脚本（每晚 18:30 由 Windows 计划任务触发）。

职责边界（第一性原理约束）：
  - ATS 是唯一事实源，本脚本只做「ATS → 表格」单向投影，禁止反向。
  - 跟踪表：复用 _daily_review.py 的 write_back（客观层 ATS 覆盖 + 主观层只填空槽）。
  - 岗位表：从 ATS 聚合各岗位进展人数，更新「目前招聘进展/已发Offer/已入职」。
  - 不做意图判读（那是 LLM 的活，留给早晨 Agent 会话产出 _signals.json）。
  - 不动候选表/JD库表（语义不明确，二期再说）。

用法：
  python notes/_sync_tables.py --dry-run   # 只算不写，打印将改的行
  python notes/_sync_tables.py             # 实跑：同步跟踪表 + 岗位表

依赖：复用 _daily_review.py（fetch_applications_all/parse_ats/write_back）
      复用 _lark_shared.py（cli/api/upsert_track_record/list_track_records）
不重造轮子。
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# 确保能 import 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _lark_shared as L  # noqa: E402
import _daily_review as DR  # noqa: E402

BASE_TOKEN = L.BASE_TOKEN
JOB_TABLE = "tbl0f9ynYsdQhYDo"  # 招聘岗位表
JOB_MAP_PATH = "notes/_job_table_map.json"
JOBS_CACHE_PATH = "notes/jobs_map.json"  # ATS job_id → {code,title,dept}
SYNC_RESULT_PATH = "notes/_sync_result.json"

STAGE_SCREEN = 1
STAGE_ASSESS = 2
STAGE_INTERVIEW = 4
STAGE_OFFER = 5
STAGE_ONBOARD = 6
STAGE_EMPLOYED = 7


# ============================================================
# 岗位表读写
# ============================================================
def _load_job_field_map():
    """加载岗位表字段映射缓存。"""
    with open(JOB_MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


def list_job_records(job_fmap, dry_run=False):
    """拉岗位表全部记录。返回 list[dict]，每条含岗位名称/进展/offer/入职 + _rid。

    复用 _lark_shared.cli + base +record-list 子命令（与 list_track_records 同模式）。
    """
    wanted = ["岗位名称", "岗位ID", "目前招聘进展", "已发Offer人数", "已入职人数", "招聘数量"]
    field_args = []
    for nm in wanted:
        fid = job_fmap["fields"].get(nm)
        if fid:
            field_args += ["--field-id", nm]

    recs = []
    for lim in (200, 100):
        raw = L.cli(["base", "+record-list", "--base-token", BASE_TOKEN,
                     "--table-id", JOB_TABLE] + field_args +
                    ["--format", "json", "--as", "user", "--limit", str(lim)])
        d = L.extract_json(raw)
        if not d:
            continue
        data = d.get("data", {})
        rows = data.get("data", [])
        rids = data.get("record_id_list", [])
        for i, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            rec = {"_rid": rids[i] if i < len(rids) else None}
            for j, nm in enumerate(wanted):
                rec[nm] = L.cell(row[j] if j < len(row) else None)
            recs.append(rec)
        if recs:
            break
    return recs


def _normalize_job_name(name):
    """归一化岗位名用于匹配（去空格/全半角括号/方向后缀差异）。

    实测 ATS 'Unity 客户端开发工程师（AI-Native 方向）' vs 岗位表
    'Unity客户端开发工程师（AI-Native方向）' 有空格差异，归一化后才能匹配。
    """
    if not name:
        return ""
    s = name.strip()
    # 全角括号转半角再统一去空格
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"\s+", "", s)  # 去所有空白
    return s.lower()


def update_job_record(record_id, fields, dry_run=False):
    """更新岗位表一行（走 base +record-upsert，--as user）。"""
    j = json.dumps(fields, ensure_ascii=False)
    if dry_run:
        print(f"  [DRY] UPDATE岗位表 {record_id}: {j[:140]}")
        return True
    args = ["base", "+record-upsert", "--base-token", BASE_TOKEN,
            "--table-id", JOB_TABLE, "--json", j, "--as", "user",
            "--record-id", record_id]
    raw = L.cli(args)
    ok = L.extract_json(raw)
    return bool(ok and ok.get("ok"))


# ============================================================
# 岗位进展聚合
# ============================================================
def _stage_to_progress(max_stage_type, has_interview, offer_count, onboard_count):
    """把该岗位「在途候选人」的最高阶段映射成岗位表「目前招聘进展」枚举。

    枚举选项：简历筛选/初面/复面/Offer/入职/结束

    第一性原理：进展反映的是「当前在途的人走到哪了」，已入职的不算在途。
      onboard>0（待入职）  → 入职（已发 offer 等入职，走到最后一步）
      offer>0（offer沟通） → Offer
      有面试阶段           → 复面（面试进行中统一算复面）
      有初筛/评估          → 初面（连面试都没排上=管道断裂预警）
      全空（无人投递）     → 简历筛选
    """
    if onboard_count > 0:
        return "入职"
    if offer_count > 0:
        return "Offer"
    if max_stage_type == STAGE_INTERVIEW:
        return "复面"
    if max_stage_type in (STAGE_SCREEN, STAGE_ASSESS):
        return "初面"  # 只有初筛没面试 = 还没人进入面试，标"初面"提示管道断裂
    if has_interview:
        return "初面"
    return "简历筛选"


def aggregate_jobs(ats_people, jobs_cache):
    """从 ats_people 按岗位聚合，返回 {normalized_job_name: 聚合结果}。

    ats_people: _daily_review.parse_ats 的输出（含 job_title, job_id, stage_type 等）
    jobs_cache: jobs_map.json 的 {job_id: {code,title,dept}}
    """
    by_job = defaultdict(lambda: {
        "title_raw": "",
        "count": 0,
        "max_stage_type": 0,        # 在途（非入职）候选人的最高阶段
        "has_interview": False,
        "offer_count": 0,           # 在 offer 沟通阶段
        "onboard_count": 0,         # 待入职阶段
        "employed_count": 0,        # 已入职（历史累计，不参与进展判断）
        "interviewing_count": 0,    # 当前在面试阶段
        "screening_count": 0,       # 当前在初筛/评估阶段
    })
    for p in ats_people:
        # job_title 优先用 parse_ats 解析的，降级用 jobs_cache
        title = p.get("job_title") or ""
        jid = p.get("job_id", "")
        if not title and jid and jid in jobs_cache:
            title = jobs_cache[jid].get("title", "")
        if not title:
            continue
        norm = _normalize_job_name(title)
        bucket = by_job[norm]
        bucket["title_raw"] = title
        bucket["count"] += 1
        st = p.get("stage_type") or 0
        if st == STAGE_INTERVIEW:
            bucket["max_stage_type"] = max(bucket["max_stage_type"], st)
            bucket["has_interview"] = True
            bucket["interviewing_count"] += 1
        elif st in (STAGE_SCREEN, STAGE_ASSESS):
            bucket["max_stage_type"] = max(bucket["max_stage_type"], st)
            bucket["screening_count"] += 1
        elif st == STAGE_OFFER:
            bucket["max_stage_type"] = max(bucket["max_stage_type"], st)
            bucket["offer_count"] += 1
        elif st == STAGE_ONBOARD:
            bucket["max_stage_type"] = max(bucket["max_stage_type"], st)
            bucket["onboard_count"] += 1
        elif st == STAGE_EMPLOYED:
            bucket["employed_count"] += 1

    # 算进展（基于在途候选人，已入职的不参与进展判断）
    result = {}
    for norm, b in by_job.items():
        b["progress"] = _stage_to_progress(
            b["max_stage_type"], b["has_interview"],
            b["offer_count"], b["onboard_count"])
        result[norm] = b
    return result


def sync_job_table(job_agg, job_recs, job_fmap, dry_run=False):
    """同步岗位表。只更新「目前招聘进展/已发Offer人数/已入职人数」。

    匹配策略：归一化岗位名精确匹配。匹配不上的岗位（ATS 有但表无）报告不建行
    （岗位表行由人工创建，本脚本不自动建岗位行——岗位创建是业务决策）。
    """
    fid = job_fmap["fields"]
    matched = 0
    unmatched_ats = []
    actions = []
    for rec in job_recs:
        table_name = rec.get("岗位名称", "")
        norm = _normalize_job_name(table_name)
        agg = job_agg.get(norm)
        if not agg:
            continue  # ATS 无此岗位投递，跳过（进展留人工值）
        matched += 1

        fields = {}
        changes = []
        # 目前招聘进展（单选：传选项名字符串）
        cur_progress = rec.get("目前招聘进展")
        new_progress = agg["progress"]
        if new_progress and cur_progress != new_progress:
            fields["目前招聘进展"] = new_progress
            changes.append(f"进展:{cur_progress}→{new_progress}")
        # 已发Offer人数
        new_offer = agg["offer_count"] + agg["onboard_count"] + agg["employed_count"]
        cur_offer = rec.get("已发Offer人数")
        if cur_offer in (None, "") or int(cur_offer or 0) != new_offer:
            fields["已发Offer人数"] = new_offer
            changes.append(f"offer:{cur_offer}→{new_offer}")
        # 已入职人数
        new_employed = agg["employed_count"]
        cur_employed = rec.get("已入职人数")
        if cur_employed in (None, "") or int(cur_employed or 0) != new_employed:
            fields["已入职人数"] = new_employed
            changes.append(f"入职:{cur_employed}→{new_employed}")

        if fields and rec.get("_rid"):
            actions.append({"rid": rec["_rid"], "name": table_name,
                            "fields": fields, "changes": changes})

    # 记录 ATS 有但岗位表无的（人工未建行）
    table_norms = {_normalize_job_name(r.get("岗位名称", "")) for r in job_recs}
    for norm, agg in job_agg.items():
        if norm not in table_norms:
            unmatched_ats.append({"job": agg["title_raw"], "people": agg["count"]})

    # 执行更新
    ok_cnt = 0
    failures = []
    for act in actions:
        if update_job_record(act["rid"], act["fields"], dry_run=dry_run):
            ok_cnt += 1
            print(f"  ✏️ {act['name']}: {', '.join(act['changes'])}")
        else:
            failures.append(act)
        time.sleep(0.05)

    return {"matched": matched, "updated": ok_cnt, "total_actions": len(actions),
            "unmatched_ats": unmatched_ats, "failures": failures}


# ============================================================
# 主流程
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="表格自动同步（ATS → 跟踪表 + 岗位表）")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--skip-track", action="store_true", help="跳过跟踪表同步（只同步岗位表）")
    ap.add_argument("--skip-job", action="store_true", help="跳过岗位表同步")
    args = ap.parse_args()

    print(f"🔄 [{'写入' if not args.dry_run else 'DRY-RUN'}模式] 表格自动同步开始")

    # ---- 第1步：跑对账（复用 _daily_review，拿带 job_id 的 ats_people）----
    print("\n⏳ 第1步：拉取 ATS 数据（复用 _daily_review 引擎）...")
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut_apps = ex.submit(DR.fetch_applications_all)
        fut_track = ex.submit(L.list_track_records)
        app_details = fut_apps.result()
        track_recs = fut_track.result()
    interviews_by_app = DR.fetch_interviews_batch(app_details)

    my_job_ids = DR._load_my_jobs()
    scope_apps = app_details
    if my_job_ids is not None:
        scope_apps = [a for a in app_details if a.get("job_id") in my_job_ids]
    active_apps = [a for a in scope_apps
                   if not a.get("termination_type")
                   and a.get("stage", {}).get("type") != STAGE_EMPLOYED]
    ats_people = DR.parse_ats(active_apps, interviews_by_app)
    print(f"  活跃投递 {len(ats_people)} 人")

    # 同时算全量（含已入职/已 offer）用于岗位表聚合——岗位表要看 offer/入职总数
    all_scoped = [a for a in scope_apps if not a.get("termination_type")]
    ats_people_all = DR.parse_ats(all_scoped, interviews_by_app)

    result = {"date": DR.TODAY_STR, "generated_at": DR.NOW.isoformat(),
              "dry_run": args.dry_run, "track": {}, "job": {}}

    # ---- 第2步：同步跟踪表（复用 write_back）----
    if not args.skip_track:
        print("\n⏳ 第2步：同步面试进度跟踪表（复用 write_back）...")
        # 拉日程供 compute_battle_list（write_back 的主观字段需要 today_interviews）
        events = DR.fetch_calendar()
        battle = DR.compute_battle_list(ats_people, track_recs, events)
        report = DR.build_report(ats_people, battle, [], track_recs)
        if args.dry_run:
            # write_back 不支持 dry_run，跳过实际写入只报告规模
            result["track"] = {"dry_run": True,
                               "active_candidates": len(ats_people),
                               "note": "跟踪表 dry-run 未写入；实跑用 _daily_review --write 的逻辑"}
            print(f"  [DRY] 跟踪表将对 {len(ats_people)} 名活跃候选人做客观字段同步（跳过写入）")
        else:
            DR.write_back(report, track_recs, ats_people)
            result["track"] = {"synced": True, "active_candidates": len(ats_people)}

    # ---- 第3步：同步岗位表 ----
    if not args.skip_job:
        print("\n⏳ 第3步：同步招聘岗位表...")
        job_fmap = _load_job_field_map()
        jobs_cache = {}
        if os.path.exists(JOBS_CACHE_PATH):
            with open(JOBS_CACHE_PATH, encoding="utf-8") as f:
                jobs_cache = json.load(f)
        job_recs = list_job_records(job_fmap, dry_run=args.dry_run)
        job_agg = aggregate_jobs(ats_people_all, jobs_cache)
        print(f"  岗位表 {len(job_recs)} 行 | ATS 聚合 {len(job_agg)} 岗位")
        job_result = sync_job_table(job_agg, job_recs, job_fmap, dry_run=args.dry_run)
        result["job"] = job_result
        if job_result["unmatched_ats"]:
            print(f"  ⚠️ ATS 有但岗位表无（人工未建行）{len(job_result['unmatched_ats'])} 个：")
            for u in job_result["unmatched_ats"]:
                print(f"     • {u['job']} ({u['people']}人)")

    # ---- 落盘报告 ----
    with open(SYNC_RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n{'✅' if not args.dry_run else '🔍 [DRY]'} 同步完成，报告: {SYNC_RESULT_PATH}")
    if not args.dry_run:
        track_s = result.get("track", {})
        job_s = result.get("job", {})
        print(f"   跟踪表: {track_s.get('active_candidates', '-')} 名候选人客观字段已同步")
        print(f"   岗位表: 匹配 {job_s.get('matched', 0)} 岗位, 更新 {job_s.get('updated', 0)} 行")


if __name__ == "__main__":
    main()
