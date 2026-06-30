# -*- coding: utf-8 -*-
"""简历文本提取 + 质检。

用法：python extract_text.py <简历文件路径>
输出：stdout 一行 JSON：{"text": ..., "char_count": N, "is_valid": bool, "issue": str|null}

支持 PDF 和 DOCX，统一用 fitz（PyMuPDF）提取。
fitz 能读出 docx 里的文本框内容，比 python-docx 更可靠。

质检规则：
- 字数 < 200 → 文本异常（疑似扫描件/图片PDF）
- 非可读字符占比 > 25% → 文本异常（疑似编码损坏）
- 有效行占比 < 50% → 文本异常（疑似多栏/竖排版式解析错乱）

第三条最关键：有些 PDF 字符够、也不是乱码，但多栏排版被 fitz
按列打散、行序错乱，拼出来读不通。有效行占比能抓住这类"字符够
但顺序碎了"的情况（正常简历 85%+，排版错乱的 20-30%）。
"""
import json
import re
import sys

import fitz  # PyMuPDF

# 字数下限：低于此值判定为文本异常（疑似扫描件）
MIN_CHARS = 200
# 非可读字符占比上限：超过判定为文本异常（疑似编码损坏）
MAX_JUNK_RATIO = 0.25
# 有效行占比下限：低于判定为文本异常（疑似多栏/竖排解析错乱）
MIN_VALID_LINE_RATIO = 0.50

# 可读字符：中文、英文、数字、常见标点、空格换行
_READABLE_RE = re.compile(
    r"[\u4e00-\u9fff\u3000-\u303f"  # 中文及CJK标点
    r"a-zA-Z0-9\s"                  # 英文数字空白
    r".,;:!?()\[\]\-+@"             # 英文标点
    r"\u2014\u2013\u2018\u2019\u201c\u201d"  # 英文引号破折号
    r"\u3001\u3002\uff0c\uff08\uff09\uff1a\uff1b\uff01\uff1f]"  # 中文标点
)

# 有效行：含>=2个中文字符，或含长度>=4的连续英文字母词
# 用来检测多栏/竖排版式被 fitz 打散后的"碎片行"过多
_VALID_LINE_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{4,}")


def extract(path):
    """提取简历文本，返回 (text, is_valid, issue)。"""
    doc = fitz.open(path)
    text = "".join(page.get_text() for page in doc)
    doc.close()

    char_count = len(text)

    # 质检0：字数过少
    if char_count < MIN_CHARS:
        return text, False, f"文本过少（{char_count}字），疑似扫描件/图片PDF，需人工看原文件"

    # 质检1：非可读字符占比过高
    readable_count = len(_READABLE_RE.findall(text))
    junk_ratio = (char_count - readable_count) / char_count
    if junk_ratio > MAX_JUNK_RATIO:
        return text, False, f"乱码占比过高（{junk_ratio:.0%}），疑似编码损坏，需人工看原文件"

    # 质检2：有效行占比过低（多栏/竖排版式被解析打散）
    lines = [l for l in text.split("\n") if l.strip()]
    if lines:
        valid_lines = sum(1 for l in lines if _VALID_LINE_RE.search(l))
        valid_line_ratio = valid_lines / len(lines)
        if valid_line_ratio < MIN_VALID_LINE_RATIO:
            return text, False, f"排版解析错乱（有效行仅{valid_line_ratio:.0%}），疑似多栏/竖排，需人工看原文件"

    return text, True, None


def extract_recovery(path):
    """对排版错乱的简历尝试按文本块坐标重排，尽力挽救。

    fitz 的 get_text() 默认按读取顺序输出，多栏简历会被按列打散。
    本函数用 blocks 模式（带坐标），按 y 坐标升序、同 y 内按 x 升序
    重排，尽量恢复阅读顺序。不保证完美，但对单页多栏简历通常能改善。
    """
    doc = fitz.open(path)
    parts = []
    for page in doc:
        blocks = page.get_text("blocks")  # [(x0,y0,x1,y1,text,block_no,block_type), ...]
        # 按 y 主序、x 次序排序
        blocks.sort(key=lambda b: (round(b[1] / 5), b[0]))  # y 分桶(容差5pt)避免同行抖动
        for b in blocks:
            t = b[4].strip()
            if t:
                parts.append(t)
    doc.close()
    return "\n".join(parts)


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "用法：python extract_text.py <简历文件路径> [--recovery]"}), file=sys.stderr)
        sys.exit(2)

    path = sys.argv[1]
    use_recovery = "--recovery" in sys.argv[2:]

    try:
        if use_recovery:
            # 重排模式：不质检，直接输出坐标重排后的文本（用于挽救排版错乱的简历）
            text = extract_recovery(path)
            result = {"text": text, "char_count": len(text), "is_valid": None, "issue": "recovery模式，未质检，需人工确认可读性"}
        else:
            text, is_valid, issue = extract(path)
            result = {"text": text, "char_count": len(text), "is_valid": is_valid, "issue": issue}
    except Exception as e:
        print(json.dumps({"error": f"提取失败：{e}"}), file=sys.stderr)
        sys.exit(1)

    # stdout 输出 JSON（纯 ASCII 安全，ensure_ascii=True 把中文转 \uXXXX）
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
