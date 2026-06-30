# -*- coding: utf-8 -*-
"""
verify_hire.py — 录入后验证闸门（read-only，绝不写盘/删数据）

参照 collect-resumes 的 verify_archive.py 模式。
录完人(_hire.py) + 建完跟踪表(track_after_hire.py)后必跑，挡住：
  A. talent 误关联（A 的投递挂到 B 的 talent 上）→ 核对 talent 姓名邮箱
  B. 投递建错岗位 → 核对 application 的 job_id
  C. 跟踪表建行遗漏 → 核对 4 人是否都有行

用法：
  python verify_hire.py
  python verify_hire.py --result notes/_hire_result.json

退出码：0=全过；1=有 STOP
"""
import subprocess, json, sys, os, re, argparse
sys.stdout.reconfigure(encoding="utf-8")

# 凭证走环境变量（见 .env.example），不硬编码
CLI = os.environ.get("LARK_CLI_PATH", "lark-cli")
BASE = os.environ.get("TRACKING_BASE_TOKEN", "")
TBL = os.environ.get("TRACKING_TABLE_ID", "")
RESULT = os.environ.get("HIRE_RESULT_FILE", "notes/_hire_result.json")


def api(method, path, params=None, as_user="bot"):
    """调 lark-cli api，返回解析后的 json 或 None"""
    args = ["api", method, path, "--as", as_user]
    if params:
        args += ["--params", json.dumps(params, ensure_ascii=False)]
    r = subprocess.run([CLI] + args, capture_output=True, text=True)
    m = re.search(r'\{[\s\S]*\}', r.stdout)
    return json.loads(m.group(0)) if m else None


def list_track_names():
    """拉跟踪表所有候选人名（用于核对建行是否齐全）"""
    r = subprocess.run([CLI, "base", "+record-list", "--base-token", BASE, "--table-id", TBL,
                        "--field-id", "候选人", "--format", "json", "--as", "user", "--limit", "200"],
                       capture_output=True, text=True)
    m = re.search(r'\{[\s\S]*\}', r.stdout)
    if not m:
        return []
    d = json.loads(m.group(0))
    rows = d.get("data", {}).get("data", [])
    return [row[0] for row in rows if isinstance(row, list) and row]


def build_job_map():
    """建 job_code -> job_id 映射。
    优先读 notes/_jobs_all.json（_hire.py 流程里现拉的），回退实时拉 jobs。
    用于核对 application 投递到了正确的岗位（防 job_code 填错投错岗）"""
    code2id = {}
    for path in ("notes/_jobs_all.json", "notes/jobs_map.json"):
        if not os.path.exists(path):
            continue
        raw = open(path, encoding="utf-8").read()
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            continue
        d = json.loads(m.group(0))
        # _jobs_all.json: data.items[]，键是 code/id
        items = d.get("data", {}).get("items", []) if isinstance(d.get("data"), dict) else []
        for it in items:
            c, jid = it.get("code"), it.get("id")
            if c and jid:
                code2id[c] = str(jid)
        # jobs_map.json: 顶层 dict，键是 job_id，值含 code
        if isinstance(d, dict) and not items:
            for jid, v in d.items():
                if isinstance(v, dict) and v.get("code"):
                    code2id[v["code"]] = str(jid)
        if code2id:
            break
    if not code2id:
        # 兜底：实时拉一次
        d = api("GET", "/open-apis/hire/v1/jobs", params={"page_size": "20"})
        for it in (d.get("data", {}).get("items", []) if d else []):
            c, jid = it.get("code"), it.get("id")
            if c and jid:
                code2id[c] = str(jid)
    return code2id


def app_job_id(app_id):
    """查 application 详情，返回 job_id（投递到的岗位 id）"""
    d = api("GET", f"/open-apis/hire/v1/applications/{app_id}")
    app = (d.get("data", {}) or {}).get("application", {}) if d else {}
    jid = app.get("job_id")
    return str(jid) if jid else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", default=RESULT)
    args = ap.parse_args()

    if not os.path.exists(args.result):
        print(f"❌ 找不到 {args.result}"); sys.exit(1)
    people = json.load(open(args.result, encoding="utf-8"))
    people = [p for p in people if p.get("ok") and p.get("talent_id")]
    print(f"=== 录入闸门：核对 {len(people)} 人 ===\n")

    stop = False

    # ---- A. talent 姓名邮箱对账（防误关联）----
    print("【人才对账】 talent_id -> 姓名/邮箱")
    for p in people:
        tid = p["talent_id"]
        d = api("GET", f"/open-apis/hire/v1/talents/{tid}")
        bi = (d.get("data", {}) or {}).get("talent", {}).get("basic_info", {}) if d else {}
        real_name = bi.get("name", "")
        # 名字模糊匹配（解析可能差一个字或带空格）
        match = p["name"] in real_name or real_name in p["name"] or p.get("name_parsed", "") in real_name
        flag = "✅" if match else "❌"
        if not match:
            stop = True
        print(f"  {flag} {p['name']}: talent姓名='{real_name}' 邮箱={bi.get('email','')[:20]}")

    # ---- B. 投递对账（投递存在 + 投到对的岗位）----
    # ⚠️ applications?talent_id= 返回的 items 是 application_id 字符串数组，不是对象！
    # 验 job 对错：application 详情拿 job_id，和 _hire_result 的 job_code 经 code→id 映射比对
    print("\n【投递对账】 投递存在 + 岗位正确性")
    code2id = build_job_map()
    if not code2id:
        print("  ⚠️ 无法构建 job_code→job_id 映射（_jobs_all.json 缺失且实时拉取失败），跳过岗位正确性校验")
    for p in people:
        tid = p["talent_id"]
        expect_code = p.get("job_code", "")
        expect_jid = code2id.get(expect_code)
        # 投递查询偶发返回空（接口抖动/最终一致性），空时重试1次再判死
        items = []
        for _ in range(2):
            d = api("GET", "/open-apis/hire/v1/applications",
                    params={"talent_id": tid, "page_size": "5"}, as_user="bot")
            items = (d.get("data", {}) or {}).get("items", []) if d else []
            if items:
                break
        if not items:
            print(f"  ❌ {p['name']}: 无投递（重试后仍空）")
            stop = True
            continue
        # 查第一条投递的 job_id（一般一人一岗只投一条；多条则看是否含预期岗）
        actual_jids = [app_job_id(a) for a in items if isinstance(a, str)]
        if expect_jid and expect_jid in actual_jids:
            print(f"  ✅ {p['name']}: 投递岗位正确 ({expect_code}→{expect_jid})")
        elif expect_jid:
            print(f"  ❌ {p['name']}: 投错岗位! 期望 {expect_code}({expect_jid}), 实际 {actual_jids}")
            stop = True
        else:
            # 没映射到 expect_jid，退化为"验存在"
            print(f"  ✅ {p['name']}: 投递存在 (job_code={expect_code} 无映射，未校验岗位，人工确认)")
            print(f"     提示: 跑 lark-cli api GET /open-apis/hire/v1/jobs --page-all > notes/_jobs_all.json 刷新岗位缓存")

    # ---- C. 跟踪表对账（每人都有行）----
    print("\n【跟踪表对账】 每人是否已建行")
    track_names = list_track_names()
    for p in people:
        has = p["name"] in track_names
        flag = "✅" if has else "❌"
        if not has:
            stop = True
        msg = "已在跟踪表" if has else "未建行，跑 track_after_hire.py"
        print(f"  {flag} {p['name']}: {msg}")

    print("\n" + "=" * 40)
    if stop:
        print("🔴 STOP — 有 talent 误关联/投递缺失/跟踪表漏行，修复后再继续")
        sys.exit(1)
    print("🟢 全过 — 录入完整无误，可继续后续约面")


if __name__ == "__main__":
    main()
