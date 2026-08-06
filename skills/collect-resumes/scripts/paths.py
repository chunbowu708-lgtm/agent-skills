# -*- coding: utf-8 -*-
"""
paths.py — Python 脚本共享路径常量单一真相源。
verify_archive.py 和 redact_salary.py 从此 import，不各自硬编码。
"""

SEVEN_ZIP = r"C:\Program Files\7-Zip\7z.exe"

# verify_archive.py 的 SHA-256 缓存目录
CACHE_DIR = "<PROJECT_ROOT>/notes/.verified_manifest"

# 归档根目录。
# ⚠️ JS 脚本用 lib/paths.mjs 的 ARCHIVE_ROOT（语言不同无法共享文件）。
# 改这里必须同步改 lib/paths.mjs:15，反之亦然。两处必须一致。
ARCHIVE_ROOT = os.environ.get("ARCHIVE_ROOT", "")
