// 全量扫描邮箱，穷尽式（分页到 has_more=false）。
//
// 用法: node scan_all.mjs [--date 2026-06-15]   (不传 --date 则列出全部)
// 输出候选邮件清单到 stdout，全量数据原子存 notes/_scan_all.json
// 任何异常都不覆盖上一份完整快照（原子发布 + 诊断文件 + 非零退出）。

import { execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { parseCliJson, cleanTipLines } from './lib/lark_mail.mjs';
import { isNotification } from './lib/notifications.mjs';
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
 * @param {function} runner - (cmdString) => rawStdoutString
 * @returns {{ messages: array, complete: boolean, error?: string }}
 */
export function runScan(runner) {
  runner = runner || ((cmd) => execSync(cmd, { encoding: 'utf8', maxBuffer: MAX_BUFFER }));

  const allMessages = [];
  const seenIds = new Set();
  let pageToken = '';
  let prevToken = null;
  let page = 0;

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
    }

    process.stderr.write(`page ${page}: 本页${items.length}封(新增${newCount}) 累计${allMessages.length} has_more=${j.has_more}\n`);

    if (!j.has_more) {
      // 正常结束：完整穷尽
      return { messages: allMessages, complete: true };
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
  let result;
  try {
    result = runScan(runner);
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

  // 完整穷尽：原子发布
  publishSnapshot(result.messages);

  // 通知只打标签，不删除
  const enriched = result.messages.map(m => ({
    ...m,
    is_notification: isNotification(m),
  }));
  const candidates = enriched.filter(m => !m.is_notification);

  console.log(`\n=== 共 ${enriched.length} 封，候选 ${candidates.length} 封，通知 ${enriched.length - candidates.length} 封 ===\n`);
  candidates.forEach(m => {
    console.log(`${m.date} | ${(m.from || '').slice(0, 28).padEnd(28)} | ${m.message_id} | ${m.subject}`);
  });

  process.stderr.write(`\n全量已原子发布: ${OUT}（${result.messages.length} 封，已按 message_id 去重）\n`);
}

// CLI 直接运行时执行 main；被 import 时不自动执行（测试可调 runScan/main）
import { fileURLToPath } from 'node:url';
const isDirectRun = process.argv[1] &&
  fileURLToPath(import.meta.url).replace(/\\/g, '/') === path.resolve(process.argv[1]).replace(/\\/g, '/');
if (isDirectRun) {
  main();
}
