// 飞书邮件附件原子下载（Windows/node 版）
// 用法: node download_attachment.mjs <MESSAGE_ID> <输出路径> [附件序号，默认0]
// 取 ATT_ID → 取 download_url → https.get 下载，一个进程内完成（auth code 有时效，不能拆两步）
//
// 配置从同目录 .env 或环境变量读取（见 .env.example）：
//   LARK_CLI_PATH  lark-cli 可执行文件路径
import https from 'https';
import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
loadEnv(path.join(__dirname, '..', '.env'));

const CLI = process.env.LARK_CLI_PATH || 'lark-cli';
const [,, MID, OUT, ATT_IDX = '0'] = process.argv;
if (!MID || !OUT) {
  console.error('用法: node download_attachment.mjs <MESSAGE_ID> <输出路径> [附件序号]');
  process.exit(1);
}

function runCli(args) {
  // 用 execSync 调 lark-cli，过滤 tip 行
  const raw = execSync(`"${CLI}" ${args}`, { encoding: 'utf8', maxBuffer: 10 * 1024 * 1024 });
  return raw.split('\n').filter(l => l.trim() && !l.startsWith('tip:')).join('\n').trim();
}

function download(url, redirectsLeft = 5) {
  return new Promise((resolve, reject) => {
    https.get(url, r => {
      if (r.statusCode >= 300 && r.statusCode < 400 && r.headers.location && redirectsLeft > 0) {
        r.resume();
        return download(r.headers.location, redirectsLeft - 1).then(resolve).catch(reject);
      }
      if (r.statusCode !== 200) { r.resume(); return reject(new Error('HTTP ' + r.statusCode)); }
      const chunks = [];
      r.on('data', c => chunks.push(c));
      r.on('end', () => resolve(Buffer.concat(chunks)));
    }).on('error', reject);
  });
}

async function main() {
  // 1. 取 attachment id
  const attId = runCli(`mail +message --as user --message-id "${MID}" -q ".data.attachments[${ATT_IDX}].id"`);
  if (!attId) { console.error('ERR: 无附件'); process.exit(2); }

  // 2. 取 download url
  const params = JSON.stringify({ user_mailbox_id: 'me', message_id: MID, attachment_ids: attId });
  // 注意：--params 的引号在 execSync 里要正确转义，用 \" 包裹
  const url = runCli(`mail user_mailbox.message.attachments download_url --as user --params "${params.replace(/"/g, '\\"')}" -q ".data.download_urls[0].download_url"`);
  if (!url) { console.error('ERR: 无下载URL'); process.exit(3); }

  // 3. 下载
  const dir = OUT.replace(/[/\\][^/\\]+$/, '');
  if (dir && !fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  const buf = await download(url);
  fs.writeFileSync(OUT, buf);
  console.log(`OK: ${OUT} (${buf.length} bytes)`);
}

main().catch(e => { console.error('FATAL:', e.message); process.exit(1); });

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
