# -*- coding: utf-8 -*-
"""
track_after_hire.py — 录入后建/补跟踪表行（治"录入与跟踪表割裂"）

吃 _hire.py 的产出 notes/_hire_result.json，自动批量建跟踪表行。
所有易错点固化在本脚本，AI 不再临时手敲 record-upsert。

固化点：
  - datetime 字段直接传毫秒时间戳整数（已验证，不用 {"type":...} 对象）
  - record_id 从 record-list 的 record_id_list 取（矩阵模式，和 data.data 平行）
  - 单选字段选项内置常量（不每次现查 field-list）
  - 部门从 job_title 反查（AI 不手填）
  - 幂等：talent_id 精确查重（主键优先，防同名错配），降级姓名查重 + 告警
  - 只写主键层 + 主观层；客观层（状态/轮次/面试时间/进入阶段日期）由 _daily_review.py --write 落地

用法：
  python track_after_hire.py                                # 只建主键+主观层（客观状态空，等 _daily_review 落地）
  python track_after_hire.py --time "罗艺=2026-07-02 10:00,许展豪=2026-07-02 14:00"  # 面试时间写进"下一步动作"
  python track_after_hire.py --result notes/_hire_result.json   # 指定结果文件
  python track_after_hire.py --dry-run                      # 只打印不写
"""
import json, sys, os, re, datetime, argparse
sys.stdout.reconfigure(encoding="utf-8")

# 复用项目共享库（仓库根 notes/ 下的 _lark_shared.py，收口 cli/extract_json）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "notes"))
from _lark_shared import cli, extract_json  # noqa: E402

BASE = os.environ.get("TRACKING_BASE_TOKEN", "")
TBL = os.environ.get("TRACKING_TABLE_ID", "")  # 跟踪表
RESULT = "notes/_hire_result.json"

# ===== 单选字段选项常量（从 field-list 实测锁定，不每次现查）=====
# ⚠️ 状态/当前轮次属客观层，由 _daily_review.py --write 写，本脚本不写——故不留 OPT_STATUS/OPT_ROUND。
OPT_FUNC = ["研发", "美术", "策划", "设计", "产品", "运营"]
OPT_PRIORITY = ["紧急", "高", "中", "低"]

# 岗位名 -> (跟踪表"岗位"选项值, "部门"选项值, "职能类别")
# 跟踪表的"岗位"单选项和飞书招聘岗位名不完全一致（如"游戏内容运营"在表里是"游戏内容运营(UGC生态)"），这里做映射
JOB_MAP = {
    "游戏内容运营": ("游戏内容运营(UGC生态)", "迷你世界项目团队", "运营"),
    "海外游戏数据产品经理": ("海外游戏数据PM", "Magnolia项目团队", "产品"),
    "游戏发行运营实习生": ("游戏发行运营实习生", "全球发行业务", "运营"),
    "3D场景设计师": ("3D场景设计师", "长青工作室", "美术"),
    "游戏场景原画设计师": ("游戏场景原画设计师", "长青工作室", "美术"),
    "UGC策划（AI UGC游戏工具方向）": ("UGC策划", "Magnolia项目团队", "策划"),
    "产品经理（AI UGC游戏平台方向）": ("AI产品经理(UGC)", "Magnolia项目团队", "产品"),
    "交互设计师（移动端游戏平台方向）": ("交互设计师(AI UGC)", "Magnolia项目团队", "设计"),
    "Unity 客户端开发工程师（AI-Native 方向）": ("Unity客户端(AI-Native)", "山海弹珠项目", "研发"),
    "游戏广告商业化策划": ("游戏广告商业化策划", "迷你世界项目团队", "策划"),
    # 按需扩展：键用 _hire_result.json 里的 job_title 值
    # 注意：岗位列的值必须在跟踪表"岗位"字段已有选项内，否则留空
}


def list_records():
    """拉全表记录，返回双索引：(by_name, by_tid)。
      by_name: {候选人姓名: {"rid": record_id, "talent_id": ...}}
      by_tid:  {talent_id:    {"rid": record_id, "name":    ...}}
    用 record-list 矩阵模式：data.data 是行矩阵（每行 [候选人, talent_id]），
    data.record_id_list 平行存 id（--field-id 候选人 --field-id talent_id 两个投影）。
    ⚠️ --limit 500 会让 lark-cli 返回空 stdout（实测），用 200 并分页兜底。
    talent_id 列可能为空（老数据未回填）——by_tid 不收空键，by_name 仍记下空 talent_id 供降级。"""
    by_name, by_tid = {}, {}
    for lim in (200, 100):  # 200 失败降级 100
        raw = cli(["base", "+record-list", "--base-token", BASE, "--table-id", TBL,
                   "--field-id", "候选人", "--field-id", "talent_id",
                   "--format", "json", "--as", "user", "--limit", str(lim)])
        d = extract_json(raw)
        if not d:
            continue
        data = d.get("data", {})
        rows = data.get("data", [])
        ids = data.get("record_id_list", [])
        for i, row in enumerate(rows):
            if not isinstance(row, list) or not row:
                continue
            name = row[0] if row[0] else None
            tid = row[1] if len(row) > 1 and row[1] else ""
            rid = ids[i] if i < len(ids) else None
            if not (name and rid):
                continue
            by_name[name] = {"rid": rid, "talent_id": tid}
            if tid:  # 空 tid 不进 by_tid（降级路径需要靠 by_name 区分"空"和"非空不匹配"）
                by_tid[tid] = {"rid": rid, "name": name}
        if by_name:  # 拿到数据就够了（查重用，不必翻全表）
            break
    return by_name, by_tid


def safe_option(value, options, field_name):
    """单选值必须命中已有选项，否则返回 None（硬填会被静默忽略）"""
    if value in options:
        return value
    print(f"  ⚠️ {field_name} 选项 '{value}' 不在 {options}，留空")
    return None


# ===== JOB_MAP 缺失时的路径推断（2026-07-16 固化）=====
# 归档目录两种层级：
#   单层: data/在招岗位候选人管理/{团队}/{岗位}/...
#   两层: data/在招岗位候选人管理/{团队}/{端}/{岗位}/...  （端=技术端/美术端/策划端）
# 部门=团队名（匹配跟踪表已有选项），职能=端映射。
ARCHIVE_ROOT = os.path.join("data", "在招岗位候选人管理")

# 归档目录里的"端"段 → 跟踪表职能类别（OPT_FUNC）
END_TO_FUNC = {"技术端": "研发", "美术端": "美术", "策划端": "策划"}

# 部门关键词 → 跟踪表部门选项（用于 JOB_MAP 缺失时从路径推断）
# 路径里出现这些关键词就归对应部门。键是路径子串，值是跟踪表选项。
DEPT_KEYWORDS = {
    "山海弹珠项目": "山海弹珠项目",
    "长青工作室": "长青工作室",
    "Magnolia项目团队": "Magnolia项目团队",
    "迷你世界项目团队": "迷你世界项目团队",
    "全球发行业务": "全球发行业务",
    "技术支持团队": "技术支持团队",
    "产品团队": "产品团队",
    "策划部门": "策划部门",
}


def infer_dept_func(path, job_title, dept_opts):
    """JOB_MAP 缺失时，从简历归档路径推断 (部门, 职能)。
    path: _hire_result.json 里每条的 path（完整归档路径）。
    dept_opts: 跟踪表"部门"字段已有选项（safe_option 校验用）。
    返回 (dept_or_None, func_or_None, job_pos_or_None)。
    job_pos（跟踪表"岗位"选项）路径推断不出来（是自定义短名），保持 None。"""
    dept, func = None, None
    p = path.replace("\\", "/")
    # 只在归档目录内才推断，避免 Downloads 等无关路径误判
    if ARCHIVE_ROOT.replace("\\", "/") not in p:
        return None, None, None
    # 部门：匹配路径里的团队关键词
    for kw, opt in DEPT_KEYWORDS.items():
        if kw in p:
            dept = opt if opt in dept_opts else None
            break
    # 职能：匹配路径里的"端"段
    for end_seg, func_val in END_TO_FUNC.items():
        if end_seg in p:
            func = func_val
            break
    return dept, func, None



def upsert_row(person, time_map, dry_run):
    """建/更新一行。person = _hire_result 的一个元素"""
    name = person.get("name") or person.get("name_parsed")
    job_title = person.get("job_title", "")
    if job_title in JOB_MAP:
        job_pos, dept, func = JOB_MAP[job_title]
    else:
        # 未配置映射：先尝试从归档路径推断部门/职能，推断不出来才留空（2026-07-16）
        path = person.get("path", "")
        dept_opts = _field_opts("部门")
        dept, func, job_pos = infer_dept_func(path, job_title, dept_opts)
        if dept or func:
            print(f"  💡 '{job_title}' 未在 JOB_MAP，从归档路径推断：部门={dept} 职能={func}")
        else:
            print(f"  ⚠️ '{job_title}' 未在 JOB_MAP 且路径无法推断，岗位/部门/职能留空。")
            print(f"     可编辑 JOB_MAP 补充：\"{job_title}\": (\"<岗位选项>\", \"<部门>\", \"<职能>\"),")
        job_pos = job_pos or ""

    # 基础字段 —— 主键层 + 主观层 only
    # ⚠️ 客观层（状态/当前轮次/面试时间/进入阶段日期/最近进展日期）写权归 _daily_review.py --write，
    #    本脚本不写——否则次日对账会用 ATS 真值覆盖回来，造成"今天已排期、明天又变待安排"的闪烁。
    #    面试时间（--time 传入）改写到主观层"下一步动作"，让用户能看到但不算客观状态。
    if name in time_map:
        next_action = f"面试时间 {time_map[name]}，待 HR 后台建面试（客观状态由 _daily_review --write 落地）"
    else:
        next_action = "录入完成，待约面（客观状态由 _daily_review --write 落地）"
    fields = {
        "候选人": name,
        "talent_id": person.get("talent_id", ""),  # 主键：治同名错配，对账精确匹配靠它
        "岗位": safe_option(job_pos, _field_opts("岗位"), "岗位"),
        "部门": safe_option(dept, _field_opts("部门"), "部门"),
        "职能类别": safe_option(func, OPT_FUNC, "职能类别"),
        "优先级": "高",
        "下一步动作": next_action,
    }
    # 主观层单选字段过选项校验
    fields["优先级"] = safe_option(fields["优先级"], OPT_PRIORITY, "优先级")
    # 去掉 None
    fields = {k: v for k, v in fields.items() if v is not None}

    # ⚠️ 不在此写任何 datetime 字段（进入阶段日期/最近进展日期/面试时间）——见上面客观层铁律

    # === 查重判定（talent_id 主键优先，防同名错配；违反原版只按 name 查重）===
    # by_tid / by_name 由 list_records() 返回的双索引。
    incoming_tid = (person.get("talent_id") or "").strip()

    if incoming_tid and incoming_tid in _by_tid:
        # ① 主键精确命中 → UPDATE（不含 talent_id，主键不该被 update）
        rid = _by_tid[incoming_tid]["rid"]
        fields_for_update = {k: v for k, v in fields.items() if k != "talent_id"}
        j = json.dumps(fields_for_update, ensure_ascii=False)
        action, action_verb = rid, "更新(tid)"
    elif name in _by_name:
        # ② 同名命中（incoming_tid 空 或 不在 by_tid）→ 看表里这行的 tid 决定
        existing_tid = _by_name[name].get("talent_id", "")
        if not existing_tid:
            # ②a 表里 tid 为空 → UPDATE 并补 tid（老数据回填场景，本次就是要补主键）
            rid = _by_name[name]["rid"]
            j = json.dumps(fields, ensure_ascii=False)  # 含 talent_id，补填
            action, action_verb = rid, "更新(name+补tid)"
            print(f"  [💡 补主键] {name} 表 talent_id 空，本次回填 {incoming_tid or '(空)'}")
        elif existing_tid == incoming_tid:
            # ②b 表里 tid = 传入（理论 ① 已捕获，防御性兜底）
            rid = _by_name[name]["rid"]
            fields_for_update = {k: v for k, v in fields.items() if k != "talent_id"}
            j = json.dumps(fields_for_update, ensure_ascii=False)
            action, action_verb = rid, "更新(tid)"
        else:
            # ②c 表里 tid 非空且 ≠ 传入 → 同名歧义，跳过防错配
            print(f"  [⚠️ 同名歧义] {name} 表 tid={existing_tid} 传入={incoming_tid}，"
                  f"跳过防错配，需人工确认（可能同名不同人）")
            return False
    else:
        # ③ 双索引都不命中 → CREATE，写全字段含 talent_id（即使是空串，占位等 backfill）
        j = json.dumps(fields, ensure_ascii=False)
        action, action_verb = None, "新建"

    if dry_run:
        print(f"  [DRY] {name} {action_verb}: {j[:120]}")
        return True

    if action:  # UPDATE（带 record-id）
        # ⚠️ lark-cli 没有 +record-update 子命令（会报 unknown subcommand）！
        # 更新现有记录也用 +record-upsert + --record-id（upsert 带指定 id 即覆盖更新）
        args = ["base", "+record-upsert", "--base-token", BASE, "--table-id", TBL,
                "--record-id", action, "--json", j, "--as", "user"]
    else:  # CREATE（不带 record-id）
        args = ["base", "+record-upsert", "--base-token", BASE, "--table-id", TBL,
                "--json", j, "--as", "user"]

    raw = cli(args)  # 走 _lark_shared.cli（已设 MSYS_NO_PATHCONV + utf-8）
    ok = extract_json(raw)
    success = bool(ok and ok.get("ok"))
    err_hint = "" if success else " 失败:" + raw[:150]
    print(f"  {'✅' if success else '❌'} {name} {action_verb}{err_hint}")
    return success


# 占位：岗位/部门选项需现场拿一次（和 _existing 一起，避免硬编码过期）
_field_opts_cache = {}


def _field_opts(field):
    """惰性取单选选项。岗位/部门选项可能增减，不硬编码。"""
    if field in _field_opts_cache:
        return _field_opts_cache[field]
    raw = cli(["base", "+field-list", "--base-token", BASE, "--table-id", TBL, "--as", "user"])
    d = extract_json(raw)
    opts_map = {}
    fields = d.get("data", {}).get("fields", []) if d else []
    for f in fields:
        nm = f.get("name")
        opts = [o.get("name") for o in (f.get("options") or []) if isinstance(o, dict)]
        if nm and opts:
            opts_map[nm] = opts
    _field_opts_cache.update(opts_map)
    return _field_opts_cache.get(field, [])


_by_name, _by_tid = {}, {}


def main():
    global _by_name, _by_tid
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", default=RESULT)
    ap.add_argument("--time", default="", help='格式: "姓名=YYYY-MM-DD HH:MM,姓名=..."')
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.result):
        print(f"❌ 找不到 {args.result}，先跑 _hire.py --list"); sys.exit(1)
    people = json.load(open(args.result, encoding="utf-8"))
    people = [p for p in people if p.get("ok")]
    print(f"=== 待建跟踪表 {len(people)} 人 ===")

    # 解析 --time
    time_map = {}
    if args.time:
        for kv in args.time.split(","):
            if "=" in kv:
                n, t = kv.split("=", 1)
                time_map[n.strip()] = t.strip()

    # 先拉现有记录（幂等查重）
    print("【查重】拉现有跟踪表记录（双索引：by_tid 精确 + by_name 降级）...")
    _by_name, _by_tid = list_records()
    print(f"  现有 {len(_by_name)} 条记录（其中 {len(_by_tid)} 条有 talent_id）")

    # 逐人建/更新
    ok_cnt = 0
    for p in people:
        ok_cnt += 1 if upsert_row(p, time_map, args.dry_run) else 0

    print(f"\n=== 完成 {ok_cnt}/{len(people)} ===")
    if ok_cnt != len(people):
        sys.exit(1)

    # 客观层（状态/当前轮次/面试时间/进入阶段日期）写权归 _daily_review.py --write，
    # 本脚本不写——提示用户接着跑对账同步客观状态。
    print("提示：客观状态（状态/轮次/面试时间/进入阶段日期）由 _daily_review.py --write 落地，")
    print("      录入完成后建议接着跑：python notes/_daily_review.py --write")


if __name__ == "__main__":
    main()
