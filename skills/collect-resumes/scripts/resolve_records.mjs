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
//   - M.DD 默认 = 该记录邮件收到日（received_at，见 lib/dates.mjs recordDate）；
//     --date 仅作人工显式覆盖。历史积压邮件按真实收到日落档，不混进操作日目录
//   - 绝不创建或修改岗位目录本身

import fs from 'node:fs';
import path from 'node:path';
import {
  readManifest, writeManifestAtomic, transitionRecord, upsertRecord, transitionToExcluded,
} from './lib/manifest.mjs';
import { getArg, isDirectRun } from './lib/cli_helpers.mjs';
import { mdd, recordDate, parseDateArg } from './lib/dates.mjs';
import { ARCHIVE_ROOT, MANIFEST as MANIFEST_DEFAULT } from './lib/paths.mjs';

/**
 * 生成日期段名 {M.DD}（如 7.29）。
 * @param {string} dateStr - 可选，如 "7.29" 或 "2026-07-29"；默认今天
 * @returns {string} 如 "7.29"
 */
export function dateSegment(dateStr) {
  if (dateStr) {
    if (/^\d{1,2}\.\d{1,2}$/.test(dateStr)) return dateStr;
    const d = parseDateArg(dateStr);
    if (d) return mdd(d);
  }
  return mdd(new Date());
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
 * 岗位名归一化：消解投递标题（BOSS文件名提取）vs 目录名的措辞差异。
 * 三层处理（双向：投递标题和目录名都跑同一函数，命中多个仍判歧义 fail-closed）：
 *   1. 去书写差异：空格/横线/·/—/・（全角半角），消解"AI Native 游戏服务端" vs "AI Native游戏服务端"
 *   2. 去括号后缀：（Go）/（AI-Native方向）/（SDK方向）等技术栈/方向修饰，不是岗位核心名
 *   3. 去开头业务前缀词（循环去，处理"资深游戏"连续前缀）：
 *      游戏=行业前缀；资深/高级/中高级/中级/初级/执行=级别修饰，不影响岗位核心方向
 */
function normalizeJobName(s) {
  let r = (s || '').replace(/[\s\-—·・]/g, '').toLowerCase();
  // 去括号及内容（全角（）/半角()）
  r = r.replace(/[（(][^）)]*[）)]/g, '');
  // 去开头的业务前缀词，循环到无变化（长词放前避免子串吞，如"中高级"在"高级"前）
  let prev;
  do {
    prev = r;
    r = r.replace(/^(中高级|资深|高级|中级|初级|执行|游戏)/, '');
  } while (r !== prev);
  return r;
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
    const exactDirs = new Set(exact.map(j => j.job_dir));
    const kin = jobDirs.filter(j =>
      !exactDirs.has(j.job_dir) &&           // 排除已精确/归一化命中的，避免 exact+kin 重复
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
  const rec = manifest.records?.[recordId];
  if (!rec) return { manifest, status: 'bad_record', detail: `记录 ${recordId} 不存在` };
  // --new-version：duplicate / 重复类 excluded 终态重开（候选人交了更新版简历，走正规归档不走旁路）。
  // excluded 只放行重复类排除码（DUPLICATE_ALREADY_ARCHIVED/SAMEDAY_CONFLICT/DUPLICATE_CANDIDATE），
  // NOT_RESUME 等人工排除不属于"重复投递"语义，不重开。
  const DUP_REOPEN_CODES = new Set(['DUPLICATE_ALREADY_ARCHIVED', 'SAMEDAY_CONFLICT', 'DUPLICATE_CANDIDATE']);
  let base = rec;
  if (opts && opts.newVersion) {
    const exCode = rec.exclude_reason && rec.exclude_reason.code;
    const reopenable = rec.status === 'duplicate' ||
      (rec.status === 'excluded' && DUP_REOPEN_CODES.has(exCode));
    if (reopenable) {
      const transitioned = transitionRecord(rec, 'needs_resolution', {
        code: 'NEW_VERSION_REOPEN',
        message: `人工确认是更新版简历，重开归档（${name || rec.candidate_name || ''}）`,
      });
      const { exclude_reason, ...rest } = transitioned;
      base = rest;
    }
  }
  if (!['needs_resolution', 'blocked'].includes(base.status)) {
    return { manifest, status: 'bad_record', detail: `记录状态 ${base.status} 不可解析（需 needs_resolution 或 blocked）` };
  }
  // 日期段：显式 --date 覆盖 > 该记录邮件收到日（received_at，fallback 链见 dates.mjs）
  const dateSeg = (opts && opts.date) ? dateSegment(opts.date) : mdd(recordDate(base));

  const dirs = jobDirs !== undefined ? jobDirs : discoverJobDirs();
  const { matched, ambiguous } = matchJobDir(jobName, dirs);

  if (ambiguous) {
    // 歧义（如 Unity 三岗）→ 保持 needs_resolution
    const updated = { ...base, candidate_name: name, job_name: jobName, target_filename: filename };
    return {
      manifest: upsertRecord(manifest, updated),
      status: 'ambiguous',
      detail: `岗位"${jobName}"匹配到多个目录：\n${matched.map(m => '  ' + m.job_dir).join('\n')}`,
    };
  }
  if (matched.length === 0) {
    const updated = { ...base, candidate_name: name, job_name: jobName, target_filename: filename };
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
  // ⚠️ 2026-08-17 修复：job_name 口径不一是历史坑——早期归档存末级岗位名（"3D角色设计师"），
  // resolve --job 传完整路径时新记录存完整路径（"长青工作室/美术端/3D角色设计师"），
  // 严格相等比对导致去重失效（谢平鑫 7.10 归档过、8.17 又归档一次没被拦）。
  // 比对前统一取末级名（岗位目录末级 = 岗位名，跨团队同名岗视为同人重复，--new-version 可覆盖）。
  const normJob = (j) => String(j || '').split('/').pop();
  const allRecords = Object.values(manifest.records || {});
  const dup = allRecords.some(r =>
    r.record_id !== recordId &&
    r.candidate_name === name &&
    normJob(r.job_name) === normJob(jobName) &&
    ['archived', 'validated'].includes(r.status)
  );
  if (dup && !opts?.newVersion) {
    // 展开姓名/岗位再转终态：不丢 candidate_name（汇总和后续 --new-version 都要用）
    const updated = transitionRecord({ ...base, candidate_name: name, job_name: jobName, target_filename: filename }, 'duplicate', {
      code: 'DUPLICATE_CANDIDATE',
      message: `候选人"${name}"已在岗位"${jobName}"归档过（重复投递/改简历重投），跳过本次归档。若是更新版简历：--new-version 重开归档`,
    });
    return {
      manifest: upsertRecord(manifest, updated),
      status: 'duplicate',
      detail: `候选人"${name}"在岗位"${jobName}"已归档过，跳过（重复投递；更新版用 --new-version）`,
    };
  }

  // 唯一匹配：目标 = 已收集简历/{M.DD}_暂定/<filename>（_暂定 由闸门收敛为 _N份）
  const collected = matched[0].collected_dir;
  const batchDirName = `${dateSeg}${PENDING_SUFFIX}`;
  const batchDir = `${collected}/${batchDirName}`;
  const target = `${batchDir}/${filename}`;

  if (!isWithinArchive(target)) {
    return {
      manifest: upsertRecord(manifest, transitionRecord(base, 'blocked', {
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
      manifest: upsertRecord(manifest, transitionRecord(base, 'blocked', {
        code: 'MKDIR_FAILED', message: `创建目录失败 ${batchDir}: ${e.message}`,
      })),
      status: 'bad_record',
      detail: `创建目录失败：${batchDir}: ${e.message}`,
    };
  }

  let updated = { ...base, candidate_name: name, job_name: jobName, target_dir: batchDir, target_filename: filename };
  // auto 失败缓存标记：人工/force 重试成功后清除（见 autoResolve）
  if (updated.auto_failed_at) { delete updated.auto_failed_at; delete updated.auto_fail_reason; }
  // new-version 重开的记录落标记：download_attachment 前置自愈的"同人同岗去重"据此豁免
  // （2026-08-18 程远娟 bug：补充版被 DUPLICATE_ALREADY_ARCHIVED 误排除，excluded 无出边死锁）
  if (opts && opts.newVersion) updated.new_version = true;
  // blocked → verified 已在状态机 TRANSITIONS 白名单中，无需手动重置
  updated = transitionRecord(updated, 'verified');
  return { manifest: upsertRecord(manifest, updated), status: 'resolved', detail: target };
}

// ---- 文件名自动解析（--auto 批量模式）----
// BOSS 投递简历的文件名本身是结构化信息（【岗位_城市_薪资】姓名_年限.ext），
// 岗位和姓名已在数据里，无需人工重新转录。
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
 * --auto 批量自动解析：
 *   mail_attachment 记录：从文件名自动提取岗位+姓名
 *   link 记录：从邮件主题提取「姓名-岗位」/「姓名_岗位」模式（QQ 超大附件邮件主题即此格式）
 * matchJobDir 唯一匹配才 resolve，否则保留报告（fail-closed 不变）。
 *
 * @returns {{ manifest, resolved: number, ambiguous: number, skipped: number, failures: [{id, name, reason}] }}
 */
// 邮件主题「姓名-岗位」模式：贺丹-游戏ui / 陈韵印_游戏UI_简历和作品集
// 前缀只跳装饰符和回复标记（可选），不得用 \w 类字符集（会误吃中文姓名首字）
const SUBJECT_NAME_JOB_RE = /^(?:(?:回复|Fw|FW|Re)\s*[:：]?\s*)?[【\[\(（\s]*([\u4e00-\u9fa5A-Za-z·]{2,4})\s*[-_－—]\s*(.+)$/;
// 「岗位 姓名」倒序模式：3D动作 王子谦个人作品以及简历（姓名后必须跟边界词，防贪婪吃字）
const SUBJECT_JOB_NAME_RE = /^(3D动作|2D动作|游戏UI|特效|Unity客户端|U3D|客户端|服务端)\s+([\u4e00-\u9fa5]{2,4})(?=个人|的|简历|作品|求职|应聘|$|\s)/;

function parseNameJobFromSubject(subject) {
  if (!subject) return null;
  const s = subject.trim();
  // 倒序模式先试（更specific）
  const m2 = SUBJECT_JOB_NAME_RE.exec(s);
  if (m2) {
    const jobWords = { '3D动作': '3D动作设计师', '2D动作': '2D动作设计师', '游戏UI': '游戏UI设计师', '特效': '特效设计师' };
    return { name: m2[2], job: jobWords[m2[1]] || m2[1] };
  }
  const m = SUBJECT_NAME_JOB_RE.exec(s);
  if (!m) return null;
  // 去噪音：_简历和作品集 / 尾部手机号 / 尾部标点
  const job = m[2].trim()
    .replace(/[_\-－]?(?:简历|作品集?|个人作品)(?:和|与|及|&)?(?:简历|作品集?|个人作品)?/g, '')
    .replace(/[\d\s]{6,}$/, '')
    .replace(/[_\-－：:]+$/, '')
    .trim();
  if (!job) return null;
  return { name: m[1], job };
}

export function autoResolve(manifest, opts = {}) {
  const dirs = discoverJobDirs();
  const records = Object.values(manifest.records || {});
  // 2026-08-18 失败缓存：auto 解析失败过的记录（auto_failed_at）不再每日重试空转
  // （文件名/主题没变，重试必然再失败；93 条僵尸 × 每天重试 = 汇总噪音+耗时）。
  // 记录状态变化（人工改了 subject/filename）或 --force 才重试。人工 resolve 单条不受影响。
  const candidates = records.filter(r =>
    r.status === 'needs_resolution' &&
    !r.auto_failed_at &&
    ((r.source_type === 'mail_attachment' && r.original_filename) || r.source_type === 'link'));
  const cachedSkipped = records.filter(r => r.status === 'needs_resolution' && r.auto_failed_at).length;

  let resolved = 0, ambiguous = 0, skipped = 0;
  const failures = [];
  const markFailed = (rec, reason) => {
    manifest = upsertRecord(manifest, { ...rec, auto_failed_at: new Date().toISOString(), auto_fail_reason: reason });
  };

  for (const rec of candidates) {
    let parsed, filename;
    if (rec.source_type === 'link') {
      // link 记录：从邮件主题提取（subject 由 verify_mails 落盘）
      parsed = parseNameJobFromSubject(rec.subject);
      if (!parsed) {
        skipped++;
        markFailed(rec, '链接类：主题无法提取「姓名-岗位」');
        failures.push({ id: rec.record_id, name: rec.subject || rec.source_url, reason: '链接类：主题无法提取「姓名-岗位」', received_at: rec.received_at });
        continue;
      }
      const kindExt = { large_attachment: 'zip', cloud_disk: 'zip', portfolio: 'pdf' }[rec.link_kind] || 'zip';
      const safeJob = parsed.job.replace(/[\\/:*?"<>|（）()]/g, '');
      filename = `${parsed.name}_${safeJob}${kindExt === 'zip' ? '_简历加作品' : ''}.${kindExt}`;
    } else {
      parsed = parseNameJobFromFilename(rec.original_filename);
      if (!parsed) {
        // 文件名不可靠提取（如 860ca9.jpg / 学历证书 / IMG_0386.PNG）→ 保留，人工处理
        skipped++;
        markFailed(rec, '文件名无法可靠提取岗位+姓名');
        failures.push({ id: rec.record_id, name: rec.original_filename, reason: '文件名无法可靠提取岗位+姓名', received_at: rec.received_at });
        continue;
      }
      filename = deriveTargetFilename(rec.original_filename, parsed.name, parsed.job);
      if (!filename) {
        skipped++;
        markFailed(rec, '无法派生规范文件名');
        failures.push({ id: rec.record_id, name: rec.original_filename, reason: '无法派生规范文件名', received_at: rec.received_at });
        continue;
      }
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
      markFailed(rec, `岗位"${parsed.job}"匹配多个目录，需人工确认`);
      failures.push({ id: rec.record_id, name: parsed.name, reason: `岗位"${parsed.job}"匹配多个目录，需人工确认`, received_at: rec.received_at });
    } else {
      // not_found / bad_record
      skipped++;
      markFailed(rec, result.detail);
      failures.push({ id: rec.record_id, name: parsed.name, reason: result.detail, received_at: rec.received_at });
    }
  }
  return { manifest, resolved, ambiguous, skipped, cachedSkipped, failures };
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

  // --exclude：人工排除一条记录（终结 blocked/无法处理的 needs_resolution），
  // 替代手改 manifest JSON。--code 用结构化原因码（见 SKILL.md 约定）。
  const excludeId = getArg(args, '--exclude');
  if (excludeId) {
    const code = getArg(args, '--code');
    const note = getArg(args, '--note') || '';
    if (!code) {
      console.error('用法: resolve_records.mjs --exclude <record_id> --code <原因码> [--note "说明"]');
      console.error('常用原因码: NOT_RESUME(非简历材料) / NO_MATERIAL(取不到材料) / IRRELEVANT(与招聘无关) / OTHER');
      process.exit(1);
    }
    const manifest = readManifest(manifestPath);
    const rec = manifest.records[excludeId];
    if (!rec) {
      console.error(`🔴 记录不存在: ${excludeId}`);
      process.exit(2);
    }
    try {
      const updated = transitionToExcluded(rec, code, note);
      writeManifestAtomic(manifestPath, upsertRecord(manifest, updated));
      console.log(`⏭️ 已排除: ${excludeId}（${rec.original_filename || rec.source_url || rec.source_type}）code=${code}${note ? ` note=${note}` : ''}`);
    } catch (e) {
      console.error(`🔴 排除失败: ${e.message}`);
      process.exit(2);
    }
    return;
  }

  // --exclude-mail：按邮件批量排除（背调材料一封邮件 13-25 个附件，逐条排除没人做）。
  // 支持逗号分隔多个 message_id 一次排除（2026-08-18：存量收敛 62 封系统邮件 62 次调用没人做）
  const excludeMailId = getArg(args, '--exclude-mail');
  if (excludeMailId) {
    const code = getArg(args, '--code');
    const note = getArg(args, '--note') || '';
    if (!code) {
      console.error('用法: resolve_records.mjs --exclude-mail <message_id[,message_id2,...]> --code <原因码> [--note "说明"]');
      console.error('排除这些邮件下所有 needs_resolution/blocked 记录（不影响已 archived/verified 的）');
      process.exit(1);
    }
    const mailIds = excludeMailId.split(',').map(s => s.trim()).filter(Boolean);
    const manifest = readManifest(manifestPath);
    const targets = Object.values(manifest.records || {}).filter(r =>
      mailIds.includes(r.message_id) && ['needs_resolution', 'blocked'].includes(r.status));
    if (!targets.length) {
      console.error(`🔴 这些邮件没有可排除的记录（needs_resolution/blocked）: ${mailIds.join(', ')}`);
      process.exit(2);
    }
    for (const rec of targets) {
      try {
        const updated = transitionToExcluded(rec, code, note);
        manifest.records[updated.record_id] = updated;
      } catch (e) {
        console.error(`⚠️ 跳过 ${rec.record_id.slice(0, 20)}: ${e.message}`);
      }
    }
    writeManifestAtomic(manifestPath, manifest);
    const mailsHit = new Set(targets.map(r => r.message_id)).size;
    console.log(`⏭️ 已按 ${mailsHit} 封邮件批量排除 ${targets.length} 条 code=${code}${note ? ` note=${note}` : ''}`);
    return;
  }

  if (auto) {
    const force = args.includes('--force');
    const manifest = readManifest(manifestPath);
    const result = autoResolve(manifest, { date, force });
    console.log(`\n==== 自动解析完成: ✅ ${result.resolved} 条 · ⚠️ 歧义 ${result.ambiguous} 条 · ⏭️ 跳过 ${result.skipped} 条${result.cachedSkipped ? ` · 💤 失败缓存跳过 ${result.cachedSkipped} 条（--force 重试）` : ''} ====`);
    // 失败清单只列近 7 天收到的（2026-08-18：历史僵尸不每日重刷屏；要看全量用 --force 或查 manifest）
    const weekAgo = Date.now() - 7 * 24 * 3600 * 1000;
    const recent = result.failures.filter(f => !f.received_at || new Date(f.received_at).getTime() >= weekAgo);
    const old = result.failures.length - recent.length;
    if (recent.length) {
      console.log(`\n需人工处理的 ${recent.length} 条（近7天收到，保留 needs_resolution）:`);
      for (const f of recent) {
        console.log(`  - ${f.name}: ${f.reason}`);
      }
    }
    if (old > 0) console.log(`\nℹ️ 另有 ${old} 条 >7 天的历史失败未列出（已记 auto_failed_at，不再重试；--force 强制重试）`);
    if (!dryRun) {
      writeManifestAtomic(manifestPath, result.manifest);
      console.log(`\n已写入 manifest（${result.resolved} 条推进到 verified，待下载）`);
    } else {
      console.log(`\n[DRY-RUN] 未写盘。去掉 --dry-run 正式执行。`);
    }
    return;
  }

  if (!recordId || !name || !jobName || !filename) {
    console.error('用法: resolve_records.mjs --record <id> --name <姓名> --job <岗位> --filename <文件名> [--manifest <path>] [--date 7.29] [--new-version]');
    console.error('      resolve_records.mjs --auto [--manifest <path>] [--date 7.29] [--dry-run]   # 批量自动解析（从文件名提取岗位+姓名）');
    console.error('      --new-version：duplicate 记录重开归档（更新版简历场景，文件名建议加 _补充AI版 等后缀）');
    process.exit(1);
  }

  const manifest = readManifest(manifestPath);
  const result = resolveRecord(manifest, recordId, name, jobName, filename, undefined, { date, newVersion: args.includes('--new-version') });
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

const _isDirectRun = isDirectRun(import.meta.url);
if (_isDirectRun) main();

export { main };
