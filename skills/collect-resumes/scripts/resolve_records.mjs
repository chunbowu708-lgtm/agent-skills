// resolve_records.mjs
// 将候选人、岗位、目标目录绑定到 manifest 记录。
//
// 用法:
//   node resolve_records.mjs --manifest <path> --record <id> \
//     --name 张三 --job 特效设计师 --filename 张三_特效设计师_5年.pdf [--date 7.29]
//
// 目标目录从归档根动态发现，路径必须唯一存在于归档根内，
// 歧义/未知岗位保持 needs_resolution，绝不创建不存在的岗位目录。
//
// 不变量：
//   - 目标路径规范化后必须位于 ARCHIVE_ROOT 内（防路径逃逸）
//   - 岗位目录必须已存在且唯一（歧义 → needs_resolution）
//   - 只创建 {M.DD}_暂定 日期子目录（中转态，由 verify_archive 收尾时 rename 为 _N份）
//   - 绝不创建或修改岗位目录本身
//
// 2026-07-29：target_dir 不再指向"已收集简历"根，改为"已收集简历/{M.DD}_暂定/"。
//   根因：旧版落根目录 → verify_archive 数量闸门因目录名无 _N份 标注而 STOP。
//   _暂定 中转态由闸门自动收敛为 _N份（数人头后 rename），N 永远自洽。

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  readManifest, writeManifestAtomic, transitionRecord, upsertRecord,
} from './lib/manifest.mjs';
import { ARCHIVE_ROOT, MANIFEST as MANIFEST_DEFAULT } from './lib/paths.mjs';

/**
 * 生成今天的日期段名 {M.DD}（如 7.29）。
 * @param {string} dateStr - 可选，如 "7.29" 或 "2026-07-29"；默认今天
 * @returns {string} 如 "7.29"
 */
export function dateSegment(dateStr) {
  if (dateStr) {
    // 接受 "7.29" 或 "2026-07-29" 两种格式
    if (/^\d{1,2}\.\d{1,2}$/.test(dateStr)) return dateStr;
    const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(dateStr);
    if (m) return `${parseInt(m[2])}.${parseInt(m[3])}`;
  }
  const now = new Date();
  return `${now.getMonth() + 1}.${now.getDate()}`;
}

/** _暂定 中转目录段名（由 verify_archive rename 为 _{N}份）。 */
const PENDING_SUFFIX = '_暂定';

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
        // collected_dir 统一正斜杠存储（2026-07-29，M2：旧版 path.join 产生反斜杠，
        // 与项目正斜杠约定不一致，下游字符串比较会出错）
        const collectedDir = (fs.existsSync(collectedPath) ? collectedPath : collectedLegacy)
          .replace(/\\/g, '/');
        results.push({
          job_dir: rel,
          collected_dir: collectedDir,
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
 * 岗位名归一化：去空格/横线/·/—（全角半角），用于消解"AI Native 游戏服务端" vs
 * "AI Native游戏服务端"这类书写差异导致的漏匹配。不改变语义（不做同义词）。
 */
function normalizeJobName(s) {
  return (s || '').replace(/[\s\-—·・]/g, '').toLowerCase();
}

/**
 * 按岗位名模糊匹配已发现的岗位目录。
 *
 * 匹配策略（fail-closed：疑似歧义就判 ambiguous，绝不静默选错）：
 *   0. 路径前缀匹配：jobName 含 '/'（如 "长青工作室/美术端/游戏UI设计师"）
 *      → 优先按路径后缀精确匹配，消除跨工作室撞名歧义。
 *      （2026-08-06：8.05 把游戏UI设计师误归坤灵UIUE的教训——
 *      Agent 传完整路径前缀时，绝不走模糊匹配）
 *   1. 精确匹配最后一级目录名（jobName === 末段）
 *   1b. 归一化精确匹配（去空格/横线，2026-08-13）：
 *       精确匹配无果时，按归一化后的末段名再精确匹配一次，消解书写差异。
 *       仍唯一命中才生效；多个命中照样判歧义（不静默选错）。
 *   2. 精确命中后，额外检测"近义岗位"：是否存在其他目录的末段【包含 jobName 或被 jobName 包含】
 *      （如 jobName="特效设计师" 精确命中后，发现还有"Unity特效设计师"包含它 → 判歧义）
 *      近义歧义必须人工确认，因为岗位名常带引擎/方向前缀，错配=归错档。
 *   3. 无精确命中 → 包含匹配，多个则歧义。
 *
 * @param {string} jobName - 岗位名或路径前缀（如"特效设计师"或"长青工作室/美术端/游戏UI设计师"）
 * @param {array} jobDirs - discoverJobDirs 的结果
 * @returns {{ matched: array, ambiguous: boolean }}
 */
export function matchJobDir(jobName, jobDirs) {
  if (!jobName) return { matched: [], ambiguous: false };
  const lower = jobName.toLowerCase();
  const lastSegOf = j => j.job_dir.split('/').pop().toLowerCase();

  // 策略0：路径前缀匹配（jobName 含 '/'）—— Agent 显式指定完整路径
  if (lower.includes('/')) {
    // 按 job_dir 后缀匹配（支持传 "工作室/岗位" 或 "工作室/团队/岗位" 任意层级前缀）
    const prefixMatches = jobDirs.filter(j =>
      j.job_dir.toLowerCase().endsWith(lower) ||
      j.job_dir.toLowerCase().includes('/' + lower)
    );
    if (prefixMatches.length === 1) {
      return { matched: prefixMatches, ambiguous: false };
    }
    if (prefixMatches.length > 1) {
      return { matched: prefixMatches, ambiguous: true };
    }
    // 路径前缀没匹配上 → 不降级到模糊匹配（fail-closed，路径写错了应该报错而非猜）
    return { matched: [], ambiguous: false };
  }

  // 策略1：精确匹配最后一级目录名
  let exact = jobDirs.filter(j => lastSegOf(j) === lower);

  // 策略1b：归一化精确匹配（2026-08-13）—— 消解"空格/横线"书写差异。
  // 仅当精确匹配无果时启用；归一化命中多个仍判歧义（fail-closed）。
  if (exact.length === 0) {
    const normTarget = normalizeJobName(lower);
    const normMatches = jobDirs.filter(j => normalizeJobName(lastSegOf(j)) === normTarget);
    if (normMatches.length >= 1) {
      exact = normMatches;
    }
  }

  if (exact.length >= 1) {
    // 近义歧义检测：精确命中后，看是否还有其他目录末段与 jobName 存在包含关系
    // （排除已精确命中的那些）。存在即疑似同岗不同细分 → fail-closed 判歧义。
    const kin = jobDirs.filter(j =>
      lastSegOf(j) !== lower &&              // 不是精确命中
      lastSegOf(j).includes(lower)           // 但末段包含 jobName（如 "Unity特效设计师" 含 "特效设计师"）
    );
    if (kin.length > 0) {
      // 合并 exact + kin 一起作为候选，交人工
      return { matched: [...exact, ...kin], ambiguous: true };
    }
    return { matched: exact, ambiguous: exact.length > 1 };
  }
  // 策略3：包含匹配
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
 * @param {object} opts - { date?: string } 日期段（如 "7.29"），默认今天
 * @returns {{ manifest: object, status: 'resolved'|'ambiguous'|'not_found'|'bad_record', detail: string }}
 */
export function resolveRecord(manifest, recordId, name, jobName, filename, jobDirs, opts) {
  const dateSeg = dateSegment(opts && opts.date);
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

  // 去重检查（2026-07-31）：同人同岗已归档过 → 跳过，不重复归档污染评估状态。
  // 工作习惯：候选人重复投递（含改简历重投）是常态，已有归档+评估记录的，
  // 再归档只会让同批次出现重复人头、污染已评估档位。命中→标 duplicate 跳过下载。
  // 比对键：candidate_name + job_name（同岗同名即视为同人，talent_id 未绑定时这是最稳的判据）。
  const allRecords = Object.values(manifest.records || {});
  const dup = allRecords.some(r =>
    r.record_id !== recordId &&
    r.candidate_name === name &&
    r.job_name === jobName &&
    ['archived', 'validated'].includes(r.status)
  );
  if (dup) {
    const updated = transitionRecord(rec, 'duplicate', {
      code: 'DUPLICATE_CANDIDATE',
      message: `候选人"${name}"已在岗位"${jobName}"归档过（重复投递/改简历重投），跳过本次归档`,
    });
    return {
      manifest: upsertRecord(manifest, updated),
      status: 'duplicate',
      detail: `候选人"${name}"在岗位"${jobName}"已归档过，跳过（重复投递）`,
    };
  }

  // 唯一匹配：目标 = 已收集简历/{M.DD}_暂定/<filename>
  // 2026-07-29：旧版直接落"已收集简历"根 → 数量闸门因无 _N份 标注 STOP。
  // _暂定 中转目录由 verify_archive 收尾 rename 为 _{N}份。
  const collected = matched[0].collected_dir;
  const batchDirName = `${dateSeg}${PENDING_SUFFIX}`;
  const batchDir = `${collected}/${batchDirName}`;
  const target = `${batchDir}/${filename}`;

  if (!isWithinArchive(target)) {
    return {
      manifest: upsertRecord(manifest, transitionRecord(rec, 'blocked', {
        code: 'PATH_ESCAPE', message: `目标路径 ${target} 不在归档根内`,
      })),
      status: 'bad_record',
      detail: `路径逃逸：${target}`,
    };
  }

  // 创建 _暂定 中转目录（仅此目录，不碰岗位目录）
  // 用正斜杠 mkdirSync（Windows 接受正斜杠）
  try {
    fs.mkdirSync(batchDir, { recursive: true });
  } catch (e) {
    return {
      manifest: upsertRecord(manifest, transitionRecord(rec, 'blocked', {
        code: 'MKDIR_FAILED', message: `创建目录失败 ${batchDir}: ${e.message}`,
      })),
      status: 'bad_record',
      detail: `创建目录失败：${batchDir}: ${e.message}`,
    };
  }

  let updated = { ...rec, candidate_name: name, job_name: jobName, target_dir: batchDir, target_filename: filename };
  // blocked → verified 已在状态机 TRANSITIONS 白名单中，无需手动重置
  updated = transitionRecord(updated, 'verified');
  return { manifest: upsertRecord(manifest, updated), status: 'resolved', detail: target };
}

// ---- 文件名自动解析（--auto 批量模式，2026-08-13 对抗式审查新增）----
// 第一性原理：BOSS 投递简历的文件名本身就是结构化信息（【岗位_城市_薪资】姓名_年限.ext），
// 岗位和姓名已在数据里，无需 AI 逐条人工转录。resolve 漏人 455 条积压一个月的根因就是
// "信息在文件名里却要人工重新输入一遍"——这是最典型的重复造轮子。
// 自动解析只在"唯一匹配 + 精确提取"时才落盘，歧义/失败一律保留 needs_resolution 报告出来，
// 绝不静默猜错（与 matchJobDir 的 fail-closed 一致）。

// BOSS 投递标题：【岗位_城市_薪资】姓名_年限.pdf 或 作品集-【岗位_城市_薪资】姓名_年限.pdf
const BOSS_TITLE_RE = /【(.+?)】\s*([^\s_\-—]+?)(?:[_\-—]\d+年|[_\-—]一年以内|[_\-—]应届|[_\-—]不限|[_\-—]N年)?\.(pdf|docx|doc|zip|rar|7z|png|jpg|jpeg)$/i;

/**
 * 从 original_filename 自动提取 { job, name }。
 * 返回 null 表示无法可靠提取（含"作品集-"前缀也能提取，但年限/岗位/姓名必须齐全）。
 */
export function parseNameJobFromFilename(filename) {
  if (!filename) return null;
  const fn = filename.trim();
  const m = BOSS_TITLE_RE.exec(fn);
  if (!m) return null;
  const job = m[1].split('_')[0].trim();  // 岗位名取第一个 _ 前（去掉城市/薪资）
  const name = m[2].trim();
  if (!job || !name) return null;
  // 姓名必须是纯中文（2-4 字），排除误提取到数字/英文/乱码
  if (!/^[\u4e00-\u9fff]{2,4}$/.test(name)) return null;
  if (!/[\u4e00-\u9fff]/.test(job)) return null;  // 岗位必须含中文
  return { job, name };
}

/**
 * 从 original_filename 派生规范目标文件名（姓名_岗位_年限.ext）。
 * 仅在能同时解析出姓名+岗位时返回；否则返回 null（需人工给 filename）。
 */
export function deriveTargetFilename(filename, name, job) {
  if (!filename) return null;
  const extMatch = /\.(pdf|docx|doc|zip|rar|7z|png|jpg|jpeg)$/i.exec(filename);
  if (!extMatch) return null;
  // 从原文件名提取年限（如"13年""一年以内""应届"）
  const yearMatch = /(?:[_\-—])(\d+年|一年以内|应届|不限)(?:\.|$)/.exec(filename);
  const year = yearMatch ? yearMatch[1] : '';
  const safeJob = job.replace(/[\\/:*?"<>|]/g, '').replace(/[\s]/g, '');
  const base = `${name}_${safeJob}`;
  return `${base}${year ? '_' + year : ''}.${extMatch[1].toLowerCase()}`;
}

/**
 * --auto 批量自动解析：扫所有 needs_resolution 的 mail_attachment 记录，
 * 从文件名自动提取岗位+姓名 → matchJobDir 唯一匹配才 resolve，否则保留报告。
 *
 * @returns {{ manifest, resolved: number, ambiguous: number, skipped: number, failures: [{id, name, reason}] }}
 */
export function autoResolve(manifest, opts = {}) {
  const dirs = discoverJobDirs();
  const records = Object.values(manifest.records || {});
  const candidates = records.filter(r => r.status === 'needs_resolution' && r.source_type === 'mail_attachment' && r.original_filename);

  let resolved = 0, ambiguous = 0, skipped = 0;
  const failures = [];

  for (const rec of candidates) {
    const parsed = parseNameJobFromFilename(rec.original_filename);
    if (!parsed) {
      // 文件名不可靠提取（如 860ca9.jpg / 学历证书 / IMG_0386.PNG）→ 保留，人工处理
      skipped++;
      failures.push({ id: rec.record_id, name: rec.original_filename, reason: '文件名无法可靠提取岗位+姓名' });
      continue;
    }
    const filename = deriveTargetFilename(rec.original_filename, parsed.name, parsed.job);
    if (!filename) {
      skipped++;
      failures.push({ id: rec.record_id, name: rec.original_filename, reason: '无法派生规范文件名' });
      continue;
    }
    // 复用 resolveRecord 的完整逻辑（去重/歧义/路径逃逸/唯一匹配）
    const result = resolveRecord(manifest, rec.record_id, parsed.name, parsed.job, filename, dirs, { date: opts.date });
    manifest = result.manifest;
    if (result.status === 'resolved') {
      resolved++;
    } else if (result.status === 'duplicate') {
      skipped++;  // 重复投递，正常跳过
    } else if (result.status === 'ambiguous') {
      ambiguous++;
      failures.push({ id: rec.record_id, name: parsed.name, reason: `岗位"${parsed.job}"匹配多个目录，需人工确认` });
    } else {
      // not_found / bad_record
      skipped++;
      failures.push({ id: rec.record_id, name: parsed.name, reason: result.detail });
    }
  }
  return { manifest, resolved, ambiguous, skipped, failures };
}

// CLI 入口
function main() {
  const args = process.argv.slice(2);
  const manifestPath = getArg(args, '--manifest') || MANIFEST_DEFAULT;
  const recordId = getArg(args, '--record');
  const name = getArg(args, '--name');
  const jobName = getArg(args, '--job');
  const filename = getArg(args, '--filename');
  const date = getArg(args, '--date');
  const auto = args.includes('--auto');
  const dryRun = args.includes('--dry-run');

  if (auto) {
    const manifest = readManifest(manifestPath);
    const result = autoResolve(manifest, { date });
    console.log(`\n==== 自动解析完成: ✅ ${result.resolved} 条 · ⚠️ 歧义 ${result.ambiguous} 条 · ⏭️ 跳过 ${result.skipped} 条 ====`);
    if (result.failures.length) {
      console.log(`\n需人工处理的 ${result.failures.length} 条（保留 needs_resolution）:`);
      for (const f of result.failures) {
        console.log(`  - ${f.name}: ${f.reason}`);
      }
    }
    if (!dryRun) {
      writeManifestAtomic(manifestPath, result.manifest);
      console.log(`\n已写入 manifest（${result.resolved} 条推进到 verified，待下载）`);
    } else {
      console.log(`\n[DRY-RUN] 未写盘。去掉 --dry-run 正式执行。`);
    }
    return;
  }

  if (!recordId || !name || !jobName || !filename) {
    console.error('用法: resolve_records.mjs --record <id> --name <姓名> --job <岗位> --filename <文件名> [--manifest <path>] [--date 7.29]');
    console.error('      resolve_records.mjs --auto [--manifest <path>] [--date 7.29] [--dry-run]   # 批量自动解析（从文件名提取岗位+姓名）');
    process.exit(1);
  }

  const manifest = readManifest(manifestPath);
  const result = resolveRecord(manifest, recordId, name, jobName, filename, undefined, { date });
  writeManifestAtomic(manifestPath, result.manifest);

  if (result.status === 'resolved') {
    console.log(`✅ 已解析: ${recordId} -> ${result.detail}`);
  } else if (result.status === 'duplicate') {
    // 重复投递跳过：正常退出（不是错误），醒目提示
    console.log(`⏭️  跳过: ${result.detail}`);
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
