// 飞书邮件附件事务式下载（record 驱动）。
//
// 用法:
//   node download_attachment.mjs --record <record_id> --manifest <path>
//   node download_attachment.mjs --records <id1,id2,...> [--throttle 500] --manifest <path>
//   node download_attachment.mjs --pending [--manifest <path>]          # 下载所有 verified 附件记录
//   node download_attachment.mjs --unsafe-manual <MID> <输出路径> [附件序号]  (仅 Downloads 隔离目录)
//
// 常规模式只接受 --record/--records/--pending，MID/附件ID/目标路径全部从 manifest 派生（防串件）。
// 下载到 .part → 校验 → 原子 rename；目标存在绝不覆盖。
// 结果写独立 .result.json 后**立即自动合并**到 manifest（推进到 archived）；
// 单进程串行无并发写风险。merge_results.mjs 保留为独立工具，用于并行进程/中断恢复。

import https from 'node:https';
import fs from 'node:fs';
import path from 'node:path';
import {
  readManifest, writeManifestAtomic, getRecord, upsertRecord, transitionToExcluded,
} from './lib/manifest.mjs';
import { mdd, recordDate } from './lib/dates.mjs';
import {
  sha256File, detectTypeFromBuffer, commitVerifiedFile, typeMatchesExtension,
} from './lib/file_identity.mjs';
import { parseCliJson } from './lib/lark_mail.mjs';
import { runWithRetry } from './lib/retry.mjs';
import { getArg, isDirectRun, runLarkSync } from './lib/cli_helpers.mjs';
import { mergeResults } from './merge_results.mjs';
import { LARK_CLI as CLI, UNSAFE_DIR, RESULTS_DIR, MANIFEST as MANIFEST_DEFAULT } from './lib/paths.mjs';

/**
 * 下载 URL 到 buffer，带 content-length 校验和重试。
 *
 * 2026-07-30 修复：旧版只看 HTTP 状态码，飞书 drive-stream 在源文件异常时会返回
 * HTTP200 + 损坏字节流（卢有全 case：505KB 有 %%EOF 尾但无 %PDF 头，前段零填充）。
 * 现在下载完校验字节数与 content-length 是否一致，不符则判失败重试，杜绝静默收下损坏内容。
 * 另：网络层错误（ECONNRESET 等）也重试。
 *
 * @param {string} url
 * @param {object} opts - { redirectsLeft, retryLeft, expectedLen? }
 * @returns {Promise<Buffer>}
 */
async function download(url, opts = {}) {
  const redirectsLeft = opts.redirectsLeft ?? 5;
  const retryLeft = opts.retryLeft ?? 3;

  for (let attempt = 0; attempt <= retryLeft; attempt++) {
    try {
      const buf = await new Promise((resolve, reject) => {
        https.get(url, r => {
          if (r.statusCode >= 300 && r.statusCode < 400 && r.headers.location && redirectsLeft > 0) {
            r.resume();
            // 重定向时继承剩余重试次数（重定向不算重试）
            return download(r.headers.location, { redirectsLeft: redirectsLeft - 1, retryLeft, expectedLen: opts.expectedLen })
              .then(resolve).catch(reject);
          }
          if (r.statusCode !== 200) { r.resume(); return reject(new Error('HTTP ' + r.statusCode)); }
          const declared = r.headers['content-length'] ? parseInt(r.headers['content-length'], 10) : null;
          const chunks = [];
          r.on('data', c => chunks.push(c));
          r.on('end', () => {
            const full = Buffer.concat(chunks);
            // content-length 校验：服务器声明了大小但实际字节数不符 → 损坏/截断，判失败
            if (declared !== null && full.length !== declared) {
              return reject(new Error(`CONTENT_LENGTH_MISMATCH: 声明 ${declared} 实际 ${full.length} 字节（下载截断/损坏）`));
            }
            resolve(full);
          });
        }).on('error', reject);
      });
      return buf;
    } catch (e) {
      if (attempt === retryLeft) throw e;
      // 仅对网络/截断类错误重试（HTTP 4xx 业务错误不重试）
      const retryable = /CONTENT_LENGTH_MISMATCH|ECONNRESET|ETIMEDOUT|EAI_AGAIN|socket hang up|HTTP 5\d\d/i.test(e.message);
      if (!retryable) throw e;
      process.stderr.write(`  ⚠️ 下载重试 (${attempt + 1}/${retryLeft}): ${e.message.slice(0, 80)}\n`);
      await new Promise(r => setTimeout(r, 3000 * (attempt + 1)));
    }
  }
}

/**
 * 从 manifest 记录派生下载所需信息并执行事务式下载。
 *
 * @param {object} manifest
 * @param {string} recordId
 * @param {function} runner - lark-cli runner（测试可注入）
 * @returns {{ outcome: string, sha256: string, result_path: string }}
 */
export async function downloadByRecord(manifest, recordId, runner) {
  runner = runner || runLarkSync;

  const rec = getRecord(manifest, recordId);
  if (!rec) throw new Error(`RECORD_NOT_FOUND: ${recordId}`);
  if (rec.status !== 'verified') throw new Error(`RECORD_NOT_VERIFIED: 状态 ${rec.status}，需先 resolve 到 verified`);
  if (!rec.target_dir || !rec.target_filename) throw new Error('RECORD_NO_TARGET: 记录缺少 target_dir/target_filename');

  // 从 manifest 派生 MID 和附件 ID（不接受外部传入）
  const mid = rec.message_id;
  const attId = rec.attachment_id;

  // 取 attachment download_url（原子：取 URL 后立即下载）
  // 2026-07-30 修复：download_url 接口极易限流（1234029），批量下载时连发必触限流。
  // 用 runWithRetry 包裹：限流时指数退避重试，最多3次；非限流错误直接抛。
  // authcode 有时效，取 URL 和下载必须原子完成——重试只在"取URL"这步，取到立即下。
  const params = JSON.stringify({ user_mailbox_id: 'me', message_id: mid, attachment_ids: attId });
  const dlUrlCmd = `${CLI} mail user_mailbox.message.attachments download_url --as user --params "${params.replace(/"/g, '\\"')}" --format json`;
  const urlResp = await runWithRetry(async () => {
    let raw;
    try {
      raw = runner(dlUrlCmd);
    } catch (e) {
      // execSync 抛错（含限流）时把 stderr 拼进 message 让 isRateLimitError 识别
      throw new Error((e.message || '') + ' ' + (e.stderr || ''));
    }
    return parseCliJson(raw); // parseCliJson 对 ok:false(限流) 也会抛 API_ERROR，被外层识别为限流重试
  }, { onRetry: (e, attempt, delay) => process.stderr.write(`  ⚠️ download_url 限流，${delay / 1000}s 后重试 (${attempt}/3): ${String(e.message).slice(0, 60)}\n`) });
  const downloadUrl = urlResp.data?.download_urls?.[0]?.download_url;
  if (!downloadUrl) throw new Error('NO_DOWNLOAD_URL: 未取到下载 URL');

  // 下载到内存 buffer
  const buf = await download(downloadUrl);

  // 内容校验
  if (buf.length === 0) throw new Error('DOWNLOAD_EMPTY: 0 字节（疑似 auth code 过期）');
  const detected = detectTypeFromBuffer(buf);
  if (detected === 'html') throw new Error('DOWNLOAD_IS_HTML: 内容是 HTML（疑似 auth 过期/登录页）');
  if (detected === 'unknown') throw new Error(`DOWNLOAD_TYPE_UNKNOWN: 文件头不像 PDF/ZIP/图片`);

  // 扩展名一致性检查
  const ext = (rec.target_filename.split('.').pop() || '').toLowerCase();
  if (!typeMatchesExtension(ext, detected)) {
    throw new Error(`TYPE_MISMATCH: 扩展名 .${ext} 与实际类型 ${detected} 不一致`);
  }

  // 写到 .part，然后事务提交
  const targetPath = path.join(rec.target_dir, rec.target_filename).replace(/\\/g, '/');
  const partPath = `${targetPath}.part.${Date.now()}`;
  fs.mkdirSync(path.dirname(partPath), { recursive: true });
  fs.writeFileSync(partPath, buf);
  const result = commitVerifiedFile(partPath, targetPath, ext === 'docx' ? 'docx' : detected);

  // 写独立结果 JSON（不直接改 manifest，由 merge_results 合并）
  // recordId 含冒号（sha256:xxx），Windows 把冒号当 NTFS ADS 分隔符截断文件名 → 用下划线替换
  fs.mkdirSync(RESULTS_DIR, { recursive: true });
  const safeRecordId = recordId.replace(/[:*?"<>|]/g, '_');
  const resultPath = path.join(RESULTS_DIR, `${safeRecordId}.result.json`);
  fs.writeFileSync(resultPath, JSON.stringify({
    record_id: recordId,
    outcome: result.outcome,
    sha256: result.sha256,
    target_path: targetPath,
    at: new Date().toISOString(),
  }, null, 2));

  return { outcome: result.outcome, sha256: result.sha256, result_path: resultPath };
}

// ---- --unsafe-manual 应急模式（只能写 Downloads 隔离目录）----
async function unsafeManual(mid, outPath, attIdx) {
  attIdx = parseInt(attIdx || '0');
  // 强制限制在 UNSAFE_DIR 内
  const resolved = path.resolve(outPath);
  const safeDir = path.resolve(UNSAFE_DIR);
  const rel = path.relative(safeDir, resolved);
  if (rel === '' || rel.startsWith('..') || path.isAbsolute(rel)) {
    console.error(`🔴 --unsafe-manual 只能写入 ${UNSAFE_DIR}，禁止写其他位置`);
    process.exit(3);
  }
  console.error(`⚠️  UNSAFE MANUAL 模式：结果不进入 manifest，不能用于正式归档`);

  // 旧式：取附件 ID → 取 URL → 下载到指定路径
  const attIdResp = parseCliJson(runner_default(`${CLI} mail +message --as user --message-id "${mid}" --format json`));
  const atts = attIdResp.data?.attachments || [];
  const attId = atts[attIdx]?.id;
  if (!attId) { console.error('ERR: 无附件'); process.exit(2); }

  const params = JSON.stringify({ user_mailbox_id: 'me', message_id: mid, attachment_ids: attId });
  const urlResp = parseCliJson(runner_default(
    `${CLI} mail user_mailbox.message.attachments download_url --as user --params "${params.replace(/"/g, '\\"')}" --format json`
  ));
  const url = urlResp.data?.download_urls?.[0]?.download_url;
  const buf = await download(url);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });
  fs.writeFileSync(resolved, buf);
  console.log(`OK (unsafe): ${resolved} (${buf.length} bytes)`);
}

function runner_default(cmd) {
  return runLarkSync(cmd);
}

// CLI 入口
async function main() {
  const args = process.argv.slice(2);

  if (args.includes('--unsafe-manual')) {
    const positional = args.filter(a => !a.startsWith('--') && a !== '--unsafe-manual');
    const [mid, out, idx] = positional;
    if (!mid || !out) {
      console.error('用法: download_attachment.mjs --unsafe-manual <MID> <输出路径> [序号]');
      console.error(`     输出路径必须在 ${UNSAFE_DIR} 内`);
      process.exit(1);
    }
    await unsafeManual(mid, out, idx);
    return;
  }

  // 常规模式：拒绝旧式 MID + OUT
  const recordId = getArg(args, '--record');
  const recordsArg = getArg(args, '--records'); // 批量：逗号分隔的多个 record id
  const pending = args.includes('--pending');   // 批量：自动取所有 verified 的附件记录
  const manifestPath = getArg(args, '--manifest') || MANIFEST_DEFAULT;
  const throttleMs = parseInt(getArg(args, '--throttle') || '500', 10); // 批量间隔毫秒（download_url 有 runWithRetry 兜底限流，无需靠长间隔避）

  // 检测旧式位置参数 → 明确拒绝
  const positional = args.filter(a => !a.startsWith('--'));
  if (!recordId && !recordsArg && !pending && positional.length >= 2) {
    console.error('🔴 拒绝旧式调用：MID + 输出路径 可导致串件。');
    console.error('   请用 --record <id> / --records <id1,id2,...> / --pending [--manifest <path>]');
    console.error('   应急下载用 --unsafe-manual <MID> <Downloads内路径>');
    process.exit(1);
  }

  // ---- 批量模式：逐个下载 + 间隔（不连发，避免打爆飞书限流）----
  // 单条失败不中断整批（记 failed 列表，最后汇总），失败的可单独 --record 重跑。
  // 下载完成后自动合并 result.json 推进 manifest 到 archived（批量是单进程串行，
  // 无并发写风险；merge_results 作为独立脚本保留，用于并行进程/中断恢复场景）。
  let batchIds = null;
  if (recordsArg) {
    batchIds = recordsArg.split(',').map(s => s.trim()).filter(Boolean);
    if (!batchIds.length) {
      console.error('🔴 --records 需提供逗号分隔的 record id');
      process.exit(1);
    }
  } else if (pending) {
    const manifest = readManifest(manifestPath);
    batchIds = Object.values(manifest.records)
      .filter(r => r.source_type === 'mail_attachment' && r.status === 'verified')
      .map(r => r.record_id);
    if (!batchIds.length) {
      console.log('没有待下载的附件记录（verified 状态 0 条）');
      return;
    }
    console.log(`--pending: ${batchIds.length} 条 verified 附件记录待下载`);
  }

  if (batchIds) {
    let manifest = readManifest(manifestPath);
    // ---- 批量前置自愈（2026-08-17 积压清理）----
    // 1) target 路径脏数据：早期 resolve 产物有反斜杠路径/无日期段（直接指向已收集简历根）/
    //    日期段=处理日而非收到日（8.13_暂定 但 received 7 月）。按契约重算：正斜杠 +
    //    日期段=received_at 的 M.DD，同日期段已有 _N份 目录则直接进，否则 _暂定（闸门收敛）。
    // 2) 已归档重复：同人同岗已有 archived/validated 记录的 verified → exclude，
    //    不重复下载污染已评估档位（--backlog 清历史积压时的大头）。
    let healed = 0, deduped = 0;
    const archivedKeys = new Set(
      Object.values(manifest.records)
        .filter(r => ['archived', 'validated'].includes(r.status) && r.candidate_name)
        .map(r => `${r.candidate_name}\u0000${r.job_name}`)
    );
    for (const rid of batchIds) {
      const rec = manifest.records[rid];
      if (!rec) continue;
      // new-version 补充版：同人同岗是预期状态（旧版已归档），不去重
      if (rec.new_version) continue;
      if (rec.candidate_name && archivedKeys.has(`${rec.candidate_name}\u0000${rec.job_name}`)) {
        manifest.records[rid] = transitionToExcluded(rec, 'DUPLICATE_ALREADY_ARCHIVED',
          `同人同岗已有归档记录，积压清理时跳过重复下载`);
        deduped++;
        continue;
      }
      if (!rec.target_dir) continue;
      let td = String(rec.target_dir).replace(/\\/g, '/');
      // 仅当 target_dir 本身以「已收集简历」结尾（无日期段）才补 M.DD_暂定；
      // 已带日期段（如 已收集简历/8.17_暂定）不能再套一层（2026-08-17 双嵌套 bug）
      if (td.replace(/\/$/, '').endsWith('已收集简历')) {
        td = `${td.replace(/\/$/, '')}/${mdd(recordDate(rec))}_暂定`;
      } else if (td.endsWith('_暂定') && !fs.existsSync(td)) {
        const day = mdd(recordDate(rec));
        const existN = fs.existsSync(parent) && fs.readdirSync(parent)
          .filter(n => n.startsWith(`${day}_`) && /_\d+份$/.test(n)).sort()[0];
        td = existN ? `${parent}/${existN}` : `${parent}/${day}_暂定`;
      }
      if (td !== rec.target_dir) {
        manifest.records[rid] = { ...rec, target_dir: td };
        healed++;
      }
    }
    if (healed || deduped) {
      writeManifestAtomic(manifestPath, manifest);
      console.log(`🔧 批量前置自愈：target 路径修正 ${healed} 条，已归档重复排除 ${deduped} 条`);
      batchIds = batchIds.filter(rid => manifest.records[rid] && manifest.records[rid].status === 'verified');
    }
    const succeeded = [], failed = [], conflicts = [];
    // 并发下载（2026-08-17）：串行时每份 ~2.3-3.5s，其中 lark-cli download_url 冷启动
    // 占 50-65%。取 URL（吃 mail 域配额）并发 3 + runWithRetry 退避兜底限流；
    // 文件落盘各写各的 .part/result.json 无共享态。SAMEDAY_CONFLICT 要写 manifest，
    // 收集到批尾串行处理，避免并发写。
    const CONCURRENCY = Math.max(1, Math.min(3, batchIds.length));
    async function mapPool(items, worker, concurrency) {
      const results = new Array(items.length);
      let next = 0;
      async function run() {
        while (true) {
          const i = next++;
          if (i >= items.length) return;
          results[i] = await worker(items[i], i);
        }
      }
      await Promise.all(Array.from({ length: concurrency }, () => run()));
      return results;
    }
    const outcomes = await mapPool(batchIds, async (rid, i) => {
      process.stderr.write(`\n[${i + 1}/${batchIds.length}] ${rid.slice(0, 20)}...\n`);
      try {
        const result = await downloadByRecord(manifest, rid);
        console.log(`  ✅ ${result.outcome} (sha256=${result.sha256.slice(0, 12)}...)`);
        if (throttleMs > 0) await new Promise(r => setTimeout(r, throttleMs));
        return { rid, ok: true };
      } catch (e) {
        return { rid, ok: false, e };
      }
    }, CONCURRENCY);
    for (const oc of outcomes) {
      if (oc.ok) { succeeded.push(oc.rid); continue; }
      const e = oc.e;
      if (e.conflictPath) {
        // 同人同日重复投递（同名目标已存在、内容不同，多为候选人当天重发同份简历）：
        // 删 conflict 副本（未脱敏内容不得残留在归档目录），record 结构化排除。
        // 真有新版简历的情况：源邮件仍在（message_id 可回溯），resolve --new-version 重开。
        try { fs.unlinkSync(e.conflictPath); } catch {}
        try {
          const cur = readManifest(manifestPath);
          const rec = cur.records[oc.rid];
          const updated = transitionToExcluded(rec, 'SAMEDAY_CONFLICT',
            `同名目标已存在且内容不同，判同日重复投递，保留已归档版本。目标: ${rec.target_filename}`);
          writeManifestAtomic(manifestPath, upsertRecord(cur, updated));
          console.log(`  ⏭️ 同日冲突已排除: ${rec.target_filename}`);
          conflicts.push(oc.rid);
        } catch (exErr) {
          console.error(`  🔴 冲突排除失败: ${exErr.message.slice(0, 80)}`);
          failed.push({ id: oc.rid, error: e.message.slice(0, 120) });
        }
      } else {
        console.error(`  🔴 失败: ${e.message.slice(0, 100)}`);
        failed.push({ id: oc.rid, error: e.message.slice(0, 120) });
      }
    }
    console.log(`\n==== 批量完成（并发${CONCURRENCY}）: ✅ ${succeeded.length} · ⏭️ 同日冲突 ${conflicts.length} · 🔴 ${failed.length} ====`);
    if (succeeded.length) {
      const { merged, skipped, errors } = mergeResults(manifestPath);
      console.log(`自动合并: ${merged} 条推进到 archived，${skipped} 条跳过${errors.length ? `，🔴 ${errors.length} 个合并错误` : ''}`);
      for (const e of errors.slice(0, 5)) console.error(`  - ${e.file}: ${e.error}`);
      if (errors.length) process.exitCode = 5;
    }
    if (failed.length) {
      console.error('失败列表（可单独 --record 重跑）:');
      for (const f of failed) console.error(`  - ${f.id}: ${f.error}`);
      process.exitCode = 4; // 有失败 → 非零退出，但已完成的保留
    }
    return;
  }

  if (!recordId) {
    console.error('用法: download_attachment.mjs --record <id> [--manifest <path>]');
    console.error('      download_attachment.mjs --records <id1,id2,...> [--throttle 2000] [--manifest <path>]');
    console.error('      download_attachment.mjs --pending [--manifest <path>]   # 下载所有 verified 附件记录');
    console.error('      download_attachment.mjs --unsafe-manual <MID> <Downloads内路径> [序号]');
    process.exit(1);
  }

  const manifest = readManifest(manifestPath);
  try {
    const result = await downloadByRecord(manifest, recordId);
    console.log(`✅ ${result.outcome}: ${recordId} (sha256=${result.sha256.slice(0, 12)}...)`);
    const { merged } = mergeResults(manifestPath);
    console.log(`自动合并: ${merged} 条推进到 archived`);
  } catch (e) {
    console.error(`🔴 下载失败: ${e.message}`);
    process.exit(4);
  }
}

const _isDirectRun = isDirectRun(import.meta.url);
if (_isDirectRun) main();
