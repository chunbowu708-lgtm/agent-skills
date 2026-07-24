# -*- coding: utf-8 -*-
"""
verify_archive 回归测试。
验证关键 P0/P1 故障路径已修复（fail-closed）。
"""

import os
import sys
import unittest
import tempfile
import zipfile

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep + ".." + os.sep + ".." + os.sep + "scripts"
sys.path.insert(0, SCRIPTS_DIR)

import verify_archive as va


def make_text_pdf(path, text="Zhang San Resume"):
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    doc.save(path)
    doc.close()


def make_docx(path, text="Li Si Developer"):
    import docx
    d = docx.Document()
    d.add_paragraph(text)
    d.save(path)


class TestVerifyArchiveFailClosed(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_no_count_label_stops(self):
        """无 _N份 标注 → STOP（旧版只告警，新版阻断）。"""
        import subprocess
        d = os.path.join(self.tmpdir, "项目", "岗位", "已收集简历", "7.01")  # 无 _N份
        os.makedirs(d)
        make_text_pdf(os.path.join(d, "ZhangSan_后端_5年.pdf"), "Zhang San Resume")
        # 直接调 main() 应该 exit 1
        old_argv = sys.argv
        sys.argv = ["verify_archive.py", d]
        try:
            rc = None
            try:
                va.main()
            except SystemExit as e:
                rc = e.code
            self.assertEqual(rc, 1, "无 _N份 标注必须 STOP（exit 1）")
        finally:
            sys.argv = old_argv

    def test_sha256_cache_supersedes_mtime(self):
        """同 mtime+size 但内容不同 → SHA-256 不同 → 不复用旧缓存。"""
        p = os.path.join(self.tmpdir, "test.pdf")
        make_text_pdf(p, "Original content Zhang San")
        sha1 = va.sha256_file(p)

        # 重写同样大小但内容不同的文件（很难精确控制大小，这里直接验证 sha256 变化）
        make_text_pdf(p, "Modified content Zhang San")
        sha2 = va.sha256_file(p)

        # 如果碰巧大小相同（不太可能），至少 sha256 必须不同
        self.assertNotEqual(sha1, sha2, "不同内容必须产生不同 SHA-256")

    def test_tier_dir_and_sibling_counted_together(self):
        """档位目录和同级新文件并存时都计入。"""
        from collections import Counter
        # 模拟 _N份 目录下既有档位子目录又有同级 PDF
        d = os.path.join(self.tmpdir, "岗位", "已收集简历", "7.01_2份")
        strong = os.path.join(d, "强推")
        os.makedirs(strong)
        make_text_pdf(os.path.join(strong, "甲_开发_3年.pdf"), "Jia")
        make_text_pdf(os.path.join(d, "乙_开发_4年.pdf"), "Yi")

        result = va.collect(d)
        # 应该收到 2 个文件（档位内 1 + 同级 1）
        names = [os.path.basename(f) for f in result.count_files]
        self.assertEqual(len(result.count_files), 2, f"档位+同级应同时统计: {names}")

    def test_docx_now_validated(self):
        """DOCX 现在参与姓名和薪酬检查（旧版完全绕过）。"""
        p = os.path.join(self.tmpdir, "李四_开发_5年.docx")
        make_docx(p, "Li Si\nSalary: 30K")
        result = va.extract_content(p)
        self.assertFalse(result.blocked)
        self.assertIn("30K", result.text)

    def test_blocked_zip_stops(self):
        """安全检查失败的 ZIP → STOP（旧版只告警）。"""
        p = os.path.join(self.tmpdir, "bad.zip")
        with open(p, "wb") as f:
            f.write(b"PK\x03\x04 corrupt not real zip")
        result = va.collect(p)
        # 阻断的 zip 应在 scan_files 中标记 __BLOCKED__
        blocked = [s for s in result.scan_files if s[0].startswith("__BLOCKED__:")]
        self.assertTrue(len(blocked) > 0, "安全检查失败的 ZIP 应标记阻断")

    def test_salary_in_zip_member_detected(self):
        """ZIP 内简历的薪酬能被检测到（旧版 os.walk 进不去 zip）。"""
        import fitz
        # 创建带薪酬的 PDF
        tmp_pdf = os.path.join(self.tmpdir, "inner.pdf")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Wang Wu\nSalary: 25-30K", fontsize=12)
        doc.save(tmp_pdf)
        doc.close()

        # 打成 ZIP
        zip_path = os.path.join(self.tmpdir, "package.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(tmp_pdf, "resume.pdf")

        result = va.collect(zip_path)
        # ZIP 内 PDF 被解压到 scan_files
        scan_displays = [s[0] for s in result.scan_files]
        self.assertTrue(any("resume.pdf" in d for d in scan_displays), "ZIP 内简历应被解压扫描")


    def test_manifest_not_validated_stops(self):
        """manifest 有未 validated 记录时闸门 STOP（P2 修复验证）。"""
        import json
        d = os.path.join(self.tmpdir, "项目", "岗位", "已收集简历", "7.01_1份")
        os.makedirs(d)
        make_text_pdf(os.path.join(d, "ZhangSan_后端_5年.pdf"), "Zhang San Resume")

        # 建一份有未 validated 记录的 manifest
        manifestPath = os.path.join(self.tmpdir, "manifest.json")
        with open(manifestPath, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "records": {
                "r1": {"record_id": "r1", "status": "archived"}  # 不是 validated
            }}, f)

        old_argv = sys.argv
        sys.argv = ["verify_archive.py", d, "--manifest", manifestPath]
        try:
            rc = None
            try:
                va.main()
            except SystemExit as e:
                rc = e.code
            self.assertEqual(rc, 1, "manifest 有未 validated 记录必须 STOP")
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main(verbosity=2)
