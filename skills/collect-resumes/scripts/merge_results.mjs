// merge_results.mjs
// 合并下载产生的独立结果 JSON 到主 manifest。
//
// 用法: node merge_results.mjs [--manifest <path>] [--results-dir <path>]
//
// 定位：修复/收尾工具。download_attachment 和 batch_download_links 批量完成后
// 已自动调用本模块的 mergeResults（单进程串行，无并发写风险）；独立运行本脚本
// 用于多进程并行下载、批量中途 crash 后的恢复、清理残留 result.json。

import fs from 'node:fs';
import path from 'node:path';
import {
  readManifest, writeManifestAtomic, getRecord, transitionRecord, upsertRecord,
} from './lib/manifest.mjs';
import { sha256File } from './lib/file_identity.mjs';
import { getArg, isDirectRun } from './lib/cli_helpers.mjs';
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

    // 整条记录处理包进 try/catch（2026-07-29，A3 修复）：
    // 旧版状态机推进抛 INVALID_TRANSITION 在 try/catch 外 → 整批中止且重跑卡死。
    // 现在单条失败记入 errors 继续处理后续记录，不阻断整批。
    try {
      const rec = getRecord(manifest, result.record_id);
      if (!rec) {
        errors.push({ file: f, error: `记录 ${result.record_id} 不在 manifest 中` });
        continue;
      }

      // 前置状态校验：只有 verified/downloading/downloaded 可推进。
      // 其余（已 archived/excluded/duplicate 等）= 结果文件已过时 → 删掉防残留。
      if (!['verified', 'downloading', 'downloaded'].includes(rec.status)) {
        try { fs.unlinkSync(resultPath); } catch {}
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

      // 状态机推进（幂等：从任何合法起点推进到 archived）
      // 2026-07-29，A3 修复：旧版对 'downloaded' 状态会再次 transitionRecord→'downloaded'
      // 抛 INVALID_TRANSITION。现改为只在未到达时推进。
      let updated = rec;
      if (updated.status === 'verified') updated = transitionRecord(updated, 'downloading');
      if (updated.status === 'downloading') updated = transitionRecord(updated, 'downloaded');
      updated.sha256 = result.sha256;
      updated.target_path = result.target_path;
      updated = transitionRecord(updated, 'archived');
      manifest = upsertRecord(manifest, updated);
      merged++;

      // 合并成功后删除结果文件
      fs.unlinkSync(resultPath);
    } catch (e) {
      errors.push({ file: f, error: `合并异常: ${e.message}` });
    }
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

const _isDirectRun = isDirectRun(import.meta.url);
if (_isDirectRun) main();

export { main };
