# -*- coding: utf-8 -*-
"""
redact_image_salary.py — 图片型简历的薪酬脱敏快路径（一条命令替代人工6步链）。

背景（2026-08-19 教训）：图片简历（jpg/图片型PDF）的薪酬脱敏，redact_salary --dir 的
OCR fallback 对几十页作品集逐页跑 easyocr 要 20 分钟+；实际薪酬只在简历信息页（前几页）。
本脚本只查前 N 页 → easyocr 定位命中块 → 精确白块覆盖，全程 1-3 分钟。

用法:
  python redact_image_salary.py <简历.jpg|png|图片型.pdf> [--pages 3] [--dpi 150] [--dry-run]

行为:
  - jpg/png: PIL 直接白块覆盖（命中块 bbox ± pad，不碰相邻字段），原地保存
  - PDF:     fitz add_redact_annot(fill=(1,1,1)) 只涂命中页，原地保存
  - 每处命中输出验证裁剪图 <原名>_check<N>.jpg（命中区域外扩，供人工/视觉复核）
  - 未命中输出"无命中"退出码 0（可放心归档）

坐标原则（AGENTS.md 脱敏铁律）: 只覆盖 OCR 命中的文本块自身 bbox（含小 padding 防锯齿），
不做几何范围扩展——相邻字段（期望城市/求职意向）分属不同 OCR 块，天然不被误删。
"""

import argparse
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from salary_pattern import SALARY  # noqa: E402

PAD = 3  # 命中块 bbox 防锯齿余量（像素），不跨字段


def _ocr_hits(image_path, reader):
    """对一张图片跑 easyocr，返回命中 SALARY 正则的 (bbox, text) 列表。"""
    import easyocr  # 延迟导入：Reader 初始化 10-20s
    res = reader.readtext(image_path)
    hits = []
    for bbox, text, conf in res:
        if SALARY.search(text):
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            hits.append(([min(xs), min(ys), max(xs), max(ys)], text))
    return hits


def _check_crop(save_path, img_w, img_h, box, renderer):
    """输出命中区域验证图（外扩 15%，供人工复核）。"""
    from PIL import Image
    pad_x = int((box[2] - box[0]) * 0.6) + 80
    pad_y = int((box[3] - box[1]) * 0.6) + 40
    crop_box = [max(0, int(box[0] - pad_x)), max(0, int(box[1] - pad_y)),
                min(img_w, int(box[2] + pad_x)), min(img_h, int(box[3] + pad_y))]
    im = renderer(crop_box)
    im.convert("RGB").save(save_path, quality=85)
    return save_path


def handle_image(path, reader, dry_run=False):
    """jpg/png：PIL 白块。easyocr 读不了中文路径 → 先复制到 ASCII 临时路径。"""
    from PIL import Image, ImageDraw
    tmp = os.path.join(tempfile.mkdtemp(prefix="_ris_"), "img.png")
    shutil.copyfile(path, tmp)
    hits = _ocr_hits(tmp, reader)
    if not hits:
        print("✅ 无薪酬命中")
        return 0
    im = Image.open(path)
    draw = ImageDraw.Draw(im)
    for i, (box, text) in enumerate(hits, 1):
        print(f"  [{i}] {text!r} @ {[round(v) for v in box]}")
        if not dry_run:
            draw.rectangle([box[0] - PAD, box[1] - PAD, box[2] + PAD, box[3] + PAD],
                           fill=(255, 255, 255))
        _check_crop(f"{path}_check{i}.jpg", im.width, im.height, box, im.crop)
    if not dry_run:
        im.save(path)
        print(f"✅ 已涂白 {len(hits)} 处（白色覆盖，验证图 *_check*.jpg 请复核）")
    else:
        print(f"ℹ dry-run：{len(hits)} 处命中未修改")
    return 0


def handle_pdf(path, reader, pages, dpi, dry_run=False):
    """图片型 PDF：渲染前 N 页 → OCR → fitz 白块只涂命中页。"""
    import fitz
    tmpdir = tempfile.mkdtemp(prefix="_ris_pdf_")
    doc = fitz.open(path)
    scale = 72.0 / dpi  # 渲染像素 → PDF pt
    total = []
    try:
        for pno in range(min(pages, len(doc))):
            pix = doc[pno].get_pixmap(dpi=dpi)
            tmp_png = os.path.join(tmpdir, f"p{pno + 1}.png")  # ASCII 路径
            pix.save(tmp_png)
            for box, text in _ocr_hits(tmp_png, reader):
                total.append((pno, box, text))
        if not total:
            print("✅ 无薪酬命中（前 %d 页）" % min(pages, len(doc)))
            return 0
        for i, (pno, box, text) in enumerate(total, 1):
            print(f"  [{i}] p{pno + 1} {text!r} @ px{[round(v) for v in box]}")
            if not dry_run:
                rect = fitz.Rect((box[0] - PAD) * scale, (box[1] - PAD) * scale,
                                 (box[2] + PAD) * scale, (box[3] + PAD) * scale)
                doc[pno].add_redact_annot(rect, fill=(1, 1, 1))
        if dry_run:
            print(f"ℹ dry-run：{len(total)} 处命中未修改")
            return 0
        for pno2 in {p for p, _, _ in total}:
            doc[pno2].apply_redactions()  # PyMuPDF 新版：Page 级方法
        tmp_out = path + ".__ris__.pdf"
        doc.save(tmp_out, garbage=3, deflate=True)
        doc.close()
        os.replace(tmp_out, path)
        # 验证图：涂白应用后渲染命中区域
        doc = fitz.open(path)
        for i, (pno, box, text) in enumerate(total, 1):
            page = doc[pno]
            pad_x = (box[2] - box[0]) * 0.6 * scale + 60
            pad_y = (box[3] - box[1]) * 0.6 * scale + 30
            rect = fitz.Rect(box[0] * scale, box[1] * scale, box[2] * scale, box[3] * scale)
            clip = fitz.Rect(max(0, rect.x0 - pad_x), max(0, rect.y0 - pad_y),
                             min(page.rect.x1, rect.x1 + pad_x), min(page.rect.y1, rect.y1 + pad_y))
            page.get_pixmap(dpi=150, clip=clip).save(f"{path}_check{i}.jpg")
        print(f"✅ 已涂白 {len(total)} 处（白色覆盖，验证图 *_check*.jpg 请复核）")
        return 0
    finally:
        doc.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="图片简历 jpg/png 或图片型 PDF")
    ap.add_argument("--pages", type=int, default=3, help="PDF 只查前 N 页（默认3，简历信息页）")
    ap.add_argument("--dpi", type=int, default=150, help="PDF 渲染 DPI（默认150）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not os.path.isfile(args.file):
        print(f"❌ 文件不存在: {args.file}", file=sys.stderr)
        return 1
    print(f"🔍 {os.path.basename(args.file)}（easyocr 初始化约 15s，请稍候）")
    import easyocr
    reader = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
    ext = os.path.splitext(args.file)[1].lower()
    if ext in (".jpg", ".jpeg", ".png"):
        return handle_image(args.file, reader, args.dry_run)
    elif ext == ".pdf":
        return handle_pdf(args.file, reader, args.pages, args.dpi)
    else:
        print(f"❌ 不支持的扩展名: {ext}（仅 jpg/png/pdf）", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
