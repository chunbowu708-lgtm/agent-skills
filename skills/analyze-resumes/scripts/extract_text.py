# -*- coding: utf-8 -*-
"""简历文本提取 + 质检。

用法：python extract_text.py <简历文件路径> [--recovery] [--render-pages <dir>]
输出：stdout 一行 JSON：{"text": ..., "char_count": N, "is_valid": bool, "issue": str|null, ...}

支持 PDF 和 DOCX，统一用 fitz（PyMuPDF）提取。
fitz 能读出 docx 里的文本框内容，比 python-docx 更可靠。

质检规则：
- 字数 < 200 → 文本异常（疑似扫描件/图片PDF）
- 非可读字符占比 > 25% → 文本异常（疑似编码损坏）
- 有效行占比 < 50% → 文本异常（疑似多栏/竖排版式解析错乱）
- BOSS 加密文本层（token 行占比高）→ 文本异常（需渲染图片走视觉识别）

最后一条是 BOSS 直聘的反爬处理：PDF 渲染层正常显示（人眼看没问题），
但文本层被替换成 hex token（如 45e9a67e755836a71HN53tS_...~~），
get_text() 提取出的是 token 不是真实文字。检测到这种 PDF 时：
  - 自动把页面渲染成图片（--render-pages 指定输出目录，或用临时目录）
  - JSON 里返回 render_pages: [图片路径...]，调用方（主会话/上层脚本）
    用视觉模型读图片内容
  - 这等效于"擦掉文本层看图像"，绕过 BOSS 的文本层加密

渲染 fallback 只渲染图片落盘，不调视觉模型——模型依赖外部 MCP 工具，
脚本环境里没有。脚本的职责是"渲染好图片告诉调用方去识别"。
"""
import json
import os
import re
import sys
import tempfile

import fitz  # PyMuPDF

# 字数下限：低于此值判定为文本异常（疑似扫描件）
MIN_CHARS = 200
# 非可读字符占比上限：超过判定为文本异常（疑似编码损坏）
MAX_JUNK_RATIO = 0.25
# 有效行占比下限：低于判定为文本异常（疑似多栏/竖排版式解析错乱）
MIN_VALID_LINE_RATIO = 0.50
# BOSS token 行占比上限：超过判定为 BOSS 加密文本层
# token 行 = 无中文 + (连续8位hex前缀 或 含~~)
# BOSS 简历约 84% 行是 token，正常简历 0%，设 60% 留余量
MAX_TOKEN_LINE_RATIO = 0.60
# 渲染图片的 DPI（200 足够视觉模型识别，文件不过大）
RENDER_DPI = 200

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

# BOSS token 行：无中文 + (连续8位hex前缀 或 含~~结尾符)
# 实测 BOSS 简历 token 形如：45e9a67e755836a71HN53tS_ElRXw4S3VfKeWOGhnPfVNBJl3w~~
_TOKEN_LINE_RE = re.compile(r"^[0-9a-f]{8,}")
_TOKEN_TAIL = "~~"


def _is_token_line(line):
    """判断一行是否是 BOSS 加密文本层的 token 行。

    BOSS 把 PDF 文本层替换成内部 ID token，特征：
    - 无中文字符
    - 以 8 位以上连续 hex (0-9a-f) 开头，或含 ~~ 结尾符
    """
    if re.search(r"[\u4e00-\u9fff]", line):  # 有中文 → 不是 token
        return False
    return bool(_TOKEN_LINE_RE.search(line)) or _TOKEN_TAIL in line


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

    # 质检3：BOSS 加密文本层（token 行占比过高）
    # 必须在有效行检测之后——否则 token 行含连续英文字母会先把 valid_line_ratio 拉高
    # 通过前两条质检但全是 token 的情况，在这里拦住
    if lines:
        token_lines = sum(1 for l in lines if _is_token_line(l))
        token_line_ratio = token_lines / len(lines)
        if token_line_ratio > MAX_TOKEN_LINE_RATIO:
            return text, False, f"BOSS加密文本层（token行占{token_line_ratio:.0%}），需渲染图片走视觉识别"

    return text, True, None


def render_pages(pdf_path, output_dir=None, dpi=RENDER_DPI, max_pages=8):
    """把 PDF 渲染成 PNG 图片（绕过 BOSS 文本层加密的 fallback）。

    返回图片路径列表。output_dir 不传时用临时目录。
    图片命名：p{页码}.png（1-based）。

    2026-08-14 性能改造：**只渲染前 max_pages 页（默认 8）**——BOSS 加密简历的
    核心信息（基本信息/求职意向/工作经历）都在前几页，后面几十页是作品集，
    不需要渲染判断档位。79 页的曹语录作品集从 79 张 200dpi 大图降到 8 张，
    渲染耗时降 90%+。需要看全页作品集时再单独渲染。
    """
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    else:
        output_dir = tempfile.mkdtemp(prefix="_extract_render_")

    doc = fitz.open(pdf_path)
    total = len(doc)
    n = min(total, max_pages)
    paths = []
    for i in range(n):
        pix = doc[i].get_pixmap(dpi=dpi)
        fp = os.path.join(output_dir, f"p{i+1}.png")
        pix.save(fp)
        paths.append(fp)
    doc.close()
    if total > n:
        print(f"  ⚠️ PDF 共 {total} 页，仅渲染前 {n} 页（作品集部分如需评估单独渲染）", file=sys.stderr)
    return paths


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
        print(json.dumps({"error": "用法：python extract_text.py <简历文件路径> [--recovery] [--render-pages <dir>]"}), file=sys.stderr)
        sys.exit(2)

    path = sys.argv[1]
    use_recovery = "--recovery" in sys.argv[2:]

    # --render-pages <dir>：渲染图片到指定目录（不传则用临时目录）
    render_dir = None
    if "--render-pages" in sys.argv[2:]:
        idx = sys.argv.index("--render-pages")
        render_dir = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

    try:
        if use_recovery:
            # 重排模式：不质检，直接输出坐标重排后的文本（用于挽救排版错乱的简历）
            text = extract_recovery(path)
            result = {"text": text, "char_count": len(text), "is_valid": None, "issue": "recovery模式，未质检，需人工确认可读性"}
        else:
            text, is_valid, issue = extract(path)
            result = {"text": text, "char_count": len(text), "is_valid": is_valid, "issue": issue}

            # 自动渲染 fallback：文本不可读（含 BOSS token 层）时，渲染图片供视觉识别
            # 调用方（主会话/collect_and_extract）读 render_pages 字段，
            # 把图片喂给视觉模型——等效于"擦掉文本层看图像"
            if not is_valid and _needs_render(issue):
                try:
                    paths = render_pages(path, render_dir)
                    result["render_pages"] = paths
                    result["render_fallback"] = True
                    # issue 追加提示，让调用方知道有图片可用
                    result["issue"] = issue + f"，已渲染{len(paths)}页图片到{'指定目录' if render_dir else '临时目录'}，用视觉模型读取"
                except Exception as e:
                    result["render_error"] = str(e)

    except Exception as e:
        print(json.dumps({"error": f"提取失败：{e}"}), file=sys.stderr)
        sys.exit(1)

    # stdout 输出 JSON（纯 ASCII 安全，ensure_ascii=True 把中文转 \uXXXX）
    print(json.dumps(result, ensure_ascii=True))


def _needs_render(issue):
    """判断该 issue 是否值得渲染图片 fallback。

    只有"文本层坏了但渲染层可能正常"的情况才渲染：
    - BOSS 加密文本层（token 行）：渲染层正常，渲染必有用
    - 文本过少（扫描件/图片PDF）：渲染层可能就是图片，渲染有用
    - 排版解析错乱：渲染层正常，但视觉模型读多栏排版也可能错，价值有限
      → 不渲染（recovery 模式更合适）
    - 乱码占比过高（编码损坏）：渲染层可能也坏，不渲染
    """
    if not issue:
        return False
    return "BOSS" in issue or "扫描件" in issue or "图片PDF" in issue


if __name__ == "__main__":
    main()
