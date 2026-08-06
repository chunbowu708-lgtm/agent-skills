# -*- coding: utf-8 -*-
"""
refresh_my_jobs.py — 刷新 notes/_my_jobs.json（我创建的岗位清单）

为什么需要：_my_jobs.json 是快照，新创建的岗位不刷新进去 → _daily_review.py 过滤投递时
把新岗的候选人筛掉 → 漏报。2026-07-10 黄锦亮今日面试漏报的根因。

用法：python notes/refresh_my_jobs.py

注意：飞书 jobs API 的 page_size > 20 会返回空（实测），翻页必须 ≤20。
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lark_shared import api, WUBO_ID

MY_OPEN_ID = WUBO_ID  # 别名（在 refresh_my_jobs 里它语义是"我的 open_id"用于岗位归属过滤）
PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_my_jobs.json")


def refresh():
    my_jobs = []
    page_token = None
    for page in range(30):
        params = {"page_size": 20}  # >20 返回空，飞书 API 限制
        if page_token:
            params["page_token"] = page_token
        d = api("GET", "/open-apis/hire/v1/jobs", "bot", params=params)
        if not d:
            break
        data = d.get("data", {})
        for job in data.get("items", []):  # item 就是 job 本身
            if job.get("create_user_id") == MY_OPEN_ID:
                my_jobs.append({
                    "id": job.get("id"),
                    "title": job.get("title", "").strip(),
                    "code": job.get("code", ""),
                })
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break

    # 读旧数据对比
    old = json.load(open(PATH, encoding="utf-8")) if os.path.exists(PATH) else {}
    old_ids = {j["id"] for j in old.get("mine", [])}
    new_ids = {j["id"] for j in my_jobs}
    added = new_ids - old_ids
    removed = old_ids - new_ids

    old["mine"] = my_jobs
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, indent=2)

    print(f"✅ _my_jobs.json 已刷新：{len(my_jobs)} 个岗位")
    if added:
        print(f"   新增 {len(added)} 个: {[j['title'] for j in my_jobs if j['id'] in added]}")
    if removed:
        print(f"   ⚠️ {len(removed)} 个旧岗位不在新列表（可能已关闭）")
    if not added and not removed:
        print("   无变化")


if __name__ == "__main__":
    refresh()
