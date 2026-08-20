// 逐封核查附件 + body 链接（防止漏作品），生成 collection_manifest records。
//
// 用法: node verify_mails.mjs [--date 2026-06-15] [--input <scan.json>] [--manifest <path>] [--force]
// 从 scan_all.json 取候选 MID，逐封查 attachments + body_html 链接，
// 产出 notes/collection_manifest.json（事实源）。
// 增量：已核查过的邮件（manifest.processed 标记或已有稳定 records）跳过详情拉取，
//       只核查新邮件；--force 忽略标记全量重拉（排查用）。
// 无法解析的详情必须 fail-closed（进 blocked），不静默记"零附件"。

import fs from 'node:fs';
import {
  parseCliJson,
} from './lib/lark_mail.mjs';
import { extractLinks, isMaterialLink } from './lib/html_links.mjs';
import {
  attachmentRecordId, linkRecordId, writeManifestAtomic, readManifest,
  transitionRecord, upsertRecord, SCHEMA_VERSION,
} from './lib/manifest.mjs';
import { isNotification } from './lib/notifications.mjs';
import { getArg, isDirectRun, makeLarkRunner } from './lib/cli_helpers.mjs';
import { parseMailDate, parseDateArg, sameLocalDay } from './lib/dates.mjs';
import { LARK_CLI, SCAN_ALL as SCAN_DEFAULT, MANIFEST as MANIFEST_DEFAULT } from './lib/paths.mjs';

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
    // 终态（blocked/excluded）重复失败幂等：只追加 error，不走状态机
    // （transitionRecord 对非法转换抛 INVALID_TRANSITION，会炸整批 verify）
    if (rec.status === 'blocked' || rec.status === 'excluded') {
      rec = { ...rec, errors: [...(rec.errors || []), { code: 'DETAIL_FETCH_FAILED', message: e.message, at: batchId }] };
    } else {
      rec = transitionRecord(rec, 'blocked', { code: 'DETAIL_FETCH_FAILED', message: e.message });
    }
    manifest = upsertRecord(manifest, rec);
    failed.push({ message_id: m.message_id, subject: m.subject, error: e.message });
    process.stderr.write(`  🔴 详情获取失败: ${e.message}\n`);
    return manifest;
  }

  const msg = detailOrErr.data || detailOrErr;
  // 结构校验：attachments 字段缺失（≠空数组）或正文双字段缺失 = 详情坏了，
  // 不能当"零附件零正文"写 processed（fail-open 会永久锁死这封邮件）
  const structInvalid = !Array.isArray(msg.attachments)
    || (msg.body_html === undefined && msg.body_plain_text === undefined);
  if (structInvalid) {
    const recId = attachmentRecordId(m.message_id, '__detail_fetch__');
    let rec = manifest.records[recId] || { record_id: recId, message_id: m.message_id, status: 'discovered', errors: [], source_type: 'mail_detail' };
    const errCode = 'DETAIL_STRUCT_INVALID';
    const errMsg = `详情结构异常：attachments=${typeof msg.attachments}, body_html=${typeof msg.body_html}（不写 processed，下次重查）`;
    if (rec.status === 'blocked' || rec.status === 'excluded') {
      rec = { ...rec, errors: [...(rec.errors || []), { code: errCode, message: errMsg, at: batchId }] };
    } else {
      rec = transitionRecord(rec, 'blocked', { code: errCode, message: errMsg });
    }
    manifest = upsertRecord(manifest, rec);
    failed.push({ message_id: m.message_id, subject: m.subject, error: errCode });
    process.stderr.write(`  🔴 ${errMsg}\n`);
    return manifest;
  }

  const atts = msg.attachments;
  // body_html 优先；缺时用 body_plain_text 兜底（纯文本邮件的裸 URL 也能被 extractLinks 提取）
  const bodyHtml = msg.body_html || msg.body_plain_text || '';
  const links = extractLinks(bodyHtml);
  const materialLinks = links.filter(isMaterialLink);

  // 核查成功：写增量标记（下次跳过详情拉取），并自愈旧的详情拉取 blocked 记录
  manifest = {
    ...manifest,
    processed: { ...(manifest.processed || {}), [m.message_id]: batchId },
  };
  const legacyDetailId = attachmentRecordId(m.message_id, '__detail_fetch__');
  const legacyBlocked = manifest.records[legacyDetailId];
  if (legacyBlocked && legacyBlocked.status === 'blocked') {
    let healed = transitionRecord(legacyBlocked, 'needs_resolution');
    healed = transitionRecord(healed, 'excluded', {
      code: 'DETAIL_FETCH_RECOVERED',
      message: '详情拉取已恢复，历史 blocked 自愈',
    });
    manifest = upsertRecord(manifest, healed);
    process.stderr.write(`  ♻️ 自愈历史详情拉取 blocked 记录: ${m.message_id}\n`);
  }

  process.stderr.write(`  附件(${atts.length}): ${atts.map(a => a.filename).join(', ') || '无'}\n`);
  if (materialLinks.length) {
    process.stderr.write(`  作品链接(${materialLinks.length}): ${materialLinks.map(l => l.url).slice(0, 3).join(' ')}\n`);
  }

    // 为每个标准附件生成 record
    for (let i = 0; i < atts.length; i++) {
      const a = atts[i];
      // 附件 id 重复时换序号后缀，避免第二个附件被 recId 冲突静默跳过
      const baseId = a.id || `idx${i}`;
      let recId = attachmentRecordId(m.message_id, baseId);
      if (manifest.records[recId] && atts.length > 1) {
        recId = attachmentRecordId(m.message_id, `${baseId}__idx${i}`);
      }
      if (!manifest.records[recId]) {
        let rec = {
          record_id: recId,
          batch_id: batchId,
          message_id: m.message_id,
          subject: m.subject || null,
          // 邮件真实收到时间（快照 date）。created_at 是首次入库时间，不是邮件时间——
          // 历史积压邮件被后续运行首次建记录时 created_at 落在当天，用它判"今日"会误判。
          received_at: m.date_formatted || m.date || null,
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
          subject: m.subject || null,
          received_at: m.date_formatted || m.date || null,
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

    // 本次提取到了材料链接 → 该邮件的 body_hint blocked 是历史提取盲区误判，自愈
    if (materialLinks.length) {
      const hintId = linkRecordId(m.message_id, `__hint__${m.message_id}`);
      const hintRec = manifest.records[hintId];
      if (hintRec && hintRec.status === 'blocked') {
        let healed = transitionRecord(hintRec, 'needs_resolution');
        healed = transitionRecord(healed, 'excluded', {
          code: 'HINT_RECOVERED',
          message: '链接提取修复后找到材料链接，历史 MATERIAL_HINT_NO_SOURCE 误判自愈',
        });
        manifest = upsertRecord(manifest, healed);
        process.stderr.write(`  ♻️ 自愈 body_hint 误判 blocked: ${m.message_id}\n`);
      }
    }

    // 正文提示有材料关键词但没有任何附件也没有可提取链接 → blocked
    // 2026-08-18 豁免：系统通知邮件（视频面试邀约等）正文天然含"简历/附件"字样，
    // 曾把 37 封邀约误标 MATERIAL_HINT_NO_SOURCE 成僵尸 blocked。
    // 边界：仅豁免 body_hint 兜底分支，且该分支前提已是"无附件且无链接"——
    // 候选人回信继承通知主题但带真简历附件/链接的，走上方附件/链接核查，不受此豁免影响
    //（不违反 notifications.mjs "不得按关键词丢邮件"教训）。
    const bodyText = bodyHtml.replace(/<[^>]*>/g, '');
    const hintsMaterial = /作品|附件|简历|portfolio|artstation|网盘|下载|链接|主页|作品集|resume/i.test(bodyText);
    if (atts.length === 0 && materialLinks.length === 0 && hintsMaterial && !isNotification({ subject: m.subject })) {
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
    process.stderr.write(`\n=== ${m.date || ''} | ${(m.subject || '').slice(0, 40)}${isNotification(m) ? ' 🏷️疑似通知' : ''} ===\n`);
    let detailOrErr;
    try {
      detailOrErr = await fetchMessageDetail(m.message_id, runner);
    } catch (e) {
      detailOrErr = e;
    }
    try {
      manifest = processMailDetail(m, detailOrErr, manifest, batchId, failed);
    } catch (e) {
      failed.push({ message_id: m.message_id, subject: m.subject, error: `PROCESS_ERROR: ${e.message}` });
      process.stderr.write(`  🔴 单封处理异常（跳过，其余继续）: ${e.message}\n`);
    }
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
    process.stderr.write(`\n=== ${m.date || ''} | ${(m.subject || '').slice(0, 40)}${isNotification(m) ? ' 🏷️疑似通知' : ''} ===\n`);
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
    // 单封处理异常隔离：一封邮件的意外 bug 不炸整批（manifest 未写盘前崩 = 整批白跑）
    try {
      manifest = processMailDetail(candidates[i], details[i], manifest, batchId, failed);
    } catch (e) {
      failed.push({ message_id: candidates[i].message_id, subject: candidates[i].subject, error: `PROCESS_ERROR: ${e.message}` });
      process.stderr.write(`  🔴 单封处理异常（跳过，其余继续）: ${e.message}\n`);
    }
  }

  return { manifest, failed };
}

/**
 * 增量过滤：把候选邮件分成「待核查」和「已核查可跳过」。
 *
 * 跳过条件（满足其一，且详情拉取不曾失败）：
 *   - manifest.processed 已标记该邮件（本脚本核查成功后写入，含"确认零附件"结论）
 *   - 该邮件已有稳定 records（兼容旧 manifest 无 processed 标记）
 * 详情拉取曾失败（mail_detail blocked）或 body_hint 误判（链接提取盲区历史遗留，
 * 重核查可被修复后的 extractLinks 自愈）→ 保留待核查。
 *
 * @param {array} candidates
 * @param {object} manifest
 * @returns {{ pending: array, skipped: array }}
 */
export function filterPending(candidates, manifest) {
  const processed = manifest.processed || {};
  const byMessage = new Map();
  for (const rec of Object.values(manifest.records || {})) {
    const e = byMessage.get(rec.message_id) || { hasRecords: false, detailBlocked: false };
    e.hasRecords = true;
    if (rec.source_type === 'mail_detail' && rec.status === 'blocked') e.detailBlocked = true;
    if (rec.source_type === 'body_hint' && rec.status === 'blocked') e.detailBlocked = true;
    byMessage.set(rec.message_id, e);
  }
  const pending = [], skipped = [];
  for (const m of candidates) {
    const e = byMessage.get(m.message_id);
    if (e && e.detailBlocked) { pending.push(m); continue; }
    if (processed[m.message_id] || (e && e.hasRecords)) { skipped.push(m); continue; }
    pending.push(m);
  }
  return { pending, skipped };
}

/**
 * 存量记录 received_at 回填：老记录创建时还没有该字段，从扫描快照按 message_id 补齐。
 * 幂等（已有 received_at 不动）、纯本地数据迁移（不调 API）、快照里没有的保持 null
 * （消费方走 recordDate 的 created_at fallback）。
 * @param {object} manifest
 * @param {array} messages - 完整扫描快照（含历史邮件）
 * @returns {{ manifest: object, count: number }}
 */
export function backfillReceivedAt(manifest, messages) {
  const dateByMid = new Map();
  for (const m of messages || []) {
    if (m && m.message_id) {
      const d = m.date_formatted || m.date;
      if (d && !dateByMid.has(m.message_id)) dateByMid.set(m.message_id, d);
    }
  }
  let count = 0;
  const records = { ...(manifest.records || {}) };
  for (const [rid, rec] of Object.entries(records)) {
    if (rec && !rec.received_at && rec.message_id && dateByMid.has(rec.message_id)) {
      records[rid] = { ...rec, received_at: dateByMid.get(rec.message_id) };
      count++;
    }
  }
  return { manifest: count ? { ...manifest, records } : manifest, count };
}

/**
 * 存量记录 subject 回填：老记录创建时未落盘邮件主题（link 类汇总显示与
 * autoResolve 主题解析都依赖 subject），从扫描快照按 message_id 补齐。幂等。
 */
export function backfillSubject(manifest, messages) {
  const subjByMid = new Map();
  for (const m of messages || []) {
    if (m && m.message_id && m.subject && !subjByMid.has(m.message_id)) {
      subjByMid.set(m.message_id, m.subject);
    }
  }
  let count = 0;
  const records = { ...(manifest.records || {}) };
  for (const [rid, rec] of Object.entries(records)) {
    if (rec && !rec.subject && rec.message_id && subjByMid.has(rec.message_id)) {
      records[rid] = { ...rec, subject: subjByMid.get(rec.message_id) };
      count++;
    }
  }
  return { manifest: count ? { ...manifest, records } : manifest, count };
}

/**
 * 按邮件收到日过滤候选（--date）。接受 "8.14" 或 "2026-08-14"，按本地日比较——
 * 快照 date 是 "2026-08-14 18:50" 格式，字符串 startsWith 对 "8.14" 永远不命中。
 */
export function filterByDate(messages, dateArg) {
  if (!dateArg) return messages;
  const target = parseDateArg(dateArg);
  if (!target) {
    process.stderr.write(`⚠️ --date "${dateArg}" 无法解析（接受 8.14 或 2026-08-14），本次不过滤\n`);
    return messages;
  }
  return (messages || []).filter(m => sameLocalDay(parseMailDate(m?.date_formatted || m?.date), target));
}

// CLI 入口（async：verify_mails 并行化，2026-07-29 缺陷④）
async function main() {
  const args = process.argv.slice(2);
  const dateFilter = getArg(args, '--date');
  const input = getArg(args, '--input') || SCAN_DEFAULT;
  const manifestPath = getArg(args, '--manifest') || MANIFEST_DEFAULT;
  const concurrency = parseInt(getArg(args, '--concurrency') || '5', 10);
  const force = args.includes('--force');

  if (!fs.existsSync(input)) {
    console.error(`先跑 scan_all.mjs 生成扫描快照（期望 ${input}）`);
    process.exit(1);
  }

  const allMessages = JSON.parse(fs.readFileSync(input, 'utf8'));
  // ⚠️ 通知关键词不丢邮件：主题关键词（如"资料收集"）会被候选人回信继承，
  // 按关键词过滤会静默漏真简历。是否相关由详情事实（附件/链接/正文提示）决定，
  // 关键词仅作展示标签。日期过滤按收到日比较（filterByDate）。
  const dateFiltered = filterByDate(allMessages, dateFilter);

  let read = readManifest(manifestPath);
  {
    const r1 = backfillReceivedAt(read, allMessages);
    const r2 = backfillSubject(r1.manifest, allMessages);
    read = r2.manifest;
    if (r1.count > 0 || r2.count > 0) {
      process.stderr.write(`♻️ 回填存量字段：received_at ${r1.count} 条，subject ${r2.count} 条（按快照邮件补齐）\n`);
    }
  }
  const prevManifest = read;
  const { pending: candidates, skipped } = force
    ? { pending: dateFiltered, skipped: [] }
    : filterPending(dateFiltered, prevManifest);
  process.stderr.write(`候选 ${dateFiltered.length} 封：待核查 ${candidates.length} 封，已核查跳过 ${skipped.length} 封（--force 可全量重拉）\n`);

  // 异步 runner：并发 fetch 详情 + 限流指数退避重试（5s/10s/20s，最多3次）
  const asyncRunner = makeLarkRunner({
    onRetry: (err, attempt, delayMs) => {
      process.stderr.write(`  ⏳ 查详情限流，${delayMs}ms 后第${attempt}次重试...\n`);
    },
  });

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
  const newRecords = Object.keys(result.manifest.records).length - Object.keys(prevManifest.records || {}).length;
  process.stderr.write(`\n已发布 manifest: ${manifestPath}（共 ${Object.keys(result.manifest.records).length} 条记录，本次新增 ${newRecords} 条）\n`);

  if (result.failed.length) {
    process.stderr.write(`\n🔴 ${result.failed.length} 封邮件需人工确认：\n`);
    for (const f of result.failed) {
      process.stderr.write(`  - mid=${f.message_id} | ${(f.subject || '').slice(0, 50)} | ${f.error}\n`);
    }
    process.exit(2);
  }
}

const _isDirectRun = isDirectRun(import.meta.url);
if (_isDirectRun) {
  main().catch(e => {
    console.error(`🔴 未捕获异常: ${e.message}`);
    process.exit(1);
  });
}

export { main };

// 测试用内部导出
const verifyMailsInternals = { processMailDetail };
export { verifyMailsInternals };
