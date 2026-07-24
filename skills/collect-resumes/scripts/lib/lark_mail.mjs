// scripts/lib/lark_mail.mjs
// lark-cli 响应严格解析：过滤 tip 行、解析 JSON、识别业务错误(ok=false)、结构校验。
//
// 核心不变量：无法解析的响应必须 fail-closed（抛错），绝不能被解释成"无附件/无数据"。

/**
 * 过滤 lark-cli 的 tip 行，避免污染 JSON 解析。
 */
export function cleanTipLines(raw) {
  return raw.split('\n').filter(l => l.trim() && !/^tip:/.test(l.trim())).join('\n');
}

/**
 * 严格解析 lark-cli stdout 为 JSON。
 * - 先去 tip 行
 * - 贪婪匹配首个 { ... } JSON 块
 * - 解析失败 → 抛 INVALID_JSON（fail-closed）
 * - JSON 含 ok:false → 抛 API_ERROR（业务错误也算失败）
 * - 正常 → 返回解析后的对象
 *
 * @param {string} raw - lark-cli 原始 stdout
 * @returns {object} 解析后的 JSON 对象
 * @throws {Error} INVALID_JSON | API_ERROR
 */
export function parseCliJson(raw) {
  const cleaned = cleanTipLines(raw);
  const m = cleaned.match(/\{[\s\S]*\}/);
  if (!m) {
    throw new Error(`INVALID_JSON: 响应中无 JSON 块，原始片段: ${cleaned.slice(0, 200)}`);
  }
  let j;
  try {
    j = JSON.parse(m[0]);
  } catch (e) {
    throw new Error(`INVALID_JSON: JSON 解析失败 (${e.message})，原始片段: ${m[0].slice(0, 200)}`);
  }
  // lark-cli 把 API 业务错误也通过 stdout 返回 JSON，但 exit=0
  // 必须检查 ok 字段，否则 user message not found 会被当成功
  if (j && j.ok === false) {
    const msg = j.error?.message || j.error?.type || 'unknown';
    throw new Error(`API_ERROR: ${msg}`);
  }
  return j;
}

/**
 * 执行 lark-cli 命令并返回严格解析后的 JSON。
 * runner 可注入（测试用），默认用 execSync。
 *
 * @param {string} args - lark-cli 参数（不含 CLI 路径）
 * @param {object} opts - { runner?: (cmd) => string, cliPath?: string }
 * @returns {object} 解析后的 JSON
 */
// 默认 runner：用 execSync 执行 lark-cli。
// 模块顶层 import 避免在函数内 require（ESM 不支持 require）。
import { execSync } from 'node:child_process';
import { LARK_CLI, MAX_BUFFER } from './paths.mjs';

export function runLarkCli(args, opts = {}) {
  const cliPath = opts.cliPath || LARK_CLI;
  const runner = opts.runner || ((cmd) => {
    return execSync(cmd, { encoding: 'utf8', maxBuffer: MAX_BUFFER });
  });
  const raw = runner(`"${cliPath}" ${args}`);
  return parseCliJson(raw);
}
