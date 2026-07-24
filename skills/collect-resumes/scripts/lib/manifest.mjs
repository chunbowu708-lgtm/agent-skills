// scripts/lib/manifest.mjs
// Manifest 核心模块：稳定记录 ID、状态机、schema 校验、原子写入。
//
// 设计依据：docs/superpowers/specs/2026-07-10-resume-collection-safety-pipeline-design.md
// 不变量：record_id 只由来源标识（message_id + attachment_id/url）决定，
//         不受目标文件名影响；状态机禁止跳过；写入原子。

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export const SCHEMA_VERSION = 1;

// ---- 状态机 ----
// 合法转换：每条记录只能按以下方向推进，禁止跳过。
const TRANSITIONS = {
  discovered: ['needs_resolution', 'excluded', 'blocked'],
  needs_resolution: ['verified', 'excluded', 'blocked'],
  verified: ['downloading', 'blocked'],
  downloading: ['downloaded', 'blocked'],
  downloaded: ['archived', 'blocked'],
  archived: ['validated', 'blocked'],
  excluded: [],   // 终态
  blocked: ['needs_resolution', 'verified', 'downloading', 'downloaded', 'archived'], // 修复后回到安全状态
  validated: [],  // 终态
};

/**
 * 验证状态转换是否合法。非法转换抛 INVALID_TRANSITION。
 * @param {object} record - { status, ... }
 * @param {string} newStatus
 * @returns {object} 更新后的 record（不可变：返回副本）
 */
export function transitionRecord(record, newStatus, context = {}) {
  const allowed = TRANSITIONS[record.status] || [];
  if (!allowed.includes(newStatus)) {
    throw new Error(
      `INVALID_TRANSITION: ${record.status} -> ${newStatus} ` +
      `(record ${record.record_id}). 允许: ${allowed.join(', ') || '(终态)'}`
    );
  }
  const updated = { ...record, status: newStatus, updated_at: new Date().toISOString() };
  if (newStatus === 'blocked') {
    updated.errors = [...(record.errors || []), {
      code: context.code || 'UNKNOWN',
      message: context.message || '',
      at: new Date().toISOString(),
    }];
  }
  if (newStatus === 'excluded') {
    // excluded 必须有结构化原因代码，不接受裸自由文本
    if (!context.code) throw new Error('EXCLUDED_REQUIRES_CODE: excluded 必须有结构化原因代码');
    updated.exclude_reason = { code: context.code, message: context.message || '' };
  }
  return updated;
}

// ---- 稳定记录 ID ----

/**
 * 标准附件的 record_id。
 * 来源绑定：message_id + attachment_id。目标文件名不影响 ID。
 */
export function attachmentRecordId(messageId, attachmentId) {
  return 'sha256:' + sha256String(`${messageId}\0${attachmentId}`);
}

/**
 * 链接附件的 record_id。
 * URL 先规范化（小写 host + decode 实体 + 排序查询参数）再哈希。
 */
export function linkRecordId(messageId, url) {
  return 'sha256:' + sha256String(`${messageId}\0${normalizeUrl(url)}`);
}

function sha256String(s) {
  return crypto.createHash('sha256').update(s, 'utf8').digest('hex');
}

/**
 * URL 规范化：去前后空格、HTML 实体 decode、小写 scheme+host、保留 path/query。
 * 查询参数按 key 排序，避免 ?b=1&a=1 与 ?a=1&b=1 生成不同 ID。
 */
export function normalizeUrl(url) {
  let u = (url || '').trim();
  // 常见 HTML 实体
  u = u.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
  try {
    const parsed = new URL(u);
    parsed.hash = ''; // 去片段
    // 排序查询参数
    const params = [...parsed.searchParams.entries()].sort(([a], [b]) => a.localeCompare(b));
    parsed.search = '';
    const base = parsed.toString().replace(/\/$/, '');
    if (params.length) {
      const qs = params.map(([k, v]) => `${k}=${v}`).join('&');
      return `${base}?${qs}`;
    }
    return base;
  } catch {
    // 不是合法 URL，按原始 decode 后的字符串规范化（小写）
    return u.toLowerCase();
  }
}

// ---- 原子写入 ----

/**
 * 原子写入 manifest JSON。
 * 先写 .tmp，校验 JSON 可解析，再 rename 替换目标。
 * 失败时保留旧目标不损坏。
 */
export function writeManifestAtomic(targetPath, data) {
  const dir = path.dirname(targetPath);
  fs.mkdirSync(dir, { recursive: true });
  const json = JSON.stringify(data, null, 2);
  // 写前自校验：确保写出的 JSON 能被解析回来
  JSON.parse(json);
  const tmp = `${targetPath}.tmp`;
  fs.writeFileSync(tmp, json, 'utf8');
  fs.renameSync(tmp, targetPath);
}

/**
 * 读取 manifest，不存在则返回空骨架。
 */
export function readManifest(targetPath) {
  if (!fs.existsSync(targetPath)) {
    return { schema_version: SCHEMA_VERSION, batches: {}, records: {} };
  }
  const m = JSON.parse(fs.readFileSync(targetPath, 'utf8'));
  if (m.schema_version !== SCHEMA_VERSION) {
    throw new Error(`MANIFEST_SCHEMA_MISMATCH: 期望 v${SCHEMA_VERSION}，实际 v${m.schema_version}`);
  }
  return m;
}

/**
 * 在 manifest 中按 record_id 查找记录。
 */
export function getRecord(manifest, recordId) {
  return manifest.records?.[recordId] || null;
}

/**
 * 安全更新单条记录（不可变：返回新 manifest 对象）。
 */
export function upsertRecord(manifest, record) {
  if (!record.record_id) throw new Error('RECORD_REQUIRES_ID');
  return {
    ...manifest,
    records: { ...manifest.records, [record.record_id]: record },
  };
}
