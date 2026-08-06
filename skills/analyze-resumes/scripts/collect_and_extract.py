# -*- coding: utf-8 -*-
"""扫描指定日期的归档目录，自动发现所有简历文件（含 zip/rar 包内简历），
逐个提取文本，输出结构化清单。

解决的核心问题（2026-08-06 铁律）：
  赵一博的简历 PDF 打包在 zip 里（和 mp4 作品一起），手动列文件时把整个 zip
  当"作品集"跳过，导致评估报告漏人。本脚本程序化扫描+解压，不靠人列清单。

用法：
  python collect_and_extract.py --date 8.05 [--archive-root <path>] [--output-dir <path>]

输出：
  1. 每份简历的文本 JSON 到 --output-dir（{姓名}.json）
  2. stdout 输出清单（Markdown 表格），含每个候选人的提取状态

扫描规则：
  - 遍历 {archive-root}/**/{M.DD}_*份/ 下所有文件（含档位子目录：强推/可推/待定·不推）
  - PDF/DOCX/DOC → 直接提取
  - ZIP/RAR → 解压，在包内找简历文件（PDF/DOCX，文件名含"简历""resume"优先）
  - 文件名含"作品集""portfolio"的独立文件 → 跳过（但 zip 打包件不跳过，要查内部）
  - 图片型（JPG/PNG）→ 标记为"图片型，需人工看原件"

依赖：
  - PyMuPDF (fitz)：PDF 提取
  - 7-Zip：解压 RAR（路径见 paths.py SEVEN_ZIP）
  - python-docx：DOCX 备选（fitz 优先）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
import shutil

# 复用 extract_text 的提取+质检逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_text import extract as extract_pdf

# ---- 配置 ----
ARCHIVE_ROOT_DEFAULT = os.environ.get("ARCHIVE_ROOT", "")
SEVEN_ZIP = r"C:/Program Files/7-Zip/7z.exe"

# 简历文件扩展名
RESUME_EXTS = {".pdf", ".docx", ".doc"}
# 打包件扩展名
ARCHIVE_EXTS = {".zip", ".rar", ".7z"}
# 图片扩展名（标记为需人工看原件）
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
# "作品集"关键词（独立作品集文件跳过，但打包件内的简历不跳过）
PORTFOLIO_KEYWORDS = {"作品集", "portfolio", "作品"}


def find_date_dirs(archive_root, date_prefix):
    """找到所有 {date_prefix}_*份 目录（含档位子目录）。"""
    results = []
    for root, dirs, files in os.walk(archive_root):
        for d in dirs:
            if d.startswith(f"{date_prefix}_") and d.endswith("份"):
                results.append(os.path.join(root, d))
    return results


def is_resume_file(filename):
    """判断是否简历文件（PDF/DOCX/DOC，且不是纯作品集）。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in RESUME_EXTS:
        return False
    name_lower = filename.lower()
    # 独立作品集文件跳过（如 xxx_作品集.pdf）
    if any(kw in name_lower for kw in PORTFOLIO_KEYWORDS):
        return False
    return True


def find_resume_in_archive(archive_path, extract_dir):
    """解压 zip/rar，在包内找简历文件。返回 (简历路径列表, 全部成员列表)。"""
    ext = os.path.splitext(archive_path)[1].lower()

    if ext == ".zip":
        try:
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(extract_dir)
        except Exception:
            return [], []
    elif ext in (".rar", ".7z"):
        r = subprocess.run(
            [SEVEN_ZIP, "x", archive_path, f"-o{extract_dir}", "-y"],
            capture_output=True, timeout=120
        )
        if r.returncode != 0:
            return [], []
    else:
        return [], []

    # 遍历解压结果，找简历文件
    resumes = []
    all_files = []
    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            fp = os.path.join(root, f)
            all_files.append(f)
            # 包内简历：PDF/DOCX，文件名含"简历""resume"优先
            ext_f = os.path.splitext(f)[1].lower()
            if ext_f in RESUME_EXTS:
                name_lower = f.lower()
                if any(kw in name_lower for kw in ["简历", "resume", "cv"]):
                    resumes.append(("resume", fp, f))
                elif not any(kw in name_lower for kw in PORTFOLIO_KEYWORDS):
                    resumes.append(("maybe", fp, f))
    return resumes, all_files


def extract_resume_text(filepath):
    """提取简历文本，返回 (text, is_valid, issue)。"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".pdf":
        return extract_pdf(filepath)
    elif ext in (".docx", ".doc"):
        # fitz 也能读 docx
        try:
            return extract_pdf(filepath)
        except Exception:
            return "", False, "DOCX 提取失败"
    return "", False, f"不支持的格式: {ext}"


def parse_name_from_dir(date_dir):
    """从日期目录路径解析出 岗位路径（相对于 archive-root）。"""
    # date_dir = .../已收集简历/8.5_3份 或 .../已收集简历/8.5_3份/强推
    # 往上找到"已收集简历"，再往上一级是岗位目录
    parts = date_dir.replace("\\", "/").split("/")
    # 找"已收集简历"的位置
    try:
        idx = parts.index("已收集简历")
    except ValueError:
        return "?", "?"
    # 岗位 = 已收集简历 的上一级目录名
    job_name = parts[idx - 1] if idx > 0 else "?"
    # 工作室 = 更上层（找"项目""工作室"关键词）
    studio_parts = parts[:idx - 1]
    studio = studio_parts[-1] if studio_parts else "?"
    # 去掉 archive-root 前缀部分
    return studio, job_name


def main():
    parser = argparse.ArgumentParser(
        description="扫描日期目录，自动发现+提取所有简历文本（含 zip/rar 包内简历）"
    )
    parser.add_argument("--date", required=True, help="日期前缀，如 8.05")
    parser.add_argument("--archive-root", default=ARCHIVE_ROOT_DEFAULT, help="归档根目录")
    parser.add_argument("--output-dir", default=None, help="文本 JSON 输出目录（默认 notes/_extract_{date}）")
    args = parser.parse_args()

    date_prefix = args.date
    # 兼容 8.05 和 8.5 两种写法（归档目录用 M.D 不补零，如 8.5_3份）
    # find_date_dirs 用 startswith，传入的 prefix 要和目录名一致
    # 目录名格式：{M.DD}_N份，如 8.5_3份、7.29_1份（月不补零、日补零到2位）
    # 但 8.05 这种用户输入也要支持 → 规范化为 M.D 格式
    if "." in date_prefix:
        month, day = date_prefix.split(".", 1)
        date_prefix = f"{int(month)}.{int(day)}"  # 去前导零：8.05 → 8.5
    archive_root = args.archive_root
    output_dir = args.output_dir or f"{os.environ.get('PROJECT_ROOT', os.getcwd())}/notes/_extract_{date_prefix.replace('.', '.')}"
    os.makedirs(output_dir, exist_ok=True)

    # 1. 扫描日期目录
    date_dirs = find_date_dirs(archive_root, date_prefix)
    if not date_dirs:
        print(f"❌ 未找到 {date_prefix}_*份 目录")
        sys.exit(1)

    print(f"📂 扫到 {len(date_dirs)} 个日期目录：")
    for d in date_dirs:
        print(f"   {d.replace(archive_root + '/', '')}")
    print()

    # 2. 遍历每个目录下的文件
    results = []  # [{name, studio, job, status, issue, text_len, source}]
    candidates = {}  # name → info（去重：同人取简历，作品集不重复计）

    for date_dir in date_dirs:
        studio, job = parse_name_from_dir(date_dir)
        # 遍历含档位子目录
        for root, dirs, files in os.walk(date_dir):
            for f in sorted(files):
                filepath = os.path.join(root, f)
                ext = os.path.splitext(f)[1].lower()

                # 从文件名解析候选人姓名（取第一个 _ 前的部分）
                name_base = f.split("_")[0] if "_" in f else os.path.splitext(f)[0]

                if ext in RESUME_EXTS:
                    # 独立简历文件
                    if is_resume_file(f):
                        text, is_valid, issue = extract_resume_text(filepath)
                        key = f"{name_base}_{job}"
                        if key not in candidates:
                            candidates[key] = {
                                "name": name_base, "studio": studio, "job": job,
                                "status": "✅已提取" if is_valid else "⚪图片型/异常",
                                "issue": issue, "text_len": len(text),
                                "source": f, "text": text if is_valid else "",
                            }
                            # 保存 JSON
                            json_path = os.path.join(output_dir, f"{name_base}.json")
                            with open(json_path, "w", encoding="utf-8") as jf:
                                json.dump({"text": text, "is_valid": is_valid, "issue": issue}, jf, ensure_ascii=False)
                    # 独立作品集文件 → 跳过（不计入）
                    continue

                elif ext in ARCHIVE_EXTS:
                    # zip/rar → 解压找简历
                    tmpdir = tempfile.mkdtemp(prefix="_extract_archive_")
                    try:
                        resumes, all_members = find_resume_in_archive(filepath, tmpdir)
                        if resumes:
                            # 优先取 "resume" 类型，其次 "maybe"
                            resumes.sort(key=lambda x: 0 if x[0] == "resume" else 1)
                            resume_type, resume_path, resume_name = resumes[0]
                            text, is_valid, issue = extract_resume_text(resume_path)
                            key = f"{name_base}_{job}"
                            if key not in candidates:
                                candidates[key] = {
                                    "name": name_base, "studio": studio, "job": job,
                                    "status": "✅已提取(zip内)" if is_valid else "⚪图片型/异常(zip内)",
                                    "issue": issue, "text_len": len(text),
                                    "source": f"{f} → {resume_name}",
                                    "text": text if is_valid else "",
                                }
                                json_path = os.path.join(output_dir, f"{name_base}.json")
                                with open(json_path, "w", encoding="utf-8") as jf:
                                    json.dump({"text": text, "is_valid": is_valid, "issue": issue}, jf, ensure_ascii=False)
                        else:
                            # 包内无简历 → 纯作品集
                            key = f"{name_base}_{job}"
                            if key not in candidates:
                                candidates[key] = {
                                    "name": name_base, "studio": studio, "job": job,
                                    "status": "🎨纯作品集(无简历)",
                                    "issue": "zip/rar 内无简历文件，需看作品集原件",
                                    "text_len": 0, "source": f, "text": "",
                                }
                    finally:
                        shutil.rmtree(tmpdir, ignore_errors=True)

                elif ext in IMAGE_EXTS:
                    # 图片简历 → 标记
                    key = f"{name_base}_{job}"
                    if key not in candidates:
                        candidates[key] = {
                            "name": name_base, "studio": studio, "job": job,
                            "status": "⚪图片型",
                            "issue": f"图片格式({ext})，需人工看原件",
                            "text_len": 0, "source": f, "text": "",
                        }

    # 3. 输出清单
    results = sorted(candidates.values(), key=lambda x: (x["studio"], x["job"], x["name"]))

    print(f"📊 共发现 {len(results)} 位候选人：\n")
    print(f"| 姓名 | 工作室 | 岗位 | 状态 | 文本长度 | 来源 |")
    print(f"|------|--------|------|------|---------|------|")
    for r in results:
        print(f"| {r['name']} | {r['studio']} | {r['job']} | {r['status']} | {r['text_len']} | {r['source'][:40]} |")

    # 汇总
    extracted = [r for r in results if "已提取" in r["status"]]
    image_type = [r for r in results if "图片型" in r["status"]]
    portfolio_only = [r for r in results if "纯作品集" in r["status"]]

    print(f"\n📈 汇总：{len(extracted)} 已提取 · {len(image_type)} 图片型(需看原件) · {len(portfolio_only)} 纯作品集(无简历) · 共 {len(results)} 人")
    print(f"📁 文本 JSON 输出：{output_dir}")

    if image_type:
        print(f"\n⚠️ 图片型简历（需人工看原件）：{', '.join(r['name'] for r in image_type)}")
    if portfolio_only:
        print(f"⚠️ 纯作品集无简历：{', '.join(r['name'] for r in portfolio_only)}")


if __name__ == "__main__":
    main()
