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

    # 找所有薪酬匹配
    for m in regex.finditer(full_text):
        start, end = m.start(), m.end()
        ctx = full_text[max(0, start-15):end+15]
        hits.append((1, ctx.strip()))

        if dry_run:
            continue

        affected = {}
        for i in range(start, end):
            mi, ci = char_map[i]
            affected.setdefault(mi, []).append(ci)

        for mi in sorted(affected.keys()):
            mt = matches[mi]
            original = mt.group(2)
            remove = set(affected[mi])
            new_content = "".join(c for ci, c in enumerate(original) if ci not in remove)
            old_full = mt.group(0)
            new_full = mt.group(1) + new_content + mt.group(3)
            xml = xml.replace(old_full, new_full, 1)
        modified = True

    if modified:
        files['word/document.xml'] = xml.encode('utf-8')
        with zipfile.ZipFile(docx_path + '.tmp', 'w', zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                zout.writestr(name, files[name])
        shutil.move(docx_path + '.tmp', docx_path)

    return hits, modified


def redact_zip(zip_path, patterns=None, dry_run=False):
    """脱敏 ZIP 内嵌的简历文件（PDF/DOCX）。"""
    regex = _build_regex(patterns)
    tmpdir = tempfile.mkdtemp(prefix="_redact_")
    hits = []
    modified = False

    try:
        # 解压
        with zipfile.ZipFile(zip_path, 'r') as zin:
            zin.extractall(tmpdir)
            names = zin.namelist()

        # 逐个处理内嵌简历
        for name in names:
            lower = name.lower()
            if not (lower.endswith('.pdf') or lower.endswith('.docx')):
                continue
            if '作品集' in name or 'portfolio' in lower:
                continue

            filepath = os.path.join(tmpdir, name)
            if lower.endswith('.pdf'):
                h, mod = redact_pdf(filepath, patterns, dry_run)
            else:
                h, mod = redact_docx(filepath, patterns, dry_run)

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
                if not (lower.endswith('.pdf') or lower.endswith('.docx')):
                    continue
                if '作品集' in f or 'portfolio' in lower:
                    continue
                filepath = os.path.join(root, f)
                if lower.endswith('.pdf'):
                    h, mod = redact_pdf(filepath, patterns, dry_run)
                else:
                    h, mod = redact_docx(filepath, patterns, dry_run)
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

        if rects or extra:
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
        if modified and not args.dry_run:
            print(f"\n✅ 已脱敏（白色覆盖）: {filepath}")
        elif args.dry_run:
            print(f"\n⚠️ 预览模式，未修改文件")
    else:
        print("✅ 无薪酬残留")

    sys.exit(0)


if __name__ == '__main__':
    main()
