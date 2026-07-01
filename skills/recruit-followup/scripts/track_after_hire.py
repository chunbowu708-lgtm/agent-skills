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
  - 幂等：按"候选人"查重，已存在则 update，不重复建行

用法：
  python track_after_hire.py                                # 只建基础行（状态=待约面，无面试时间）
  python track_after_hire.py --time "罗艺=2026-07-02 10:00,许展豪=2026-07-02 14:00"
  python track_after_hire.py --result notes/_hire_result.json   # 指定结果文件
  python track_after_hire.py --dry-run                      # 只打印不写
"""
import subprocess, json, sys, os, re, datetime, argparse
sys.stdout.reconfigure(encoding="utf-8")

# 凭证走环境变量（见 .env.example），不硬编码
CLI = os.environ.get("LARK_CLI_PATH", "lark-cli")
BASE = os.environ.get("TRACKING_BASE_TOKEN", "")
TBL = os.environ.get("TRACKING_TABLE_ID", "")  # 跟踪表
RESULT = os.environ.get("HIRE_RESULT_FILE", "notes/_hire_result.json")

# ===== 单选字段选项常量（从 field-list 实测锁定，不每次现查）=====
OPT_STATUS = ["待安排", "已排期", "已完成", "通过", "未通过", "终止", "等测题"]
OPT_ROUND = ["已发简历", "待约面", "一面(技术面)", "二面(业务负责人)", "三面(HR面)", "四面(部门负责人)"]
OPT_FUNC = ["研发", "美术", "策划", "设计", "产品", "运营"]
OPT_PRIORITY = ["紧急", "高", "中", "低"]

# 岗位名 -> (跟踪表"岗位"选项值, "部门"选项值, "职能类别")
# 跟踪表的"岗位"单选项和飞书招聘岗位名不完全一致（如"游戏内容运营"在表里是"游戏内容运营(UGC生态)"），这里做映射
JOB_MAP = {
    "游戏内容运营": ("游戏内容运营(UGC生态)", "迷你世界项目团队", "运营"),
    "海外游戏数据产品经理": ("海外游戏数据PM", "Magnolia项目团队", "产品"),
    # 按需扩展：键用 _hire_result.json 里的 job_title 值
}


def cli(args):
    """跑 lark-cli，返回 stdout+stderr 合并（错误信息常在 stderr，中文安全）"""
    r = subprocess.run([CLI] + args, capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def extract_json(raw):
    """从可能混了 tip/日志的输出里抠出第一个 JSON 对象"""
    m = re.search(r'\{[\s\S]*\}', raw)
    return json.loads(m.group(0)) if m else None


def to_ms(dt_str):
    """ISO 时间字符串 -> 毫秒时间戳（+08:00 时区）"""
    dt = datetime.datetime.fromisoformat(dt_str).replace(
        tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    return int(dt.timestamp() * 1000)


def list_records():
    """拉全表记录，返回 {候选人姓名: record_id} 字典。
    用 record-list 矩阵模式：data.data 是行矩阵，data.record_id_list 平行存 id。
    ⚠️ --limit 500 会让 lark-cli 返回空 stdout（实测），用 200 并分页兜底"""
    out = {}
    for lim in (200, 100):  # 200 失败降级 100
        raw = cli(["base", "+record-list", "--base-token", BASE, "--table-id", TBL,
                   "--field-id", "候选人", "--format", "json", "--as", "user", "--limit", str(lim)])
        d = extract_json(raw)
        if not d:
            continue
        data = d.get("data", {})
        rows = data.get("data", [])
        ids = data.get("record_id_list", [])
        for i, row in enumerate(rows):
            name = row[0] if isinstance(row, list) and row else str(row)
            rid = ids[i] if i < len(ids) else None
            if name and rid:
                out[name] = rid
        if out:  # 拿到数据就够了（查重用，不必翻全表）
            break
    return out


def safe_option(value, options, field_name):
    """单选值必须命中已有选项，否则返回 None（硬填会被静默忽略）"""
    if value in options:
        return value
    print(f"  ⚠️ {field_name} 选项 '{value}' 不在 {options}，留空")
    return None


def upsert_row(person, time_map, dry_run):
    """建/更新一行。person = _hire_result 的一个元素"""
    name = person.get("name") or person.get("name_parsed")
    job_title = person.get("job_title", "")
    if job_title in JOB_MAP:
        job_pos, dept, func = JOB_MAP[job_title]
    else:
        # 未配置映射：明确告警，让用户补 JOB_MAP（跟踪表单选项值是自定义的，不能自动猜）
        print(f"  ⚠️ '{job_title}' 未在 JOB_MAP 配置，岗位/部门/职能将留空。请编辑脚本的 JOB_MAP 补充：")
        print(f"     \"{job_title}\": (\"<跟踪表岗位选项>\", \"<部门选项>\", \"<职能类别>\"),")
        job_pos, dept, func = "", "", ""

    # 基础字段
    fields = {
        "候选人": name,
        "talent_id": person.get("talent_id", ""),  # talent_id 列（对账用，治漏报根因）
        "岗位": safe_option(job_pos, _field_opts("岗位"), "岗位"),
        "部门": safe_option(dept, _field_opts("部门"), "部门"),
        "职能类别": safe_option(func, OPT_FUNC, "职能类别"),
        "状态": "待安排" if name not in time_map else "已排期",  # 状态选项：待安排/已排期/...；"待约面"是轮次不是状态
        "当前轮次": "待约面",
        "优先级": "高",
        "下一步动作": "录入完成，待约面" if name not in time_map else f"面试时间 {time_map[name]}，待确认面试官",
    }
    # 状态/轮次也要过选项校验
    fields["状态"] = safe_option(fields["状态"], OPT_STATUS, "状态")
    fields["当前轮次"] = safe_option(fields["当前轮次"], OPT_ROUND, "当前轮次")
    fields["优先级"] = safe_option(fields["优先级"], OPT_PRIORITY, "优先级")
    # 去掉 None
    fields = {k: v for k, v in fields.items() if v is not None}

    # datetime 字段（毫秒整数）—— 动态取当天，不再硬编码日期
    today = datetime.date.today().isoformat()  # YYYY-MM-DD
    now_ms = to_ms(f"{today}T09:00:00")  # 进入阶段日期/最近进展，跑当天
    fields["进入阶段日期"] = now_ms
    fields["最近进展日期"] = now_ms
    if name in time_map:
        fields["面试时间"] = to_ms(time_map[name].replace(" ", "T"))

    existing = _existing_records.get(name)
    j = json.dumps(fields, ensure_ascii=False)
    if dry_run:
        print(f"  [DRY] {name} {'UPDATE' if existing else 'CREATE'}: {j[:120]}")
        return True

    if existing:
        # ⚠️ lark-cli 没有 +record-update 子命令（会报 unknown subcommand）！
        # 更新现有记录也用 +record-upsert + --record-id（upsert 带指定 id 即覆盖更新）
        args = ["base", "+record-upsert", "--base-token", BASE, "--table-id", TBL,
                "--record-id", existing, "--json", j, "--as", "user"]
        action = "更新"
    else:
        args = ["base", "+record-upsert", "--base-token", BASE, "--table-id", TBL,
                "--json", j, "--as", "user"]
        action = "新建"

    r = subprocess.run([CLI] + args, capture_output=True, text=True)
    raw = r.stdout + r.stderr  # 错误信息在 stderr
    ok = extract_json(raw)
    success = bool(ok and ok.get("ok"))
    err_hint = "" if success else " 失败:" + raw[:150]
    print(f"  {'✅' if success else '❌'} {name} {action}{err_hint}")
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


_existing_records = {}


def main():
    global _existing_records
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
    print("【查重】拉现有跟踪表记录...")
    _existing_records = list_records()
    print(f"  现有 {len(_existing_records)} 条记录")

    # 逐人建/更新
    ok_cnt = 0
    for p in people:
        ok_cnt += 1 if upsert_row(p, time_map, args.dry_run) else 0

    print(f"\n=== 完成 {ok_cnt}/{len(people)} ===")
    if ok_cnt != len(people):
        sys.exit(1)


if __name__ == "__main__":
    main()
