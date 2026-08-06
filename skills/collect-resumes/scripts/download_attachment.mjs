// 飞书邮件附件事务式下载（record 驱动）。
//
// 用法:
//   node download_attachment.mjs --record <record_id> --manifest <path>
//   node download_attachment.mjs --unsafe-manual <MID> <输出路径> [附件序号]  (仅 Downloads 隔离目录)
//
// 常规模式只接受 --record，MID/附件ID/目标路径全部从 manifest 派生（防串件）。
// 下载到 .part → 校验 → 原子 rename；目标存在绝不覆盖。
// 结果写独立 .result.json，由 merge_results.mjs 串行合并（防并发覆盖）。

import https from 'node:https';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';
import {
  readManifest, getRecord,
} from './lib/manifest.mjs';
import {
  sha256File, detectTypeFromBuffer, commitVerifiedFile, typeMatchesExtension,
} from './lib/file_identity.mjs';
import { parseCliJson } from './lib/lark_mail.mjs';
import { runLarkCliWithRetry, runWithRetry } from './lib/retry.mjs';
import { LARK_CLI as CLI, UNSAFE_DIR, RESULTS_DIR, MANIFEST as MANIFEST_DEFAULT, MAX_BUFFER } from './lib/paths.mjs';

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
  runner = runner || ((cmd) => execSync(cmd, { encoding: 'utf8', maxBuffer: MAX_BUFFER }));

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
  return execSync(cmd, { encoding: 'utf8', maxBuffer: MAX_BUFFER });
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
  const manifestPath = getArg(args, '--manifest') || MANIFEST_DEFAULT;
  const throttleMs = parseInt(getArg(args, '--throttle') || '500', 10); // 批量间隔毫秒，默认0.5秒（download_url 有 runWithRetry 兜底限流，无需靠长间隔避）

  // 检测旧式位置参数 → 明确拒绝
  const positional = args.filter(a => !a.startsWith('--'));
  if (!recordId && !recordsArg && positional.length >= 2) {
    console.error('🔴 拒绝旧式调用：MID + 输出路径 可导致串件。');
    console.error('   请用 --record <id> --manifest <path>');
    console.error('   批量用 --records <id1,id2,...> --manifest <path> [--throttle 2000]');
    console.error('   应急下载用 --unsafe-manual <MID> <Downloads内路径>');
    process.exit(1);
  }

  // ---- 批量模式：逐个下载 + 间隔（不连发，避免打爆飞书限流）----
  // 2026-07-30：旧版无批量入口，调用方自己写循环连发 → 18连发打爆限流 → 拿损坏数据。
  // 现在内置批量：一个一个下，每个之间间隔 throttleMs，让飞书限流窗口喘气。
  // 单条失败不中断整批（记 failed 列表，最后汇总），失败的可单独重跑。
  if (recordsArg) {
    const ids = recordsArg.split(',').map(s => s.trim()).filter(Boolean);
    if (!ids.length) {
      console.error('🔴 --records 需提供逗号分隔的 record id');
      process.exit(1);
    }
    const manifest = readManifest(manifestPath);
    const succeeded = [], failed = [];
    for (let i = 0; i < ids.length; i++) {
      const rid = ids[i];
      process.stderr.write(`\n[${i + 1}/${ids.length}] ${rid.slice(0, 20)}...\n`);
      try {
        const result = await downloadByRecord(manifest, rid);
        console.log(`  ✅ ${result.outcome} (sha256=${result.sha256.slice(0, 12)}...)`);
        succeeded.push(rid);
      } catch (e) {
        console.error(`  🔴 失败: ${e.message.slice(0, 100)}`);
        failed.push({ id: rid, error: e.message.slice(0, 120) });
      }
      // 间隔（最后一个不等）
      if (i < ids.length - 1 && throttleMs > 0) {
        await new Promise(r => setTimeout(r, throttleMs));
      }
    }
    console.log(`\n==== 批量完成: ✅ ${succeeded.length} · 🔴 ${failed.length} ====`);
    if (failed.length) {
      console.error('失败列表（可单独 --record 重跑）:');
      for (const f of failed) console.error(`  - ${f.id}: ${f.error}`);
      process.exit(4); // 有失败 → 非零退出，但已完成的保留
    }
    return;
  }

  if (!recordId) {
    console.error('用法: download_attachment.mjs --record <id> [--manifest <path>]');
    console.error('      download_attachment.mjs --records <id1,id2,...> [--throttle 2000] [--manifest <path>]');
    console.error('      download_attachment.mjs --unsafe-manual <MID> <Downloads内路径> [序号]');
    process.exit(1);
  }

  const manifest = readManifest(manifestPath);
  try {
    const result = await downloadByRecord(manifest, recordId);
    console.log(`✅ ${result.outcome}: ${recordId} (sha256=${result.sha256.slice(0, 12)}...)`);
    console.log(`   结果待合并: ${result.result_path}`);
  } catch (e) {
    console.error(`🔴 下载失败: ${e.message}`);
    process.exit(4);
  }
}

function getArg(args, name) {
  const i = args.indexOf(name);
  return i !== -1 ? args[i + 1] : undefined;
}

const isDirectRun = process.argv[1] &&
  fileURLToPath(import.meta.url).replace(/\\/g, '/') === path.resolve(process.argv[1]).replace(/\\/g, '/');
if (isDirectRun) main();
