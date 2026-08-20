# -*- coding: utf-8 -*-
"""
nurture_state.py — 候选人保温状态管理器

跟踪每个候选人的触达历史，支持话术升级、去重、终止提示。
状态文件：notes/_nurture_state.json（脚本自动创建，不存在则初始化空状态）。

用法：
    python nurture_state.py --read                          # 查看所有活跃候选人保温状态
    python nurture_state.py --touch TID --name 姓名 --action 催面评 [--channel 飞书IM]
    python nurture_state.py --response TID --status replied|no_reply
    python nurture_state.py --stale                         # 列出 >3天未碰的
    python nurture_state.py --reset TID                     # 重置（候选人重新进管道/终止后）

依赖：无第三方包，纯 json + argparse。
"""

import os
import json, os, argparse, datetime

# ============================================================
# 配置
# ============================================================
STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "miniwanob", "notes", "_nurture_state.json",
)
# 兜底：上面拼的路径在 skill 安装在 .agents 下时指向 <PROJECT_ROOT>/notes
# 如果不存在则尝试项目根
ALT_STATE_FILE = os.path.join(os.environ.get("PROJECT_ROOT", os.getcwd()), "notes", "_nurture_state.json")

STALE_THRESHOLD_DAYS = 3  # 超过这个天数未碰 → stale
TZ = datetime.timezone(datetime.timedelta(hours=8))


def _state_path():
    """返回实际使用的状态文件路径（优先 ALT，确保 <PROJECT_ROOT> 下）。"""
    return ALT_STATE_FILE


def _today():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


def _days_between(date_str):
    """date_str (YYYY-MM-DD) 到今天的天数差；解析失败返回 0。"""
    try:
        d = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=TZ)
        return (datetime.datetime.now(TZ) - d).days
    except (ValueError, TypeError):
        return 0


def _load():
    """加载状态文件，不存在则返回空结构。"""
    path = _state_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": _today(), "candidates": {}}


def _save(state):
    """原子保存：写临时文件再 rename，防止写一半被读到。"""
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state["last_updated"] = _today()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _find_candidate(state, tid):
    """按 talent_id 查找候选人状态，不存在返回 None。"""
    return state["candidates"].get(tid)


def _get_or_create(state, tid, name):
    """获取或创建候选人状态条目。"""
    c = state["candidates"].get(tid)
    if c is None:
        c = {
            "name": name or tid,
            "last_touch_date": None,
            "last_touch_action": None,
            "last_touch_channel": None,
            "touch_count": 0,
            "escalation_level": 1,
            "last_response": None,
            "status": "active",
        }
        state["candidates"][tid] = c
    elif name and c.get("name") != name:
        c["name"] = name  # 更新姓名（可能改名）
    return c


# ============================================================
# 命令实现
# ============================================================
def cmd_read(state):
    """打印所有活跃候选人保温状态（跳过已终止的）。"""
    cands = state["candidates"]
    if not cands:
        print("（保温状态为空，还没有触达记录）")
        return
    # 按最后触达日期排序（最近在前）
    items = sorted(cands.items(), key=lambda kv: kv[1].get("last_touch_date") or "", reverse=True)
    print(f"=== 保温状态（{len(items)} 人）===")
    print(f"最后更新：{state.get('last_updated', '?')}\n")
    for tid, c in items:
        if c["status"] == "terminated":
            continue
        esc = c.get("escalation_level", 1)
        esc_label = {1: "轻量", 2: "正常", 3: "强催"}.get(esc, str(esc))
        resp = c.get("last_response") or "未记录"
        days_ago = _days_between(c.get("last_touch_date") or "")
        days_label = f"{days_ago}天前" if c.get("last_touch_date") else "未碰过"
        status_flag = ""
        if c["status"] == "escalated":
            status_flag = " ⚠️建议终止"
        print(f"  {c['name']} (L{esc}:{esc_label}) | 最后碰: {days_label} | "
              f"碰{c['touch_count']}次 | 回复: {resp}{status_flag}")
        if c.get("last_touch_action"):
            print(f"    └ 上次动作: {c['last_touch_action']} ({c.get('last_touch_channel','')})")


def cmd_touch(state, tid, name, action, channel):
    """记录一次触达，自动处理升级逻辑。"""
    c = _get_or_create(state, tid, name)
    today = _today()

    # 升级逻辑：如果上次没回复 且 已经碰过 ≥1 次（这次是第2次+），升级
    prev_count = c["touch_count"]
    if c.get("last_response") == "no_reply" and prev_count >= 1 and today != c.get("last_touch_date"):
        new_level = min(c.get("escalation_level", 1) + 1, 3)
        if new_level > c.get("escalation_level", 1):
            c["escalation_level"] = new_level
            print(f"  ⬆️ 升级: {name} escalation_level → L{new_level}")
        # L3 且仍 no_reply → escalated
        if new_level >= 3 and c.get("last_response") == "no_reply":
            c["status"] = "escalated"
            print(f"  ⚠️ {name} 已达 L3 且未回复，标记 escalated（建议终止）")

    # 同一天重复碰 → 提示但不阻断（有时需要多渠道）
    if c.get("last_touch_date") == today:
        print(f"  ⚠️ {name} 今天已碰过（{c.get('last_touch_action')}），本次为追加触达")

    c["last_touch_date"] = today
    c["last_touch_action"] = action
    c["last_touch_channel"] = channel or "飞书IM"
    c["touch_count"] = prev_count + 1
    # 触达后重置回复状态为 null（等新的回复）
    c["last_response"] = None
    # 如果之前是 escalated 但用户又碰了，恢复 active
    if c["status"] == "escalated":
        c["status"] = "active"
        c["escalation_level"] = min(c["escalation_level"], 2)  # 降回 L2

    _save(state)
    print(f"  ✅ 记录触达: {name} | 第{c['touch_count']}次 | {action} | {c['last_touch_channel']}")


def cmd_response(state, tid, status):
    """记录候选人回复状态。"""
    c = _get_or_create(state, tid, name=None)
    if not c.get("name"):
        c["name"] = tid
    c["last_response"] = status
    if status == "replied":
        # 回复了 → 重置升级等级
        c["escalation_level"] = 1
        if c["status"] == "escalated":
            c["status"] = "active"
        print(f"  ✅ {c['name']} 已回复，重置 escalation → L1")
    else:
        print(f"  📝 {c['name']} 未回复（记录中）")
    _save(state)


def cmd_stale(state):
    """列出超过阈值天数未碰的活跃候选人。"""
    cands = state["candidates"]
    stale = []
    for tid, c in cands.items():
        if c["status"] in ("terminated",):
            continue
        last = c.get("last_touch_date")
        if not last:
            # 从未碰过，看是否该碰（touch_count==0 但在 candidates 里说明曾经被纳入过）
            if c.get("touch_count", 0) == 0:
                continue
            stale.append((c, tid, None))
        else:
            days = _days_between(last)
            if days >= STALE_THRESHOLD_DAYS:
                stale.append((c, tid, days))
    if not stale:
        print(f"（没有超过 {STALE_THRESHOLD_DAYS} 天未碰的候选人）")
        return
    stale.sort(key=lambda x: x[2] if x[2] is not None else 999, reverse=True)
    print(f"=== 超过 {STALE_THRESHOLD_DAYS} 天未碰（{len(stale)} 人）===\n")
    for c, tid, days in stale:
        label = f"{days}天" if days is not None else "从未碰(待纳入)"
        print(f"  {c['name']} | 最后碰: {label}前 | L{c.get('escalation_level',1)} | {c['status']}")


def cmd_reset(state, tid):
    """重置候选人状态（终止后或重新进管道）。"""
    c = state["candidates"].get(tid)
    if c is None:
        print(f"  （{tid} 不在状态文件中，无需重置）")
        return
    c["touch_count"] = 0
    c["escalation_level"] = 1
    c["last_response"] = None
    c["last_touch_date"] = None
    c["last_touch_action"] = None
    c["last_touch_channel"] = None
    c["status"] = "terminated"  # 终止：不再出现在 read/stale 里
    _save(state)
    print(f"  ✅ 已重置 {c['name']}（标记 terminated，不再出现在保温清单）")


# ============================================================
# 入口
# ============================================================
def main():
    p = argparse.ArgumentParser(description="候选人保温状态管理器")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--read", action="store_true", help="查看所有活跃候选人保温状态")
    g.add_argument("--touch", metavar="TID", help="记录一次触达（需配合 --name --action）")
    g.add_argument("--response", metavar="TID", help="记录候选人回复状态（需配合 --status）")
    g.add_argument("--stale", action="store_true", help="列出超过阈值天数未碰的")
    g.add_argument("--reset", metavar="TID", help="重置候选人状态")
    p.add_argument("--name", help="候选人姓名（--touch 时必填）")
    p.add_argument("--action", help="触达动作描述（如 催面评/保温-正常/推进通知）")
    p.add_argument("--channel", default="飞书IM", help="触达渠道（默认 飞书IM）")
    p.add_argument("--status", choices=["replied", "no_reply"], help="回复状态")
    args = p.parse_args()

    state = _load()

    if args.read:
        cmd_read(state)
    elif args.touch:
        if not args.name or not args.action:
            p.error("--touch 需要 --name 和 --action")
        cmd_touch(state, args.touch, args.name, args.action, args.channel)
    elif args.response:
        if not args.status:
            p.error("--response 需要 --status replied|no_reply")
        cmd_response(state, args.response, args.status)
    elif args.stale:
        cmd_stale(state)
    elif args.reset:
        cmd_reset(state, args.reset)


if __name__ == "__main__":
    main()
