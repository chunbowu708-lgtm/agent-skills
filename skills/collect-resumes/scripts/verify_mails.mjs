// 逐封核查附件 + body 链接（防止漏作品）
// 用法: node verify_mails.mjs [--date 2026-06-15]
// 自动从 scan_all.json 取候选 MID，逐封查 attachments + body_html 链接
//
// 配置从同目录 .env 或环境变量读取（见 .env.example）：
//   LARK_CLI_PATH  lark-cli 可执行文件路径
//   SCAN_OUTPUT    scan_all.mjs 产出的全量数据路径
import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
loadEnv(path.join(__dirname, '..', '.env'));

const CLI = process.env.LARK_CLI_PATH || 'lark-cli';
const dateFilter = process.argv.includes('--date') ? process.argv[process.argv.indexOf('--date') + 1] : '';
const SCAN = process.env.SCAN_OUTPUT || path.join(__dirname, '..', 'notes', '_scan_all.json');

if (!fs.existsSync(SCAN)) {
  console.error('先跑 scan_all.mjs 生成 _scan_all.json');
  process.exit(1);
}

const allMessages = JSON.parse(fs.readFileSync(SCAN, 'utf8'));
const notif = /奋斗食代|员工关爱|视频面试邀约|资料收集|欢迎加入|系统通知|日程提醒/;
let candidates = allMessages.filter(m => !notif.test(m.subject || '') && !notif.test(m.from || ''));
if (dateFilter) candidates = candidates.filter(m => (m.date || '').startsWith(dateFilter));

const LINK_KW = ['wx.mail.qq.com/ftn', 'mail.163.com/large', '126.com', 'pan.baidu.com', 'aliyundrive', 'portfolio', 'artstation', '作品'];

function runCli(args) {
  try {
    const raw = execSync(`"${CLI}" ${args}`, { encoding: 'utf8', maxBuffer: 10 * 1024 * 1024 });
    return raw.split('\n').filter(l => l.trim() && !l.startsWith('tip:')).join('\n');
  } catch { return ''; }
}

for (const m of candidates) {
  console.log(`\n=== ${m.date} | ${(m.subject || '').slice(0, 40)} ===`);
  // 查附件
  const attRaw = runCli(`mail +message --as user --message-id "${m.message_id}" -q ".data.attachments"`);
  const attMatch = attRaw.match(/\[[\s\S]*\]/);
  let atts = [];
  try { atts = attMatch ? JSON.parse(attMatch[0]) : []; } catch {}
  console.log(`  附件(${atts.length}): ${atts.map(a => a.filename).join(', ') || '无'}`);

  // 查 body 链接（每封都查，不管附件数）
  const body = runCli(`mail +message --as user --message-id "${m.message_id}" -q ".data.body_html"`)
    .replace(/\\u003c/g, '<').replace(/\\u003e/g, '>').replace(/\\"/g, '"').replace(/\\\//g, '/');
  const found = LINK_KW.filter(k => body.toLowerCase().includes(k.toLowerCase()));
  if (found.length) console.log(`  ⚠️ body含链接关键词: ${found.join(', ')}`);
  const urls = [...new Set((body.match(/https?:\/\/[^""\s<>]+/g) || [])
    .filter(u => /ftn|163\.com\/large|pan\.baidu|aliyun|artstation|portfolio/i.test(u)))];
  if (urls.length) console.log(`  作品链接: ${urls.slice(0, 2).join(' ')}`);
}

function loadEnv(file) {
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const eq = t.indexOf('=');
    if (eq < 0) continue;
    const k = t.slice(0, eq).trim();
    let v = t.slice(eq + 1).trim();
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1);
    if (!(k in process.env)) process.env[k] = v;
  }
}
