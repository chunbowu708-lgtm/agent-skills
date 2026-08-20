// batch_download_links.mjs — 批量下载 QQ/网易大附件链接（Playwright 驱动）
//
// 解决"逐个手动浏览器下载"问题：一个脚本批量处理多个链接。
//
// 用法:
//   # 从 manifest 读所有 verified 的 link 记录批量下载（清历史积压）
//   node batch_download_links.mjs --manifest <PROJECT_ROOT>/notes/collection_manifest.json
//
//   # 指定 record ID（逗号分隔）——当天收简历推荐，精准只下今天的
//   node batch_download_links.mjs --records "sha256:xxx,sha256:yyy" --manifest <path>
//
//   # 直接传 URL（绕过 manifest，不进状态闭环）
//   node batch_download_links.mjs --urls "https://wx.mail.qq.com/..." "https://..."
//
// 输出到 Downloads 目录。下载成功自动写 result.json + 移文件到 target_dir
// + 自动合并推进 archived；链接确认失效的记录推进 blocked（LINK_EXPIRED），
// 让闸门能发现"人没归档"的缺失，而不是永远静默重试。

import fs from 'node:fs';
import path from 'node:path';
import { DOWNLOADS_DIR, MANIFEST as MANIFEST_DEFAULT, RESULTS_DIR, PLAYWRIGHT_FALLBACK } from './lib/paths.mjs';
import { sha256File, detectTypeFromBuffer } from './lib/file_identity.mjs';
import { getArg, isDirectRun } from './lib/cli_helpers.mjs';
import { readManifest, writeManifestAtomic, transitionRecord, upsertRecord } from './lib/manifest.mjs';
import { mergeResults } from './merge_results.mjs';

const DOWNLOAD_TRIGGER_TIMEOUT = 15_000; // 点击后等待 download 事件触发的超时（正常1-2秒触发，15秒没触发=有问题）
const DOWNLOAD_COMPLETE_TIMEOUT = 300_000; // 下载传输完成的超时（大文件5分钟）

/**
 * 解析 CLI 参数为下载任务列表。
 * @returns {{ tasks: array, manifestPath: string, manifestDriven: boolean }}
 */
export function buildTasks(args) {
  const manifestPath = getArg(args, '--manifest') || MANIFEST_DEFAULT;
  const recordsFlag = getArg(args, '--records');

  let tasks = [];

  if (args.includes('--urls')) {
    const urls = args.filter((a, i) => i > args.indexOf('--urls') && !a.startsWith('--'));
    tasks = urls.map((url, i) => ({ url, name: `link_${i + 1}`, filename: null }));
    return { tasks, manifestPath, manifestDriven: false };
  }

  const mf = readManifest(manifestPath);
  const recs = Object.values(mf.records || {});
  const linkRecs = recs.filter(r => r.source_type === 'link' && r.source_url);
  let target;
  if (recordsFlag) {
    const ids = new Set(recordsFlag.split(',').map(s => s.trim()));
    target = linkRecs.filter(r => ids.has(r.record_id));
  } else {
    target = linkRecs.filter(r => r.status === 'verified');
  }
  tasks = target.map(r => ({
    url: r.source_url,
    name: r.candidate_name || r.record_id.slice(7, 19),
    filename: r.target_filename || null,
    record_id: r.record_id,
    target_dir: r.target_dir || null,
    target_filename: r.target_filename || null,
  }));
  return { tasks, manifestPath, manifestDriven: true };
}

/**
 * 查找下载按钮。QQ邮箱文件分享页有文字"下载"的可点击 div。
 */
async function findDownloadButton(page) {
  // QQ邮箱文件分享页：.operate-btn 是下载按钮的外层容器（点击它才触发下载）
  // 内层 .ui-btn-text 只是文字 span，点它不触发
  const selectors = [
    '.operate-btn',              // QQ邮箱专用 class
    'text="下载"',
    '[class*="download"]',
    'button:has-text("下载")',
  ];
  for (const sel of selectors) {
    try {
      const el = page.locator(sel).first();
      if (await el.isVisible({ timeout: 2000 })) return el; // 每个选择器最多等2秒（4个=最坏8秒）
    } catch { /* 继续试下一个 */ }
  }
  return null;
}

async function batchDownload(tasks) {
  if (tasks.length === 0) {
    console.log('没有待下载的链接。');
    return [];
  }

  // playwright 装在项目根 <PROJECT_ROOT>，skill 目录找不到，用 fallback 路径加载
  let chromium;
  try {
    ({ chromium } = await import('playwright'));
  } catch {
    ({ chromium } = await import('file:///' + PLAYWRIGHT_FALLBACK.replace(/\\/g, '/')));
  }
  console.log(`启动浏览器，批量下载 ${tasks.length} 个链接...\n`);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();

  const results = [];

  for (let i = 0; i < tasks.length; i++) {
    const task = tasks[i];
    const tag = `[${i + 1}/${tasks.length}] ${task.name}`;
    console.log(`${tag} 打开链接...`);

    try {
      await page.goto(task.url, { waitUntil: 'domcontentloaded', timeout: 30_000 });

      // SPA 页面 JS 动态渲染内容（失效提示/下载按钮），等 2 秒让页面渲染完。
      await page.waitForTimeout(2000);

      // 检测"分享已失效"（优先用 page.evaluate 拿完整 body 文本）
      const bodyText = await page.evaluate(() => document.body?.innerText || '').catch(() => '');
      if (bodyText.includes('失效') || bodyText.includes('已过期')) {
        console.log(`${tag} 🔴 链接已失效，跳过`);
        results.push({ ...task, status: 'expired' });
        continue;
      }

      const btn = await findDownloadButton(page);
      if (!btn) {
        console.log(`${tag} ⚠ 未找到下载按钮（可能需登录），跳过`);
        results.push({ ...task, status: 'no_button' });
        continue;
      }

      console.log(`${tag} 点击下载，等待触发...`);
      // QQ邮箱下载通过 popup 触发，download 事件在 page 上触发。
      // 用回调+轮询方式（比 waitForEvent 更可靠，避免事件注册时序问题）
      let downloadObj = null;
      const downloadHandler = (d) => { downloadObj = d; };
      page.on('download', downloadHandler);

      await btn.click();

      // 阶段1：等待 download 事件触发（快速失败：15秒没触发=按钮无效/需登录，不傻等）
      const triggerDeadline = Date.now() + DOWNLOAD_TRIGGER_TIMEOUT;
      while (!downloadObj && Date.now() < triggerDeadline) {
        await page.waitForTimeout(500);
      }
      page.off('download', downloadHandler);

      if (!downloadObj) {
        console.log(`${tag} 🔴 下载未触发（${DOWNLOAD_TRIGGER_TIMEOUT / 1000}s内无响应，按钮可能无效或需登录）`);
        results.push({ ...task, status: 'timeout' });
        continue;
      }

      // 阶段2：等待下载传输完成（大文件允许5分钟）
      const suggested = downloadObj.suggestedFilename();
      const savePath = path.join(DOWNLOADS_DIR, task.filename || suggested);
      try {
        await downloadObj.saveAs(savePath, { timeout: DOWNLOAD_COMPLETE_TIMEOUT });
      } catch (e) {
        console.log(`${tag} 🔴 下载传输失败/超时: ${e.message.slice(0, 100)}`);
        results.push({ ...task, status: 'download_failed', error: e.message });
        continue;
      }
      const sizeMB = (fs.statSync(savePath).size / 1048576).toFixed(1);
      console.log(`${tag} ✅ ${path.basename(savePath)} (${sizeMB}MB)`);
      // 2026-08-14：超大附件（美术岗作品集/简历 zip 常见 100MB+）走 QQ 超大附件 Playwright 下载，
      // 受 QQ 服务器限速，动辄几分钟~十几分钟。下载完成后醒目提示——下次这类文件优先人工下载
      // （浏览器/QQ 客户端更快且有断点续传），Agent 只负责归档。
      if (parseFloat(sizeMB) > 150) {
        console.log(`  ⚠️ 超大附件 ${sizeMB}MB（${path.basename(savePath)}）——本次自动下载完成；下次建议人工下载到 Downloads（浏览器/QQ 客户端更快，可断点续传），Agent 负责归档。`);
      }

      // manifest 驱动的记录：把文件移到 target_dir/target_filename 并写 result.json，
      // 随后由 main() 自动合并推进 archived（闭环：不写 result.json 状态会永远卡 verified）。
      // --urls 模式（无 record_id）不走 manifest 闭环，保持原行为（只留 Downloads）。
      let finalPath = savePath;
      if (task.record_id && task.target_dir && task.target_filename) {
        // magic bytes 校正扩展名（2026-08-17）：QQ 超大附件的 resolve target 扩展名是猜的
        // （PDF/RAR 都可能被存成 .zip），按文件头纠正，并同步回写 manifest 的 target_filename
        // （merge_results 有路径绑定校验，两边不一致会拒合并）
        const detected = detectTypeFromBuffer(fs.readFileSync(savePath));
        if (detected && detected !== 'unknown') {
          const extByType = { pdf: '.pdf', zip: '.zip', rar: '.rar', '7z': '.7z', image: null };
          const wantExt = extByType[detected];
          if (wantExt && !task.target_filename.toLowerCase().endsWith(wantExt)) {
            const base = task.target_filename.replace(/\.[^.]+$/, '');
            const renamed = base + wantExt;
            console.log(`${tag} ℹ 实际类型 ${detected}，扩展名校正: ${path.basename(task.target_filename)} → ${renamed}`);
            const mm = readManifest(MANIFEST_DEFAULT);
            if (mm.records[task.record_id]) {
              mm.records[task.record_id] = { ...mm.records[task.record_id], target_filename: renamed };
              writeManifestAtomic(MANIFEST_DEFAULT, mm);
            }
            task.target_filename = renamed;
            task.record_filename = renamed;
          }
        }
        const dest = path.join(task.target_dir, task.target_filename).replace(/\\/g, '/');
        try {
          fs.mkdirSync(path.dirname(dest), { recursive: true });
          if (fs.existsSync(dest)) {
            // 已存在不覆盖（与 download_attachment.mjs 的幂等语义一致）
            console.log(`${tag} ℹ 目标已存在，保留原文件: ${path.basename(dest)}`);
            fs.unlinkSync(savePath); // 删掉 Downloads 里的重复副本
          } else {
            fs.renameSync(savePath, dest);
          }
          finalPath = dest;

          // 写 result.json（格式与 download_attachment.mjs 一致，merge_results.mjs 消费）
          fs.mkdirSync(RESULTS_DIR, { recursive: true });
          const safeRecordId = task.record_id.replace(/[:*?"<>|]/g, '_');
          const resultPath = path.join(RESULTS_DIR, `${safeRecordId}.result.json`);
          fs.writeFileSync(resultPath, JSON.stringify({
            record_id: task.record_id,
            outcome: 'committed',
            sha256: sha256File(dest),
            target_path: dest,
            at: new Date().toISOString(),
          }, null, 2));
        } catch (moveErr) {
          console.log(`${tag} ⚠ 移动到归档目录失败，文件保留在 Downloads: ${moveErr.message.slice(0, 80)}`);
        }
      }
      results.push({ ...task, status: 'ok', file: finalPath });

    } catch (e) {
      console.log(`${tag} 🔴 失败: ${e.message.slice(0, 150)}`);
      results.push({ ...task, status: 'error', error: e.message });
    }
  }

  await browser.close();

  // 汇总
  const ok = results.filter(r => r.status === 'ok');
  const bad = results.filter(r => r.status !== 'ok');
  console.log(`\n========== ✅ 成功 ${ok.length} · 🔴 失败 ${bad.length} ==========`);
  for (const r of bad) {
    console.log(`  ${r.name}: ${r.status}${r.error ? ' — ' + r.error.slice(0, 80) : ''}`);
  }

  return results;
}

/**
 * 确认失效的链接记录 → blocked（LINK_EXPIRED）。
 * 失效是永久性的，留在 verified 会每次全量扫都白试一遍；
 * 推进 blocked 让闸门能发现"该人材料从未归档"，而不是静默缺失。
 */
function markExpiredRecords(manifestPath, expiredTasks) {
  if (!expiredTasks.length) return;
  let manifest = readManifest(manifestPath);
  for (const t of expiredTasks) {
    const rec = manifest.records[t.record_id];
    if (!rec || rec.status !== 'verified') continue;
    try {
      manifest = upsertRecord(manifest, transitionRecord(rec, 'blocked', {
        code: 'LINK_EXPIRED',
        message: `链接已失效（${t.url.slice(0, 80)}），材料无法下载，需联系候选人重发`,
      }));
    } catch { /* 状态不合法（如已 blocked）则跳过 */ }
  }
  writeManifestAtomic(manifestPath, manifest);
  console.log(`已标记 ${expiredTasks.length} 条失效链接为 blocked（LINK_EXPIRED）`);
}

export async function main() {
  const args = process.argv.slice(2);
  const { tasks, manifestPath, manifestDriven } = buildTasks(args);
  if (tasks.length === 0) {
    console.log('没有待下载的链接。用法:');
    console.log('  node batch_download_links.mjs --manifest <path>');
    console.log('  node batch_download_links.mjs --records "id1,id2" --manifest <path>');
    console.log('  node batch_download_links.mjs --urls "https://..." "https://..."');
    return { ok: 0, failed: 0 };
  }

  const results = await batchDownload(tasks);

  if (manifestDriven) {
    // 失效链接 → blocked；成功记录 → 自动合并推进 archived
    markExpiredRecords(manifestPath, results.filter(r => r.status === 'expired' && r.record_id));
    const okWithRecord = results.filter(r => r.status === 'ok' && r.record_id);
    if (okWithRecord.length) {
      const { merged, skipped, errors } = mergeResults(manifestPath);
      console.log(`自动合并: ${merged} 条推进到 archived，${skipped} 条跳过${errors.length ? `，🔴 ${errors.length} 个合并错误` : ''}`);
      for (const e of errors.slice(0, 5)) console.error(`  - ${e.file}: ${e.error}`);
    }
  }

  const ok = results.filter(r => r.status === 'ok').length;
  return { ok, failed: results.length - ok };
}

const _isDirectRun = isDirectRun(import.meta.url);
if (_isDirectRun) {
  main().then(r => {
    if (r && r.failed) process.exitCode = 3; // 有失败 → 非零退出，但已完成的保留
  }).catch(e => {
    console.error('批量下载异常:', e.message);
    process.exit(1);
  });
}
