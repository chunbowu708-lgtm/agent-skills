// 全量扫描邮箱，穷尽式（分页到 has_more=false）。
//
// 用法: node scan_all.mjs
// 输出候选邮件清单到 stdout，全量数据原子存 notes/_scan_all.json
// 任何异常都不覆盖上一份完整快照（原子发布 + 诊断文件 + 非零退出）。
// （不支持 --date；日期过滤在 verify_mails --date 做）

import { execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { parseCliJson } from './lib/lark_mail.mjs';
import { isNotification } from './lib/notifications.mjs';
import { isDirectRun } from './lib/cli_helpers.mjs';
import { LARK_CLI, SCAN_ALL as OUT, DIAG_DIR, MAX_BUFFER } from './lib/paths.mjs';

const MAX_PAGES = 50; // 安全上限（200×50=10000 封）

/**
 * 执行一次 triage 分页。
 * runner 可注入（测试用），默认 execSync。
 */
function fetchPage(pageToken, runner) {
  const cmd = pageToken
    ? `${LARK_CLI} mail +triage --as user --max 200 --format json --page-token "${pageToken}"`
    : `${LARK_CLI} mail +triage --as user --max 200 --format json`;
  const raw = runner(cmd);
  return parseCliJson(raw); // 严格解析：失败直接抛错
}

/**
 * 核心扫描逻辑（可注入 runner，测试用）。
 *
 * 2026-08-14 增量改造：不再每次全量分页拉几百封（每天固定几分钟）。
 * 传入上次快照的 message_id 集合，triage 分页倒序（新在前）——遇到已见过的
 * message_id 说明后续页全是旧邮件，立即停止翻页，只拉"新邮件"几页。
 * 结果由调用方合并：新邮件 + 旧快照 = 完整快照（新在前，旧在后，按 message_id 去重）。
 *
 * @param {function} runner - (cmdString) => rawStdoutString
 * @param {Set<string>} prevIds - 上次快照的 message_id 集合（可空=全量穷尽，兼容首轮）
 * @returns {{ messages: array, complete: boolean, error?: string, incremental?: boolean }}
 */
export function runScan(runner, prevIds = null) {
  runner = runner || ((cmd) => execSync(cmd, { encoding: 'utf8', maxBuffer: MAX_BUFFER }));

  const allMessages = [];
  const seenIds = new Set();
  let pageToken = '';
  let prevToken = null;
  let page = 0;
  let hitPrevBoundary = false; // 已遇到上次快照的邮件 → 后续（若排序稳定）全是旧的
  let quietPages = 0; // 边界命中后连续"0 新邮件"页数（防排序扰动：回复置顶/线程聚合会把新邮件排进旧区）
  const QUIET_PAGES_TO_STOP = 2;

  while (true) {
    page++;
    const j = fetchPage(pageToken, runner);

    const items = j.messages || j.data?.messages || [];
    let newCount = 0;
    for (const it of items) {
      const id = it.message_id;
      if (id && !seenIds.has(id)) {
        seenIds.add(id);
        allMessages.push(it);
        newCount++;
      }
      // 增量边界：本页出现上次快照已有的邮件
      if (prevIds && id && prevIds.has(id)) {
        hitPrevBoundary = true;
      }
    }
    if (newCount > 0) quietPages = 0; else quietPages++;

    process.stderr.write(`page ${page}: 本页${items.length}封(新增${newCount}) 累计${allMessages.length} has_more=${j.has_more}${hitPrevBoundary ? ` 增量边界命中(静默页${quietPages}/${QUIET_PAGES_TO_STOP})` : ''}\n`);

    if (hitPrevBoundary && quietPages >= QUIET_PAGES_TO_STOP) {
      // 边界命中后连续 N 页零新邮件 → 确认旧邮件区，安全停止。
      // （不再"命中即停"：排序扰动时新邮件可能藏在边界后的 1-2 页）
      return { messages: allMessages, complete: true, incremental: true };
    }

    if (!j.has_more) {
      // 正常结束：完整穷尽（首轮或新邮件量小无边界命中）
      return { messages: allMessages, complete: true, incremental: false };
    }

    const nextToken = j.page_token;
    // 分页游标异常 → 必须 STOP（继续会无限重复拉第1页）
    if (!nextToken) {
      return {
        messages: allMessages,
        complete: false,
        error: `page ${page}: has_more=true 但 page_token 为空，分页游标失效。已停止，前 ${allMessages.length} 封有效。`,
      };
    }
    if (nextToken === prevToken) {
      return {
        messages: allMessages,
        complete: false,
        error: `page ${page}: page_token 与上一页相同，游标未推进（疑似API异常）。已停止，前 ${allMessages.length} 封有效。`,
      };
    }
    prevToken = pageToken;
    pageToken = nextToken;

    if (page >= MAX_PAGES) {
      return {
        messages: allMessages,
        complete: false,
        error: `安全停止: 达到 MAX_PAGES=${MAX_PAGES}（${allMessages.length} 封），但 has_more 仍为 true。`,
      };
    }
  }
}

/**
 * 原子发布完整快照：先写 .tmp 再 rename。
 * 只有 complete=true 才调用此函数。
 */
function publishSnapshot(messages) {
  const data = JSON.stringify(messages, null, 2);
  JSON.parse(data); // 写前自校验
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  const tmp = `${OUT}.tmp`;
  fs.writeFileSync(tmp, data, 'utf8');
  fs.renameSync(tmp, OUT);
}

/**
 * 把部分/失败结果写到诊断目录（不污染主快照）。
 */
function writeDiagnostic(messages, error) {
  fs.mkdirSync(DIAG_DIR, { recursive: true });
  const ts = new Date().toISOString().replace(/[:.]/g, '-');
  const diagPath = path.join(DIAG_DIR, `partial_${ts}.json`);
  fs.writeFileSync(diagPath, JSON.stringify({ error, partial_count: messages.length, messages }, null, 2), 'utf8');
  return diagPath;
}

/**
 * 主入口。
 */
export function main(runner) {
  // 增量：读上次快照的 message_id 集合（不存在则全量穷尽，兼容首轮）
  let prevMessages = [];
  if (fs.existsSync(OUT)) {
    try {
      prevMessages = JSON.parse(fs.readFileSync(OUT, 'utf8'));
      if (!Array.isArray(prevMessages)) prevMessages = [];
    } catch (e) {
      console.error(`⚠️ 旧快照解析失败，退回全量扫描: ${e.message}`);
      prevMessages = [];
    }
  }
  const prevIds = new Set(prevMessages.map(m => m && m.message_id).filter(Boolean));

  let result;
  try {
    result = runScan(runner, prevIds);
  } catch (e) {
    // CLI 执行失败或 JSON 解析失败：不覆盖旧快照，写诊断，非零退出
    const diagPath = writeDiagnostic([], e.message);
    console.error(`🔴 扫描失败: ${e.message}`);
    console.error(`   诊断已存: ${diagPath}`);
    console.error(`   主快照 ${OUT} 未被修改（保留上一份完整数据）`);
    process.exit(2);
  }

  if (!result.complete) {
    // 未穷尽：写诊断，不覆盖主快照，非零退出
    const diagPath = writeDiagnostic(result.messages, result.error);
    console.error(`🔴 未穷尽: ${result.error}`);
    console.error(`   部分结果（${result.messages.length} 封）已存诊断: ${diagPath}`);
    console.error(`   主快照 ${OUT} 未被修改——归档决策前必须重试或确认无遗漏`);
    process.exit(2);
  }

  // 合并：新邮件 + 旧快照（新在前，旧在后，按 message_id 去重，新邮件优先覆盖旧字段）
  let merged = result.messages;
  if (result.incremental && prevMessages.length) {
    const newIds = new Set(result.messages.map(m => m.message_id));
    const stale = prevMessages.filter(m => !newIds.has(m.message_id));
    merged = [...result.messages, ...stale];
    console.log(`\n📈 增量扫描: 新增 ${result.messages.length} 封 + 旧快照 ${stale.length} 封 = ${merged.length} 封（跳过历史 ${prevMessages.length - stale.length} 封重复）`);
  }

  // 完整穷尽/增量截断：原子发布
  publishSnapshot(merged);

  // 通知只打标签，不删除
  const enriched = merged.map(m => ({
    ...m,
    is_notification: isNotification(m),
  }));
  const candidates = enriched.filter(m => !m.is_notification);

  console.log(`\n=== 共 ${enriched.length} 封，候选 ${candidates.length} 封，通知 ${enriched.length - candidates.length} 封 ===`);
  // 2026-08-18：stdout 不再倾倒全量候选清单（913 封×每次 collect = 终端噪音）。
  // 增量模式只打新增；全量清单写文件按需查。完整清单永远在快照 JSON 里。
  if (result.incremental && result.messages.length) {
    const newCandidates = result.messages.filter(m => !isNotification(m));
    console.log(`本次新增 ${newCandidates.length} 封候选:`);
    newCandidates.forEach(m => {
      console.log(`${m.date} | ${(m.from || '').slice(0, 28).padEnd(28)} | ${m.subject}`);
    });
  } else if (!result.incremental) {
    const listPath = OUT.replace(/\.json$/, '_candidates.txt');
    try {
      fs.writeFileSync(listPath, candidates.map(m => `${m.date} | ${m.from || ''} | ${m.message_id} | ${m.subject}`).join('\n'), 'utf-8');
      console.log(`全量候选清单已写文件: ${listPath}（${candidates.length} 封，不再刷屏）`);
    } catch { /* 写文件失败不阻塞扫描 */ }
  }

  process.stderr.write(`\n快照已原子发布: ${OUT}（${merged.length} 封，已按 message_id 去重${result.incremental ? '，增量模式' : '，全量穷尽'}）\n`);
}

// CLI 直接运行时执行 main；被 import 时不自动执行（测试可调 runScan/main）
const _isDirectRun = isDirectRun(import.meta.url);
if (_isDirectRun) {
  main();
}
