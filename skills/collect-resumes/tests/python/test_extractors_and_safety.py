# -*- coding: utf-8 -*-
"""
content_extractors 和 archive_safety 的单元测试。
使用 unittest，不需要 pytest。
生成合成的 PDF/DOCX/ZIP fixtures，不含真实简历数据。
"""

import os
import sys
import io
import unittest
import tempfile
import zipfile

# 把 scripts 目录加入 import 路径
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep + ".." + os.sep + ".." + os.sep + "scripts"
sys.path.insert(0, SCRIPTS_DIR)

from content_extractors import extract, extract_pdf, extract_docx, ExtractResult
from archive_safety import check_zip, SafetyResult


def make_text_pdf(path, text="Zhang San Resume\nExperience: 5 years backend\nEducation: Bachelor"):
    """用 PyMuPDF 创建一个文本型 PDF（用 ASCII 避免字体缺中文问题）。"""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    doc.save(path)
    doc.close()


def make_scanned_pdf(path):
    """创建一个无文本但有嵌入图片的 PDF（模拟扫描件）。"""
    import fitz
    # 先创建一个 PNG 图片像素数据
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100))
    pix.clear_with(255)  # 白底
    # 画几条黑线模拟文字行
    for y in range(20, 80, 10):
        pix.set_rect(fitz.IRect(10, y, 90, y + 2), (0, 0, 0))

    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(50, 50, 250, 250), pixmap=pix)
    doc.save(path)
    doc.close()
    pix = None


def make_docx(path, text="Li Si\nPython Engineer\nExpected salary: 25-30K"):
    """用 python-docx 创建 DOCX。"""
    import docx
    d = docx.Document()
    d.add_paragraph(text)
    d.save(path)


def make_safe_zip(path, pdf_name="resume.pdf", pdf_text="Wang Wu Resume"):
    """创建一个安全的 ZIP（含一个 PDF）。"""
    tmp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_pdf.close()
    make_text_pdf(tmp_pdf.name, pdf_text)
    with zipfile.ZipFile(path, "w") as zf:
        zf.write(tmp_pdf.name, pdf_name)
    os.unlink(tmp_pdf.name)


class TestContentExtractors(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_text_pdf_extracts_name(self):
        p = os.path.join(self.tmpdir, "text.pdf")
        make_text_pdf(p, "Zhao Liu\nSenior Engineer\n8 years")
        result = extract(p)
        self.assertFalse(result.blocked, f"文本 PDF 不应阻断: {result.block_reason}")
        self.assertIn("Zhao", result.text)
        self.assertEqual(result.fmt, "pdf")

    def test_scanned_pdf_blocks_without_ocr(self):
        """图片型 PDF + tesseract 不可用 → 阻断（不假放行）。"""
        p = os.path.join(self.tmpdir, "scanned.pdf")
        make_scanned_pdf(p)
        result = extract_pdf(p)
        # tesseract 不在 PATH → 必须 blocked
        self.assertTrue(result.blocked, "图片型 PDF 在 OCR 不可用时必须阻断")
        self.assertIn("OCR", result.block_reason)

    def test_docx_extracts_text(self):
        p = os.path.join(self.tmpdir, "resume.docx")
        make_docx(p, "Sun Qi\nExpected salary: 25-30K\nPython Developer")
        result = extract(p)
        self.assertFalse(result.blocked, f"DOCX 不应阻断: {result.block_reason}")
        self.assertEqual(result.fmt, "docx")
        self.assertIn("Sun Qi", result.text)
        self.assertIn("25-30K", result.text)

    def test_unsupported_format_blocks(self):
        p = os.path.join(self.tmpdir, "file.rar")
        with open(p, "wb") as f:
            f.write(b"fake rar content")
        result = extract(p)
        self.assertTrue(result.blocked)
        self.assertIn("不支持", result.block_reason)


class TestArchiveSafety(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_safe_zip_passes(self):
        p = os.path.join(self.tmpdir, "safe.zip")
        make_safe_zip(p, "简历.pdf", "测试内容")
        result = check_zip(p)
        self.assertFalse(result.blocked, f"安全 ZIP 不应阻断: {result.reason}")
        self.assertEqual(len(result.members), 1)

    def test_zip_slip_blocked(self):
        p = os.path.join(self.tmpdir, "slip.zip")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("../../escape.pdf", b"%PDF-1.7 fake")
        result = check_zip(p)
        self.assertTrue(result.blocked)
        self.assertIn("路径穿越", result.reason)

    def test_encrypted_zip_blocked(self):
        """构造加密 ZIP：Python zipfile 不能写加密 ZIP，手工构造含中央目录的完整 ZIP，
        local file header 和 central directory 的 flag_bits 都置 0x1。"""
        p = os.path.join(self.tmpdir, "enc.zip")
        import struct
        name = b"secret.pdf"
        data = b"%PDF-1.7 fake encrypted"
        crc = 0
        comp_size = len(data)
        uncomp_size = len(data)

        # Local file header (PK\x03\x04)
        lfh = struct.pack(
            "<4sHHHHHIIIHH",
            b"PK\x03\x04", 20, 0x0001, 0, 0, 0,
            crc, comp_size, uncomp_size, len(name), 0,
        )
        # Central directory file header (PK\x01\x02)
        cd_offset = len(lfh) + len(name) + len(data)
        cdh = struct.pack(
            "<4sHHHHHHIIIHHHHHII",
            b"PK\x01\x02",
            20,             # version made by
            20,             # version needed
            0x0001,         # flag_bits: encrypted
            0,              # method
            0, 0,           # time, date
            crc, comp_size, uncomp_size,
            len(name), 0,   # filename len, extra len
            0,              # comment len
            0,              # disk number
            0,              # internal attrs
            0,              # external attrs
            0,              # local header offset
        )
        # End of central directory (PK\x05\x06)
        eocd = struct.pack(
            "<4sHHHHIIH",
            b"PK\x05\x06", 0, 0, 1, 1,
            len(cdh) + len(name), cd_offset, 0,
        )

        with open(p, "wb") as f:
            f.write(lfh + name + data + cdh + name + eocd)

        result = check_zip(p)
        self.assertTrue(result.blocked)
        self.assertIn("加密", result.reason)

    def test_nested_archive_blocked(self):
        p = os.path.join(self.tmpdir, "nested.zip")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("inner.zip", b"PK fake inner zip")
        result = check_zip(p)
        self.assertTrue(result.blocked)
        self.assertIn("嵌套归档", result.reason)

    def test_empty_zip_blocked(self):
        p = os.path.join(self.tmpdir, "empty.zip")
        with zipfile.ZipFile(p, "w") as zf:
            pass
        result = check_zip(p)
        self.assertTrue(result.blocked)
        self.assertIn("无任何可验证", result.reason)

    def test_corrupt_zip_blocked(self):
        p = os.path.join(self.tmpdir, "corrupt.zip")
        with open(p, "wb") as f:
            f.write(b"PK\x03\x04 this is not a real zip")
        result = check_zip(p)
        self.assertTrue(result.blocked)

    def test_unknown_member_ext_blocked(self):
        p = os.path.join(self.tmpdir, "unknown.zip")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("file.exe", b"MZ fake exe")
        result = check_zip(p)
        self.assertTrue(result.blocked)
        self.assertIn("不在允许列表", result.reason)

    def test_pure_portfolio_zip_allowed_with_warning(self):
        """纯作品 ZIP（只有 mp4/jpg，无简历 PDF）→ 允许但标注为纯作品包。"""
        p = os.path.join(self.tmpdir, "portfolio.zip")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("demo.mp4", b"\x00\x00\x00\x20 ftypisom")  # fake mp4
            zf.writestr("screenshot.jpg", b"\xFF\xD8\xFF\xE0 fake jpg")
        result = check_zip(p)
        self.assertFalse(result.blocked, f"纯作品 ZIP 不应阻断: {result.reason}")
        self.assertTrue(any("纯作品包" in w for w in result.warnings))

    def test_mixed_zip_resume_extracted_portfolio_skipped(self):
        """混合 ZIP（简历 PDF + 作品 mp4）→ PDF 被提取验证，mp4 被允许但不提取。"""
        p = os.path.join(self.tmpdir, "mixed.zip")
        tmp_pdf = os.path.join(self.tmpdir, "inner.pdf")
        make_text_pdf(tmp_pdf, "Test Resume")
        with zipfile.ZipFile(p, "w") as zf:
            zf.write(tmp_pdf, "resume.pdf")
            zf.writestr("showreel.mp4", b"\x00\x00\x00\x20 ftypisom")
        result = check_zip(p)
        self.assertFalse(result.blocked)
        resume_members = [m for m in result.members if m["is_resume"]]
        self.assertEqual(len(resume_members), 1)
        self.assertEqual(resume_members[0]["ext"], ".pdf")
        # 不应有"纯作品包"警告（因为有简历）
        self.assertFalse(any("纯作品包" in w for w in result.warnings))


class TestSalaryRegex(unittest.TestCase):
    """薪酬正则专项测试（2026-07-16：26k-28k BOSS模板格式漏匹配修复）。
    防止以后改回旧正则导致漏脱敏。"""

    def setUp(self):
        import sys, os
        skill_scripts = os.path.join(os.path.expanduser("~"), ".agents", "skills",
                                     "collect-resumes", "scripts")
        if skill_scripts not in sys.path:
            sys.path.insert(0, skill_scripts)
        from redact_salary import SALARY
        self.SALARY = SALARY

    def _hits(self, text):
        return bool(self.SALARY.search(text))

    def test_boss_format_double_k(self):
        """BOSS直聘标准格式：26k-28k（两端都带k，最常见，曾漏匹配）"""
        self.assertTrue(self._hits("26k-28k"))
        self.assertTrue(self._hits("15k-20k"))
        self.assertTrue(self._hits("26K-28K"))

    def test_single_k_format(self):
        """单端k格式：25-30K（原有用例，不能回归）"""
        self.assertTrue(self._hits("25-30K"))
        self.assertTrue(self._hits("25-30k"))

    def test_with_keyword(self):
        """带关键词的格式"""
        self.assertTrue(self._hits("期望薪资：25-30K"))
        self.assertTrue(self._hits("薪资 26k-28k"))
        self.assertTrue(self._hits("月薪15k"))

    def test_in_context(self):
        """BOSS模板完整行（真实案例：卢东成简历）"""
        self.assertTrue(self._hits("在职，正在找工作 丨广州 丨U3D 丨26k-28k"))

    def test_no_false_positive(self):
        """正常数字不应误伤（百分比/版本号/日期/性能数据）"""
        for safe in ["降低50%", "包体大小降低70%", "AssetBundle数量降低95%",
                      "Android 4.4", "2021年12月", "2016.09 - 2019.06"]:
            self.assertFalse(self._hits(safe), f"误伤正常文本: {safe}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
