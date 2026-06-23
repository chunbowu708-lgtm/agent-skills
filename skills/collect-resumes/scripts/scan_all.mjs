// 全量扫描邮箱，穷尽式（分页到 has_more=false）
// 用法: node scan_all.mjs [--date 2026-06-15]   (不传 --date 则列出全部)
// 输出候选邮件清单到 stdout，全量数据存 notes/_scan_all.json
//
// 配置从同目录 .env 或环境变量读取（见 .env.example）：
//   LARK_CLI_PATH  lark-cli 可执行文件路径
//   SCAN_OUTPUT    全量数据落盘路径（默认 ./notes/_scan_all.json）
import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
loadEnv(path.join(__dirname, '..', '.env'));

const CLI = process.env.LARK_CLI_PATH || 'lark-cli';
const dateFilter = process.argv.includes('--date') ? process.argv[process.argv.indexOf('--date') + 1] : '';
const OUT = process.env.SCAN_OUTPUT || path.join(__dirname, '..', 'notes', '_scan_all.json');

let allMessages = [];
let pageToken = '';
let page = 0;
while (true) {
  page++;
  const cmd = pageToken
    ? `"${CLI}" mail +triage --as user --max 200 --format json --page-token "${pageToken}"`
    : `"${CLI}" mail +triage --as user --max 200 --format json`;
  const raw = execSync(cmd, { encoding: 'utf8', maxBuffer: 50 * 1024 * 1024 });
  const m = raw.match(/\{[\s\S]*\}/);
  if (!m) { console.error(`page ${page} 无 JSON`); break; }
  const j = JSON.parse(m[0]);
  const items = j.messages || j.data?.messages || [];
  allMessages.push(...items);
  process.stderr.write(`page ${page}: +${items.length} (total ${allMessages.length}) has_more=${j.has_more}\n`);
  if (!j.has_more) break;
  pageToken = j.page_token;
  if (page > 20) { console.error('安全停止: page>20'); break; }
}

// 排除通知类邮件（按你公司的实际通知 subject 调整正则）
const notif = /奋斗食代|员工关爱|视频面试邀约|资料收集|欢迎加入|系统通知|日程提醒/;
let candidates = allMessages.filter(m => !notif.test(m.subject || '') && !notif.test(m.from || ''));
if (dateFilter) candidates = candidates.filter(m => (m.date || '').startsWith(dateFilter));

console.log(`\n=== 共 ${allMessages.length} 封，候选 ${candidates.length} 封${dateFilter ? '（' + dateFilter + '）' : ''} ===\n`);
candidates.forEach(m => {
  console.log(`${m.date} | ${(m.from || '').slice(0, 28).padEnd(28)} | ${m.message_id} | ${m.subject}`);
});

fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(OUT, JSON.stringify(allMessages, null, 2));
process.stderr.write(`\n全量已存: ${OUT}\n`);

// ---------- 极简 .env 加载器（不依赖 dotenv） ----------
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
