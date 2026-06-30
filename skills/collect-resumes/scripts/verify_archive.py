# -*- coding: utf-8 -*-
"""
verify_archive.py — 归档闸门（read-only，绝不写盘/不删文件）

防两类历史事故：
  A. 手填 MID 串行错位 → 文件名姓名 ∉ 简历正文（姓名闸门）
  B. cp/mv 后静默丢文件 → 操作前后数量不对（数量闸门）

用法：
  python verify_archive.py <简历目录或单个pdf> [--no-salary] [--names-only]

退出码：0=全过；1=有报警（CI/流程里可用，AI 流程里看 stdout 标记）
"""
import sys, os, re, argparse, zipfile, tempfile, shutil, hashlib, json
sys.stdout.reconfigure(encoding="utf-8")

try:
    import fitz
except ImportError:
    print("ERR: 缺 PyMuPDF，pip install pymupdf"); sys.exit(2)

CN = re.compile(r"[\u4e00-\u9fa5]{2,4}")
SALARY = re.compile(
    # 1) 明确带薪酬关键词的（最可靠，必命中）
    r"(期望薪资|期望薪水|当前薪资|目前薪资|薪资[：:]|薪水[：:]?|月薪|年薪|"
    r"待遇[：:]|薪金|税前|税后|到手|base|底薪|薪资面议|面议)"
    # 2) "期望薪资25-30K"这类关键词紧跟数字范围（无冒号也命中）
    r"|(期望薪资|薪资|月薪|年薪)\s*\d[\d,.\s]*[-—~至]+\d[\d,.\s]*\s*[Kk万]"
    # 3) 纯4位数范围（如 20000-30000 月薪）须带薪字上下文，避免"2023-2027"年份误伤
    r"|(薪|月薪|年薪|税前|税后|到手|期望).{0,6}\d{4,}\s*[-—~至]\s*\d{4,}"
    # 4) "25-30K"/"25k-30k" 两位数范围+K/万（w/W 不含，避免"10W播放量""16K贴图"误伤）
    r"|\d{1,2}\s*[-—~至]\s*\d{1,2}\s*[Kk](?!PT|HZ|分辨率|帧|视频|贴图)"
    # 5) 单值 "25K"/"2万" 前面带薪字上下文（避免裸"16K贴图""4K分辨率"误伤）
    r"|(薪|薪水|薪资|月薪|年薪|税前|税后|到手).{0,6}\d{1,2}\s*[Kk万wW]"
)
# 压缩包扩展名（美术岗"简历加作品.zip"必须解压扫包内 PDF，否则三道闸门全盲）
ZIP_EXT = (".zip",)
# 单独传入脚本时（非目录递归），也支持直接给一个 zip


def extract(p):
    """返回 (正文, 可提取字数, 图片对象数)"""
    doc = fitz.open(p)
    txt = "\n".join(pg.get_text() for pg in doc)
    nimg = sum(len(pg.get_images()) for pg in doc)
    doc.close()
    return txt, len(txt.strip()), nimg


def _is_resume_pdf(name):
    """是否为简历本体 PDF（跳过纯作品集）。zip 内常见"简历.pdf/作品集.pdf"混放。"""
    low = name.lower()
    if "作品集" in name or "portfolio" in low:
        return False
    return low.endswith(".pdf")


# analyze-resumes 评估后按档位分发的子目录名（强推/可推/待定·不推）。
# 这些子目录里的简历仍计入人头，但子目录名本身不当人头条目。
TIER_DIR_NAMES = {"强推", "可推", "待定", "待定·不推", "不推", "推荐给业务"}
def _is_tier_dir(name):
    """是否为评估档位子目录。支持各种分隔写法（待定·不推 / 待定/不推 等）。"""
    n = name.strip().rstrip("/")
    if n in TIER_DIR_NAMES:
        return True
    # 兼容 "待定·不推" / "待定-不推" / "待定_不推" 写法
    for t in ("待定", "不推", "强推", "可推"):
        if t in n and ("不推" in n or t in ("强推", "可推")):
            return True
    return False


class CollectResult:
    """collect() 的结果。
    - count_files: 用于数量闸门的"顶层文件"清单（一个 zip 算 1 个，不展开）。
      每项是原始磁盘路径。
    - scan_files: 用于姓名/薪酬闸门的"需扫描 PDF"清单（zip 内 PDF 已解压到临时目录）。
      每项是 (显示路径, 真实读取路径)。显示路径=zip原路径 + '!' + 包内名，保持分组正确。
    """
    def __init__(self):
        self.count_files = []
        self.scan_files = []   # list of (display_path, real_path)
        self._tmpdir = None

    def cleanup(self):
        if self._tmpdir and os.path.isdir(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)


def collect(root):
    """
    递归收集简历文件。
    - 顶层 .pdf → 计数 1 个 + 扫描 1 个
    - 顶层 .zip（美术岗"简历加作品"）→ 计数 1 个（整个人算 1 份）+ 解压扫包内简历 PDF
    - 跳过名字含"作品集/portfolio"的散落 PDF（纯作品集，非简历本体）
    返回 CollectResult。
    """
    res = CollectResult()

    def handle_zip(zip_path):
        """解压 zip 到临时目录，把包内简历 PDF 加入 scan_files。read-only：只读临时副本。"""
        if res._tmpdir is None:
            res._tmpdir = tempfile.mkdtemp(prefix="verify_archive_")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                # 只解压 PDF（大文件作品图不解压，省时省空间）
                pdf_names = [n for n in zf.namelist() if _is_resume_pdf(os.path.basename(n))]
                for n in pdf_names:
                    try:
                        target = os.path.join(res._tmpdir, f"z{len(res.scan_files)}_{os.path.basename(n)}")
                        with zf.open(n) as src, open(target, "wb") as dst:
                            dst.write(src.read())
                        # 显示路径用 zip原路径!包内名，保证分组时归到 zip 所在的 _N份 目录
                        display = zip_path + "!" + n
                        res.scan_files.append((display, target))
                    except Exception as e:
                        print(f"  ⚠️ 解压 {zip_path}!{n} 失败: {e}（跳过该文件）")
        except zipfile.BadZipFile:
            print(f"  ⚠️ {os.path.basename(zip_path)} 不是合法 zip 或已损坏（可能为 rar，本工具不处理）")

    if os.path.isfile(root):
        low = root.lower()
        if low.endswith(".pdf"):
            res.count_files.append(root)
            res.scan_files.append((root, root))
        elif low.endswith(ZIP_EXT):
            res.count_files.append(root)
            handle_zip(root)
        return res

    # 目录递归。scan_files 收所有待扫 PDF（含 zip 内 PDF）。
    # count_files 记每个 _N份 目录下的人头条目，供数量闸门按姓名去重算人头。
    for d, dirs, fs in os.walk(root):
        for f in fs:
            full = os.path.join(d, f)
            low = f.lower()
            if low.endswith(".pdf") and _is_resume_pdf(f):
                res.scan_files.append((full, full))
            elif low.endswith(ZIP_EXT):
                handle_zip(full)
        # 只对 _N份 目录记顶层条目（避免散落根目录误计）
        if re.search(r"_\d+份$", os.path.basename(d.rstrip("/"))):
            # analyze-resumes 会把简历按档位分到 强推/可推/待定·不推 子目录。
            # 识别档位子目录：若存在，递归收档位内 PDF（人头），子目录名本身不当条目；
            # 若不存在（传统平铺），顶层文件直接当条目。两种形态同一套计数。
            tier_dirs = [dr for dr in dirs if _is_tier_dir(dr)]
            if tier_dirs:
                for tdr in tier_dirs:
                    tpath = os.path.join(d, tdr)
                    for tf in os.listdir(tpath):
                        res.count_files.append(os.path.join(tpath, tf))
            else:
                for entry in list(fs) + list(dirs):
                    res.count_files.append(os.path.join(d, entry))
    return res


# 学历关键词（用于识别实习生命名格式 {届}_{学历}_{姓名}_{学校}）
DEGREE = re.compile(r"^(本科|硕士|博士|大专|专科|学士|研一|研二|研三|MBA|mba)$")
# 届（用于识别实习生命名格式首段）
GRADE = re.compile(r"届|级")
# 岗位/角色关键词（命名首段若含这些 → 不是姓名，是岗位词，说明命名不规范）
ROLE_KW = re.compile(r"实习生|工程师|设计师|经理|开发|策划|运营|产品|前端|后端|服务端|客户端")
# 姓名段的非姓名后缀词/描述词（如"张三简历""李利兵特效简历作品"→ 剥离后得姓名）
NAME_TAIL = re.compile(
    r"(个人简历|的简历|简历作品|简历加作品|特效简历作品|简历|作品集|作品|"
    r"resume|portfolio|PDF|pdf|\d{4}|\d+|特效|设计师|工程师|[\-－_]+.*)"
)


def parse_name(fname):
    """
    按命名规则从文件名解析姓名段。
    规则（archive-naming.md）:
      - 实习生: {届}_{学历}_{姓名}_{学校}.pdf   → 姓名是第 3 段
      - 正职:   {姓名}_{岗位简称}_{经验}年.pdf   → 姓名是第 1 段
    策略:
      1. 按 _ 切段。若第1段含"届/级"、第2段是学历词 → 实习生格式取第3段。
      2. 否则视为正职，取第1段；但若第1段像岗位词（含"实习生/工程师/设计师"等），
         说明命名不规范（如"产品经理实习生_本科_王芳"），返回 "" 触发人工确认，
         绝不把岗位词当姓名。
      3. 兜底: 取第一个含 2-4 连续汉字的段。
    每个候选段在抽取姓名前，先剥离"简历/个人简历/resume"等后缀词，
    以支持 zip 内常见命名"张三简历.pdf""李四个人简历.pdf"。
    返回 "" 表示解析不出 → 调用方应提示"命名不规范/解析不出姓名，需人工确认"。
    """
    stem = re.sub(r"\.(pdf|zip|rar|7z)$", "", fname, flags=re.IGNORECASE)
    # 剥离【...】/「...」/[...] 等岗位标注段（如"【iOS客户端开发工程师】林志平_7年"）
    stem = re.sub(r"[【\[「【].*?[\】\]」】]", "", stem).strip()
    parts = [p.strip() for p in stem.split("_") if p.strip()]
    if not parts:
        return ""

    def name_of(seg):
        """从一段里剥离后缀词后取连续汉字姓名。"""
        seg2 = NAME_TAIL.sub("", seg).strip()
        m = CN.findall(seg2)
        return m[0] if m else ""

    # 实习生格式判定: 段数>=3 且 第1段含"届/级" 且 第2段是学历词
    if len(parts) >= 3 and GRADE.search(parts[0]) and DEGREE.match(parts[1]):
        nm = name_of(parts[2])
        if nm:
            return nm
    # 正职格式: 第1段
    if ROLE_KW.search(parts[0]):
        return ""  # 首段是岗位词，命名不规范，宁可走人工也不误取
    nm = name_of(parts[0])
    if nm:
        return nm
    # 兜底: 扫所有段
    for p in parts:
        nm = name_of(p)
        if nm:
            return nm
    return ""


def name_in_text(name, txt):
    """
    判断文件名姓名与正文署名是否一致。返回三态: 'pass' / 'manual' / 'miss'。
    - 'miss'  : name 在正文里连子串都不是 → 疑似下载填串（严重错配），调用方应 STOP。
    - 'pass'  : name 以完整形态出现 → 放行。
    - 'manual': name 仅作为更长汉字串的中间子串出现（无法确认是简称还是另一人）→ 人工确认。

    设计依据（第一性原理）: 本闸门的核心价值是抓"姓名完全错配"的下载填串（name 完全不在正文），
    这是能可靠检测的。中文无词边界，"李明"是否为"李明华"的简称无法可靠区分，故 2 字名遇到
    "右侧紧接汉字"的模糊情况一律走人工，宁可误报人工也不自动放行串岗简历。
    3 字以上姓名极少是他人姓名的真子串，子串出现即视为本人。
    """
    if not name:
        return "miss"
    # 只去空格/tab，保留换行作为词边界（姓名常在行首/行尾）
    compact = re.sub(r"[ \t]+", "", txt)
    if name not in compact:
        return "miss"
    if len(name) >= 3:
        return "pass"  # 3-4 字姓名，子串出现基本即本人
    # 2 字名: 检查是否存在一处"右侧非汉字"（完整出现: 姓名后接换行/标点/数字/英文/末尾）
    for m in re.finditer(re.escape(name), compact):
        end = m.end()
        if end >= len(compact) or not ("\u4e00" <= compact[end] <= "\u9fa5"):
            return "pass"
    return "manual"  # 2 字名只作为长汉字串中间子串 → 模糊，人工确认


# ---------- 增量扫描 manifest ----------
# manifest 记录每个"通过校验"的 pdf 的 (mtime, size, 姓名, 薪酬命中数)。
# 下次扫描时若 mtime+size 未变 → 跳过 extract 直接复用结果（省 PDF 解析）。
# 安全约束: 只缓存"姓名 pass 且无薪酬残留"的文件；任何 STOP/⚠️/图片型/manual 一律不缓存，
# 确保问题文件每次都重新校验，脱敏后重跑不会被旧缓存掩盖。
MANIFEST_DIR = "F:/miniwanob/notes/.verified_manifest"


def _manifest_path(target):
    """按 target 绝对路径 hash 命名 manifest，避免中文路径问题。"""
    h = hashlib.md5(os.path.abspath(target).encode("utf-8")).hexdigest()[:16]
    return os.path.join(MANIFEST_DIR, f"{h}.json")


def load_manifest(target):
    p = _manifest_path(target)
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_manifest(target, manifest):
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    with open(_manifest_path(target), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def file_sig(real):
    """文件的 (mtime, size) 指纹，用于判断是否变动。"""
    st = os.stat(real)
    return (int(st.st_mtime), st.st_size)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--no-salary", action="store_true", help="跳过薪酬扫描")
    ap.add_argument("--names-only", action="store_true", help="只跑姓名闸门")
    ap.add_argument("--no-cache", action="store_true", help="禁用增量缓存，强制全量扫描")
    args = ap.parse_args()

    res = collect(args.target)
    try:
        if not res.count_files:
            print("⚠️ 没找到简历（pdf 或 zip）"); sys.exit(1)

        # ---- 数量闸门：按"已收集简历/收集到简历"下一层目录分组（zip 整体算 1 份）----
        print("【数量闸门】")
        folders = {}
        for f in res.count_files:
            parts = f.replace("\\", "/").split("/")
            key = "散落根目录"
            for i, p in enumerate(parts):
                # 兼容 legacy "收集到简历" 与规范 "已收集简历"
                if p in ("已收集简历", "收集到简历") and i + 1 < len(parts):
                    key = parts[i + 1]
                    break
            folders.setdefault(key, []).append(f)
        count_mismatch = False
        for k, fs in folders.items():
            m = re.search(r"_(\d+)份", k)
            if m:
                claimed = int(m.group(1))
                # 人头口径：顶层条目按姓名去重，排除 temp/临时 打包暂存目录
                # 同一人 zip+pdf（姓名相同）去重为 1；解析不出姓名的条目各算独立项
                parsed_entries = [path for path in fs
                                  if not re.search(r"temp|临时|tmp", os.path.basename(path.rstrip("/")), re.IGNORECASE)]
                heads = set()
                for i, p in enumerate(parsed_entries):
                    nm = parse_name(os.path.basename(p.rstrip("/")))
                    heads.add(nm if nm else f"__unnamed_{i}")  # 无姓名的保留独立计数
                actual = len(heads)
                flag = "✅" if claimed == actual else "❌ 数量不符!"
                if claimed != actual:
                    count_mismatch = True
                print(f"  {flag} {k}: 标注{claimed}份, 实际{actual}份（按姓名去重，排除temp暂存目录）")
            else:
                print(f"  ⚠️ {k}: {len(fs)}份（目录名无 _N份 标注，无法校验数量 → 归档后请立即标注再校验）")

        # ---- 姓名 + 薪酬闸门合并扫描（extract 每个文件只做一次；增量缓存跳过未变动文件）----
        print("\n【姓名闸门】 file_name vs 正文署名")
        name_issues = []
        sal_hits = []  # 薪酬命中（薪酬闸门与姓名闸门同一遍扫描，复用 extract 结果）
        scan_salary = not args.names_only and not args.no_salary
        use_cache = not args.no_cache and not args.names_only  # --names-only 不缓存（薪酬未扫全）
        manifest = load_manifest(args.target) if use_cache else {}
        new_manifest = {}
        cached_n = 0
        for display, real in sorted(res.scan_files, key=lambda x: x[0]):
            fname = display.split("!")[-1].split("/")[-1]  # zip 内文件取包内名
            fn_name = parse_name(os.path.basename(fname))

            # 增量缓存: 文件未变动 + 上次是干净通过 → 跳过 extract，直接复用
            cache_key = display  # zip 内文件用 display(含包路径) 唯一标识
            cached = manifest.get(cache_key)
            try:
                sig = file_sig(real)
            except OSError:
                sig = None
            if use_cache and cached and sig and tuple(cached.get("sig", [])) == sig and fn_name == cached.get("name"):
                # 缓存命中: 上次 pass 且无薪酬 → 直接放行，省掉 extract
                print(f"  ✅ {fname}  ({fn_name}) [缓存命中，跳过解析]")
                new_manifest[cache_key] = cached  # 续期
                cached_n += 1
                continue

            # extract 一次，姓名闸门和薪酬闸门共用
            try:
                txt, nchar, nimg = extract(real)
            except Exception as e:
                print(f"  ❌ {fname} 读取失败: {e}")
                name_issues.append(display)
                continue

            # 薪酬扫描（复用同一份 txt，不再二次 extract）
            this_sal = []
            if scan_salary:
                for m in SALARY.finditer(txt):
                    snip = m.group(0).strip().replace("\n", " ")[:30]
                    this_sal.append((fname, snip))
                sal_hits.extend(this_sal)

            # 姓名闸门
            if not fn_name:
                print(f"  ⚠️ {fname}  文件名解析不出姓名，跳过姓名校验 → 需人工确认")
                continue  # 不缓存
            compact = txt.replace(" ", "").replace("\n", "")
            head = compact[:400]
            cands = CN.findall(head)
            if nchar < 20 and nimg > 0:
                print(f"  ⚠️ {fname}  图片型PDF(无文本)，无法核姓名，候选:{cands[:3]} → 需人工确认")
                continue  # 图片型不缓存
            verdict = name_in_text(fn_name, txt)
            file_clean = False
            if verdict == "pass":
                print(f"  ✅ {fname}  ({fn_name})")
                file_clean = True
            elif verdict == "manual":
                print(f"  ⚠️ {fname}  正文含'{fn_name}'但疑似他人姓名子串，候选:{cands[:5]} → 需人工确认")
            else:  # miss
                print(f"  ❌ {fname}  正文无'{fn_name}'，候选:{cands[:5]} → 疑下载填串，立即停！")
                name_issues.append(display)
            # 只有"姓名 pass 且无薪酬残留"才进缓存；任何 ⚠️/❌/图片型 都不缓存（每次重扫）
            if file_clean and scan_salary and not this_sal and sig:
                new_manifest[cache_key] = {"sig": list(sig), "name": fn_name}

        if use_cache:
            save_manifest(args.target, new_manifest)  # 全量覆盖：淘汰已删文件/不再干净的旧记录
            if cached_n:
                print(f"  ℹ️ 增量缓存命中 {cached_n} 个文件（未变动，跳过解析）。强制全量扫描加 --no-cache")

        # 薪酬闸门结果汇总输出
        if scan_salary:
            print("\n【薪酬闸门】 归档后不得残留薪酬段")
            if not sal_hits:
                print("  ✅ 无薪酬残留")
            else:
                for fn, snip in sal_hits[:15]:
                    print(f"  ⚠️ {fn}  [{snip}]")

        print("\n" + "=" * 40)
        # 姓名不符 / 数量不符 → STOP；薪酬命中也 STOP（薪酬残留不得带进评估）
        if name_issues or count_mismatch:
            print("🔴 STOP — 有数量不符或姓名不符，修复后再进评估")
            sys.exit(1)
        if sal_hits:
            print("🔴 STOP — 检出薪酬残留，脱敏后重跑（本脚本只检测不修复，需用 PyMuPDF redact）")
            sys.exit(1)
        print("🟢 全过 — 可进评估")
    finally:
        res.cleanup()


if __name__ == "__main__":
    main()
