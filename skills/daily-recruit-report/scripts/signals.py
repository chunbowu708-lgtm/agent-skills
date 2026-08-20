# -*- coding: utf-8 -*-
"""
signals.py — 信号与决策落盘工具

把 LLM 判读结果（signals）和用户决策（decisions）原子写入 notes/_signals.json。
Agent 判读后只产出「姓名 + type + evidence + time + source + ats_landed」，
talent_id 由本脚本从 _daily_review.json 的 structured.ats 按 name 自动匹配——Agent 不用手查 talent_id。

用法：
    python signals.py --set notes/_signals_tmp.json      # 写入 signals（自动补 talent_id + 校验枚举 + 原子写）
    python signals.py --decide notes/_decisions_tmp.json # 写入 decisions
    python signals.py --read                             # 读当前 signals + decisions
    python signals.py --clear                            # 清空 signals + decisions（重跑判读时用）

--set 输入文件格式（JSON 数组，每条不含 talent_id，脚本自动补）：
    [
      {"name": "陈龙", "type": "invite", "source": "长青工作室美术岗招聘沟通群",
       "evidence": "「2026-08-05 11:20 张书瑞」@吴春波 这个聊聊哈",
       "time": "2026-08-05 11:20", "ats_landed": false}
    ]

--decide 输入文件格式（JSON 数组）：
    [
      {"name": "陈龙", "decision": "今天约", "notes": "等业务确认后约"}
    ]

依赖：无第三方包。--set/--decide 需要 _daily_review.json 已存在（talent_id 匹配源）。
"""
import json, os, argparse, datetime

SIGNALS_FILE = "<PROJECT_ROOT>/notes/_signals.json"
DAILY_FILE = "<PROJECT_ROOT>/notes/_daily_review.json"
TZ = datetime.timezone(datetime.timedelta(hours=8))

VALID_TYPES = {"invite", "reject", "hold", "discuss"}
VALID_DECISIONS = {"今天约", "催面评", "再等等", "终止"}


def _now():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _today():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


def _load_signals():
    if os.path.exists(SIGNALS_FILE):
        with open(SIGNALS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"date": _today(), "generated_at": _now(), "signals": [], "decisions": []}


def _load_name_to_tid():
    """从 _daily_review.json 建 name -> talent_id 映射（同名多人返回列表，标记冲突）。"""
    if not os.path.exists(DAILY_FILE):
        return {}
    with open(DAILY_FILE, encoding="utf-8") as f:
        d = json.load(f)
    mapping = {}
    for p in d.get("structured", {}).get("ats", []):
        name = p.get("name", "")
        tid = p.get("talent_id", "")
        if name and tid:
            mapping.setdefault(name, set()).add(tid)
    return mapping


def _save(state):
    os.makedirs(os.path.dirname(SIGNALS_FILE), exist_ok=True)
    tmp = SIGNALS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SIGNALS_FILE)


def cmd_set(args):
    """写入 signals：读输入 JSON 数组，自动补 talent_id + 校验，覆盖 signals 部分（保留 decisions）。"""
    if not os.path.exists(args.path):
        print(f"❌ 输入文件不存在: {args.path}")
        return 1
    with open(args.path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        print("❌ 输入必须是 JSON 数组")
        return 1

    name_to_tid = _load_name_to_tid()
    state = _load_signals()
    out = []
    warn = []
    for s in raw:
        name = s.get("name", "")
        typ = s.get("type", "")
        # 校验 type 枚举
        if typ not in VALID_TYPES:
            print(f"❌ {name or '(无名)'}: type={typ!r} 非法，应为 {sorted(VALID_TYPES)}")
            return 1
        # 校验必填字段
        for field in ("name", "type", "source", "evidence", "time"):
            if not s.get(field):
                print(f"❌ {name or '(无名)'}: 缺字段 {field}")
                return 1
        # 校验 ats_landed 是 bool
        if not isinstance(s.get("ats_landed"), bool):
            print(f"❌ {name}: ats_landed 必须是 true/false，不是 {s.get('ats_landed')!r}")
            return 1
        # talent_id：输入显式传了就用（同名多档案/ATS 无记录时人工指定）；否则自动匹配
        tids = name_to_tid.get(name, set())
        tid = s.get("talent_id") or ""
        if tid:
            pass
        elif len(tids) == 1:
            tid = next(iter(tids))
        elif len(tids) > 1:
            warn.append(f"{name} 在 ATS 有 {len(tids)} 个 talent（同名），需人工确认，已留空")
        out.append({
            "talent_id": tid,
            "name": name,
            "type": typ,
            "source": s.get("source", ""),
            "evidence": s.get("evidence", ""),
            "time": s.get("time", ""),
            "ats_landed": s.get("ats_landed", False),
            "note": s.get("note", ""),
        })
    state["signals"] = out
    state["date"] = _today()
    state["generated_at"] = _now()
    _save(state)
    print(f"✅ 写入 {len(out)} 条 signals")
    for w in warn:
        print(f"  ⚠️ {w}")
    return 0


def cmd_decide(args):
    """写入 decisions：读输入 JSON 数组，校验枚举，追加（不去重）。"""
    if not os.path.exists(args.path2):
        print(f"❌ 输入文件不存在: {args.path2}")
        return 1
    with open(args.path2, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        print("❌ 输入必须是 JSON 数组")
        return 1

    name_to_tid = _load_name_to_tid()
    state = _load_signals()
    out = []
    for d in raw:
        name = d.get("name", "")
        decision = d.get("decision", "")
        if decision not in VALID_DECISIONS:
            print(f"❌ {name or '(无名)'}: decision={decision!r} 非法，应为 {sorted(VALID_DECISIONS)}")
            return 1
        tids = name_to_tid.get(name, set())
        tid = next(iter(tids)) if len(tids) == 1 else d.get("talent_id", "")
        out.append({
            "talent_id": tid,
            "name": name,
            "decision": decision,
            "decided_at": _now(),
            "notes": d.get("notes", ""),
        })
    state["decisions"] = out
    state["date"] = _today()
    state["generated_at"] = _now()
    _save(state)
    print(f"✅ 写入 {len(out)} 条 decisions")
    return 0


def cmd_read():
    state = _load_signals()
    print(f"=== _signals.json（date={state.get('date')}）===")
    print(f"\n--- signals（{len(state.get('signals', []))} 条）---")
    for s in state.get("signals", []):
        tid = s.get("talent_id") or "(无talent_id)"
        print(f"  [{s.get('type')}] {s.get('name')} (tid={tid}) ats_landed={s.get('ats_landed')}")
        print(f"    source={s.get('source')} | time={s.get('time')}")
    print(f"\n--- decisions（{len(state.get('decisions', []))} 条）---")
    for d in state.get("decisions", []):
        print(f"  [{d.get('decision')}] {d.get('name')} | {d.get('notes', '')}")
    return 0


def cmd_clear():
    state = {"date": _today(), "generated_at": _now(), "signals": [], "decisions": []}
    _save(state)
    print("✅ 已清空 signals + decisions")
    return 0


def main():
    p = argparse.ArgumentParser(description="信号与决策落盘工具")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--set", metavar="FILE", dest="path", help="写 signals（自动补 talent_id）")
    g.add_argument("--decide", metavar="FILE", dest="path2", help="写 decisions")
    g.add_argument("--read", action="store_true", help="读当前 signals + decisions")
    g.add_argument("--clear", action="store_true", help="清空 signals + decisions")
    args = p.parse_args()

    if args.read:
        return cmd_read()
    if args.clear:
        return cmd_clear()
    if args.path:
        return cmd_set(args)
    if args.path2:
        return cmd_decide(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
