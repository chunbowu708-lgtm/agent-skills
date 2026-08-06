// 逐封核查附件 + body 链接（防止漏作品），生成 collection_manifest records。
//
// 用法: node verify_mails.mjs [--date 2026-06-15] [--input <scan.json>] [--manifest <path>]
// 从 scan_all.json 取候选 MID，逐封查 attachments + body_html 链接，
// 产出 notes/collection_manifest.json（事实源）。
// 无法解析的详情必须 fail-closed（进 blocked），不静默记"零附件"。

import fs from 'node:fs';
import { execSync, exec } from 'node:child_process';
import { promisify } from 'node:util';
import {
  parseCliJson,
} from './lib/lark_mail.mjs';
import { extractLinks, isMaterialLink } from './lib/html_links.mjs';
import {
  attachmentRecordId, linkRecordId, writeManifestAtomic, readManifest,
  transitionRecord, upsertRecord, SCHEMA_VERSION,
} from './lib/manifest.mjs';
import { isNotification } from './lib/notifications.mjs';
import { runWithRetry } from './lib/retry.mjs';
import { LARK_CLI, SCAN_ALL as SCAN_DEFAULT, MANIFEST as MANIFEST_DEFAULT, MAX_BUFFER } from './lib/paths.mjs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

/**
 * 取一封邮件的完整详情（attachments + body_html）。
 * runner 可注入（测试用）；兼容同步和异步 runner（Promise）。
 * @returns {object|Promise<object>} 严格解析后的 message 对象（含 attachments, body_html）
 */
function fetchMessageDetail(messageId, runner) {
  const cmd = `${LARK_CLI} mail +message --as user --message-id "${messageId}" --format json`;
  const raw = runner(cmd);
  // 兼容异步 runner：若返回 Promise 则 await 后再解析
  if (raw && typeof raw.then === 'function') {
    return raw.then(r => parseCliJson(r));
  }
  return parseCliJson(raw);
}

/**
 * 并发执行异步任务，限制最大并发数（避免 lark-cli 限流）。
 * 保持结果顺序与输入一致。
 * @param {array} items
 * @param {function} worker - (item) => Promise<result>
 * @param {number} concurrency
 * @returns {Promise<array>} 结果数组（顺序与 items 一致）
 */
async function mapPool(items, worker, concurrency = 5) {
  const results = new Array(items.length);
  let next = 0;
  async function run() {
    while (true) {
      const i = next++;
      if (i >= items.length) return;
      results[i] = await worker(items[i], i);
    }
  }
  const n = Math.min(concurrency, items.length);
  await Promise.all(Array.from({ length: Math.max(1, n) }, () => run()));
  return results;
}

/**
 * 处理单封邮件的详情 → 生成 record(s) 并合并进 manifest（纯函数，无副作用到 manifest 外）。
 * 抽取出来便于并行 fetch、串行 process（manifest 更新需串行保证确定性）。
 *
 * @param {object} m - 候选邮件
 * @param {object|Error} detailOrErr - 已 fetch 的详情对象，或 fetch 抛出的 Error
 * @param {object} manifest - 当前 manifest（会被 upsert 返回新副本）
 * @param {string} batchId
 * @param {array} failed - 失败列表（push 副作用）
 * @returns {object} 更新后的 manifest
 */
function processMailDetail(m, detailOrErr, manifest, batchId, failed) {
  // fetch 失败分支
  if (detailOrErr instanceof Error) {
    const e = detailOrErr;
    const recId = attachmentRecordId(m.message_id, '__detail_fetch__');
    let rec = manifest.records[recId] || { record_id: recId, message_id: m.message_id, status: 'discovered', errors: [], source_type: 'mail_detail' };
    rec = transitionRecord(rec, 'blocked', { code: 'DETAIL_FETCH_FAILED', message: e.message });
    manifest = upsertRecord(manifest, rec);
    failed.push({ message_id: m.message_id, subject: m.subject, error: e.message });
    process.stderr.write(`  🔴 详情获取失败: ${e.message}\n`);
    return manifest;
  }

  const msg = detailOrErr.data || detailOrErr;
  // 严格校验详情结构
  const atts = Array.isArray(msg.attachments) ? msg.attachments : [];
  const bodyHtml = msg.body_html || '';
  const links = extractLinks(bodyHtml);
  const materialLinks = links.filter(isMaterialLink);

  process.stderr.write(`  附件(${atts.length}): ${atts.map(a => a.filename).join(', ') || '无'}\n`);
  if (materialLinks.length) {
    process.stderr.write(`  作品链接(${materialLinks.length}): ${materialLinks.map(l => l.url).slice(0, 3).join(' ')}\n`);
  }

    // 为每个标准附件生成 record
    for (let i = 0; i < atts.length; i++) {
      const a = atts[i];
      const recId = attachmentRecordId(m.message_id, a.id || `idx${i}`);
      if (!manifest.records[recId]) {
        let rec = {
          record_id: recId,
          batch_id: batchId,
          message_id: m.message_id,
          attachment_id: a.id || `idx${i}`,
          attachment_index: i,
          source_type: 'mail_attachment',
          original_filename: a.filename,
          candidate_name: null,
          job_name: null,
          target_dir: null,
          target_filename: null,
          status: 'discovered',
          errors: [],
          created_at: batchId,
        };
        rec = transitionRecord(rec, 'needs_resolution');
        manifest = upsertRecord(manifest, rec);
      }
    }

    // 为每个材料类链接生成 record
    for (const link of materialLinks) {
      const recId = linkRecordId(m.message_id, link.url);
      if (!manifest.records[recId]) {
        let rec = {
          record_id: recId,
          batch_id: batchId,
          message_id: m.message_id,
          source_type: 'link',
          source_url: link.url,
          link_text: link.text,
          link_kind: link.kind,
          original_filename: null,
          candidate_name: null,
          job_name: null,
          target_dir: null,
          target_filename: null,
          status: 'discovered',
          errors: [],
          created_at: batchId,
        };
        rec = transitionRecord(rec, 'needs_resolution');
        manifest = upsertRecord(manifest, rec);
      }
    }

    // 正文提示有材料关键词但没有任何附件也没有可提取链接 → blocked
    const bodyText = bodyHtml.replace(/<[^>]*>/g, '');
    const hintsMaterial = /作品|附件|简历|portfolio|artstation/i.test(bodyText);
    if (atts.length === 0 && materialLinks.length === 0 && hintsMaterial) {
      const recId = linkRecordId(m.message_id, `__hint__${m.message_id}`);
      let rec = manifest.records[recId] || {
        record_id: recId, message_id: m.message_id, status: 'discovered', errors: [], source_type: 'body_hint',
      };
      if (rec.status === 'discovered') {
        rec = transitionRecord(rec, 'blocked', {
          code: 'MATERIAL_HINT_NO_SOURCE',
          message: '正文提示有作品/附件/简历关键词，但既无标准附件也无可提取链接',
        });
        manifest = upsertRecord(manifest, rec);
        failed.push({ message_id: m.message_id, subject: m.subject, error: 'MATERIAL_HINT_NO_SOURCE' });
      }
    }

  return manifest;
}

/**
 * 核心核查逻辑（可注入 runner，测试用）——串行版（兼容旧测试）。
 * 遍历候选邮件，为每封生成 record(s)，返回 { manifest, failed }。
 *
 * @param {array} candidates - 候选邮件列表
 * @param {function} runner - (cmd) => rawStdout（同步）
 * @param {object} prevManifest - 上一份 manifest（增量合并，不丢已有记录）
 * @returns {{ manifest: object, failed: array }}
 */
export async function runVerify(candidates, runner, prevManifest) {
  let manifest = prevManifest || {
    schema_version: SCHEMA_VERSION,
    batches: {},
    records: {},
  };
  const failed = [];
  const batchId = new Date().toISOString();

  for (const m of candidates) {
    process.stderr.write(`\n=== ${m.date || ''} | ${(m.subject || '').slice(0, 40)} ===\n`);
    let detailOrErr;
    try {
      detailOrErr = await fetchMessageDetail(m.message_id, runner);
    } catch (e) {
      detailOrErr = e;
    }
    manifest = processMailDetail(m, detailOrErr, manifest, batchId, failed);
  }

  return { manifest, failed };
}

/**
 * 并行核查（CLI 用）：并发 fetch 详情（限流），串行处理保证 manifest 确定性。
 *
 * @param {array} candidates
 * @param {function} asyncRunner - (cmd) => Promise<rawStdout>
 * @param {object} prevManifest
 * @param {number} concurrency - 并发上限（默认 5）
 * @returns {Promise<{ manifest: object, failed: array }>}
 */
export async function runVerifyParallel(candidates, asyncRunner, prevManifest, concurrency = 5) {
  // 阶段1：并发 fetch 所有详情（失败转 Error 对象，不中断批次）
  const details = await mapPool(candidates, async (m) => {
    process.stderr.write(`\n=== ${m.date || ''} | ${(m.subject || '').slice(0, 40)} ===\n`);
    try {
      // fetchMessageDetail 对异步 runner 返回 Promise，必须 await 才能捕获 reject
      return await fetchMessageDetail(m.message_id, asyncRunner);
    } catch (e) {
      return e;
    }
  }, concurrency);

  // 阶段2：串行处理（manifest 更新需确定性，fail-closed 语义不变）
  let manifest = prevManifest || {
    schema_version: SCHEMA_VERSION,
    batches: {},
    records: {},
  };
  const failed = [];
  const batchId = new Date().toISOString();

  for (let i = 0; i < candidates.length; i++) {
    manifest = processMailDetail(candidates[i], details[i], manifest, batchId, failed);
  }

  return { manifest, failed };
}

// CLI 入口（async：verify_mails 并行化，2026-07-29 缺陷④）
async function main() {
  const args = process.argv.slice(2);
  const dateFilter = getArg(args, '--date');
  const input = getArg(args, '--input') || SCAN_DEFAULT;
  const manifestPath = getArg(args, '--manifest') || MANIFEST_DEFAULT;
  const concurrency = parseInt(getArg(args, '--concurrency') || '5', 10);

  if (!fs.existsSync(input)) {
    console.error(`先跑 scan_all.mjs 生成扫描快照（期望 ${input}）`);
    process.exit(1);
  }

  const allMessages = JSON.parse(fs.readFileSync(input, 'utf8'));
  // 通知只打标签不删除（scan_all 已打标签，这里兼容旧快照没标签的情况）
  const candidates = allMessages.filter(m => {
    return !(m.is_notification || isNotification(m));
  }).filter(m => !dateFilter || (m.date || '').startsWith(dateFilter));

  const prevManifest = readManifest(manifestPath);
  // 异步 runner：并发 fetch 详情（旧版串行 execSync，29 封要几分钟）
  // 2026-07-31：mail +message 查详情也吃限流（99991400），旧版无重试 → 限流即整封 blocked。
  // 套 runWithRetry：识别限流错误指数退避重试（5s/10s/20s，最多3次），彻底治标。
  const execAsync = promisify(exec);
  const asyncRunner = async (cmd) => {
    return runWithRetry(async () => {
      try {
        const { stdout } = await execAsync(cmd, { encoding: 'utf8', maxBuffer: MAX_BUFFER });
        return stdout;
      } catch (e) {
        // exec reject 时把 stderr 拼进 message，让 isRateLimitError 能识别 lark-cli 的限流 JSON
        const stderr = e.stderr || '';
        const enriched = new Error((e.message || '') + ' ' + stderr);
        enriched.stderr = stderr;
        throw enriched;
      }
    }, {
      onRetry: (err, attempt, delayMs) => {
        process.stderr.write(`  ⏳ 查详情限流，${delayMs}ms 后第${attempt}次重试...\n`);
      },
    });
  };

  let result;
  try {
    result = await runVerifyParallel(candidates, asyncRunner, prevManifest, concurrency);
  } catch (e) {
    console.error(`🔴 verify 失败: ${e.message}`);
    console.error(`   manifest ${manifestPath} 未被修改`);
    process.exit(2);
  }

  // 原子发布新 manifest
  writeManifestAtomic(manifestPath, result.manifest);
  process.stderr.write(`\n已发布 manifest: ${manifestPath}（${Object.keys(result.manifest.records).length} 条记录）\n`);

  if (result.failed.length) {
    process.stderr.write(`\n🔴 ${result.failed.length} 封邮件需人工确认：\n`);
    for (const f of result.failed) {
      process.stderr.write(`  - mid=${f.message_id} | ${(f.subject || '').slice(0, 50)} | ${f.error}\n`);
    }
    process.exit(2);
  }
}

function getArg(args, name) {
  const i = args.indexOf(name);
  return i !== -1 ? args[i + 1] : undefined;
}

const isDirectRun = process.argv[1] &&
  fileURLToPath(import.meta.url).replace(/\\/g, '/') === path.resolve(process.argv[1]).replace(/\\/g, '/');
if (isDirectRun) {
  main().catch(e => {
    console.error(`🔴 未捕获异常: ${e.message}`);
    process.exit(1);
  });
}

export { main };
