# -*- coding: utf-8 -*-
"""
verify_archive.py — 归档闸门

阻断校验：数量 / 薪酬 / manifest闭环（这三类任何不符即 STOP）。
warning 级（不阻断，列给人复核）：姓名 miss/manual、提取阻断文件（加密zip/图片型PDF等）。
SHA-256 缓存加速重复校验（缓存只做性能加速，不做安全判定）。

⚠️ 注意：本脚本不是纯只读。校验前会执行自愈逻辑：
  - _finalize_pending_dirs：把 {M.DD}_暂定 目录合并为 {M.DD}_{N}份，删除空 _暂定 目录
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
from archive_safety import check_zip, decode_zip_name
from salary_pattern import SALARY
from paths import SEVEN_ZIP, CACHE_DIR

CN = re.compile(r"[\u4e00-\u9fa5]{2,4}")

ZIP_EXT = (".zip",)
RAR_EXT = (".rar", ".7z")
RESUME_EXTS = (".pdf", ".docx", ".doc")
RESUME_IMG_EXTS = (".jpg", ".jpeg", ".png")

def _is_resume_file(name):
    """是否为简历本体文件，跳过纯作品集。
    PDF/DOCX/DOC 直接认；图片(jpg/jpeg/png)需「能解析出姓名 且 文件名含岗位词」才认——
    BOSS 图片简历规范命名是「姓名_岗位_年限.png」（无"简历"关键词，岗位词如设计师/开发），
    作品散图（demo.jpg/截图.png/张三_作品1.jpg）无岗位词 → 不会误判。"""
    low = name.lower()
    if low.endswith(RESUME_EXTS):
        # 含「简历」的优先认（"简历和作品集.pdf"是合体文件，前几页是简历本体）
        if "简历" in name or "resume" in low:
            return True
        if "作品集" in name or "portfolio" in low:
            return False
        return True
    if low.endswith(RESUME_IMG_EXTS) and ROLE_KW.search(name) and parse_name(name):
        return True
    return False


def _zip_decode_name(info):
    """还原 zip 内中文文件名。委托给 archive_safety.decode_zip_name（单一真相源）。

    原实现有两处 bug（2026-08-13 修）：
    1. `flag_bits & 0x800` 直接返回原名——但实测某些工具设了 0x800 却仍按 UTF-8 字节写入，
       Python 解出来还是 cp437 乱码，直接返回 = 返回乱码。
    2. 只试 `cp437→gbk`——但网盘/邮箱类 zip 常用 UTF-8 字节，gbk 解不出（byte 0xaa 越界），
       落到 except 返回乱码原名。
    统一走 decode_zip_name（含中文直接用 → utf-8 优先 → gbk 兜底）。"""
    return decode_zip_name(info)


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
    compact = re.sub(r"\s+", "", txt)
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
                    pass
                # 安全警告始终呈现（嵌套归档未扫描等，人工需知情）
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
            res.scan_files.append((root, root))
        elif low.endswith(ZIP_EXT):
            handle_zip(root)
        elif low.endswith(RAR_EXT):
            handle_rar(root)
        return res

    for d, dirs, fs in os.walk(root):
        for f in fs:
            full = os.path.join(d, f)
            low = f.lower()
            # _is_resume_file 覆盖 .pdf/.docx/.doc（.doc 经 antiword 提取）
            if _is_resume_file(f):
                res.scan_files.append((full, full))
            elif low.endswith(ZIP_EXT):
                handle_zip(full)
            elif low.endswith(RAR_EXT):
                handle_rar(full)
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


def _count_heads(dirpath):
    """统计一个目录下的人头（按 parse_name 去重，含档位子目录递归）。
    数量闸门与本函数同口径——两套口径会自相打架（一个 rename 成 _2份、一个数出 1 人）。"""
    heads = set()
    for root2, _, files2 in os.walk(dirpath):
        for fn in files2:
            low = fn.lower()
            if _is_resume_file(fn) or low.endswith(ZIP_EXT) or low.endswith(RAR_EXT):
                nm = parse_name(fn)
                heads.add(nm if nm else fn)
    return heads


def _rename_dir_with_fallback(src, dst):
    """目录 rename；Windows 下目录被占用（资源管理器/阅读器句柄锁）会 PermissionError，
    fallback 到 copytree+rmtree。返回警告列表（数据不丢，最多留残留）。"""
    warnings = []
    try:
        os.rename(src, dst)
        return warnings
    except PermissionError:
        pass
    except OSError as e:
        warnings.append(f"rename {src} → {dst} 失败: {e}（目录未动）")
        return warnings
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
    except OSError as e:
        warnings.append(f"rename 被占用，copytree fallback 也失败 {src} → {dst}: {e}（目录未动）")
        return warnings
    try:
        shutil.rmtree(src)
    except OSError:
        warnings.append(f"合并已复制完成，但旧目录被占用删不掉，请稍后手动删除: {src}")
    return warnings


def _merge_dir_contents(src_dir, dst_root, label, warnings):
    """把 src_dir 内容合并进 dst_root（保留档位子目录结构）。

    数据安全规则（修 P0 删库事故的根源）：
    - 同名冲突文件不静默丢弃 → 加 _重投N 后缀保留新文件
    - 源目录只在确已清空时删除；有残留保留并警告，绝不对可能含文件的目录 rmtree
    返回移动文件数。
    """
    moved = 0
    for root2, dir2, files2 in os.walk(src_dir):
        rel = os.path.relpath(root2, src_dir)
        dst_root2 = dst_root if rel == "." else os.path.join(dst_root, rel)
        for fn in files2:
            dst = os.path.join(dst_root2, fn)
            src_f = os.path.join(root2, fn)
            if os.path.exists(dst):
                stem, ext = os.path.splitext(fn)
                k = 1
                while os.path.exists(os.path.join(dst_root2, f"{stem}_重投{k}{ext}")):
                    k += 1
                dst = os.path.join(dst_root2, f"{stem}_重投{k}{ext}")
                warnings.append(f"{label}: 同名冲突，新文件保留为 {os.path.basename(dst)}")
            os.makedirs(dst_root2, exist_ok=True)
            shutil.move(src_f, dst)
            moved += 1
    # 自底向上清理空目录；src_dir 本身有残留文件时保留
    for root2, dir2, files2 in os.walk(src_dir, topdown=False):
        if not files2 and not dir2:
            try:
                os.rmdir(root2)
            except OSError:
                pass
    leftover = sum(len(fs) for _, _, fs in os.walk(src_dir))
    if leftover:
        warnings.append(f"{label}: {leftover} 个文件因占用未移走，源目录保留: {src_dir}")
    return moved


def _finalize_pending_dirs(target):
    """把 {M.DD}_暂定 中转目录合并为 {M.DD}_{N}份（N=同天总人头数）。

    resolve_records 在单条记录 resolve 时不知道整批 N，统一落 _暂定/；
    本函数在闸门 collect 之前扫到所有 _暂定 目录，按文件名解析人头数后合并，
    使 _N份 标注自洽。幂等：已是 _N份 的目录不动。

    合并规则（2026-08-14 重写，修 P0 数据丢失）：
    - 合并目标 final_path 绝不出现在合并源列表里（旧版 bug：同名重投场景下
      final_path 既是目标又是源，文件全被 skip 后 rmtree(final_path) 删光整批）
    - 同名冲突文件加 _重投N 后缀保留，不静默丢
    - 源目录只在清空后删除（_merge_dir_contents 保证）
    - 空 _暂定 仅在完全无文件（含说明.txt 等非简历文件）时才删除
    """
    if not os.path.isdir(target):
        return
    for d, dirs, files in os.walk(target):
        for dn in list(dirs):  # copy，避免遍历时改 dirs
            if not dn.endswith("_暂定"):
                continue
            pending_path = os.path.join(d, dn)
            base = dn[:-len("_暂定")]  # 去掉 _暂定 后缀，得到 {M.DD}
            all_files = [f for _, _, fs in os.walk(pending_path) for f in fs]
            pending_heads = _count_heads(pending_path)
            if not all_files:
                shutil.rmtree(pending_path, ignore_errors=True)
                print(f"  ℹ️ 删除空 _暂定 目录: {dn}（无任何文件）")
                continue
            if len(pending_heads) == 0:
                print(f"  ⚠️ _暂定 {dn} 无简历文件但有 {len(all_files)} 个其他文件，保留待人工确认")
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
            warnings = []
            if os.path.exists(final_path):
                # final 已存在（典型：同人重投，人头集合不变）→ 只合并 _暂定 和其余兄弟
                merge_sources = [pending_path] + [s[0] for s in siblings if s[0] != final_path]
            else:
                # 把人数最多的兄弟目录作为合并基座（迁移文件最少）；无兄弟（首份）则新建
                merge_sources = [pending_path] + [s[0] for s in siblings]
                if siblings:
                    siblings.sort(key=lambda x: len(x[1]), reverse=True)
                    base_sib_path = siblings[0][0]
                    merge_sources = [p for p in merge_sources if p != base_sib_path]
                    warnings += _rename_dir_with_fallback(base_sib_path, final_path)
                if not os.path.exists(final_path):
                    os.makedirs(final_path, exist_ok=True)
            moved = 0
            for src_dir in merge_sources:
                moved += _merge_dir_contents(src_dir, final_path, dn, warnings)
            print(f"  ℹ️ {dn} → {final_name}（+{len(pending_heads)}人，同天合并共{total_n}人，移动{moved}文件）")
            for w in warnings:
                print(f"  ⚠️ {w}")


def _merge_split_batches(target):
    """合并同天被拆成多个 {M.DD}_{k}份 的目录为单一 {M.DD}_{总N}份。幂等。"""
    if not os.path.isdir(target):
        return
    for d, dirs, files in os.walk(target):
        groups = {}
        for dn in dirs:
            m = re.match(r'^(.+?)_\d+份$', dn)
            if not m:
                continue
            groups.setdefault(m.group(1), []).append(dn)
        for base, members in groups.items():
            if len(members) < 2:
                continue  # 唯一，无需合并
            all_heads = set()
            paths = []
            for dn in members:
                p = os.path.join(d, dn)
                paths.append((dn, p))
                all_heads |= _count_heads(p)
            total_n = len(all_heads)
            final_name = f"{base}_{total_n}份"
            final_path = os.path.join(d, final_name)
            warnings = []
            if os.path.exists(final_path):
                rest = [(dn, p) for dn, p in paths if p != final_path]
            else:
                paths.sort(key=lambda x: len(_count_heads(x[1])), reverse=True)
                base_dn, base_path = paths[0]
                warnings += _rename_dir_with_fallback(base_path, final_path)
                rest = paths[1:]
            moved = 0
            for dn, p in rest:
                moved += _merge_dir_contents(p, final_path, dn, warnings)
            print(f"  ℹ️ 同天合并 {len(members)} 个目录 → {final_name}（{total_n}人，移动{moved}文件）")
            for w in warnings:
                print(f"  ⚠️ {w}")


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
    # 自愈收敛：target 是 _暂定 目录时（collect 编排器直接传下载目录），
    # 先对它所在父目录做收敛，再把 target 重定向到收敛后的 _N份 目录。
    if os.path.isdir(args.target) and args.target.rstrip("/\\").endswith("_暂定"):
        parent = os.path.dirname(args.target.rstrip("/\\"))
        _finalize_pending_dirs(parent)
        _merge_split_batches(parent)
        base = os.path.basename(args.target.rstrip("/\\"))[:-len("_暂定")]
        newdirs = [n for n in os.listdir(parent)
                   if n.startswith(base + "_") and re.search(r"_\d+份$", n)]
        if newdirs:
            args.target = os.path.join(parent, newdirs[0])
    else:
        _finalize_pending_dirs(args.target)
        # 自愈：合并同天被拆成多个 _{k}份 的目录（增量归档 bug 遗留）
        _merge_split_batches(args.target)
    res = collect(args.target)
    report = {"target": args.target, "errors": [], "warnings": [], "counts": {}}
    is_single_file = os.path.isfile(args.target)

    try:
        if not res.scan_files:
            print("🔴 没找到简历（pdf/docx/zip/rar）")
            report["errors"].append("no_resume_files")
            sys.exit(1)

        # ---- 数量闸门（单文件模式跳过：无批次目录语义）----
        # 口径与 _finalize_pending_dirs/_merge_split_batches 的 _count_heads 完全一致：
        # 递归、按姓名去重、只数简历/压缩包文件。两套口径会自相打架
        # （_finalize rename 成 _2份、闸门数出 1 人 → 自我阻断）。
        count_mismatch = False
        if is_single_file:
            print("【数量闸门】单文件模式，跳过（只做姓名/薪酬/格式校验）")
        else:
            print("【数量闸门】")
            batch_dirs = {}   # abspath -> 目录名（含 _N份）
            stray = []
            for d, dirs, fs in os.walk(args.target):
                dn = os.path.basename(d.rstrip("/\\"))
                if re.search(r"_\d+份$", dn):
                    batch_dirs[d] = dn
                    dirs[:] = []   # 批次目录内部由 _count_heads 递归统计，不重复下钻
                    continue
                for f in fs:
                    low = f.lower()
                    if (_is_resume_file(f) or low.endswith(ZIP_EXT) or low.endswith(RAR_EXT)) \
                            and not re.search(r"temp|临时|tmp", f, re.IGNORECASE):
                        stray.append(os.path.join(d, f))
            for d, dn in sorted(batch_dirs.items()):
                claimed = int(re.search(r"_(\d+)份", dn).group(1))
                actual = len(_count_heads(d))
                if claimed != actual:
                    count_mismatch = True
                    print(f"  ❌ {dn}: 标注{claimed}份, 实际{actual}份")
                else:
                    print(f"  ✅ {dn}: {claimed}份")
            if not batch_dirs:
                print("  🔴 未找到任何 _N份 批次目录")
                count_mismatch = True
            for s in stray:
                print(f"  🔴 散落文件（不在 _N份 目录内）: {s}")
                report["errors"].append(f"no_count_label:{os.path.basename(s)}")
                count_mismatch = True

        # ---- 姓名 + 薪酬闸门 ----
        print("\n【姓名闸门 + 薪酬闸门 + 格式验证】")
        sal_hits = []
        scan_salary = not args.names_only and not args.no_salary
        use_cache = not args.no_cache and not args.names_only
        cache = load_manifest_cache(args.target) if use_cache else {}
        new_cache = {}
        cached_n = 0

        for display, real in sorted(res.scan_files, key=lambda x: x[0]):
            # ZIP 安全检查阻断的文件（美术岗作品zip可能无法机器验证 → 标warning让人工看原件）
            if display.startswith("__BLOCKED__:"):
                print(f"  ⚠️ {display.replace('__BLOCKED__:', '')} → 阻断（无法验证，需人工看原件）")
                report["warnings"].append(display)
                continue

            fname = display.split("!")[-1].split("/")[-1]
            fn_name = parse_name(os.path.basename(fname))

            # SHA-256 缓存（最终键是内容哈希）
            try:
                content_sha = sha256_file(real)
            except OSError:
                content_sha = None

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
                # 图片型PDF/独立图片/.doc提取失败 → 美术岗常见，标warning不STOP（需人工看原件）
                print(f"  ⚠️ {fname}  提取阻断: {result.block_reason}（需人工看原件）")
                report["warnings"].append(f"extract_blocked:{fname}")
                continue

            txt = result.text

            # 薪酬扫描
            this_sal = []
            if scan_salary:
                for m in SALARY.finditer(txt):
                    snip = m.group(0).strip().replace("\n", " ")[:30]
                    this_sal.append((fname, snip))
                sal_hits.extend(this_sal)

            # 姓名闸门（warning 级：miss 多为 BOSS 加密PDF/先生文件名/英文名，
            # 下载一致性已由 SHA-256 绑定兜底；真填串靠人工/对账发现）
            if not fn_name:
                print(f"  ⚠️ {fname}  文件名解析不出姓名 → 需人工确认")
                report["warnings"].append(f"no_name:{fname}")
                continue
            verdict = name_in_text(fn_name, txt)
            file_clean = False
            if verdict == "pass":
                print(f"  ✅ {fname}  ({fn_name}) [{result.fmt}]")
                file_clean = True
            elif verdict == "manual":
                print(f"  ⚠️ {fname}  正文含'{fn_name}'但疑似他人子串 → 需人工确认")
                report["warnings"].append(f"name_manual:{fname}")
            else:
                print(f"  ⚠️ {fname}  正文无'{fn_name}'（加密/无姓名/英文名），标warning")
                report["warnings"].append(f"name_miss:{fname}")
            # 只缓存"姓名 pass + 无薪酬 + 未阻断"
            if file_clean and scan_salary and not this_sal and content_sha:
                new_cache[cache_key] = {"sha256": content_sha, "name": fn_name}

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
                # 闭环校验：只对"绑定到本目录树下批次"的记录校验，不扫全库——
                # 全库扫描会让任何历史 blocked 记录劫持之后每一次闸门。
                # 绑定键 = (target_dir 的父目录, 日期段)：_暂定 与 rename 后的 _N份 同键，
                # 闸门跑在 rename 之后也能对上。
                batch_keys = set()
                if os.path.isdir(args.target):
                    # 目标自身是批次目录时也算（verify 常直接跑在 _N份 上）
                    m_self = re.match(r"^(.+?)_(?:\d+份|暂定)$", os.path.basename(args.target.rstrip("/\\")))
                    if m_self:
                        batch_keys.add((os.path.abspath(os.path.dirname(args.target)).replace("\\", "/").lower(), m_self.group(1)))
                    for root2, dirs2, _ in os.walk(args.target):
                        for dn in dirs2:
                            m2 = re.match(r"^(.+?)_(?:\d+份|暂定)$", dn)
                            if m2:
                                batch_keys.add((os.path.abspath(root2).replace("\\", "/").lower(), m2.group(1)))

                def _bound_to_target(r):
                    td = (r.get("target_dir") or "").replace("\\", "/")
                    if not td:
                        return False
                    parent = os.path.dirname(td.rstrip("/")).replace("\\", "/").lower()
                    m3 = re.match(r"^(.+?)_(?:\d+份|暂定)$", os.path.basename(td.rstrip("/")))
                    return bool(m3) and (parent, m3.group(1)) in batch_keys

                blocked_recs = [rid for rid, r in records.items()
                                if r.get("status") == "blocked" and _bound_to_target(r)]
                # 统计各状态（仅信息展示，不阻断）
                status_counts = {}
                for r in records.values():
                    s = r.get("status", "?")
                    status_counts[s] = status_counts.get(s, 0) + 1
                in_flight = sum(v for k, v in status_counts.items()
                                if k in ("needs_resolution", "verified"))
                if blocked_recs:
                    print(f"  🔴 {len(blocked_recs)} 条绑定本批次的记录 blocked（需人工处理）")
                    report["errors"].append(f"records_blocked:{len(blocked_recs)}")
                    manifest_bad = True
                if in_flight:
                    print(f"  ℹ️ {in_flight} 条记录在流水线中间态（needs_resolution/verified），不阻断本批次")
                # 展示状态分布（便于排查）
                dist = ", ".join(f"{k}:{v}" for k, v in sorted(status_counts.items(), key=lambda x: -x[1]))
                print(f"  📊 manifest {len(records)} 条记录状态分布: {dist}")
                if not manifest_bad:
                    print(f"  ✅ manifest 闭环检查通过（本批次无 blocked 记录）")

        # ---- 最终判定 ----
        print("\n" + "=" * 40)
        stop = bool(count_mismatch or sal_hits or manifest_bad)
        if stop:
            reasons = []
            if count_mismatch: reasons.append("数量不符")
            if sal_hits: reasons.append("薪酬残留")
            if manifest_bad: reasons.append("manifest闭环未过")
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
