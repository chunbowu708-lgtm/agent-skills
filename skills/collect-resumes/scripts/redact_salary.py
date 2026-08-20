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

import sys, os, re, io, json, zipfile, tempfile, shutil, argparse, subprocess

try:
    import fitz  # PyMuPDF
except ImportError:
    print("❌ 需要 PyMuPDF: pip install PyMuPDF", file=sys.stderr)
    sys.exit(1)

from salary_pattern import SALARY
from paths import SEVEN_ZIP
from archive_safety import check_zip, decode_zip_name

# 白色覆盖（铁律：禁止黑色）
FILL_COLOR = (1, 1, 1)

# 图片型 PDF 渲染 DPI（薪酬定位需要足够清晰，200dpi 够 OCR）
RENDER_DPI = 200


def redact_pdf(pdf_path, patterns=None, dry_run=False, ocr_pages=None):
    """脱敏 PDF：用白色矩形覆盖薪酬文本。
    返回 (命中数, 是否修改)。

    自动检测图片型/BOSS加密文本层：当文本层提取不到薪酬且页面有图片时，
    fallback 到 OCR 路径（渲染页面→OCR定位薪酬像素bbox→反推PDF坐标→白矩形覆盖）。
    ocr_pages：图片型 PDF 的 OCR 页数上限（None=默认前3页，0=全页）。
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
        os.replace(pdf_path + '.tmp', pdf_path)
    else:
        doc.close()

    # 文本层无命中 → 检查是否图片型/BOSS加密，是则走 OCR 路径
    # （图片型 PDF 文本层是空的或乱码 token，薪酬文字只在渲染层图像里）
    if not hits:
        ocr_hits, ocr_modified = _redact_pdf_via_ocr(pdf_path, patterns, dry_run, ocr_pages)
        if ocr_hits:
            return ocr_hits, ocr_modified

    return hits, modified


def _is_image_pdf(doc):
    """检测是否是图片型 PDF：文本层字符少 或 页面以图片为主。"""
    total_text_len = sum(len(page.get_text().strip()) for page in doc)
    if total_text_len < 50:  # 几乎无文本 → 扫描件/纯图片
        return True
    # 检查是否有 BOSS token 行（文本层是乱码）
    for page in doc:
        text = page.get_text()
        lines = [l for l in text.split("\n") if l.strip()]
        if lines:
            token_like = sum(1 for l in lines
                             if not re.search(r"[\u4e00-\u9fff]", l)
                             and (re.match(r"^[0-9a-f]{8,}", l) or "~~" in l))
            if token_like / len(lines) > 0.5:
                return True
    return False


def _get_ocr_engine():
    """获取可用的 OCR 引擎（延迟导入）。返回 (engine, name) 或 (None, None)。

    优先级：easyocr（纯Python不需外部二进制）> pytesseract（需tesseract二进制）。
    """
    try:
        import easyocr
        # chi_sim+en 模型首次用会自动下载（约几十MB）
        if not hasattr(_get_ocr_engine, "_reader"):
            _get_ocr_engine._reader = easyocr.Reader(["ch_sim", "en"], gpu=False)
        return _get_ocr_engine._reader, "easyocr"
    except ImportError:
        pass
    try:
        import pytesseract
        pytesseract.get_tesseract_version()  # 测试二进制是否可用
        return pytesseract, "pytesseract"
    except Exception:
        pass
    return None, None


def _ocr_locate_salary(img_path, regex, engine_name, engine):
    """用 OCR 识别图片文字+位置，匹配薪酬正则，返回 [(text, [pixel_bboxes])]。

    pixel_bbox = [x_min, y_min, x_max, y_max]（像素坐标，左上角0,0）
    """
    results = []
    if engine_name == "easyocr":
        # easyocr 返回 [(bbox, text, confidence)]
        detections = engine.readtext(img_path)
        for bbox, text, conf in detections:
            if regex.search(text):
                # bbox 是 4 个角点 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
                pixel_rect = [min(xs), min(ys), max(xs), max(ys)]
                results.append((text, [pixel_rect]))
    elif engine_name == "pytesseract":
        from PIL import Image
        img = Image.open(img_path)
        # image_to_data 返回每个词的位置
        data = engine.image_to_data(img, lang="chi_sim+eng", output_type=engine.Output.DICT)
        # 把相邻词组合成行，按行匹配薪酬正则
        lines = {}
        for i in range(len(data["text"])):
            if not data["text"][i].strip():
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            if key not in lines:
                lines[key] = {"texts": [], "lefts": [], "tops": [], "rights": [], "bottoms": []}
            lines[key]["texts"].append(data["text"][i])
            l, t, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            lines[key]["lefts"].append(l)
            lines[key]["tops"].append(t)
            lines[key]["rights"].append(l + w)
            lines[key]["bottoms"].append(t + h)
        for key, info in lines.items():
            line_text = "".join(info["texts"])
            if regex.search(line_text):
                pixel_rect = [min(info["lefts"]), min(info["tops"]),
                              max(info["rights"]), max(info["bottoms"])]
                results.append((line_text, [pixel_rect]))
    return results


def _pixel_to_pdf_rect(pixel_bbox, img_width, img_height, page_rect):
    """像素坐标 → PDF 坐标（fitz.Rect）。

    渲染时 dpi=200，PDF 原始坐标按比例缩放回来。
    page_rect 是 fitz 页面的 Rect（PDF 点坐标）。
    """
    x_min, y_min, x_max, y_max = pixel_bbox
    # 归一化（0-1）再映射到 PDF 坐标
    sx0 = page_rect.x0 + (x_min / img_width) * page_rect.width
    sy0 = page_rect.y0 + (y_min / img_height) * page_rect.height
    sx1 = page_rect.x0 + (x_max / img_width) * page_rect.width
    sy1 = page_rect.y0 + (y_max / img_height) * page_rect.height
    # 稍微向内收缩避免覆盖到相邻字段，但保证覆盖完整（不收缩太多）
    return fitz.Rect(sx0, sy0, sx1, sy1)


def _redact_pdf_via_ocr(pdf_path, patterns=None, dry_run=False, ocr_pages=None):
    """图片型/BOSS加密 PDF 的 OCR 脱敏路径。

    1. 检测是否图片型（不是则返回空，让上层走正常文本路径）
    2. 获取 OCR 引擎（easyocr/pytesseract），无可用引擎则渲染图片提示主会话处理
    3. 渲染前 ocr_pages 页→OCR定位薪酬→像素bbox反推PDF坐标→白色覆盖

    2026-08-20：只查前 N 页（默认3）。图片型作品集 PDF 动辄几十页（08-19 吴雨坤72页
    逐页OCR跑15分钟未完），薪酬只出现在简历信息页（前几页），后续是作品图无薪酬字段。
    需要全页扫时 --ocr-pages 0。
    """
    doc = fitz.open(pdf_path)
    if not _is_image_pdf(doc):
        doc.close()
        return [], False

    engine, engine_name = _get_ocr_engine()
    if engine is None:
        # 无 OCR 引擎 → 渲染图片输出路径，提示主会话用视觉模型定位
        doc.close()
        if dry_run:
            print("  ⚠️  图片型PDF检测到，但无OCR引擎（easyocr/pytesseract）", file=sys.stderr)
            print("     安装任一：pip install easyocr（或装 tesseract 二进制）", file=sys.stderr)
            print("     或用主会话视觉模型定位薪酬后传坐标", file=sys.stderr)
        return [("__NO_OCR_ENGINE__", "图片型PDF需OCR引擎，未安装")], False

    regex = _build_regex(patterns)
    hits = []
    modified = False
    zoom = RENDER_DPI / 72  # PDF 默认 72dpi
    mat = fitz.Matrix(zoom, zoom)
    tmpdir = tempfile.mkdtemp(prefix="_redact_ocr_")

    try:
        pages_to_scan = len(doc) if ocr_pages == 0 else min(len(doc), ocr_pages if ocr_pages else 3)
        if pages_to_scan < len(doc):
            print(f"  ℹ️ 图片型PDF共{len(doc)}页，只OCR前{pages_to_scan}页（简历信息页；全页扫用 --ocr-pages 0）")
        for page_num in range(pages_to_scan):
            page = doc[page_num]
            pix = page.get_pixmap(matrix=mat)
            img_path = os.path.join(tmpdir, f"p{page_num+1}.png")
            pix.save(img_path)

            # OCR 定位薪酬
            ocr_hits = _ocr_locate_salary(img_path, regex, engine_name, engine)
            for match_text, pixel_bboxes in ocr_hits:
                rects = []
                for pb in pixel_bboxes:
                    rect = _pixel_to_pdf_rect(pb, pix.width, pix.height, page.rect)
                    rects.append(rect)
                    if not dry_run:
                        page.add_redact_annot(rect, fill=FILL_COLOR)
                hits.append((f"p{page_num+1}(OCR)", match_text))

            if ocr_hits and not dry_run:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
                modified = True

        if modified and not dry_run:
            doc.save(pdf_path + '.tmp')
            doc.close()
            os.replace(pdf_path + '.tmp', pdf_path)
        else:
            doc.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

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
        os.replace(docx_path + '.tmp', docx_path)

    return hits, modified


def redact_zip(zip_path, patterns=None, dry_run=False):
    """脱敏 ZIP 内嵌的简历文件（PDF/DOCX/.doc）。"""
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
        for w in safety.warnings:
            print(f"  ℹ️ {os.path.basename(zip_path)}: {w}")

        # 解压（还原中文名后手动落盘，避免 extractall 把 cp437 乱码名写进磁盘）
        # 2026-08-13 修：extractall 用 info.filename 当目标路径，中文名被 cp437 解成
        # 乱码（如"游戏主美"→"µ╕╕µêÅ"），重打包后 zip 内文件名仍是乱码。
        names = []
        with zipfile.ZipFile(zip_path, 'r') as zin:
            for info in zin.infolist():
                if info.is_dir():
                    continue
                real_name = decode_zip_name(info)
                norm = real_name.replace("\\", "/")
                if "__MACOSX/" in norm or os.path.basename(norm).startswith("._"):
                    continue
                target = os.path.join(tmpdir, real_name.replace("\\", "/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zin.open(info) as src, open(target, 'wb') as dst:
                    dst.write(src.read())
                names.append(real_name)

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
            os.replace(tmp_zip, zip_path)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return hits, modified


def _doc_to_pdf(doc_path):
    """老式 .doc → PDF（Windows Word COM 优先，LibreOffice soffice fallback）。

    返回转换成功的 pdf 路径；全部失败返回 None（不抛异常，调用方 fallback）。
    转换后的 PDF 与 .doc 同目录同名（如 a.doc → a.pdf），供 redact_pdf 脱敏。
    """
    import subprocess, shutil
    pdf_path = os.path.splitext(doc_path)[0] + '.pdf'
    if os.path.exists(pdf_path):
        return pdf_path

    # 方式1：Word COM（Windows 装了 Office/WPS 即可用；SaveAs 17 = wdFormatPDF）
    try:
        ps = (
            "$ErrorActionPreference='Stop'; "
            "$w = New-Object -ComObject Word.Application; $w.Visible=$false; "
            f"$d = $w.Documents.Open('{doc_path}', $false, $true); "
            f"$d.SaveAs([ref]'{pdf_path}', [ref]17); $d.Close($false); $w.Quit()"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=120,
        )
        if r.returncode == 0 and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            return pdf_path
    except Exception as e:
        print(f"  ⚠️ Word COM 转 PDF 失败: {e}", file=sys.stderr)

    # 方式2：LibreOffice headless
    try:
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if soffice:
            outdir = os.path.dirname(doc_path) or '.'
            r = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", outdir, doc_path],
                capture_output=True, timeout=180,
            )
            if r.returncode == 0 and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                return pdf_path
    except Exception as e:
        print(f"  ⚠️ soffice 转 PDF 失败: {e}", file=sys.stderr)

    return None


def redact_doc(doc_path, patterns=None, dry_run=False):
    """老式 .doc 薪酬处理。

    返回 (命中数, modified)。
    - **优先**：转 PDF（Word COM / soffice）后走 redact_pdf 原地脱敏，
      转换成功且非 dry_run 时删除原 .doc（内容已进 PDF，避免闸门再次拦截）。
    - fallback：antiword 只扫不删（原逻辑，fail-closed：命中即阻断提示转 PDF）。

    策略依据：.doc 是 OLE2 二进制格式，antiword 仅提取纯文本无法涂白，
    PyMuPDF 不认 .doc。正确做法是转 PDF 后脱敏；转换不可用时才 fail-closed。
    """
    import shutil, subprocess

    # 优先：自动转 PDF 后走标准脱敏链路（2026-08-14：鲍鑫浩 403MB zip 内 .doc 简历卡死整个闸门，
    # 手动 Word COM 转 PDF 才放行——现在固化为脚本能力）
    pdf_path = _doc_to_pdf(doc_path)
    if pdf_path:
        hits, modified = redact_pdf(pdf_path, patterns, dry_run, args.ocr_pages)
        if not dry_run:
            # 原 .doc 已被 PDF 替代；删除失败仅警告（保留会导致闸门再次拦截）
            try:
                os.remove(doc_path)
            except OSError as e:
                print(f"⚠️ 原 .doc 删除失败（请手动删，避免闸门拦截）: {doc_path}: {e}", file=sys.stderr)
        return hits, modified

    if not shutil.which("antiword"):
        print("❌ 老式 .doc 需要 antiword（PATH 未找到）且本机无 Word/LibreOffice 可转 PDF", file=sys.stderr)
        print("   无法扫描薪酬：请手动把 .doc 转 PDF 后再跑脱敏", file=sys.stderr)
        return [("antiword 不可用", "需安装 antiword 或手动转 PDF")], False

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
    output 不传时产出同名 .zip（如 a.rar → a.zip）；相对路径按调用方 CWD 解析，
    会自动转绝对路径（防 zip 生成到意外的 CWD）。
    返回 (hits, modified, out_path)；out_path 在 dry_run 时为 None。
    """
    if not os.path.isfile(SEVEN_ZIP):
        print(f"❌ 需要 7-Zip: {SEVEN_ZIP}", file=sys.stderr)
        return [], False, None

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
            return [("__BLOCKED__", bad)], False, None

        # 7z 解压
        r = subprocess.run(
            [SEVEN_ZIP, "x", rar_path, f"-o{tmpdir}", "-y"],
            capture_output=True, timeout=300
        )
        if r.returncode != 0:
            print(f"❌ 7z 解压失败: {r.stderr[:200]!r}", file=sys.stderr)
            return [], False, None

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
        out_path = None
        if not dry_run:
            # 相对路径统一转绝对（防 zip 生成到调用方 CWD 的坑）
            out_path = os.path.abspath(output or (os.path.splitext(rar_path)[0] + '.zip'))
            with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zout:
                for root, _, files in os.walk(tmpdir):
                    for f in files:
                        fp = os.path.join(root, f)
                        arcname = os.path.relpath(fp, tmpdir).replace(os.sep, '/')
                        zout.write(fp, arcname)
            print(f"✅ 已转换: {os.path.basename(rar_path)} → {out_path}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return hits, modified, out_path


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
    # ⚠️ 7-Zip `-slt` 的第一条 Path = 恒为归档自身（archive header），
    # 不是成员；后续 Path = 才是成员（相对路径如 "子目录/文件"）。
    # 无条件跳过第一条——不靠路径形式判断：用相对路径调 redact_salary 时
    # 归档头也是相对路径，靠盘符正则会漏判，导致归档头被当成"嵌套 .rar 成员"误阻断。
    paths = []
    seen_first_path = False
    for line in r.stdout.splitlines():
        if line.startswith("Path ="):
            val = line[len("Path ="):].strip()
            if not seen_first_path:
                seen_first_path = True
                continue  # 第一条 Path = 恒为归档头，无条件跳过
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

def redact_pdf_rects(pdf_path, rects_json, dry_run=False):
    """用外部传入的坐标直接在 PDF 上画白色矩形覆盖。

    用于图片型/BOSS加密 PDF：OCR 精度不够时，由主会话用视觉模型
    定位薪酬位置，把坐标传给本函数画白矩形。

    rects_json 格式：
    [
      {"page": 1, "dpi": 200, "bbox": [x_min, y_min, x_max, y_max]},
      ...
    ]
    page 是 1-based 页码，dpi 是渲染时的 DPI（用于坐标反推），
    bbox 是该 DPI 下渲染图片的像素坐标。
    """
    rects = json.loads(rects_json) if isinstance(rects_json, str) else rects_json
    doc = fitz.open(pdf_path)
    hits = []
    modified = False

    for r in rects:
        page_num = r["page"] - 1  # 转 0-based
        dpi = r.get("dpi", 200)
        bbox = r["bbox"]
        if page_num >= len(doc):
            continue
        page = doc[page_num]
        zoom = dpi / 72
        # 像素坐标 → PDF 坐标
        sx0 = bbox[0] / zoom
        sy0 = bbox[1] / zoom
        sx1 = bbox[2] / zoom
        sy1 = bbox[3] / zoom
        rect = fitz.Rect(sx0, sy0, sx1, sy1)
        if not dry_run:
            page.add_redact_annot(rect, fill=FILL_COLOR)
        hits.append((r["page"], f"外部坐标 {bbox}"))

    if hits and not dry_run:
        for r in rects:
            page = doc[r["page"] - 1]
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        doc.save(pdf_path + '.tmp')
        doc.close()
        os.replace(pdf_path + '.tmp', pdf_path)
        modified = True
    else:
        doc.close()

    return hits, modified


def main():
    parser = argparse.ArgumentParser(description="简历薪酬脱敏（白色覆盖，禁止黑色）+ rar转zip")
    parser.add_argument("file", nargs="?", help="PDF / DOCX / ZIP / RAR / 7z 文件路径（单文件模式）")
    parser.add_argument("--dir", help="目录模式：递归脱敏该目录下所有 pdf/docx/zip/rar（批量，一条命令代替逐个跑）")
    parser.add_argument("--patterns", nargs="*", help="自定义薪酬关键词（默认用 verify_archive 同款正则）")
    parser.add_argument("--dry-run", action="store_true", help="只报告命中，不改文件")
    parser.add_argument("--output", help="输出文件路径（rar转zip时指定规范命名；zip默认原地覆盖）")
    parser.add_argument("--report-json", help="批量 --dir 模式：机器可读 JSON 报告输出路径")
    parser.add_argument("--redact-rects", help="外部传入薪酬坐标JSON（图片型PDF专用）：[{page,dpi,bbox:[x,y,x,y]}]")
    parser.add_argument("--ocr-pages", type=int, default=None, help="图片型PDF的OCR页数上限（默认3=只查简历信息页；0=全页逐页扫，慢）")
    args = parser.parse_args()

    if args.dir:
        if args.output or args.redact_rects:
            print("❌ --dir 批量模式不支持 --output / --redact-rects（这两个是单文件专用）", file=sys.stderr)
            sys.exit(1)
        _redact_dir(args)
        return

    if not args.file:
        parser.print_help()
        sys.exit(1)

    _redact_one(args.file, args)


# 批量脱敏时逐个处理的文件扩展名（与单文件模式一致）
_BATCH_EXTS = (".pdf", ".docx", ".doc", ".zip", ".rar", ".7z")


def _redact_dir(args):
    """目录批量脱敏：递归找简历文件逐个处理，汇总报告。

    批量模式 fail-closed：任一文件存在无法脱敏的命中（__UNLOCATABLE__/__BLOCKED__）
    或格式不支持，记入失败列表，最后统一非零退出（不假装已脱敏）。
    rar/7z 转换成功且无失败 → 自动删除原 rar（归档目录不留未脱敏 rar，
    防下次重转 + 薪酬泄漏源）。图片简历等批量范围外文件列入 skipped 报告
    （图片简历需先 enhance_ocr_pdf.py 转 PDF）。
    """
    if not os.path.isdir(args.dir):
        print(f"❌ 目录不存在: {args.dir}", file=sys.stderr)
        sys.exit(1)

    targets = []
    skipped_other = []
    SKIP_NAMES = ('.tmp', '.part', '.json')  # 中间产物/报告不列
    IMG_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')
    for root, dirs, files in os.walk(args.dir):
        # 跳过临时/缓存目录，避免误处理中间产物
        dirs[:] = [d for d in dirs if not re.search(r"temp|临时|tmp|__pycache__", d, re.IGNORECASE)]
        for f in sorted(files):
            fp = os.path.join(root, f)
            low = f.lower()
            if low.endswith(_BATCH_EXTS):
                targets.append(fp)
            elif low.endswith(SKIP_NAMES) or f.startswith('.') or '__MACOSX' in fp:
                continue
            elif low.endswith(IMG_EXTS):
                skipped_other.append((fp, "图片文件：若是图片简历，先 enhance_ocr_pdf.py 转PDF"))
            else:
                skipped_other.append((fp, "不在批量处理扩展名内，请人工确认"))

    if not targets:
        print("✅ 目录下无 pdf/docx/zip/rar 简历文件")
        _write_dir_report(args, 0, 0, [], skipped_other, 0)
        return

    print(f"📂 批量脱敏 {len(targets)} 个文件：{args.dir}")
    if args.dry_run:
        print("⚠️ 预览模式，不改文件")

    total_hits = 0
    failed = []   # [(path, reason)]
    converted_rars = []  # [(rar_path, zip_path)] 转换成功、待确认无失败后清理
    for i, fp in enumerate(targets, 1):
        print(f"\n[{i}/{len(targets)}] {'预览' if args.dry_run else '脱敏'}: {fp}")
        try:
            if fp.lower().endswith(('.rar', '.7z')):
                hits, modified, out_path = redact_rar(fp, args.patterns, args.dry_run)
                if out_path:
                    converted_rars.append((fp, out_path))
            else:
                hits, modified = _run_redact(fp, args)
        except SystemExit as e:
            # _run_redact 内部遇到不可恢复错误会 exit，这里捕获并记录，继续处理其余文件
            failed.append((fp, f"退出码 {e.code}"))
            continue
        total_hits += len(hits)
        unlocatable = any("__UNLOCATABLE__" in str(c) or "__BLOCKED__" in str(c) for _, c in hits)
        if unlocatable:
            failed.append((fp, "存在无法脱敏的命中（定位不到矩形/安全阻断）"))
        elif fp.lower().endswith('.doc') and hits:
            failed.append((fp, ".doc 命中薪酬或 antiword 不可用（只读无法涂白），需转 PDF 后脱敏"))
        elif hits and not modified and not args.dry_run:
            failed.append((fp, "有薪酬命中但未修改（异常）"))

    # rar 清理：仅删"转换成功且该文件无失败"的（有失败的原 rar 留给人工处理）
    removed_rars = 0
    failed_paths = {p for p, _ in failed}
    if not args.dry_run:
        for rar_path, _zip in converted_rars:
            if rar_path in failed_paths:
                continue
            try:
                os.remove(rar_path)
                removed_rars += 1
                print(f"🗑️ 已删原压缩包（已转 zip）: {os.path.basename(rar_path)}")
            except OSError as e:
                print(f"⚠️ 原压缩包删除失败（请手动删，避免未脱敏 rar 残留）: {rar_path}: {e}")

    print(f"\n{'=' * 40}")
    print(f"📊 批量完成：{len(targets)} 个文件，共命中 {total_hits} 处薪酬，rar→zip 清理 {removed_rars} 个")
    if skipped_other:
        print(f"ℹ️ {len(skipped_other)} 个文件不在批量范围（图片简历需先转PDF，其余人工确认）：")
        for fp, reason in skipped_other[:10]:
            print(f"   - {fp}: {reason}")
        if len(skipped_other) > 10:
            print(f"   ... 还有 {len(skipped_other) - 10} 个（见 --report-json）")
    _write_dir_report(args, len(targets), total_hits, failed, skipped_other, removed_rars)
    if failed:
        print(f"🔴 {len(failed)} 个文件需人工处理：")
        for fp, reason in failed:
            print(f"   - {fp}: {reason}")
        sys.exit(2)
    print("✅ 批量脱敏完成，可进闸门")


def _write_dir_report(args, processed, hits, failed, skipped_other, removed_rars):
    """批量模式产出机器可读报告（编排器/下游脚本消费）。"""
    if not args.report_json:
        return
    report = {
        "dir": os.path.abspath(args.dir),
        "dry_run": bool(args.dry_run),
        "processed": processed,
        "salary_hits": hits,
        "failed": [{"file": p, "reason": r} for p, r in failed],
        "skipped_other": [{"file": p, "reason": r} for p, r in skipped_other],
        "removed_rars": removed_rars,
    }
    with open(args.report_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"📄 报告已写: {args.report_json}")


def _redact_one(filepath, args):
    if not os.path.exists(filepath):
        print(f"❌ 文件不存在: {filepath}", file=sys.stderr)
        sys.exit(1)

    print(f"{'预览' if args.dry_run else '脱敏'}: {filepath}")
    hits, modified = _run_redact(filepath, args)

    if hits:
        print(f"\n命中 {len(hits)} 处薪酬文本：")
        for loc, ctx in hits:
            print(f"  [{loc}] ...{ctx}...")
        # fail-closed：有命中但未完全脱敏 → 退出非0
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


def _run_redact(filepath, args):
    """按扩展名分派脱敏，返回 (hits, modified)。单文件与批量模式共用。"""
    lower = filepath.lower()

    # 外部坐标模式（图片型 PDF 的主会话 fallback，仅单文件）
    if args.redact_rects:
        if not lower.endswith('.pdf'):
            print("❌ --redact-rects 只支持 PDF", file=sys.stderr)
            sys.exit(1)
        return redact_pdf_rects(filepath, args.redact_rects, args.dry_run)

    if lower.endswith('.pdf'):
        return redact_pdf(filepath, args.patterns, args.dry_run, args.ocr_pages)
    if lower.endswith('.docx'):
        return redact_docx(filepath, args.patterns, args.dry_run)
    if lower.endswith('.doc'):
        # .doc 是老式二进制格式：antiword 只能提取文本，无法涂白矩形
        # 扫描薪酬命中即报告并阻断（fail-closed），提示转 PDF 后脱敏
        return redact_doc(filepath, args.patterns, args.dry_run)
    if lower.endswith('.zip'):
        return redact_zip(filepath, args.patterns, args.dry_run)
    if lower.endswith(('.rar', '.7z')):
        hits, modified, _out = redact_rar(filepath, args.patterns, args.dry_run, args.output)
        return hits, modified
    print(f"❌ 不支持的格式: {filepath}", file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    main()
