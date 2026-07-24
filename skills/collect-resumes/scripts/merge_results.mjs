// merge_results.mjs
// 串行合并并行下载产生的独立结果 JSON 到主 manifest。
//
// 用法: node merge_results.mjs [--manifest <path>] [--results-dir <path>]
//
// 为什么需要合并：多个 download_attachment 进程并行下载时，
// 各自只写独立 .result.json（不改主 manifest），避免并发覆盖。
// 下载全部完成后，由本脚本单进程串行读取所有结果并更新 manifest。

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  readManifest, writeManifestAtomic, getRecord, transitionRecord, upsertRecord,
} from './lib/manifest.mjs';
import { sha256File } from './lib/file_identity.mjs';
import { MANIFEST as MANIFEST_DEFAULT, RESULTS_DIR as RESULTS_DIR_DEFAULT } from './lib/paths.mjs';

/**
 * 合并所有结果文件到 manifest。
 * @param {string} manifestPath
 * @param {string} resultsDir
 * @returns {{ merged: number, skipped: number, errors: array }}
 */
export function mergeResults(manifestPath, resultsDir) {
  resultsDir = resultsDir || RESULTS_DIR_DEFAULT;
  let manifest = readManifest(manifestPath);
  const resultFiles = fs.existsSync(resultsDir)
    ? fs.readdirSync(resultsDir).filter(f => f.endsWith('.result.json'))
    : [];

  let merged = 0, skipped = 0;
  const errors = [];

  for (const f of resultFiles) {
    const resultPath = path.join(resultsDir, f);
    let result;
    try {
      result = JSON.parse(fs.readFileSync(resultPath, 'utf8'));
    } catch (e) {
      errors.push({ file: f, error: `结果 JSON 解析失败: ${e.message}` });
      continue;
    }

    const rec = getRecord(manifest, result.record_id);
    if (!rec) {
      errors.push({ file: f, error: `记录 ${result.record_id} 不在 manifest 中` });
      continue;
    }

    // 前置状态校验：只有 verified/downloading/downloaded 可推进
    if (!['verified', 'downloading', 'downloaded'].includes(rec.status)) {
      skipped++;
      continue;
    }

    // 路径绑定校验：result.target_path 必须与 manifest 的 target_dir+target_filename 一致
    // 防止伪造结果 JSON 指向归档外或他人的文件
    if (!rec.target_dir || !rec.target_filename) {
      errors.push({ file: f, error: `记录 ${result.record_id} 缺少 target_dir/target_filename` });
      continue;
    }
    const expectedPath = path.join(rec.target_dir, rec.target_filename).replace(/\\/g, '/');
    const actualResultPath = (result.target_path || '').replace(/\\/g, '/');
    if (actualResultPath !== expectedPath) {
      errors.push({ file: f, error: `target_path 与 manifest 绑定不符: 期望 ${expectedPath}，结果 ${actualResultPath}` });
      continue;
    }

    // 校验目标文件确实存在且哈希一致（防结果文件被篡改）
    if (!fs.existsSync(result.target_path)) {
      errors.push({ file: f, error: `目标文件不存在: ${result.target_path}` });
      continue;
    }
    const actualSha = sha256File(result.target_path);
    if (actualSha !== result.sha256) {
      errors.push({ file: f, error: `哈希不一致: manifest 期望 ${result.sha256}，实际 ${actualSha}` });
      continue;
    }

    // 状态机推进
    let updated = rec;
    if (updated.status === 'verified') updated = transitionRecord(updated, 'downloading');
    updated = transitionRecord(updated, 'downloaded');
    updated.sha256 = result.sha256;
    updated.target_path = result.target_path;
    updated = transitionRecord(updated, 'archived');
    manifest = upsertRecord(manifest, updated);
    merged++;

    // 合并成功后删除结果文件
    fs.unlinkSync(resultPath);
  }

  writeManifestAtomic(manifestPath, manifest);
  return { merged, skipped, errors };
}

function main() {
  const args = process.argv.slice(2);
  const manifestPath = getArg(args, '--manifest') || MANIFEST_DEFAULT;
  const resultsDir = getArg(args, '--results-dir') || RESULTS_DIR_DEFAULT;

  const { merged, skipped, errors } = mergeResults(manifestPath, resultsDir);
  console.log(`合并完成: ${merged} 条已归档，${skipped} 条跳过`);
  if (errors.length) {
    console.error(`\n🔴 ${errors.length} 个错误：`);
    for (const e of errors) console.error(`  ${e.file}: ${e.error}`);
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
