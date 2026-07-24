# -*- coding: utf-8 -*-
"""一次性回填脚本：给跟踪表里 talent_id 为空的行回填 talent_id。

治存量同名错配——track_after_hire.py 已修复（新录入会写 talent_id），本脚本治历史49行。

用法：
  python backfill_talent_id.py            # dry-run：只打印匹配结果，不写
  python backfill_talent_id.py --write    # 真正回填

逻辑：
  1. 拉 ATS 全量 applications（含已终止/已入职，不活跃的也要回填）
  2. 建 name -> talent_id 映射
  3. 读跟踪表全量，找 talent_id 空的行
  4. 按姓名匹配，dry-run 打印 / --write 回填

⚠️ 同名风险：若 ATS 里同名有多人，跳过并告警（不盲填）。
"""
import sys, os, json

# 复用项目脚本（不重复造轮子）
sys.path.insert(0, "F:/miniwanob/notes")
from _lark_shared import (  # noqa: E402
    list_track_records, upsert_track_record,
)

# import _daily_review 的拉取函数（它在 notes/ 下）
sys.path.insert(0, "F:/miniwanob/notes")
import _daily_review as dr  # noqa: E402


def build_name_to_tid_map():
    """拉 ATS 全量 applications，建 name -> talent_id 映射（同名多人标记冲突）"""
    print("⏳ 拉 ATS 全量 applications...")
    apps = dr.fetch_applications_all()
    print(f"  共 {len(apps)} 条投递")

    # 批量取姓名
    tids = list({a.get("talent_id") for a in apps if a.get("talent_id")})
    name_cache = dr._batch_get_names(tids)
    print(f"  talent姓名缓存 {len(name_cache)} 人")

    # 建 name -> [tid, tid...] 映射（同名多人 = 列表>1）
    name_to_tids = {}
    for a in apps:
        tid = a.get("talent_id", "")
        name = name_cache.get(tid, "")
        if name and tid:
            name_to_tids.setdefault(name, []).append(tid)

    # 去重（同一个人多次投递 = 同一个 tid 出现多次）
    for name in name_to_tids:
        name_to_tids[name] = list(set(name_to_tids[name]))
    return name_to_tids


def main():
    do_write = "--write" in sys.argv

    name_to_tids = build_name_to_tid_map()

    print("\n⏳ 读跟踪表全量...")
    track = list_track_records()
    print(f"  共 {len(track)} 行")

    # 找 talent_id 空的行
    need_fill = [r for r in track if not r.get("talent_id")]
    already = len(track) - len(need_fill)
    print(f"  talent_id 已填 {already}，待回填 {len(need_fill)}")

    matched = []
    skipped_conflict = []
    skipped_no_match = []

    for r in need_fill:
        name = r.get("候选人", "")
        rid = r.get("_rid", "")
        tids = name_to_tids.get(name, [])
        if len(tids) == 0:
            skipped_no_match.append(name)
        elif len(tids) > 1:
            skipped_conflict.append((name, tids))
        else:
            matched.append((name, rid, tids[0]))

    print(f"\n=== 匹配结果 ===")
    print(f"  ✅ 唯一匹配可回填: {len(matched)}")
    print(f"  ⚠️ 同名冲突跳过: {len(skipped_conflict)}")
    for name, tids in skipped_conflict:
        print(f"     {name}: {tids}")
    print(f"  ❌ ATS 无此姓名: {len(skipped_no_match)}")
    if skipped_no_match:
        print(f"     {skipped_no_match}")

    if not matched:
        print("\n无可回填项，退出。")
        return

    print(f"\n=== 待回填清单（{len(matched)} 行）===")
    for name, rid, tid in matched:
        print(f"  {name:8s} rid={rid} -> talent_id={tid}")

    if not do_write:
        print(f"\n[DRY-RUN] 以上 {len(matched)} 行未写入。加 --write 执行回填。")
        return

    print(f"\n⏳ 回填中...")
    ok_cnt = 0
    fail_list = []
    for name, rid, tid in matched:
        if upsert_track_record({"talent_id": tid}, record_id=rid):
            ok_cnt += 1
            print(f"  ✅ {name}")
        else:
            fail_list.append((name, rid))
            print(f"  ❌ {name}")

    print(f"\n✏️ 回填 talent_id {ok_cnt}/{len(matched)} 条")
    if fail_list:
        print(f"⚠️ 失败 {len(fail_list)} 条: {fail_list}")


if __name__ == "__main__":
    main()
