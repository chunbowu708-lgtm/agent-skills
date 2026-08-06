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
