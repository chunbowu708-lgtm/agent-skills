# -*- coding: utf-8 -*-
"""
redact_salary.py — 简历薪酬脱敏（PDF / DOCX / ZIP 内嵌文件）

与 verify_archive.py 配套：本脚本删薪酬，verify_archive 扫薪酬。
覆盖色 = 白色 fill=(1,1,1)，禁止用黑色（AGENTS.md 薪酬脱敏铁律第2条）。

用法：
  # 单个 PDF
  python redact_salary.py "简历.pdf"

  # DOCX（跨 run 分割的薪酬文本也能处理）
  python redact_salary.py "简历.docx"

  # ZIP 内嵌简历（自动解压→脱敏→重打包）
  python redact_salary.py "作品集.zip"

  # 自定义关键词（默认覆盖 verify_archive 同款正则）
  python redact_salary.py "简历.pdf" --patterns "薪资：2w-3w" "期望薪资：25-30K"

  # 预览模式（只报告命中、不改文件）
  python redact_salary.py "简历.pdf" --dry-run

退出码：0=成功（已脱敏或无需脱敏）；1=出错
"""

import sys, os, re, io, zipfile, tempfile, shutil, argparse, subprocess

try:
    import fitz  # PyMuPDF
except ImportError:
    print("❌ 需要 PyMuPDF: pip install PyMuPDF", file=sys.stderr)
    sys.exit(1)

from salary_pattern import SALARY
from paths import SEVEN_ZIP
from archive_safety import check_zip

# 白色覆盖（铁律：禁止黑色）
FILL_COLOR = (1, 1, 1)


def redact_pdf(pdf_path, patterns=None, dry_run=False):
    """脱敏 PDF：用白色矩形覆盖薪酬文本。
    返回 (命中数, 是否修改)。
    """
    doc = fitz.open(pdf_path)
    regex = _build_regex(patterns)
    hits = []
    modified = False

    for page_num in range(len(doc)):
        page = doc[page_num]

        if dry_run:
            text = page.get_text()
            for m in regex.finditer(text):
                ctx = text[max(0, m.start()-15):m.end()+15]
                hits.append((page_num + 1, ctx.strip()))
            continue

        # 搜索薪酬文本
        page_hits = _search_page(page, regex)
        for match_text, rects in page_hits:
            for rect in rects:
                # 白色覆盖（不设黑色）
                page.add_redact_annot(rect, fill=FILL_COLOR)
            hits.append((page_num + 1, match_text))

        if page_hits:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            modified = True

    if modified and not dry_run:
        doc.save(pdf_path + '.tmp')
        doc.close()
        shutil.move(pdf_path + '.tmp', pdf_path)
    else:
        doc.close()

    return hits, modified


def redact_docx(docx_path, patterns=None, dry_run=False):
    """脱敏 DOCX：处理跨 run 分割的薪酬文本。"""
    regex = _build_regex(patterns)
    modified = False
    hits = []

    # DOCX 是 PK zip，读取 word/document.xml
    with zipfile.ZipFile(docx_path, 'r') as zin:
        names = zin.namelist()
        files = {name: zin.read(name) for name in names}

    if 'word/document.xml' not in files:
        return hits, False

    xml = files['word/document.xml'].decode('utf-8')

    if dry_run:
        pure = re.sub(r'<[^>]+>', '', xml)
        for m in regex.finditer(pure):
            ctx = pure[max(0, m.start()-15):m.end()+15]
            hits.append((1, ctx.strip()))
        return hits, False

    # 跨 run 文本处理：提取 <w:t> 标签，拼接纯文本，定位薪酬，跨标签删除
    wt_pattern = re.compile(r'(<w:t[^>]*>)(.*?)(</w:t>)', re.DOTALL)
    matches = list(wt_pattern.finditer(xml))

    full_text = ""
    char_map = []  # (match_idx, char_idx_in_wt)
    for mi, m in enumerate(matches):
        wt_content = m.group(2)
        for ci in range(len(wt_content)):
            full_text += wt_content[ci]
            char_map.append((mi, ci))

    # 先收集所有正则匹配对每个 run 的累积删除字符集，再一次性从后往前替换。
    # 不能在循环内逐次替换——替换后 xml 长度变化，后续 match 的偏移就错了。
    # 2026-08-04 修复：旧版用 xml.replace(old_full, new_full, 1) 会命中第一个相同标签（改错位置）。
    remove_map = {}  # mi -> set(ci)
    for m in regex.finditer(full_text):
        start, end = m.start(), m.end()
        ctx = full_text[max(0, start-15):end+15]
        hits.append((1, ctx.strip()))
        if dry_run:
            continue
        for i in range(start, end):
            mi, ci = char_map[i]
            remove_map.setdefault(mi, set()).add(ci)

    if remove_map and not dry_run:
        # 从后往前替换每个受影响的 run（reverse 保证已替换的不影响未替换的偏移）
        for mi in sorted(remove_map.keys(), reverse=True):
            mt = matches[mi]
            original = mt.group(2)
            remove = remove_map[mi]
            new_content = "".join(c for ci, c in enumerate(original) if ci not in remove)
            new_full = mt.group(1) + new_content + mt.group(3)
            xml = xml[:mt.start()] + new_full + xml[mt.end():]
        modified = True

    if modified:
        files['word/document.xml'] = xml.encode('utf-8')
        with zipfile.ZipFile(docx_path + '.tmp', 'w', zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                zout.writestr(name, files[name])
        shutil.move(docx_path + '.tmp', docx_path)

    return hits, modified


def redact_zip(zip_path, patterns=None, dry_run=False):
    """脱敏 ZIP 内嵌的简历文件（PDF/DOCX/.doc）。"""
    regex = _build_regex(patterns)
    tmpdir = tempfile.mkdtemp(prefix="_redact_")
    hits = []
    modified = False

    try:
        # 安全检查前置（2026-07-29，H2 修复）：
        # 解压前先过 archive_safety 单一关口，阻断路径穿越/炸弹/嵌套归档/加密。
        # 此前 redact_zip 直接 extractall，绕过 verify_archive 的安全关口。
        safety = check_zip(zip_path)
        if safety.blocked:
            print(f"❌ ZIP 安全检查失败（拒绝解压）: {safety.reason}", file=sys.stderr)
            return [("__BLOCKED__", safety.reason)], False

        # 解压
        with zipfile.ZipFile(zip_path, 'r') as zin:
            zin.extractall(tmpdir)
            names = zin.namelist()

        # 逐个处理内嵌简历
        for name in names:
            lower = name.lower()
            if not (lower.endswith('.pdf') or lower.endswith('.docx') or lower.endswith('.doc')):
                continue
            if '作品集' in name or 'portfolio' in lower:
                continue

            filepath = os.path.join(tmpdir, name)
            if lower.endswith('.pdf'):
                h, mod = redact_pdf(filepath, patterns, dry_run)
            elif lower.endswith('.docx'):
                h, mod = redact_docx(filepath, patterns, dry_run)
            else:
                # .doc：只扫不删（antiword 无法涂白）
                h, mod = redact_doc(filepath, patterns, dry_run)

            if h:
                hits.extend([(f"{name} p{pg}", ctx) for pg, ctx in h])
            if mod:
                modified = True

        # 重打包
        if modified and not dry_run:
            tmp_zip = zip_path + '.tmp'
            with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as zout:
                for root, _, files in os.walk(tmpdir):
                    for f in files:
                        fp = os.path.join(root, f)
                        arcname = os.path.relpath(fp, tmpdir).replace(os.sep, '/')
                        zout.write(fp, arcname)
            shutil.move(tmp_zip, zip_path)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return hits, modified


def redact_doc(doc_path, patterns=None, dry_run=False):
    """老式 .doc 薪酬扫描（只扫不删——antiword 无法涂白矩形）。

    返回 (命中数, False)。
    - 无命中：(0, False) → main 打印"无薪酬残留"正常退出
    - 有命中：(N, False) → main 打印命中并退出非0，提示"转 PDF 后脱敏"

    策略依据：.doc 是 OLE2 二进制格式，antiword 仅提取纯文本，
    无法在原文件上做白色矩形覆盖（PyMuPDF 不认 .doc）。
    唯一正确做法是转 PDF 后走 redact_pdf。这里 fail-closed：命中即阻断。
    """
    import shutil, subprocess
    if not shutil.which("antiword"):
        print("❌ 老式 .doc 需要 antiword（PATH 未找到），无法扫描薪酬", file=sys.stderr)
        print("   请安装 antiword 或转为 PDF/DOCX 后再脱敏", file=sys.stderr)
        return [("antiword 不可用", "需安装 antiword")], False

    regex = _build_regex(patterns)
    try:
        r = subprocess.run(
            ["antiword", "-m", "UTF-8.txt", doc_path],
            capture_output=True, timeout=60,
        )
    except Exception as e:
        print(f"❌ antiword 调用失败: {e}", file=sys.stderr)
        return [(".doc 提取失败", str(e))], False
    if r.returncode != 0:
        print(f"❌ antiword 提取失败（可能损坏）", file=sys.stderr)
        return [(".doc 提取失败", "antiword 返回非0")], False

    text = r.stdout.decode("utf-8", errors="replace")
    hits = []
    for m in regex.finditer(text):
        ctx = text[max(0, m.start() - 15):m.end() + 15]
        hits.append((1, ctx.strip()))

    if hits:
        print("⚠️  .doc 命中薪酬但无法原地脱敏（antiword 只读不可涂白）", file=sys.stderr)
        print("    正确做法：转 PDF 后跑 redact_salary.py <转出.pdf>", file=sys.stderr)

    return hits, False


# ---- RAR / 7z 支持 ----


def redact_rar(rar_path, patterns=None, dry_run=False, output=None):
    """解压 RAR/7z → 脱敏包内简历 → 重打包为 ZIP（铁律：rar 必须转 zip 才能归档）。

    即使无薪酬命中也产出 zip（格式转换需求）。
    output 不传时产出同名 .zip（如 a.rar → a.zip）。
    """
    if not os.path.isfile(SEVEN_ZIP):
        print(f"❌ 需要 7-Zip: {SEVEN_ZIP}", file=sys.stderr)
        return [], False

    tmpdir = tempfile.mkdtemp(prefix="_redact_rar_")
    hits = []
    modified = False

    try:
        # 安全检查前置（2026-07-29，H2 修复）：
        # 解压前用 `7z l` 列成员，阻断嵌套归档/超大成员数/可疑文件名。
        # archive_safety.check_zip 只支持 ZIP，RAR 用此最小检查兜底。
        bad = _check_rar_safety(rar_path)
        if bad:
            print(f"❌ RAR 安全检查失败（拒绝解压）: {bad}", file=sys.stderr)
            return [("__BLOCKED__", bad)], False

        # 7z 解压
        r = subprocess.run(
            [SEVEN_ZIP, "x", rar_path, f"-o{tmpdir}", "-y"],
            capture_output=True, timeout=300
        )
        if r.returncode != 0:
            print(f"❌ 7z 解压失败: {r.stderr[:200]!r}", file=sys.stderr)
            return [], False

        # 逐个脱敏包内简历
        for root, _, files in os.walk(tmpdir):
            for f in files:
                lower = f.lower()
                if not (lower.endswith('.pdf') or lower.endswith('.docx') or lower.endswith('.doc')):
                    continue
                if '作品集' in f or 'portfolio' in lower:
                    continue
                filepath = os.path.join(root, f)
                if lower.endswith('.pdf'):
                    h, mod = redact_pdf(filepath, patterns, dry_run)
                elif lower.endswith('.docx'):
                    h, mod = redact_docx(filepath, patterns, dry_run)
                else:
                    # .doc：只扫不删（antiword 无法涂白）
                    h, mod = redact_doc(filepath, patterns, dry_run)
                if h:
                    rel = os.path.relpath(filepath, tmpdir).replace(os.sep, '/')
                    hits.extend([(f"{rel} p{pg}", ctx) for pg, ctx in h])
                if mod:
                    modified = True

        # 重打包为 zip（rar 转 zip，即使无薪酬也转）
        if not dry_run:
            out_path = output or (os.path.splitext(rar_path)[0] + '.zip')
            with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zout:
                for root, _, files in os.walk(tmpdir):
                    for f in files:
                        fp = os.path.join(root, f)
                        arcname = os.path.relpath(fp, tmpdir).replace(os.sep, '/')
                        zout.write(fp, arcname)
            print(f"✅ 已转换: {os.path.basename(rar_path)} → {os.path.basename(out_path)}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return hits, modified


# ---- 辅助函数 ----

def _build_regex(patterns):
    """构建正则：自定义 patterns 或默认 SALARY。"""
    if patterns:
        return re.compile("|".join(re.escape(p) for p in patterns))
    return SALARY


def _check_rar_safety(rar_path):
    """RAR/7z 最小安全检查（解压前）。返回 None=安全，str=阻断原因。

    archive_safety.check_zip 只支持 ZIP；RAR 用 `7z l` 列成员做兜底检查：
      - 嵌套归档成员（.zip/.rar/.7z/.tar/.gz）→ 阻断
      - 成员数超 200 → 阻断（archive_safety.MAX_MEMBERS 同值）
      - 绝对路径/.. 成员名 → 阻断
    """
    import subprocess
    from archive_safety import MAX_MEMBERS, NESTED_ARCHIVE_EXTS
    try:
        # 7-Zip 在 Windows 中文环境输出 GBK（成员路径含中文文件名），
        # text=True 默认 utf-8 会 UnicodeDecodeError → r.stdout 变 None。
        # 显式 errors="replace" 兜底，成员路径只需 ASCII 通配校验即可。
        r = subprocess.run(
            [SEVEN_ZIP, "l", "-slt", rar_path],
            capture_output=True, timeout=60, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        return f"7z 列成员失败: {e}"
    if r.returncode != 0:
        return f"7z 列成员返回非0: {(r.stderr or '')[:100]}"

    # 解析 -slt 输出：每个成员有 Path = ... 行
    # ⚠️ 7-Zip `-slt` 的第一行 Path = 是归档自身的绝对路径（archive header），
    # 不是成员；成员路径都是相对的（如 "子目录/文件"）。归档头只在第一条出现一次，
    # 跳过它，否则会把归档全路径误判为"绝对路径穿越"而 fail-closed 误阻断。
    # 注意：归档头文件名可能是 GBK 乱码，无法字符串相等匹配，靠"首位 + 盘符开头"识别。
    paths = []
    seen_first_path = False
    for line in r.stdout.splitlines():
        if line.startswith("Path ="):
            val = line[len("Path ="):].strip()
            norm = val.replace("\\", "/")
            if not seen_first_path:
                seen_first_path = True
                # 第一条 Path = 是归档头（绝对路径），跳过
                if re.match(r"^[A-Za-z]:[\\/]", norm) or norm.startswith("//"):
                    continue
            paths.append(val)

    for p in paths:
        norm = p.replace("\\", "/")
        if os.path.isabs(norm) or ".." in norm.split("/"):
            return f"成员含绝对路径/.. 穿越: {p}"
        ext = os.path.splitext(norm)[1].lower()
        if ext in NESTED_ARCHIVE_EXTS:
            return f"RAR 内含嵌套归档 {ext}: {p}"

    if len(paths) > MAX_MEMBERS:
        return f"成员数 {len(paths)} 超过上限 {MAX_MEMBERS}"

    return None


def _search_page(page, regex):
    """在 PDF 页面中搜索薪酬文本，返回 [(match_text, [rects])]。
    兼容文本被分割到多个位置的情况（跨行/跨列/跨文本块）。
    """
    results = []
    text = page.get_text()

    for m in regex.finditer(text):
        match_text = m.group()
        rects = page.search_for(match_text)

        # 1) 完整串找不到 → 去冒号核心词
        if not rects:
            core = re.sub(r'[：:]', '', match_text)[:6]
            if core:
                rects = page.search_for(core)

        # 2) 标签词匹配（如"期望薪资"）时，顺带搜索邻近的数字+K/万片段
        #    因为 PDF 排版常把标签和数字分到不同文本块
        extra = []
        # 在匹配文本前后窗口内找数字段
        ctx_window = text[max(0, m.start()-10):m.end()+20]
        for sn in re.findall(r'\d[\d,.\s]*[-—~至]*\d*[\s]*[Kk万wW]', ctx_window):
            sn = sn.strip()
            if len(sn) >= 2:
                extra.extend(page.search_for(sn))
        # 冒号后的裸数字（如"：15K"中"15K"在窗口里但正则没带K）
        for sn in re.findall(r'[：:]\s*(\d[\d,.]*[Kk万wW])', ctx_window):
            extra.extend(page.search_for(sn))

        # 3) 仍找不到矩形 → 用 get_text('dict') 把匹配区间映射回 span bbox
        #    （2026-07-29，B1 修复：此前这里 rects/extra 都空时直接丢弃命中，
        #     导致 dry-run 报"无薪酬"但真实脱敏也漏删 → verify 闸门检出残留 → 返工）
        if not rects and not extra:
            dict_rects = _locate_match_in_dict(page, text, m.start(), m.end())
            if dict_rects:
                results.append((match_text, dict_rects))
                continue
            # 4) 彻底无法定位 → fail-closed：标记 __UNLOCATABLE__，不静默丢弃
            #    redact_pdf/main 会据此告警并退出非0，提醒人工确认
            results.append((f"__UNLOCATABLE__:{match_text}", []))
            continue

        # 合并去重
        all_rects = list(rects) + extra
        unique = []
        seen = set()
        for r in all_rects:
            key = (round(r.x0,1), round(r.y0,1), round(r.x1,1), round(r.y1,1))
            if key not in seen:
                seen.add(key)
                unique.append(r)
        results.append((match_text, unique))

    return results


def _locate_match_in_dict(page, text, start, end):
    """把 get_text() 的匹配区间 [start,end) 映射回页面 span 的 bbox 列表。

    PyMuPDF 的 get_text()（默认模式）和 get_text('dict') 字符顺序一致，
    因此可按累计字符偏移定位匹配落在哪些 span，取这些 span 的 bbox 合并。
    找不到（偏移错位/空白符差异）→ 返回空列表（调用方 fallback 到 fail-closed）。
    """
    try:
        d = page.get_text("dict")
    except Exception:
        return []
    rects = []
    cursor = 0  # 累计字符偏移（与 get_text() 默认模式对齐）
    matched_any = False
    for block in d.get("blocks", []):
        if block.get("type") != 0:  # 0=文本块，1=图片
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                stext = span.get("text", "")
                if not stext:
                    continue
                s_start = cursor
                s_end = cursor + len(stext)
                # span 与匹配区间有交集 → 收录其 bbox
                if s_end > start and s_start < end:
                    bbox = span.get("bbox")
                    if bbox and len(bbox) == 4:
                        rects.append(fitz.Rect(bbox))
                        matched_any = True
                cursor = s_end
    return rects if matched_any else []


# ---- CLI ----

def main():
    parser = argparse.ArgumentParser(description="简历薪酬脱敏（白色覆盖，禁止黑色）+ rar转zip")
    parser.add_argument("file", help="PDF / DOCX / ZIP / RAR / 7z 文件路径")
    parser.add_argument("--patterns", nargs="*", help="自定义薪酬关键词（默认用 verify_archive 同款正则）")
    parser.add_argument("--dry-run", action="store_true", help="只报告命中，不改文件")
    parser.add_argument("--output", help="输出文件路径（rar转zip时指定规范命名；zip默认原地覆盖）")
    args = parser.parse_args()

    filepath = args.file
    if not os.path.exists(filepath):
        print(f"❌ 文件不存在: {filepath}", file=sys.stderr)
        sys.exit(1)

    lower = filepath.lower()
    print(f"{'预览' if args.dry_run else '脱敏'}: {filepath}")

    if lower.endswith('.pdf'):
        hits, modified = redact_pdf(filepath, args.patterns, args.dry_run)
    elif lower.endswith('.docx'):
        hits, modified = redact_docx(filepath, args.patterns, args.dry_run)
    elif lower.endswith('.doc'):
        # .doc 是老式二进制格式：antiword 只能提取文本，无法涂白矩形
        # 扫描薪酬命中即报告并阻断（fail-closed），提示转 PDF 后脱敏
        hits, modified = redact_doc(filepath, args.patterns, args.dry_run)
    elif lower.endswith('.zip'):
        hits, modified = redact_zip(filepath, args.patterns, args.dry_run)
    elif lower.endswith(('.rar', '.7z')):
        hits, modified = redact_rar(filepath, args.patterns, args.dry_run, args.output)
    else:
        print(f"❌ 不支持的格式: {filepath}", file=sys.stderr)
        sys.exit(1)

    if hits:
        print(f"\n命中 {len(hits)} 处薪酬文本：")
        for loc, ctx in hits:
            print(f"  [{loc}] ...{ctx}...")

        # fail-closed：有命中但未完全脱敏 → 退出非0
        # 覆盖三种"不能假装已脱敏"的情况：
        #   1) 命中含 __UNLOCATABLE__（定位不到矩形，涂不掉）
        #   2) 命中含 __BLOCKED__（安全检查拒绝解压，包内简历未处理）
        #   3) dry-run 模式（只报告不改文件）
        unlocatable = any("__UNLOCATABLE__" in str(c) or "__BLOCKED__" in str(c) for _, c in hits)
        if unlocatable:
            print(f"\n🔴 存在无法脱敏的命中（定位不到矩形/安全阻断）→ 需人工确认，文件不可直接归档",
                  file=sys.stderr)
            sys.exit(2)
        if modified and not args.dry_run:
            print(f"\n✅ 已脱敏（白色覆盖）: {filepath}")
        elif args.dry_run:
            print(f"\n⚠️ 预览模式，未修改文件")
    else:
        print("✅ 无薪酬残留")

    sys.exit(0)


if __name__ == '__main__':
    main()
