// batch_download_links.mjs — 批量下载 QQ/网易大附件链接（Playwright 驱动）
//
// 解决"逐个手动浏览器下载"问题：一个脚本批量处理多个链接。
//
// 用法:
//   # 从 manifest 读所有 verified 的 link 记录批量下载
//   node batch_download_links.mjs --manifest <PROJECT_ROOT>/notes/collection_manifest.json
//
//   # 指定 record ID（逗号分隔）
//   node batch_download_links.mjs --records "sha256:xxx,sha256:yyy" --manifest <path>
//
//   # 直接传 URL（绕过 manifest）
//   node batch_download_links.mjs --urls "https://wx.mail.qq.com/..." "https://..."
//
// 输出到 F:/Users/wuchunbo/Downloads。

import fs from 'node:fs';
import path from 'node:path';
import { DOWNLOADS_DIR, MANIFEST as MANIFEST_DEFAULT, RESULTS_DIR } from './lib/paths.mjs';
import { sha256File } from './lib/file_identity.mjs';
const DOWNLOAD_TRIGGER_TIMEOUT = 15_000; // 点击后等待 download 事件触发的超时（正常1-2秒触发，15秒没触发=有问题）
const DOWNLOAD_COMPLETE_TIMEOUT = 300_000; // 下载传输完成的超时（大文件5分钟）

function parseArgs() {
  const args = process.argv.slice(2);
  const get = (name) => { const i = args.indexOf(name); return i !== -1 ? args[i + 1] : undefined; };
  const manifestPath = get('--manifest') || MANIFEST_DEFAULT;
  const recordsFlag = get('--records');

  let tasks = [];

  if (args.includes('--urls')) {
    const urls = args.filter((a, i) => i > args.indexOf('--urls') && !a.startsWith('--'));
    tasks = urls.map((url, i) => ({ url, name: `link_${i + 1}`, filename: null }));
  } else {
    const mf = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
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
  }
  return tasks;
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

  // playwright 装在项目根 <PROJECT_ROOT>，skill 目录找不到，用绝对路径加载
  let chromium;
  try {
    ({ chromium } = await import('playwright'));
  } catch {
    const pwPath = process.env.PLAYWRIGHT_PATH || 'playwright';
    ({ chromium } = await import('file:///' + pwPath.replace(/\\/g, '/')));
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

      // manifest 驱动的记录：把文件移到 target_dir/target_filename 并写 result.json，
      // 让 merge_results.mjs 能推进状态到 archived（修复闭环：旧版只下到 Downloads 不写 result.json，
      // 导致状态永远卡在 verified，下次全量扫会重复下载）。
      // --urls 模式（无 record_id）不走 manifest 闭环，保持原行为（只留 Downloads）。
      let finalPath = savePath;
      if (task.record_id && task.target_dir && task.target_filename) {
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
          console.log(`${tag} 📎 已写 result.json，待 merge_results 推进 archived`);
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

// ---- CLI ----
const tasks = parseArgs();
if (tasks.length === 0) {
  console.log('没有待下载的链接。用法:');
  console.log('  node batch_download_links.mjs --manifest <path>');
  console.log('  node batch_download_links.mjs --records "id1,id2" --manifest <path>');
  console.log('  node batch_download_links.mjs --urls "https://..." "https://..."');
  process.exit(0);
}

batchDownload(tasks).catch(e => {
  console.error('批量下载异常:', e.message);
  process.exit(1);
});
