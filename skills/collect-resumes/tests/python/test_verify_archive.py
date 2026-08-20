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


def run_main_capture_exit(argv):
    """调 va.main()，捕获 SystemExit 退出码。"""
    old_argv = sys.argv
    sys.argv = ["verify_archive.py"] + argv
    try:
        rc = None
        try:
            va.main()
        except SystemExit as e:
            rc = e.code
        return rc
    finally:
        sys.argv = old_argv


class TestVerifyArchiveFailClosed(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_no_count_label_stops(self):
        """简历不在 _N份 目录内 → STOP。"""
        d = os.path.join(self.tmpdir, "项目", "岗位", "已收集简历", "7.01")  # 无 _N份
        os.makedirs(d)
        make_text_pdf(os.path.join(d, "张三_后端_5年.pdf"), "张三简历正文")
        rc = run_main_capture_exit([d])
        self.assertEqual(rc, 1, "无 _N份 标注必须 STOP（exit 1）")

    def test_sha256_cache_supersedes_mtime(self):
        """同 mtime+size 但内容不同 → SHA-256 不同 → 不复用旧缓存。"""
        p = os.path.join(self.tmpdir, "test.pdf")
        make_text_pdf(p, "Original content Zhang San")
        sha1 = va.sha256_file(p)
        make_text_pdf(p, "Modified content Zhang San")
        sha2 = va.sha256_file(p)
        self.assertNotEqual(sha1, sha2, "不同内容必须产生不同 SHA-256")

    def test_count_heads_tier_and_sibling_same_caliber(self):
        """数量闸门与 _count_heads 同口径：档位子目录 + 同级文件都计入人头。"""
        d = os.path.join(self.tmpdir, "岗位", "已收集简历", "7.01_2份")
        strong = os.path.join(d, "强推")
        os.makedirs(strong)
        open(os.path.join(strong, "张三_开发_3年.pdf"), "w").close()
        open(os.path.join(d, "李四_开发_4年.pdf"), "w").close()
        heads = va._count_heads(d)
        self.assertEqual(len(heads), 2, f"档位+同级应同时统计: {heads}")

    def test_count_gate_passes_on_consistent_dir(self):
        """_2份 目录人头=2 → 数量闸门 ✅（姓名 miss 只 warning，不 STOP）。"""
        d = os.path.join(self.tmpdir, "岗位", "已收集简历", "7.01_2份")
        os.makedirs(d)
        make_text_pdf(os.path.join(d, "张三_开发_3年.pdf"), "正文无姓名（模拟加密PDF）")
        make_text_pdf(os.path.join(d, "李四_开发_4年.pdf"), "正文无姓名（模拟加密PDF）")
        rc = run_main_capture_exit([d])
        self.assertEqual(rc, 0, "数量一致 + 姓名 miss 仅 warning → 应放行")

    def test_count_gate_counts_only_resume_files(self):
        """档位目录里的非简历文件（备注.txt）不虚增人头 → 不误 STOP。"""
        d = os.path.join(self.tmpdir, "岗位", "已收集简历", "7.01_1份")
        os.makedirs(d)
        make_text_pdf(os.path.join(d, "张三_开发_3年.pdf"), "正文无姓名")
        open(os.path.join(d, "备注.txt"), "w", encoding="utf-8").write("张三约了 8.15 面试")
        rc = run_main_capture_exit([d])
        self.assertEqual(rc, 0, "备注.txt 不应被当人头（旧版 os.listdir 全数 → 虚增误 STOP）")

    def test_image_resume_with_name_recognized(self):
        """BOSS 图片简历（姓名_岗位_年限.png，无"简历"关键词）→ 闸门认作简历。"""
        d = os.path.join(self.tmpdir, "岗位", "已收集简历", "7.02_1份")
        os.makedirs(d)
        open(os.path.join(d, "曾家敏_游戏ui设计师_6年.png"), "wb").write(b"\x89PNG fake")
        rc = run_main_capture_exit([d])
        self.assertEqual(rc, 0, "能解析出姓名的图片简历应被认（旧版因无'简历'关键词整目录 STOP）")

    def test_plain_artwork_image_not_resume(self):
        """作品散图（demo.jpg 等解析不出姓名）→ 不认作简历。"""
        self.assertFalse(va._is_resume_file("demo.jpg"))
        self.assertFalse(va._is_resume_file("截图.png"))
        self.assertFalse(va._is_resume_file("作品1.jpeg"))
        self.assertTrue(va._is_resume_file("曾家敏_游戏ui设计师_6年.png"))
        self.assertTrue(va._is_resume_file("王赫_UI设计师_12年.jpg"))

    def test_single_file_mode_skips_count_gate(self):
        """单文件模式：跳过数量闸门（只做姓名/薪酬/格式），不再必然 STOP。"""
        p = os.path.join(self.tmpdir, "王五_开发_5年.pdf")
        make_text_pdf(p, "Wang Wu Resume")
        rc = run_main_capture_exit([p])
        self.assertEqual(rc, 0, "单文件模式应可用（旧版落到散落根目录必 STOP，功能事实已死）")

    def test_docx_now_validated(self):
        """DOCX 参与姓名和薪酬检查。"""
        p = os.path.join(self.tmpdir, "李四_开发_5年.docx")
        make_docx(p, "Li Si\nSalary: 30K")
        result = va.extract_content(p)
        self.assertFalse(result.blocked)
        self.assertIn("30K", result.text)

    def test_blocked_zip_warns(self):
        """安全检查失败的 ZIP → 标记阻断（warning 级，需人工看原件）。"""
        p = os.path.join(self.tmpdir, "bad.zip")
        with open(p, "wb") as f:
            f.write(b"PK\x03\x04 corrupt not real zip")
        result = va.collect(p)
        blocked = [s for s in result.scan_files if s[0].startswith("__BLOCKED__:")]
        self.assertTrue(len(blocked) > 0, "安全检查失败的 ZIP 应标记阻断")

    def test_salary_in_zip_member_detected(self):
        """ZIP 内简历的薪酬能被检测到。"""
        import fitz
        tmp_pdf = os.path.join(self.tmpdir, "inner.pdf")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Wang Wu\nSalary: 25-30K", fontsize=12)
        doc.save(tmp_pdf)
        doc.close()

        zip_path = os.path.join(self.tmpdir, "package.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(tmp_pdf, "resume.pdf")

        result = va.collect(zip_path)
        scan_displays = [s[0] for s in result.scan_files]
        self.assertTrue(any("resume.pdf" in d for d in scan_displays), "ZIP 内简历应被解压扫描")

    def test_manifest_blocked_scoped_to_target_dir(self):
        """manifest blocked 只拦"绑定本批次目录"的记录（全库扫描会被历史记录劫持）。"""
        import json
        collected = os.path.join(self.tmpdir, "项目", "岗位", "已收集简历")
        d = os.path.join(collected, "7.01_1份")
        os.makedirs(d)
        make_text_pdf(os.path.join(d, "张三_后端_5年.pdf"), "正文无姓名")

        # 场景A：blocked 绑定到本目录批次（target_dir=collected/7.01_暂定）→ STOP
        manifest_a = os.path.join(self.tmpdir, "ma.json")
        with open(manifest_a, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "records": {
                "r1": {"record_id": "r1", "status": "blocked",
                       "target_dir": os.path.join(collected, "7.01_暂定"),
                       "errors": [{"code": "LINK_EXPIRED"}]},
            }}, f)
        rc = run_main_capture_exit([d, "--manifest", manifest_a])
        self.assertEqual(rc, 1, "绑定本批次的 blocked 记录必须 STOP")

        # 场景B：blocked 绑定到其他目录（9.9 批次）→ 不拦
        manifest_b = os.path.join(self.tmpdir, "mb.json")
        with open(manifest_b, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "records": {
                "r2": {"record_id": "r2", "status": "blocked",
                       "target_dir": os.path.join(self.tmpdir, "别的岗位", "已收集简历", "9.9_暂定"),
                       "errors": [{"code": "LINK_EXPIRED"}]},
            }}, f)
        rc = run_main_capture_exit([d, "--manifest", manifest_b])
        self.assertEqual(rc, 0, "无关目录的 blocked 不应劫持本批次闸门")


class TestRedactSalaryConsistency(unittest.TestCase):
    """redact_salary 真实脱敏与 dry-run 一致性。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_real_redaction_catches_split_layout_salary(self):
        """薪酬标签和数字在不同 x 位置 → 真实脱敏仍能涂掉。"""
        sys.path.insert(0, SCRIPTS_DIR)
        import redact_salary as rs
        import fitz

        p = os.path.join(self.tmpdir, "split.pdf")
        doc = fitz.open()
        pg = doc.new_page()
        pg.insert_text((72, 72), "Salary:", fontsize=12)
        pg.insert_text((220, 72), "25-30K", fontsize=12)
        doc.save(p)
        doc.close()

        h_dry, _ = rs.redact_pdf(p, dry_run=True)
        self.assertGreater(len(h_dry), 0, "dry-run 应检出薪酬")

        h_real, mod = rs.redact_pdf(p, dry_run=False)
        self.assertTrue(mod, "真实脱敏应修改文件（不再静默丢弃）")
        doc = fitz.open(p)
        text = doc[0].get_text()
        doc.close()
        self.assertNotIn("25-30K", text, "薪酬应被白色矩形覆盖")


class TestPendingDirAutoRename(unittest.TestCase):
    """_暂定 中转目录自动合并为 _{N}份。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.collected = os.path.join(self.tmpdir, "岗位", "已收集简历")

    def test_pending_dir_renamed_to_N份(self):
        """resolve 落 {M.DD}_暂定/ → 闸门自动 rename 为 {M.DD}_{N}份 再校验。"""
        d = os.path.join(self.collected, "7.29_暂定")
        os.makedirs(d)
        open(os.path.join(d, "张三_后端_5年.pdf"), "w").close()
        open(os.path.join(d, "李四_后端_3年.pdf"), "w").close()

        va._finalize_pending_dirs(self.collected)

        names = os.listdir(self.collected)
        self.assertIn("7.29_2份", names, "_暂定 应 rename 为 _2份")
        self.assertNotIn("7.29_暂定", names, "_暂定 不应残留")

    def test_empty_pending_dir_removed(self):
        """空 _暂定 目录（无任何文件）→ 删除。"""
        d = os.path.join(self.collected, "7.29_暂定")
        os.makedirs(d)
        va._finalize_pending_dirs(self.collected)
        self.assertFalse(os.path.exists(d), "空 _暂定 目录应被删除")

    def test_pending_with_only_non_resume_files_kept(self):
        """_暂定 只有非简历文件（说明.txt）→ 保留不删（旧版 rmtree 会误删数据）。"""
        d = os.path.join(self.collected, "7.29_暂定")
        os.makedirs(d)
        open(os.path.join(d, "说明.txt"), "w", encoding="utf-8").close()
        va._finalize_pending_dirs(self.collected)
        self.assertTrue(os.path.exists(d), "含非简历文件的 _暂定 不应被删")

    def test_same_name_reinvest_no_dataloss(self):
        """P0 回归：已有 7.29_2份 + 同名重投 _暂定 → 绝不删既有批次，同路径冲突加后缀保留。

        旧版 bug：final_path（7.29_2份）同时是合并目标和合并源 → 文件全被 skip
        → rmtree(final_path) 把整批 2 人档案删光。
        """
        existing = os.path.join(self.collected, "7.29_2份")
        strong = os.path.join(existing, "强推")
        os.makedirs(strong)
        open(os.path.join(existing, "张三_后端_5年.pdf"), "w").close()   # 根级
        open(os.path.join(strong, "李四_后端_3年.pdf"), "w").close()     # 已评估进档位

        pending = os.path.join(self.collected, "7.29_暂定")
        os.makedirs(pending)
        open(os.path.join(pending, "张三_后端_5年.pdf"), "w").close()  # 同名重投（同路径冲突）

        va._finalize_pending_dirs(self.collected)

        # 既有目录必须还在，3 个文件一个不丢（同路径冲突 → _重投 后缀保留）
        self.assertTrue(os.path.isdir(existing), "既有 _N份 批次绝不能被删（P0）")
        all_files = [f for _, _, fs in os.walk(existing) for f in fs]
        self.assertEqual(len(all_files), 3,
                         f"旧档2 + 重投1 全保留（重投带 _重投 后缀）: {all_files}")
        self.assertIn("李四_后端_3年.pdf", all_files)
        self.assertTrue(any("重投" in f for f in all_files), "同路径冲突文件应加 _重投 后缀保留")
        # 人头仍 2（张三去重），目录名自洽
        self.assertEqual(len(va._count_heads(existing)), 2)
        self.assertEqual(os.path.basename(existing), "7.29_2份")

    def test_merge_split_batches_preserves_conflicts(self):
        """同天两个 _N份 目录合并：冲突文件加后缀保留，不静默丢。"""
        a = os.path.join(self.collected, "7.30_1份")
        b = os.path.join(self.collected, "7.30_2份")
        os.makedirs(a); os.makedirs(b)
        open(os.path.join(a, "张三_后端_5年.pdf"), "w").close()
        open(os.path.join(b, "张三_后端_5年.pdf"), "w").close()  # 同名冲突
        open(os.path.join(b, "王五_后端_1年.pdf"), "w").close()

        va._merge_split_batches(self.collected)

        merged = os.path.join(self.collected, "7.30_2份")
        self.assertTrue(os.path.isdir(merged))
        all_files = sorted(f for _, _, fs in os.walk(merged) for f in fs)
        self.assertEqual(all_files,
                         ["张三_后端_5年.pdf", "张三_后端_5年_重投1.pdf", "王五_后端_1年.pdf"],
                         f"冲突双保留: {all_files}")
        self.assertEqual(len(va._count_heads(merged)), 2)

    def test_gate_on_pending_target_converges_and_passes(self):
        """闸门直接跑在 _暂定 目录上：先收敛为 _N份，再数量校验通过（编排器场景）。"""
        d = os.path.join(self.collected, "7.31_暂定")
        os.makedirs(d)
        make_text_pdf(os.path.join(d, "张三_开发_5年.pdf"), "正文无姓名（模拟加密）")
        make_text_pdf(os.path.join(d, "李四_开发_3年.pdf"), "正文无姓名（模拟加密）")
        rc = run_main_capture_exit([d])
        self.assertEqual(rc, 0, f"_暂定 target 应自动收敛且数量自洽（exit 0），实际 {rc}")
        # 目录已被收敛
        names = os.listdir(self.collected)
        self.assertIn("7.31_2份", names, "_暂定 应被收敛为 _2份")
        self.assertNotIn("7.31_暂定", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
