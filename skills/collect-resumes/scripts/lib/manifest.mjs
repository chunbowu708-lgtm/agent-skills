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
  needs_resolution: ['verified', 'excluded', 'duplicate', 'blocked'],
  verified: ['downloading', 'blocked'],
  downloading: ['downloaded', 'blocked'],
  downloaded: ['archived', 'blocked'],
  archived: ['validated', 'blocked'],
  excluded: ['needs_resolution'],   // 仅重复类排除码（DUPLICATE_*）可经 resolve --new-version 重开；resolveRecord 侧按码白名单把关，NOT_RESUME 等人工排除不放行
  duplicate: ['needs_resolution'],  // 仅一条重开路：resolve --new-version（候选人更新版简历场景）
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

/**
 * 把一条记录推进到 excluded（结构化原因），按状态机合法路径多跳
 * （verified→blocked→needs_resolution→excluded；状态机无 verified→excluded 直达边）。
 * 单一实现：resolve_records --exclude 与下载器的同日冲突自动排除共用。
 */
export function transitionToExcluded(record, code, message) {
  let r = record;
  if (r.status === 'verified') {
    r = transitionRecord(r, 'blocked', { code, message: `排除前置（${message}）` });
  }
  if (r.status === 'blocked') r = transitionRecord(r, 'needs_resolution');
  if (['discovered', 'needs_resolution', 'downloading', 'downloaded'].includes(r.status)) {
    r = transitionRecord(r, 'excluded', { code, message });
  }
  if (r.status !== 'excluded') {
    throw new Error(`EXCLUDE_UNSUPPORTED: 状态 "${record.status}" 的记录不能排除（终态记录保留历史）`);
  }
  return r;
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
 * 先写唯一后缀的 .tmp（pid+timestamp，防并发互踩），校验 JSON 可解析，再 rename 替换目标。
 * 失败时保留旧目标不损坏。
 *
 * 2026-08-04 修复：旧版 .tmp 文件名固定（`${targetPath}.tmp`），两个脚本并发写时
 * 互踩同一 .tmp → 后写者覆盖前写者的半成品 → rename 落地损坏内容。
 */
export function writeManifestAtomic(targetPath, data) {
  const dir = path.dirname(targetPath);
  fs.mkdirSync(dir, { recursive: true });
  const json = JSON.stringify(data, null, 2);
  // 写前自校验：确保写出的 JSON 能被解析回来
  JSON.parse(json);
  const tmp = `${targetPath}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmp, json, 'utf8');
  try {
    fs.renameSync(tmp, targetPath);
  } catch (e) {
    // rename 失败时清理 .tmp，避免残留
    try { fs.unlinkSync(tmp); } catch {}
    throw e;
  }
}

/**
 * 读取 manifest，不存在则返回空骨架。
 * 读取时归一化历史非法状态（如 'skipped'，TRANSITIONS 无此键、永久卡死只能手改 JSON）
 * → excluded（保留原因），任意写入方下次落盘即完成迁移。
 */
export function readManifest(targetPath) {
  if (!fs.existsSync(targetPath)) {
    return { schema_version: SCHEMA_VERSION, batches: {}, records: {}, processed: {} };
  }
  const m = JSON.parse(fs.readFileSync(targetPath, 'utf8'));
  if (m.schema_version !== SCHEMA_VERSION) {
    throw new Error(`MANIFEST_SCHEMA_MISMATCH: 期望 v${SCHEMA_VERSION}，实际 v${m.schema_version}`);
  }
  return migrateLegacyStates(m);
}

const KNOWN_STATUSES = new Set(Object.keys(TRANSITIONS));

function migrateLegacyStates(m) {
  const entries = Object.entries(m.records || {});
  if (!entries.some(([, rec]) => rec && rec.status && !KNOWN_STATUSES.has(rec.status))) return m;
  const records = {};
  for (const [rid, rec] of entries) {
    if (rec && rec.status && !KNOWN_STATUSES.has(rec.status)) {
      records[rid] = {
        ...rec,
        status: 'excluded',
        exclude_reason: {
          code: `LEGACY_${String(rec.status).toUpperCase()}_MIGRATED`,
          message: rec.skip_reason || `历史非法状态 "${rec.status}"，读取时自动归一化为 excluded`,
        },
      };
    } else {
      records[rid] = rec;
    }
  }
  return { ...m, records };
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
