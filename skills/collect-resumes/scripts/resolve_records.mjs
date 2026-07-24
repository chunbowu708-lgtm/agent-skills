// resolve_records.mjs
// 将候选人、岗位、目标目录绑定到 manifest 记录。
//
// 用法:
//   node resolve_records.mjs --manifest <path> --record <id> \
//     --name 张三 --job 特效设计师 --filename 张三_特效设计师_5年.pdf
//
// 目标目录从归档根动态发现，路径必须唯一存在于归档根内，
// 歧义/未知岗位保持 needs_resolution，绝不创建不存在的目录。
//
// 不变量：
//   - 目标路径规范化后必须位于 ARCHIVE_ROOT 内（防路径逃逸）
//   - 岗位目录必须已存在且唯一（歧义 → needs_resolution）
//   - 不创建任何目录或文件（只更新 manifest）

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  readManifest, writeManifestAtomic, transitionRecord, upsertRecord,
} from './lib/manifest.mjs';
import { ARCHIVE_ROOT, MANIFEST as MANIFEST_DEFAULT } from './lib/paths.mjs';

/**
 * 从归档根递归发现所有含"已收集简历"的岗位目录。
 * @returns {Array<{ job_dir: string, collected_dir: string }>}
 *   job_dir 是相对于 ARCHIVE_ROOT 的岗位路径（如 "山海弹珠项目/美术端/特效设计师"）
 *   collected_dir 是绝对路径（如 .../特效设计师/已收集简历）
 */
export function discoverJobDirs(root) {
  root = root || ARCHIVE_ROOT;
  const results = [];
  if (!fs.existsSync(root)) return results;

  function walk(dir) {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      if (!e.isDirectory()) continue;
      const full = path.join(dir, e.name);
      // 发现"已收集简历"子目录 → 这是一个岗位目录
      const collectedPath = path.join(full, '已收集简历');
      const collectedLegacy = path.join(full, '收集到简历');
      if (fs.existsSync(collectedPath) || fs.existsSync(collectedLegacy)) {
        const rel = path.relative(root, full).replace(/\\/g, '/');
        results.push({
          job_dir: rel,
          collected_dir: fs.existsSync(collectedPath) ? collectedPath : collectedLegacy,
        });
      }
      // 继续递归（支持团队/分组层）
      walk(full);
    }
  }
  walk(root);
  return results;
}

/**
 * 按岗位名模糊匹配已发现的岗位目录。
 * @param {string} jobName - 用户指定的岗位名（如"特效设计师"）
 * @param {array} jobDirs - discoverJobDirs 的结果
 * @returns {{ matched: array, ambiguous: boolean }}
 */
export function matchJobDir(jobName, jobDirs) {
  if (!jobName) return { matched: [], ambiguous: false };
  const lower = jobName.toLowerCase();
  // 精确匹配最后一级目录名
  const exact = jobDirs.filter(j => {
    const lastSeg = j.job_dir.split('/').pop().toLowerCase();
    return lastSeg === lower;
  });
  if (exact.length >= 1) return { matched: exact, ambiguous: exact.length > 1 };
  // 包含匹配
  const partial = jobDirs.filter(j => j.job_dir.toLowerCase().includes(lower));
  return { matched: partial, ambiguous: partial.length > 1 };
}

/**
 * 验证目标路径是否在归档根内（防路径逃逸）。
 */
export function isWithinArchive(targetPath, root) {
  root = path.resolve(root || ARCHIVE_ROOT);
  const target = path.resolve(targetPath);
  const rel = path.relative(root, target);
  return rel !== '' && !rel.startsWith('..') && !path.isAbsolute(rel);
}

/**
 * 核心解析逻辑：为一条 record 绑定候选人/岗位/目标路径。
 *
 * @param {object} manifest
 * @param {string} recordId
 * @param {string} name - 候选人姓名
 * @param {string} jobName - 岗位名
 * @param {string} filename - 目标文件名（不含目录）
 * @param {array} jobDirs - discoverJobDirs 结果（可注入，测试用）
 * @returns {{ manifest: object, status: 'resolved'|'ambiguous'|'not_found'|'bad_record', detail: string }}
 */
export function resolveRecord(manifest, recordId, name, jobName, filename, jobDirs) {
  const rec = manifest.records?.[recordId];
  if (!rec) return { manifest, status: 'bad_record', detail: `记录 ${recordId} 不存在` };
  if (!['needs_resolution', 'blocked'].includes(rec.status)) {
    return { manifest, status: 'bad_record', detail: `记录状态 ${rec.status} 不可解析（需 needs_resolution 或 blocked）` };
  }

  const dirs = jobDirs !== undefined ? jobDirs : discoverJobDirs();
  const { matched, ambiguous } = matchJobDir(jobName, dirs);

  if (ambiguous) {
    // 歧义（如 Unity 三岗）→ 保持 needs_resolution
    const updated = { ...rec, candidate_name: name, job_name: jobName, target_filename: filename };
    return {
      manifest: upsertRecord(manifest, updated),
      status: 'ambiguous',
      detail: `岗位"${jobName}"匹配到多个目录：\n${matched.map(m => '  ' + m.job_dir).join('\n')}`,
    };
  }
  if (matched.length === 0) {
    const updated = { ...rec, candidate_name: name, job_name: jobName, target_filename: filename };
    return {
      manifest: upsertRecord(manifest, updated),
      status: 'not_found',
      detail: `岗位"${jobName}"未找到已存在的岗位目录（不自动创建）`,
    };
  }

  // 唯一匹配
  const target = path.join(matched[0].collected_dir, filename).replace(/\\/g, '/');
  if (!isWithinArchive(target)) {
    return {
      manifest: upsertRecord(manifest, transitionRecord(rec, 'blocked', {
        code: 'PATH_ESCAPE', message: `目标路径 ${target} 不在归档根内`,
      })),
      status: 'bad_record',
      detail: `路径逃逸：${target}`,
    };
  }

  let updated = { ...rec, candidate_name: name, job_name: jobName, target_dir: matched[0].collected_dir, target_filename: filename };
  if (updated.status === 'blocked') {
    // 从 blocked 恢复到 needs_resolution 再推进
    updated = { ...updated, status: 'needs_resolution', errors: [...(updated.errors || [])] };
  }
  updated = transitionRecord(updated, 'verified');
  return { manifest: upsertRecord(manifest, updated), status: 'resolved', detail: target };
}

// CLI 入口
function main() {
  const args = process.argv.slice(2);
  const manifestPath = getArg(args, '--manifest') || MANIFEST_DEFAULT;
  const recordId = getArg(args, '--record');
  const name = getArg(args, '--name');
  const jobName = getArg(args, '--job');
  const filename = getArg(args, '--filename');

  if (!recordId || !name || !jobName || !filename) {
    console.error('用法: resolve_records.mjs --record <id> --name <姓名> --job <岗位> --filename <文件名> [--manifest <path>]');
    process.exit(1);
  }

  const manifest = readManifest(manifestPath);
  const result = resolveRecord(manifest, recordId, name, jobName, filename);
  writeManifestAtomic(manifestPath, result.manifest);

  if (result.status === 'resolved') {
    console.log(`✅ 已解析: ${recordId} -> ${result.detail}`);
  } else {
    console.error(`⚠️ ${result.status}: ${result.detail}`);
    process.exit(2);
  }
}

function getArg(args, name) {
  const i = args.indexOf(name);
  return i !== -1 ? args[i + 1] : undefined;
}

const isDirectRun = process.argv[1] &&
  fileURLToPath(import.meta.url).replace(/\\/g, '/') === path.resolve(process.argv[1]).replace(/\\/g, '/');
if (isDirectRun) main();

export { main };
