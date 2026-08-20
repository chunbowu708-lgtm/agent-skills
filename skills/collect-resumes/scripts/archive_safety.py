# -*- coding: utf-8 -*-
"""
archive_safety.py — ZIP 归档安全检查 + 中文文件名还原

防：
  - 路径穿越（Zip Slip）：绝对路径、盘符、.. 路径
  - 符号链接成员
  - 加密 ZIP
  - 损坏 ZIP
  - 嵌套归档（zip 内套 zip/rar/7z）
  - 压缩炸弹（超高压缩比）
  - 未知文档格式
  - 零有效简历
  - 多候选人（业务契约：一包一人）

decode_zip_name：还原 zip 内中文文件名（cp437 乱码 → utf-8 优先 → gbk 兜底），
供 check_zip / verify_archive / redact_salary 复用（单一真相源）。

任何无法完整验证的情况返回 SafetyResult(blocked=True)，
verify_archive 不得放行。
"""

import os
import re
import zipfile

# 安全限制
MAX_MEMBERS = 1000         # ZIP 内最大成员数（美术作品集文件多，200→500→1000）
MAX_TOTAL_UNCOMPRESSED = 4 * 1024 * 1024 * 1024  # 4GB 总解压大小
MAX_SINGLE_FILE_DOC = 500 * 1024 * 1024          # 500MB 简历文档单文件
MAX_SINGLE_FILE_MEDIA = 2 * 1024 * 1024 * 1024   # 2GB 作品视频单文件
MAX_COMPRESSION_RATIO = 100    # 压缩比上限（解压大小/压缩大小）

# 允许的成员扩展名（只验证这些格式，其余阻断）
# .doc 加在此处（2026-07-29：老式 .doc 经 antiword 可提取，纳入校验）
ALLOWED_MEMBER_EXTS = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".gif", ".bmp"}
# 作品素材扩展名（允许存在于 ZIP 内，但不做姓名/薪酬提取——视频无法 OCR）
PORTFOLIO_MEDIA_EXTS = {".mp4", ".mov", ".avi", ".mpg", ".webm", ".wmv", ".psd", ".ai", ".riff", ".ase", ".ppt", ".pptx", ".md", ".svg", ".tiff", ".xml", ".txt", ".rels", ".spine", ".skel", ".json", ".atlas"}
# 简历扩展名（需要做姓名/薪酬提取）
# .doc 是老式二进制 Word 格式（python-docx 不支持，走 antiword 提取）
# 2026-07-29：从 PORTFOLIO_MEDIA_EXTS 移到此处，使 .doc 在 zip 内也参与姓名/薪酬校验
RESUME_MEMBER_EXTS = {".pdf", ".docx", ".doc"}
# 嵌套归档扩展名（在 ZIP 内出现 → 阻断）
NESTED_ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz"}


def decode_zip_name(info):
    """还原 zip 内文件名（单一真相源，供 check_zip/verify_archive/redact_salary 复用）。

    根因：Windows/网盘/邮箱等工具生成 zip 时，中文文件名用 UTF-8（或 GBK）字节
    写入，但未必正确设置 0x800 UTF-8 标志位，Python zipfile 会按 cp437 解码出
    乱码（如"游戏主美"→"µ╕╕µêÅ..."）。

    策略（按顺序尝试，先成功者胜）：
    1. 已含中文 → Python 已正确解码，直接返回
    2. 乱码 → encode('cp437') 还原原始字节，按 utf-8 → gbk 顺序解码，先解出中文者胜
    3. 都失败 → 返回原名（含纯 ASCII 名，如 resume.pdf）
    """
    name = info.filename
    if re.search(r"[\u4e00-\u9fff]", name):
        return name
    try:
        raw = name.encode("cp437")
    except Exception:
        return name
    for enc in ("utf-8", "gbk"):
        try:
            decoded = raw.decode(enc)
            if re.search(r"[\u4e00-\u9fff]", decoded):
                return decoded
        except Exception:
            continue
    return name


class SafetyResult:
    """ZIP 安全检查结果。"""

    def __init__(self, blocked=False, reason="", members=None, warnings=None):
        self.blocked = blocked
        self.reason = reason
        self.members = members or []  # 安全成员列表 [{name, size, ext}]
        self.warnings = warnings or []

    def __repr__(self):
        return f"SafetyResult(blocked={self.blocked}, reason='{self.reason}', members={len(self.members)})"


def check_zip(zip_path):
    """
    检查 ZIP 安全性。read-only：不实际解压到磁盘（只读元数据）。
    返回 SafetyResult。
    """
    if not os.path.isfile(zip_path):
        return SafetyResult(blocked=True, reason=f"文件不存在: {zip_path}")

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        return SafetyResult(blocked=True, reason="不是合法 ZIP 或已损坏（可能为 RAR，本工具不处理）")
    except Exception as e:
        return SafetyResult(blocked=True, reason=f"ZIP 打开失败: {e}")

    try:
        infos = zf.infolist()
    except Exception as e:
        zf.close()
        return SafetyResult(blocked=True, reason=f"ZIP 中央目录读取失败: {e}")

    # 成员数检查
    if len(infos) > MAX_MEMBERS:
        zf.close()
        return SafetyResult(blocked=True, reason=f"成员数 {len(infos)} 超过上限 {MAX_MEMBERS}")

    safe_members = []
    nested_members = []
    total_uncompressed = 0

    for info in infos:
        name = decode_zip_name(info)  # 还原中文名后再做安全检查/记录（乱码名无法正确判扩展名）
        # 跳过目录
        if info.is_dir():
            continue

        normalized = name.replace("\\", "/")

        # 跳过 macOS 元数据（__MACOSX/ 目录和 ._ 前缀文件）
        if "__MACOSX/" in normalized or os.path.basename(normalized).startswith("._"):
            continue

        # 1. 路径穿越检查（Zip Slip）
        if os.path.isabs(normalized) or re.match(r"^[A-Za-z]:", normalized):
            zf.close()
            return SafetyResult(blocked=True, reason=f"成员含绝对路径/盘符: {name}")
        if ".." in normalized.split("/"):
            zf.close()
            return SafetyResult(blocked=True, reason=f"成员含 .. 路径穿越: {name}")

        # 2. 符号链接检查（ZipInfo 的 external_attr 高位可能标记 symlink）
        #    unix mode 0xA000 = symlink
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if (unix_mode & 0xF000) == 0xA000:
            zf.close()
            return SafetyResult(blocked=True, reason=f"成员是符号链接: {name}")

        # 3. 嵌套归档（美术作品集可能含嵌套打包，跳过该成员不阻断）
        ext = os.path.splitext(name)[1].lower()
        if not ext:
            continue  # 无扩展名文件（如 _rels/.rels 等 Office 包隐藏元数据）跳过
        if ext in NESTED_ARCHIVE_EXTS:
            # 不解压不扫描（工具不做递归拆包），但必须显式警告——
            # 静默跳过会让"嵌套 zip 里藏薪酬简历"三层全放行
            nested_members.append(name)
            continue

        # 4. 加密检查
        #    ZIP 加密标志位：flag_bits & 0x1
        if info.flag_bits & 0x1:
            zf.close()
            return SafetyResult(blocked=True, reason=f"成员加密: {name}")

        # 5. 大小检查（按类型区分：文档 500MB / 视频素材 2GB）
        size_limit = MAX_SINGLE_FILE_MEDIA if ext in PORTFOLIO_MEDIA_EXTS else MAX_SINGLE_FILE_DOC
        if info.file_size > size_limit:
            zf.close()
            return SafetyResult(blocked=True, reason=f"成员过大 ({info.file_size} bytes > {size_limit}): {name}")
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
            zf.close()
            return SafetyResult(blocked=True, reason=f"总解压大小超过上限 {MAX_TOTAL_UNCOMPRESSED}")

        # 6. 压缩比检查（防炸弹）
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_COMPRESSION_RATIO:
                zf.close()
                return SafetyResult(blocked=True, reason=f"成员压缩比 {ratio:.0f}x 超过上限 {MAX_COMPRESSION_RATIO}x: {name}")

        # 7. 允许的扩展名：简历格式 + 作品素材格式都允许（ext 在第3步已声明）
        if ext not in ALLOWED_MEMBER_EXTS and ext not in PORTFOLIO_MEDIA_EXTS:
            zf.close()
            return SafetyResult(blocked=True, reason=f"成员格式 {ext} 不在允许列表: {name}")

        safe_members.append({"name": name, "size": info.file_size, "ext": ext, "is_resume": ext in RESUME_MEMBER_EXTS})

    zf.close()

    if not safe_members:
        result = SafetyResult(blocked=True, reason="ZIP 内无任何可验证的有效成员")
        if nested_members:
            result.warnings.append(f"含嵌套归档 {len(nested_members)} 个（未扫描内部）: {', '.join(nested_members[:3])}")
        return result

    # 区分：有简历成员 vs 纯作品包
    has_resume = any(m["is_resume"] for m in safe_members)
    result = SafetyResult(members=safe_members)
    if nested_members:
        result.warnings.append(f"含嵌套归档 {len(nested_members)} 个（未扫描内部，如藏薪酬需人工拆包确认）: {', '.join(nested_members[:3])}")
    if not has_resume:
        result.warnings.append("纯作品包：无 PDF/DOCX 简历成员，跳过包内姓名/薪酬检查（简历 PDF 应在 zip 外单独验证）")
    return result
