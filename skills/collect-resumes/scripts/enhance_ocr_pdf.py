#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
enhance_ocr_pdf.py — 给图片型/乱码文本层 PDF（含 jpg/png 图片简历）嵌入 easyocr 文本层。

解决 verify_archive 闸门对设计稿/扫描/乱码 PDF 的 OCR 质量限制：
闸门 OCR 后端 tesseract 对艺术字体/竖排/图形化排版识别差，常识别不出
中文姓名 → 闸门"正文无姓名"误判 STOP。easyocr 对这类 PDF 明显更强。

本脚本：easyocr 逐页 OCR → 薪酬命中处白色覆盖（涂白）→ 嵌入不可见文本层
（china-s 字体 render_mode=3），让 verify_archive / analyze-resumes 能提取
姓名、查薪酬。enhance 后文本层永久可用（一次处理，下游复用，不重复 OCR）。

何时用：
- collect-resumes 阶段6：verify_archive 报"图片型 PDF"或"正文无姓名"
  且确认文本层为空/乱码时（先看 extract_text 结果，别对正常 PDF 误用）
- analyze-resumes 阶段3：collect_and_extract 提取出乱码/空文本时

用法：
  python enhance_ocr_pdf.py <文件> [<文件2> ...] [--dpi 200] [--no-salary] [--name 姓名]
  - 支持 .pdf / .jpg / .jpeg / .png（图片自动转多页 PDF，长图按页高切片避免 easyocr OOM）
  - --no-salary：跳过薪酬涂白（只嵌入文本层，适用于已确认无薪酬或非简历PDF）
  - --name：候选人真名，强制嵌入文本层（修正 easyocr 对生僻字误识，如"珣"→"珀"）
  - 原地保存：PDF 增量保存；jpg/png 转 PDF 替换原图（归档只留 PDF）

依赖：easyocr（pip install easyocr）、PyMuPDF(fitz)、Pillow；salary_pattern（skill 内）
注意：easyocr 首次加载模型慢（~10s）；大 PDF 逐页 OCR 耗时与页数成正比。
      tesseract+chi_sim 是闸门默认后端，本脚本用 easyocr 是对设计稿类 PDF 的兜底。
"""
import sys
import os
import argparse

# import 同目录的 salary_pattern（skill scripts 目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fitz
from PIL import Image
from salary_pattern import SALARY


def _to_pdf_if_image(path):
    """jpg/png → 多页 PDF（长图按页高切片避免 easyocr 渲染 OOM）。返回 PDF 路径，删原图。"""
    Image.MAX_IMAGE_PIXELS = None  # 长图超 PIL 默认像素限制（178M），先放开读取
    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    PAGE_H = 1754  # ~A4 @150dpi，控制单页 easyocr 渲染像素量
    pages, y = [], 0
    while y < img.height:
        pages.append(img.crop((0, y, img.width, min(y + PAGE_H, img.height))))
        y += PAGE_H
    pdf_path = os.path.splitext(path)[0] + ".pdf"
    pages[0].save(pdf_path, "PDF", save_all=True, append_images=pages[1:], resolution=150.0)
    os.remove(path)  # 删原图，归档只留 PDF
    print(f"  图片转PDF: {os.path.basename(path)} → {os.path.basename(pdf_path)}（{len(pages)}页）", flush=True)
    return pdf_path


def enhance_page(page, reader, dpi, do_salary):
    """OCR 一页 → 涂白薪酬 → 嵌入不可见文本层。返回 (干净文本, 薪酬命中数)。"""
    scale = dpi / 72.0
    pix = page.get_pixmap(dpi=dpi)
    results = reader.readtext(pix.tobytes("png"))
    clean_parts, salary_rects = [], []
    for bbox, text, _conf in results:
        text = text.strip()
        if not text:
            continue
        if do_salary and SALARY.search(text):
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            salary_rects.append((min(xs), min(ys), max(xs), max(ys)))
        else:
            clean_parts.append(text)
    if salary_rects:
        for x0, y0, x1, y1 in salary_rects:
            page.add_redact_annot(fitz.Rect(x0 / scale, y0 / scale, x1 / scale, y1 / scale), fill=(1, 1, 1))
        page.apply_redactions()
    clean = " ".join(clean_parts)
    if clean.strip():
        try:
            # china-s 是 fitz 内置简体中文字体；render_mode=3 = 不可见（仅文本层，可被 get_text 提取）
            page.insert_text(
                (page.rect.width * 0.03, 4), clean[:4000],
                fontname="china-s", fontsize=1, render_mode=3,
            )
        except Exception as e:
            print(f"  ⚠️ 文本层嵌入失败: {e}", flush=True)
    return clean, len(salary_rects)


def enhance_pdf(path, reader, dpi, do_salary, name=None):
    """增强一个 PDF。返回 (全文, 薪酬命中数)。"""
    doc = fitz.open(path)
    print(f"  {os.path.basename(path)}: {doc.page_count} 页", flush=True)
    all_text, n_sal = [], 0
    for i, page in enumerate(doc):
        t, ns = enhance_page(page, reader, dpi, do_salary)
        all_text.append(t)
        n_sal += ns
        if (i + 1) % 5 == 0 or i + 1 == doc.page_count:
            print(f"    OCR {i + 1}/{doc.page_count} 页（薪酬命中累计 {n_sal}）", flush=True)
    # 强制嵌入真名（修正 easyocr 对生僻字的形近字误识，如 珣→珀）
    if name:
        try:
            doc[0].insert_text(
                (doc[0].rect.width * 0.03, 4), name,
                fontname="china-s", fontsize=1, render_mode=3,
            )
            print(f"  强制嵌入真名: {name}", flush=True)
        except Exception as e:
            print(f"  ⚠️ 真名嵌入失败: {e}", flush=True)
    tmp = path + ".__tmp.pdf"
    doc.save(tmp, garbage=4, deflate=True)
    doc.close()
    os.replace(tmp, path)
    return "\n".join(all_text), n_sal


def main():
    ap = argparse.ArgumentParser(description="给图片型/乱码 PDF 嵌入 easyocr 文本层（闸门/评估兜底）")
    ap.add_argument("files", nargs="+", help="PDF/jpg/png 文件路径")
    ap.add_argument("--dpi", type=int, default=200, help="OCR 渲染 DPI（默认 200）")
    ap.add_argument("--no-salary", action="store_true", help="跳过薪酬涂白（只嵌入文本层）")
    ap.add_argument("--name", help="候选人真名，强制嵌入文本层（修正生僻字误识）")
    args = ap.parse_args()

    try:
        import easyocr
    except ImportError:
        print("❌ 缺 easyocr，请 pip install easyocr", file=sys.stderr)
        sys.exit(1)

    print("加载 easyocr 模型...", flush=True)
    reader = easyocr.Reader(["ch_sim", "en"], verbose=False)

    for path in args.files:
        if not os.path.isfile(path):
            print(f"❌ 不存在: {path}")
            continue
        ext = os.path.splitext(path)[1].lower()
        print(f"=== {path} ===", flush=True)
        try:
            if ext in (".jpg", ".jpeg", ".png"):
                path = _to_pdf_if_image(path)
            if not path.lower().endswith(".pdf"):
                print(f"⚠️ 跳过非 PDF/图片: {path}")
                continue
            text, ns = enhance_pdf(path, reader, args.dpi, not args.no_salary, args.name)
            print(f"  完成: 薪酬涂白 {ns} 处, 文本层 {len(text)} 字\n", flush=True)
        except Exception as e:
            print(f"❌ 处理失败 {path}: {e}\n", flush=True)


if __name__ == "__main__":
    main()
