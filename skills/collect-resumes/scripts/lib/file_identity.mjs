// scripts/lib/file_identity.mjs
// 文件身份与安全提交：SHA-256、magic bytes 类型检测、事务式原子提交、冲突保护。
// 下载到 .part，校验通过后才原子 rename 提交；目标已存在绝不覆盖（同哈希幂等/异哈希阻断）。

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

/**
 * 计算文件 SHA-256（同步逐块流式，支持大文件）。
 *
 * 2026-08-04 修复：旧版 readFileSync 整份载入内存，QQ/网易大附件 1GB+ 直接 OOM。
 * 现在用 fs.openSync + 逐块 readSync（64KB/块），内存占用恒定。
 */
export function sha256File(filePath) {
  const h = crypto.createHash('sha256');
  const fd = fs.openSync(filePath, 'r');
  const buf = Buffer.alloc(65536);
  try {
    let bytesRead;
    while ((bytesRead = fs.readSync(fd, buf, 0, 65536, null)) > 0) {
      h.update(buf.subarray(0, bytesRead));
    }
  } finally {
    fs.closeSync(fd);
  }
  return h.digest('hex');
}

/**
 * 检测磁盘文件真实类型。只读文件头部（magic bytes 判定最多用前 200 字节，
 * 大文件整读会 OOM——美术作品集 zip 可达 GB 级）。
 * @returns {'pdf'|'zip'|'doc'|'rar'|'7z'|'image'|'html'|'unknown'}
 */
export function detectFileType(filePath) {
  const fd = fs.openSync(filePath, 'r');
  try {
    const buf = Buffer.alloc(512);
    const bytesRead = fs.readSync(fd, buf, 0, buf.length, 0);
    return detectTypeFromBuffer(buf.subarray(0, bytesRead));
  } finally {
    fs.closeSync(fd);
  }
}

/**
 * OLE2/CFB 头（老式 .doc/.xls/.ppt 复合文档二进制格式）。
 * 固定 8 字节：D0 CF 11 E0 A1 B1 1A E1
 */
const OLE2_MAGIC = [0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1];

/**
 * 从 Buffer 检测类型。供下载完成（内存 buffer）和磁盘文件共用。
 */
export function detectTypeFromBuffer(buf) {
  if (buf.length < 4) return 'unknown';
  const head8 = buf.slice(0, 8).toString('latin1');
  if (head8.startsWith('%PDF')) return 'pdf';
  if (head8.startsWith('PK')) {
    // ZIP 和 DOCX 都是 PK 开头。区分：DOCX 内部必含 word/ 目录
    // 简单启发：PK + 文件扩展名交给调用方，这里先返回 'zip'（容器格式）
    // 调用方结合 expectedType 和 parseDocx 判断
    return 'zip';
  }
  // RAR 压缩包（RAR4: Rar!\x1a\x07\x00, RAR5: Rar!\x1a\x07\x01\x00）
  if (head8.startsWith('Rar!\x1a\x07')) return 'rar';
  // 7z 压缩包
  if (buf.slice(0, 6).toString('latin1') === '7z\xbc\xaf\x27\x1c') return '7z';
  // OLE2/CFB：老式 .doc/.xls/.ppt 二进制格式（2026-07-29：曾因无此分支返回 unknown 导致下载被拒）
  if (buf.length >= 8) {
    const b = buf;
    let isOle2 = true;
    for (let i = 0; i < OLE2_MAGIC.length; i++) {
      if (b[i] !== OLE2_MAGIC[i]) { isOle2 = false; break; }
    }
    if (isOle2) return 'doc';
  }
  // 常见图片 magic bytes
  if (head8.startsWith('\x89PNG')) return 'image';
  if (head8.startsWith('\xFF\xD8\xFF')) return 'image'; // JPEG
  if (head8.startsWith('GIF8')) return 'image';
  if (head8.startsWith('BM')) return 'image'; // BMP
  // HTML 错误页检测
  const head200 = buf.slice(0, 200).toString('latin1');
  if (/<\s*html|<\s*!doctype/i.test(head200)) return 'html';
  return 'unknown';
}

/**
 * 验证下载内容类型与扩展名是否一致。
 * @param {string} ext - 文件扩展名（不含点，小写）
 * @param {string} detectedType - detectTypeFromBuffer 返回值
 * @returns {boolean} 是否一致
 */
export function typeMatchesExtension(ext, detectedType) {
  const extLower = (ext || '').toLowerCase();
  if (extLower === 'pdf') return detectedType === 'pdf';
  // DOCX 本质是 zip 容器，detectTypeFromBuffer 对 PK 头统一返回 'zip'
  if (extLower === 'docx') return detectedType === 'zip';
  if (extLower === 'doc') return detectedType === 'doc';
  if (extLower === 'zip') return detectedType === 'zip';
  if (extLower === 'rar') return detectedType === 'rar';
  if (extLower === '7z') return detectedType === '7z';
  if (['png', 'jpg', 'jpeg', 'gif', 'bmp'].includes(extLower)) return detectedType === 'image';
  // 未知扩展名不自动拒绝（可能是 rar 等，交给上层判断）
  return true;
}

/**
 * 事务式提交：把已校验通过的 .part 文件原子提交为正式目标。
 *
 * 规则：
 * - 目标不存在 → rename .part 到目标，返回 { outcome:'committed', sha256 }
 * - 目标存在且 SHA-256 相同 → 幂等成功，删除 .part，返回 { outcome:'idempotent', sha256 }
 * - 目标存在但 SHA-256 不同 → 抛 TARGET_CONFLICT（绝不覆盖），保留两份
 *
 * @param {string} partPath - 已下载校验通过的临时文件
 * @param {string} targetPath - 正式归档目标
 * @param {string} expectedType - 期望类型（'pdf'|'docx'|'zip'）
 * @returns {{ outcome:string, sha256:string }}
 */
export function commitVerifiedFile(partPath, targetPath, expectedType) {
  const detected = detectFileType(partPath);
  if (detected === 'html') {
    throw new Error(`DOWNLOAD_IS_HTML: ${partPath} 内容是 HTML（疑似 auth 过期/登录页），不提交`);
  }
  if (detected === 'unknown') {
    throw new Error(`DOWNLOAD_TYPE_UNKNOWN: ${partPath} 文件头不像 PDF/ZIP/图片`);
  }
  if (expectedType && detected !== expectedType && !(expectedType === 'docx' && detected === 'zip')) {
    throw new Error(`TYPE_MISMATCH: 期望 ${expectedType}，实际 ${detected}`);
  }
  const sha = sha256File(partPath);

  if (!fs.existsSync(targetPath)) {
    fs.mkdirSync(path.dirname(targetPath), { recursive: true });
    fs.renameSync(partPath, targetPath);
    return { outcome: 'committed', sha256: sha };
  }

  // 目标已存在：比较哈希
  const existingSha = sha256File(targetPath);
  if (existingSha === sha) {
    // 幂等：内容完全相同，删除多余 .part
    fs.unlinkSync(partPath);
    return { outcome: 'idempotent', sha256: sha };
  }

  // 冲突：绝不覆盖，保留冲突副本供排查（调用方可通过 err.conflictPath 清理）
  const conflictPath = `${targetPath}.conflict.${Date.now()}`;
  fs.renameSync(partPath, conflictPath);
  const err = new Error(
    `TARGET_CONFLICT: 目标 ${targetPath} 已存在但内容不同（新文件保留为 ${conflictPath}）。` +
    `原有 sha=${existingSha} 新文件 sha=${sha}。需人工裁决。`
  );
  err.conflictPath = conflictPath;
  throw err;
}
