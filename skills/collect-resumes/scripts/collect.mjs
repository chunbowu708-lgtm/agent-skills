// collect.mjs — 收简历一条命令（编排器）。
//
// 用法:
//   node collect.mjs                       # 日常：今天收到的邮件全流程到闸门
//   node collect.mjs --backlog             # 清积压：所有 verified 记录（含历史）都下载
//   node collect.mjs --no-finish           # 只到下载+合并，不跑脱敏/闸门（人工处理时）
//   node collect.mjs --date 7.29           # 只处理收到日=7.29 的记录（默认今天收到的）
//
// 阶段（全部复用既有脚本，本文件只做编排，不重复实现任何逻辑）：
//   scan_all → verify_mails(增量) → resolve --auto（目录日期段=各记录邮件收到日）
//   → download_attachment(scope 内 verified) → batch_download_links(scope 内链接)
//   → [自动合并已内置] → redact_salary --dir → verify_archive（每个受影响目录）
//
// 失败语义（fail-closed）：
//   - 阶段脚本非零退出 → 如实报告，已完成的保留；闸门 STOP 不掩盖。
//   - 歧义岗位/无法解析的记录保留 needs_resolution，在汇总里列出需人工处理清单。
//   - 退出码：0=全绿；2=有人工事项（歧义/下载失败/闸门STOP）；1=流程性错误。

import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { getArg, isDirectRun } from './lib/cli_helpers.mjs';
import { readManifest } from './lib/manifest.mjs';
import { parseDateArg, sameLocalDay, recordDate } from './lib/dates.mjs';
import { MANIFEST as MANIFEST_DEFAULT } from './lib/paths.mjs';

const SCRIPTS_DIR = path.dirname(fileURLToPath(import.meta.url));

function runNode(script, args) {
  const cmd = [script, ...args].join(' ');
  console.log(`\n━━━ ▶ node ${cmd}`);
  const r = spawnSync(process.execPath, [path.join(SCRIPTS_DIR, script), ...args], { stdio: 'inherit' });
  return r.status === null ? 1 : r.status;
}

function resolvePython() {
  // 2026-08-14 修复：PATH 里的 python 常缺 PyMuPDF/python-docx → redact 阶段整批失败（"需要 PyMuPDF"）。
  // 优先用项目 venv（依赖齐全），仍可用 PYTHON 环境变量覆盖；都不可用才退回 PATH python/py。
  if (process.env.PYTHON) return process.env.PYTHON;
  const candidates = [
    'python',
  ];
  for (const c of candidates) {
    try { if (require('fs').statSync(c).isFile()) return c; } catch { /* 继续下一个 */ }
  }
  return 'python';
}

function runPython(script, args) {
  // ⚠️ 不能 shell:true：中文路径含空格/全角括号时，cmd 会按空格/括号重新拆参
  // （实测 "交互设计师（AI UGC平台）" 被截断成 "交互设计师（AI" → 误报目录不存在）。
  // 无 shell spawn：Windows 下 node 按 PATHEXT 找 python.exe；找不到再试 py 启动器。
  const py = resolvePython();
  console.log(`\n━━━ ▶ python ${script} ${args.map(a => `"${a}"`).join(' ')}`);
  let r = spawnSync(py, [path.join(SCRIPTS_DIR, script), ...args], { stdio: 'inherit' });
  if (r.error && r.error.code === 'ENOENT' && (py === 'python' || py === 'py')) {
    r = spawnSync('py', ['-3', path.join(SCRIPTS_DIR, script), ...args], { stdio: 'inherit' });
  }
  return r.status === null ? 1 : r.status;
}

async function main() {
  const args = process.argv.slice(2);
  const manifestPath = getArg(args, '--manifest') || MANIFEST_DEFAULT;
  const backlog = args.includes('--backlog');
  const finish = !args.includes('--no-finish');
  const date = getArg(args, '--date');
  const humanNotes = [];

  // ---- 阶段1-2：扫描 + 增量核查 ----
  if (runNode('scan_all.mjs', []) !== 0) { console.error('\n🔴 阶段1 扫描失败，中止'); process.exit(1); }
  // verify 退出码 2 = 有邮件需人工确认（blocked 已入 manifest），流程继续，汇总里呈现
  const verifyCode = runNode('verify_mails.mjs', ['--manifest', manifestPath]);
  if (verifyCode === 1) { console.error('\n🔴 阶段2 核查异常，中止'); process.exit(1); }
  if (verifyCode === 2) humanNotes.push('verify：有邮件详情拉取失败/正文提示材料但无来源（见上方 blocked 清单）');

  // ---- 阶段3：自动解析（歧义保留 needs_resolution；目录日期段=各记录邮件收到日）----
  const resolveCode = runNode('resolve_records.mjs', ['--auto', '--manifest', manifestPath]);
  if (resolveCode !== 0 && resolveCode !== 2) {
    humanNotes.push(`🔴 resolve --auto 异常退出（退出码 ${resolveCode}），本批记录可能全部未解析——重跑或单条 resolve`);
  }

  // ---- 阶段4：下载（默认今天收到的记录；--date 限定收到日；--backlog 全量）----
  // "何时收到"唯一判定依据 = received_at（邮件真实时间，见 lib/dates.mjs）。
  // updated_at 会被增量核查刷新、created_at 是首次入库时间，都不能代表邮件时间。
  const scopeDate = date ? parseDateArg(date) : new Date();
  if (date && !scopeDate) {
    console.error(`🔴 --date "${date}" 无法解析（接受 8.14 或 2026-08-14）`);
    process.exit(1);
  }
  const scopeLabel = date ? `收到日 ${date}` : '今日';
  // 近 7 天 verified 兜底：处理日滞后于收到日（深夜邮件次日处理、周末没人跑）
  // 的记录默认纳入下载 scope，防止转 verified 后滑进"历史积压"永不下载
  const RECENT_DAYS = 7;
  const recentCutoff = Date.now() - RECENT_DAYS * 24 * 60 * 60 * 1000;
  const manifest = readManifest(manifestPath);
  const recs = Object.values(manifest.records || {});
  // 下载前已 archived 的记录集合——阶段5 只对"本次新转 archived"的目录跑脱敏/闸门
  // （--backlog 下 inScope 恒真，不记基线会对全部历史目录重跑闸门，几小时级灾难）
  const archivedBefore = new Set(recs.filter(r => r.status === 'archived').map(r => r.record_id));
  const inScope = (r) => backlog
    || sameLocalDay(recordDate(r), scopeDate)
    || (['verified', 'archived'].includes(r.status) && recordDate(r) && recordDate(r).getTime() >= recentCutoff);
  const attIds = recs
    .filter(r => r.source_type === 'mail_attachment' && r.status === 'verified' && inScope(r))
    .map(r => r.record_id);
  const linkIds = recs
    .filter(r => r.source_type === 'link' && r.status === 'verified' && inScope(r))
    .map(r => r.record_id);

  let downloadFailed = 0;
  if (attIds.length) {
    const code = runNode('download_attachment.mjs', ['--records', attIds.join(','), '--manifest', manifestPath]);
    if (code !== 0) { downloadFailed++; humanNotes.push(`附件下载有失败项（退出码 ${code}），失败记录保留 verified，可重跑`); }
  } else {
    console.log(`\nℹ️ 无待下载附件（${scopeLabel} scope 0 条）`);
  }
  if (linkIds.length) {
    const code = runNode('batch_download_links.mjs', ['--records', linkIds.join(','), '--manifest', manifestPath]);
    if (code !== 0) { downloadFailed++; humanNotes.push(`链接下载有失败/失效项（退出码 ${code}），失效已标 blocked`); }
  } else {
    console.log('ℹ️ 无待下载链接');
  }

  // ---- 阶段5：脱敏 + 闸门（受影响目录 = 本次新 archived 记录的 target_dir）----
  const gateResults = [];
  if (finish) {
    const after = readManifest(manifestPath);
    const touchedDirs = [...new Set(Object.values(after.records || {})
      .filter(r => r.status === 'archived' && !archivedBefore.has(r.record_id) && r.target_dir)
      .map(r => r.target_dir))];
    if (!touchedDirs.length) {
      console.log('\nℹ️ 本次无新归档目录，跳过脱敏/闸门');
    }
    for (const dir of touchedDirs) {
      const redactCode = runPython('redact_salary.py', ['--dir', dir]);
      if (redactCode !== 0) {
        gateResults.push({ dir, ok: false, stage: 'redact', code: redactCode });
        continue; // 脱敏失败不跑闸门（闸门必然 STOP，浪费一轮）
      }
      const gateCode = runPython('verify_archive.py', [dir, '--manifest', manifestPath]);
      gateResults.push({ dir, ok: gateCode === 0, stage: 'gate', code: gateCode });
    }
  }

  // ---- 汇总 ----
  // 新到 vs 积压分界：近 7 天收到 = 新到（完整列出、决定退出码）；更早 = 积压（只报数量）。
  // 反例教训：114 条存量 needs_resolution 按插入序取前 20 条显示，新到的贺丹排第 113 位
  // 永远不可见，QQ 超大附件简历静默漏掉。
  const final = readManifest(manifestPath);
  const finalRecs = Object.values(final.records || {});
  const needsHuman = finalRecs.filter(r => r.status === 'needs_resolution');
  const blocked = finalRecs.filter(r => r.status === 'blocked');
  const verifiedPending = finalRecs.filter(r => r.status === 'verified');
  const label = (r) => {
    const name = r.subject || r.original_filename || r.source_url || r.record_id;
    return `${name}${r.job_name ? `（${r.job_name}）` : ''}${r.received_at ? ` [${String(r.received_at).slice(5, 16)}]` : ''}`;
  };
  const isRecent = (r) => {
    const d = recordDate(r);
    return !d || d.getTime() >= recentCutoff; // 无日期的当新到处理（fail-closed）
  };
  const recentNeedsHuman = needsHuman.filter(isRecent);
  const staleNeedsHuman = needsHuman.filter(r => !isRecent(r));
  const recentBlocked = blocked.filter(isRecent);
  const staleBlocked = blocked.filter(r => !isRecent(r));

  // 2026-08-18 汇总可行动性改造：①新到标注"已挂 N 天"（同一批僵尸别再无声重复 7 天，
  // 挂久了就该 resolve/--exclude 落终态）；②历史积压附构成分类（黑盒总数曾掩盖
  // "37/40 blocked 实为邀约误判、93 条待解析 55% 是 6 月死简历"的事实）
  const daysHeld = (r) => {
    const d = recordDate(r);
    if (!d) return '?';
    return Math.max(0, Math.floor((Date.now() - d.getTime()) / (24 * 3600 * 1000)));
  };
  const classifyStale = (r) => {
    const s = r.subject || '';
    if (/视频面试邀约|欢迎加入|【资料收集】|资料收集|薪酬|信息征集|信息记录/.test(s)) return '系统/流程邮件';
    if (/BOSS直聘|应聘|简历/.test(s)) return '老投递';
    return '其他';
  };
  console.log('\n' + '═'.repeat(52));
  console.log('📋 collect 汇总');
  console.log('═'.repeat(52));
  if (recentNeedsHuman.length) {
    console.log(`\n🔴 新到待解析 ${recentNeedsHuman.length} 条（近${RECENT_DAYS}天收到，需处理，完整列出）：`);
    for (const r of recentNeedsHuman.sort((a, b) => String(b.received_at).localeCompare(String(a.received_at)))) {
      const held = daysHeld(r);
      console.log(`  - ${label(r)}${held >= 2 ? ` ⏳已挂${held}天——今天该 resolve 或 --exclude 落终态` : ''}`);
    }
  }
  if (staleNeedsHuman.length) {
    const byClass = {};
    for (const r of staleNeedsHuman) byClass[classifyStale(r)] = (byClass[classifyStale(r)] || 0) + 1;
    const breakdown = Object.entries(byClass).map(([k, v]) => `${k} ${v}`).join(' / ');
    console.log(`\nℹ️ 历史积压待解析 ${staleNeedsHuman.length} 条（>7天；构成：${breakdown}；清理：--exclude-mail 批量或逐条 resolve）`);
  }
  if (recentBlocked.length) {
    console.log(`\n🔴 新到 blocked ${recentBlocked.length} 条（近${RECENT_DAYS}天，需人工：重发材料或 --exclude）：`);
    for (const r of recentBlocked.sort((a, b) => String(b.received_at).localeCompare(String(a.received_at)))) {
      const last = (r.errors && r.errors[r.errors.length - 1]) || {};
      console.log(`  - ${label(r)}: ${last.code || ''} ${(last.message || '').slice(0, 60)}`);
    }
  }
  if (staleBlocked.length) {
    const byClass = {};
    for (const r of staleBlocked) byClass[classifyStale(r)] = (byClass[classifyStale(r)] || 0) + 1;
    const breakdown = Object.entries(byClass).map(([k, v]) => `${k} ${v}`).join(' / ');
    console.log(`\nℹ️ 历史积压 blocked ${staleBlocked.length} 条（>7天；构成：${breakdown}；逐条 --exclude 或催重发）`);
  }
  if (!backlog && verifiedPending.length > 0) {
    console.log(`\nℹ️ 历史积压：${verifiedPending.length} 条 verified 未下载（collect --backlog 可清理）`);
  }
  // scope 外新记录提示：处理日滞后于收到日（深夜邮件次日处理）时防止漏关注
  const outOfScopeNew = verifiedPending.filter(r => !inScope(r) && isRecent(r));
  if (outOfScopeNew.length) {
    console.log(`\n⚠️ 有 ${outOfScopeNew.length} 条近${RECENT_DAYS}天的 verified 记录不在${scopeLabel} scope（收到日更早），未下载——按需 --date <收到日> 处理：`);
    for (const r of outOfScopeNew.slice(0, 10)) console.log(`  - ${label(r)}`);
  }
  if (gateResults.length) {
    console.log('\n🚦 闸门结果：');
    for (const g of gateResults) {
      console.log(`  ${g.ok ? '🟢' : '🔴'} ${g.dir} ${g.stage === 'gate' ? '' : `（${g.stage} 阶段失败，未跑闸门）`}`);
    }
  }
  if (humanNotes.length) {
    console.log('\n📝 过程注意：');
    humanNotes.forEach(n => console.log(`  - ${n}`));
  }
  const allGatesOk = gateResults.every(g => g.ok);
  // exit 2 只由"新到"事项触发；存量积压不触发（避免永远黄灯，狼来了效应）
  const hasNewHumanWork = recentNeedsHuman.length > 0 || recentBlocked.length > 0 || downloadFailed > 0 || !allGatesOk;
  if (hasNewHumanWork) {
    console.log('\n🔴 完成但有【新到】人工事项需处理（见上）');
  } else if (staleNeedsHuman.length + staleBlocked.length + verifiedPending.length > 0) {
    console.log('\n🟡 本次批次干净；存在历史积压（见上，不阻塞本批）');
  } else {
    console.log('\n🟢 全部完成，闸门全过');
  }
  process.exit(hasNewHumanWork ? 2 : 0);
}

const _isDirectRun = isDirectRun(import.meta.url);
if (_isDirectRun) {
  main().catch(e => {
    console.error(`🔴 collect 异常: ${e.message}`);
    process.exit(1);
  });
}

export { main };
