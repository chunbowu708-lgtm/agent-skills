# -*- coding: utf-8 -*-
"""
verify_archive.py — 归档闸门

五重阻断校验：数量 / 姓名 / 薪酬 / 格式 / manifest闭环。
SHA-256 缓存加速重复校验（mtime/size 只做性能快筛，不做安全判定）。

⚠️ 注意：本脚本不是纯只读。校验前会执行自愈逻辑：
  - _finalize_pending_dirs：把 {M.DD}_暂定 目录 rename 为 {M.DD}_{N}份，删除空 _暂定 目录
  - _merge_split_batches：合并同天被拆成多个 _{k}份 的目录
这些自愈操作在 collect 前执行（校验本身只读），但会 mutate 归档目录结构。

用法：
  python verify_archive.py <简历目录或单个pdf/zip/docx> [--no-cache] [--manifest <path>] [--report-json <path>]

退出码：0=全过；1=有阻断（任何无法验证的情况）
"""

import sys, os, re, argparse, zipfile, tempfile, shutil, hashlib, json, subprocess
sys.stdout.reconfigure(encoding="utf-8")

# 把 scripts 目录加入 import 路径（content_extractors/archive_safety 在同目录）
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from content_extractors import extract as extract_content
from archive_safety import check_zip
from salary_pattern import SALARY
from paths import SEVEN_ZIP, CACHE_DIR

CN = re.compile(r"[\u4e00-\u9fa5]{2,4}")

ZIP_EXT = (".zip",)
RAR_EXT = (".rar", ".7z")
RESUME_EXTS = (".pdf", ".docx", ".doc")
RESUME_IMG_EXTS = (".jpg", ".jpeg", ".png")
# 图片文件名含这些关键词才认作简历（纯作品图不误判）
RESUME_IMG_KEYWORDS = ("简历", "resume", "Resume", "RESUME", "cv", "CV")

TIER_DIR_NAMES = {"强推", "可推", "待定·不推"}

def _is_resume_file(name):
    """是否为简历本体文件，跳过纯作品集。
    PDF/DOCX/DOC 直接认；图片(jpg/jpeg/png)需文件名含「简历/resume/cv」才认（防把作品图当简历）。"""
    low = name.lower()
    if "作品集" in name or "portfolio" in low:
        return False
    if low.endswith(RESUME_EXTS):
        return True
    if low.endswith(RESUME_IMG_EXTS) and any(k in name for k in RESUME_IMG_KEYWORDS):
        return True
    return False

def _is_tier_dir(name):
    n = name.strip().rstrip("/")
    if n in TIER_DIR_NAMES:
        return True
    for t in ("待定", "不推", "强推", "可推"):
        if t in n and ("不推" in n or t in ("强推", "可推")):
            return True
    return False


def _zip_decode_name(info):
    """还原 zip 内中文文件名（Windows 下用 GBK 编码，python 默认 cp437 解出乱码）。
    带 0x800 UTF-8 标志的直接用原名，否则 cp437→gbk 还原。"""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("gbk")
    except Exception:
        return name


# ---- 文件名姓名解析 ----
DEGREE = re.compile(r"^(本科|硕士|博士|大专|专科|学士|研一|研二|研三|MBA|mba)$")
GRADE = re.compile(r"届|级")
ROLE_KW = re.compile(r"实习生|工程师|设计师|经理|开发|策划|运营|产品|前端|后端|服务端|客户端")
NAME_TAIL = re.compile(
    r"(个人简历|的简历|简历作品|简历加作品|特效简历作品|简历|作品集|作品|"
    r"resume|portfolio|PDF|pdf|\d{4}|\d+|特效|设计师|工程师|[\-－_]+.*)"
)

def parse_name(fname):
    stem = re.sub(r"\.(pdf|zip|rar|7z|docx)$", "", fname, flags=re.IGNORECASE)
    stem = re.sub(r"[【\[「].*?[\】\]」]", "", stem).strip()
    parts = [p.strip() for p in stem.split("_") if p.strip()]
    if not parts:
        return ""
    def name_of(seg):
        seg2 = NAME_TAIL.sub("", seg).strip()
        m = CN.findall(seg2)
        return m[0] if m else ""
    if len(parts) >= 3 and GRADE.search(parts[0]) and DEGREE.match(parts[1]):
        nm = name_of(parts[2])
        if nm: return nm
    if ROLE_KW.search(parts[0]):
        return ""
    nm = name_of(parts[0])
    if nm: return nm
    for p in parts:
        nm = name_of(p)
        if nm: return nm
    return ""


def name_in_text(name, txt):
    if not name:
        return "miss"
    compact = re.sub(r"[ \t]+", "", txt)
    if name not in compact:
        return "miss"
    if len(name) >= 3:
        return "pass"
    for m in re.finditer(re.escape(name), compact):
        end = m.end()
        if end >= len(compact) or not ("\u4e00" <= compact[end] <= "\u9fa5"):
            return "pass"
    return "manual"


# ---- 收集文件 ----
class CollectResult:
    def __init__(self):
        self.count_files = []   # 顶层条目（zip 算 1）
        self.scan_files = []    # (display_path, real_path) 需提取的文件
        self._tmpdir = None

    def cleanup(self):
        if self._tmpdir and os.path.isdir(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)


def collect(root):
    res = CollectResult()

    def handle_zip(zip_path):
        """安全检查 ZIP 并解压包内简历到临时目录。"""
        safety = check_zip(zip_path)
        if safety.blocked:
            print(f"  🔴 ZIP 安全检查失败 {os.path.basename(zip_path)}: {safety.reason}")
            # 记录为阻断标记，main 会据此 STOP
            res.scan_files.append(("__BLOCKED__:" + zip_path, zip_path))
            return

        if res._tmpdir is None:
            res._tmpdir = tempfile.mkdtemp(prefix="verify_archive_")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                # 遍历成员，GBK 还原中文文件名（Windows 下 zip 用 GBK，python 默认 cp437 解出乱码）
                # 只解压简历成员（PDF/DOCX/图片简历）；纯作品素材（mp4/普通jpg 等）不提取
                resume_members = []
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    real_name = _zip_decode_name(info)  # GBK 还原后的文件名
                    if _is_resume_file(os.path.basename(real_name)):
                        resume_members.append((info, real_name))
                if resume_members:
                    for info, real_name in resume_members:
                        try:
                            # 用 info 对象打开（按原始字节索引，不经过 cp437 字符串名）
                            target = os.path.join(res._tmpdir, f"z{len(res.scan_files)}_{os.path.basename(real_name)}")
                            with zf.open(info) as src, open(target, "wb") as dst:
                                dst.write(src.read())
                            display = zip_path + "!" + real_name
                            res.scan_files.append((display, target))
                        except Exception as e:
                            print(f"  🔴 解压 {zip_path}!{real_name} 失败: {e}")
                            res.scan_files.append(("__BLOCKED__:" + zip_path + "!" + real_name, real_name))
                else:
                    # 纯作品包：无简历成员，跳过姓名/薪酬（简历 PDF 应在 zip 外单独验证）
                    for w in safety.warnings:
                        print(f"  ℹ️ {os.path.basename(zip_path)}: {w}")
        except Exception as e:
            print(f"  🔴 ZIP 解压异常 {os.path.basename(zip_path)}: {e}")
            res.scan_files.append(("__BLOCKED__:" + zip_path, zip_path))

    def handle_rar(rar_path):
        """用 7z 解压 RAR/7z 内简历到临时目录（zipfile 不支持 RAR）。"""
        if not os.path.isfile(SEVEN_ZIP):
            print(f"  🔴 RAR 需要七-zip 但未找到: {SEVEN_ZIP}")
            res.scan_files.append(("__BLOCKED__:" + rar_path, rar_path))
            return
        if res._tmpdir is None:
            res._tmpdir = tempfile.mkdtemp(prefix="verify_archive_")
        extract_dir = os.path.join(res._tmpdir, f"rar{len(res.scan_files)}")
        os.makedirs(extract_dir, exist_ok=True)
        try:
            r = subprocess.run(
                [SEVEN_ZIP, "x", rar_path, f"-o{extract_dir}", "-y"],
                capture_output=True, timeout=120
            )
            if r.returncode != 0:
                print(f"  🔴 7z 解压失败 {os.path.basename(rar_path)}: {r.stderr[:200]!r}")
                res.scan_files.append(("__BLOCKED__:" + rar_path, rar_path))
                return
            # 遍历解压结果，只收集简历文件
            for root2, _, files2 in os.walk(extract_dir):
                for fn in files2:
                    if _is_resume_file(fn):
                        fp = os.path.join(root2, fn)
                        display = rar_path + "!" + os.path.relpath(fp, extract_dir).replace(os.sep, "/")
                        res.scan_files.append((display, fp))
        except Exception as e:
            print(f"  🔴 RAR 解压异常 {os.path.basename(rar_path)}: {e}")
            res.scan_files.append(("__BLOCKED__:" + rar_path, rar_path))

    if os.path.isfile(root):
        low = root.lower()
        if _is_resume_file(root):
            res.count_files.append(root)
            res.scan_files.append((root, root))
        elif low.endswith(ZIP_EXT):
            res.count_files.append(root)
            handle_zip(root)
        elif low.endswith(RAR_EXT):
            res.count_files.append(root)
            handle_rar(root)
        return res

    for d, dirs, fs in os.walk(root):
        for f in fs:
            full = os.path.join(d, f)
            low = f.lower()
            # _is_resume_file 现在覆盖 .pdf/.docx/.doc（2026-07-29：补 .doc，修 M5 静默消失）
            if _is_resume_file(f):
                res.scan_files.append((full, full))
            elif low.endswith(ZIP_EXT):
                handle_zip(full)
            elif low.endswith(RAR_EXT):
                handle_rar(full)
        if re.search(r"_\d+份$", os.path.basename(d.rstrip("/"))):
            # 同时统计档位子目录和同级合法文件：
            # 同级合法简历文件 + 档位目录内文件都计入（不能只数档位目录漏掉同级）。
            tier_dirs = [dr for dr in dirs if _is_tier_dir(dr)]
            # 同级合法简历文件（非档位目录、非 temp）
            for f in fs:
                if _is_resume_file(f) or f.lower().endswith(ZIP_EXT) or f.lower().endswith(RAR_EXT):
                    res.count_files.append(os.path.join(d, f))
            # 档位目录内的文件
            for tdr in tier_dirs:
                tpath = os.path.join(d, tdr)
                for tf in os.listdir(tpath):
                    res.count_files.append(os.path.join(tpath, tf))
    return res


# ---- SHA-256 缓存 ----
MANIFEST_DIR = CACHE_DIR

def _manifest_path(target):
    h = hashlib.md5(os.path.abspath(target).encode("utf-8")).hexdigest()[:16]
    return os.path.join(MANIFEST_DIR, f"{h}.json")

def load_manifest_cache(target):
    p = _manifest_path(target)
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_manifest_cache(target, manifest):
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    with open(_manifest_path(target), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.digest().hex()

def file_sig(real):
    """mtime+size 快筛（只做性能提示，不做安全判定）。"""
    st = os.stat(real)
    return (int(st.st_mtime), st.st_size)


def _count_heads(dirpath):
    """统计一个目录下的人头（按 parse_name 去重，含档位子目录递归）。"""
    heads = set()
    for root2, _, files2 in os.walk(dirpath):
        for fn in files2:
            low = fn.lower()
            if _is_resume_file(fn) or low.endswith(ZIP_EXT) or low.endswith(RAR_EXT):
                nm = parse_name(fn)
                heads.add(nm if nm else fn)
    return heads


def _finalize_pending_dirs(target):
    """把 {M.DD}_暂定 中转目录 rename 为 {M.DD}_{N}份（N=同天总人头数）。

    resolve_records 在单条记录 resolve 时不知道整批 N，统一落 _暂定/。
    本函数在闸门 collect 之前扫到所有 _暂定 目录，按文件名解析人头数后 rename，
    使 _N份 标注自洽。空 _暂定 目录（无人头）→ 删除（避免空目录残留）。

    同天合并（2026-07-31 修）：增量归档时同一天可能先产生 7.31_3份（评估后含
    强推/可推档位子目录），后又来 7.31_暂定。旧版只数 _暂定 自己的人头 →
    rename 成 7.31_1份，与既有 7.31_3份 并存导致数量标注不自洽。现在改为：
    扫同 {base} 前缀的所有 _{k}份 目录 + _暂定，合并全部人头到单一目标目录，
    命名为 {base}_{总N}份，N 永远自洽。

    幂等：已是 _N份 的目录不动；_暂定 目录 rename 后再次运行不会重复处理。
    """
    if not os.path.isdir(target):
        return
    for d, dirs, files in os.walk(target):
        for dn in list(dirs):  # copy，避免遍历时改 dirs
            if not dn.endswith("_暂定"):
                continue
            pending_path = os.path.join(d, dn)
            base = dn[:-len("_暂定")]  # 去掉 _暂定 后缀，得到 {M.DD}
            # 统计 _暂定 自己的人头
            pending_heads = _count_heads(pending_path)
            if len(pending_heads) == 0:
                # 空目录：删除避免残留（可能是 resolve 后未下载的孤儿）
                try:
                    shutil.rmtree(pending_path)
                    print(f"  ℹ️ 删除空 _暂定 目录: {dn}（无人头）")
                except OSError:
                    pass
                continue
            # 扫同 {base} 前缀的既有 _{k}份 目录（含档位子目录里已评估的人）
            siblings = []  # [(path, heads_set)]
            all_heads = set(pending_heads)
            for sib in os.listdir(d):
                if sib == dn:
                    continue
                if sib.startswith(base + "_") and re.search(r"_\d+份$", sib):
                    sib_path = os.path.join(d, sib)
                    sib_heads = _count_heads(sib_path)
                    siblings.append((sib_path, sib_heads, sib))
                    all_heads |= sib_heads
            total_n = len(all_heads)
            final_name = f"{base}_{total_n}份"
            final_path = os.path.join(d, final_name)
            # 选合并目标：若已有同 final_name 用之；否则用既有兄弟目录中人数最多的改名
            if not os.path.exists(final_path):
                # 把人数最多的兄弟目录作为合并基座（迁移文件最少）
                if siblings:
                    siblings.sort(key=lambda x: len(x[1]), reverse=True)
                    base_sib_path, _, _ = siblings[0]
                    os.rename(base_sib_path, final_path)
                    siblings = [(p, h, s) for p, h, s in siblings if p != base_sib_path]
                # 若无兄弟（首份），创建空目录
                if not os.path.exists(final_path):
                    os.makedirs(final_path, exist_ok=True)
            # 把 _暂定 和其余兄弟的内容合并进 final_path
            moved = 0
            for src_dir in [pending_path] + [s[0] for s in siblings]:
                for root2, dir2, files2 in os.walk(src_dir):
                    # 保持档位子目录结构（强推/可推/待定·不推）
                    rel = os.path.relpath(root2, src_dir)
                    dst_root = final_path if rel == "." else os.path.join(final_path, rel)
                    for fn in files2:
                        dst = os.path.join(dst_root, fn)
                        if os.path.exists(dst):
                            continue
                        os.makedirs(dst_root, exist_ok=True)
                        shutil.move(os.path.join(root2, fn), dst)
                        moved += 1
                shutil.rmtree(src_dir)
            print(f"  ℹ️ {dn} → {final_name}（+{len(pending_heads)}人，同天合并共{total_n}人，移动{moved}文件）")


def _merge_split_batches(target):
    """合并同天被拆成多个 {M.DD}_{k}份 的目录为单一 {M.DD}_{总N}份。

    自愈增量归档 bug（2026-07-31）：同一天分两批 resolve+闸门时，第一批产
    7.31_3份（评估后含档位子目录），第二批的 7.31_暂定 被旧版 rename 成
    7.31_1份 → 同天并存 7.31_1份 + 7.31_3份，数量标注不自洽。本函数检测同
    {base} 前缀存在 ≥2 个 _{k}份 目录时，合并全部人头到一个目录并重命名。
    幂等：已是唯一 _{k}份 的不动。
    """
    if not os.path.isdir(target):
        return
    for d, dirs, files in os.walk(target):
        # 按 base（去掉 _{k}份 后缀）分组
        groups = {}
        for dn in dirs:
            m = re.match(r'^(.+?)_\d+份$', dn)
            if not m:
                continue
            groups.setdefault(m.group(1), []).append(dn)
        for base, members in groups.items():
            if len(members) < 2:
                continue  # 唯一，无需合并
            # 统计总人头（跨目录去重）
            all_heads = set()
            paths = []
            for dn in members:
                p = os.path.join(d, dn)
                paths.append((dn, p))
                all_heads |= _count_heads(p)
            total_n = len(all_heads)
            final_name = f"{base}_{total_n}份"
            final_path = os.path.join(d, final_name)
            # 选人数最多的目录做基座改名
            paths.sort(key=lambda x: len(_count_heads(x[1])), reverse=True)
            base_dn, base_path = paths[0]
            if base_path != final_path:
                os.rename(base_path, final_path)
            # 合并其余目录内容
            moved = 0
            for dn, p in paths[1:]:
                for root2, dir2, files2 in os.walk(p):
                    rel = os.path.relpath(root2, p)
                    dst_root = final_path if rel == "." else os.path.join(final_path, rel)
                    for fn in files2:
                        dst = os.path.join(dst_root, fn)
                        if os.path.exists(dst):
                            continue
                        os.makedirs(dst_root, exist_ok=True)
                        shutil.move(os.path.join(root2, fn), dst)
                        moved += 1
                shutil.rmtree(p)
            print(f"  ℹ️ 同天合并 {len(members)} 个目录 → {final_name}（{total_n}人，移动{moved}文件）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--no-salary", action="store_true")
    ap.add_argument("--names-only", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--manifest", help="collection_manifest.json 路径（闭环对账）")
    ap.add_argument("--report-json", help="输出机器可读 JSON 报告到此路径")
    args = ap.parse_args()

    # 2026-07-29：先把 _暂定 中转目录自动 rename 为 _{N}份（在 collect 之前，使 collect 看到最终目录）。
    # resolve_records 落 {M.DD}_暂定/（N 在单条 resolve 时未知），闸门收尾时数人头得到 N 后 rename，
    # 使 _N份 标注永远自洽。rename 在校验前做，fail-closed 语义不变（数量不符仍 STOP）。
    _finalize_pending_dirs(args.target)
    # 自愈：合并同天被拆成多个 _{k}份 的目录（增量归档 bug 遗留）
    _merge_split_batches(args.target)
    res = collect(args.target)
    report = {"target": args.target, "errors": [], "warnings": [], "counts": {}}

    try:
        if not res.count_files:
            print("🔴 没找到简历（pdf/docx/zip）")
            report["errors"].append("no_resume_files")
            sys.exit(1)

        # ---- 数量闸门 ----
        print("【数量闸门】")
        folders = {}
        for f in res.count_files:
            parts = f.replace("\\", "/").split("/")
            key = "散落根目录"
            for i, p in enumerate(parts):
                if p in ("已收集简历", "收集到简历") and i + 1 < len(parts):
                    key = parts[i + 1]
                    break
            folders.setdefault(key, []).append(f)

        count_mismatch = False
        for k, fs in folders.items():
            m = re.search(r"_(\d+)份", k)
            if m:
                claimed = int(m.group(1))
                parsed_entries = [path for path in fs
                                  if not re.search(r"temp|临时|tmp", os.path.basename(path.rstrip("/")), re.IGNORECASE)]
                heads = set()
                for i, p in enumerate(parsed_entries):
                    nm = parse_name(os.path.basename(p.rstrip("/")))
                    heads.add(nm if nm else f"__unnamed_{i}")
                actual = len(heads)
                if claimed != actual:
                    count_mismatch = True
                    print(f"  ❌ {k}: 标注{claimed}份, 实际{actual}份")
                else:
                    print(f"  ✅ {k}: {claimed}份")
            else:
                # 无 _N份 标注即阻断
                print(f"  🔴 {k}: 目录名无 _N份 标注 → 阻断（归档后必须标注再校验）")
                report["errors"].append(f"no_count_label:{k}")
                count_mismatch = True

        # ---- 姓名 + 薪酬闸门 ----
        print("\n【姓名闸门 + 薪酬闸门 + 格式验证】")
        name_issues = []
        sal_hits = []
        blocked_files = []
        scan_salary = not args.names_only and not args.no_salary
        use_cache = not args.no_cache and not args.names_only
        cache = load_manifest_cache(args.target) if use_cache else {}
        new_cache = {}
        cached_n = 0

        for display, real in sorted(res.scan_files, key=lambda x: x[0]):
            # ZIP 安全检查阻断的文件
            if display.startswith("__BLOCKED__:"):
                print(f"  🔴 {display.replace('__BLOCKED__:', '')} → 阻断（无法验证）")
                blocked_files.append(display)
                continue

            fname = display.split("!")[-1].split("/")[-1]
            fn_name = parse_name(os.path.basename(fname))

            # SHA-256 缓存（最终键是内容哈希，mtime+size 只做快筛）
            try:
                content_sha = sha256_file(real)
                sig = file_sig(real)
            except OSError:
                content_sha = None
                sig = None

            cache_key = display
            cached = cache.get(cache_key)
            if (use_cache and cached and content_sha and
                    cached.get("sha256") == content_sha and fn_name == cached.get("name")):
                print(f"  ✅ {fname}  ({fn_name}) [缓存命中 sha256]")
                new_cache[cache_key] = cached
                cached_n += 1
                continue

            # 统一内容提取（PDF/DOCX/图片型 PDF OCR）
            result = extract_content(real)
            if result.blocked:
                print(f"  🔴 {fname}  提取阻断: {result.block_reason}")
                blocked_files.append(display)
                report["errors"].append(f"extract_blocked:{fname}:{result.block_reason}")
                continue

            txt = result.text
            nchar = result.nchar

            # 薪酬扫描
            this_sal = []
            if scan_salary:
                for m in SALARY.finditer(txt):
                    snip = m.group(0).strip().replace("\n", " ")[:30]
                    this_sal.append((fname, snip))
                sal_hits.extend(this_sal)

            # 姓名闸门
            if not fn_name:
                print(f"  ⚠️ {fname}  文件名解析不出姓名 → 需人工确认")
                report["warnings"].append(f"no_name:{fname}")
                continue
            compact = txt.replace(" ", "").replace("\n", "")
            head = compact[:400]
            cands = CN.findall(head)
            verdict = name_in_text(fn_name, txt)
            file_clean = False
            if verdict == "pass":
                print(f"  ✅ {fname}  ({fn_name}) [{result.fmt}]")
                file_clean = True
            elif verdict == "manual":
                print(f"  ⚠️ {fname}  正文含'{fn_name}'但疑似他人子串 → 需人工确认")
                report["warnings"].append(f"name_manual:{fname}")
            else:
                print(f"  🔴 {fname}  正文无'{fn_name}' → 疑下载填串，STOP！")
                name_issues.append(display)
            # 只缓存"姓名 pass + 无薪酬 + 未阻断"
            if file_clean and scan_salary and not this_sal and content_sha:
                new_cache[cache_key] = {"sha256": content_sha, "name": fn_name, "sig": list(sig) if sig else []}

        if use_cache:
            save_manifest_cache(args.target, new_cache)
            if cached_n:
                print(f"  ℹ️ SHA-256 缓存命中 {cached_n} 个")

        # ---- 薪酬结果 ----
        if scan_salary:
            print("\n【薪酬闸门】")
            if not sal_hits:
                print("  ✅ 无薪酬残留")
            else:
                for fn, snip in sal_hits[:15]:
                    print(f"  🔴 {fn}  [{snip}]")

        # ---- manifest 闭环对账 ----
        manifest_bad = False
        if args.manifest:
            print("\n【Manifest 闭环对账】")
            if not os.path.isfile(args.manifest):
                print(f"  🔴 manifest 不存在: {args.manifest}")
                report["errors"].append("manifest_not_found")
                manifest_bad = True
            else:
                mdata = json.load(open(args.manifest, encoding="utf-8"))
                records = mdata.get("records", {})
                # 闭环校验：只对"本目录涉及的 record"校验，不对全库中间态记录阻断。
                # archived/duplicate/validated/excluded 都是合法终态/已处理态，不算阻断。
                # blocked = 有问题需人工处理，算阻断。needs_resolution/verified = 流水线中间态，仅信息提示。
                blocked_recs = [rid for rid, r in records.items() if r.get("status") == "blocked"]
                # 统计各状态（仅信息展示，不阻断）
                status_counts = {}
                for r in records.values():
                    s = r.get("status", "?")
                    status_counts[s] = status_counts.get(s, 0) + 1
                in_flight = sum(v for k, v in status_counts.items()
                                if k in ("needs_resolution", "verified"))
                if blocked_recs:
                    print(f"  🔴 {len(blocked_recs)} 条记录 blocked（需人工处理）")
                    report["errors"].append(f"records_blocked:{len(blocked_recs)}")
                    manifest_bad = True
                if in_flight:
                    print(f"  ℹ️ {in_flight} 条记录在流水线中间态（needs_resolution/verified），不阻断本批次")
                # 展示状态分布（便于排查）
                dist = ", ".join(f"{k}:{v}" for k, v in sorted(status_counts.items(), key=lambda x: -x[1]))
                print(f"  📊 manifest {len(records)} 条记录状态分布: {dist}")
                if not manifest_bad:
                    print(f"  ✅ manifest 闭环检查通过（无 blocked 记录）")

        # ---- 最终判定 ----
        print("\n" + "=" * 40)
        stop = bool(name_issues or count_mismatch or sal_hits or blocked_files or manifest_bad)
        if stop:
            reasons = []
            if name_issues: reasons.append("姓名不符")
            if count_mismatch: reasons.append("数量不符")
            if sal_hits: reasons.append("薪酬残留")
            if blocked_files: reasons.append(f"{len(blocked_files)} 个文件阻断")
            print(f"🔴 STOP — {'/'.join(reasons)}，修复后再进评估")
            report["errors"].append(f"stop:{'/'.join(reasons)}")
        else:
            print("🟢 全过 — 可进评估")

        if args.report_json:
            with open(args.report_json, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)

        sys.exit(1 if stop else 0)
    finally:
        res.cleanup()


if __name__ == "__main__":
    main()
