// scripts/lib/retry.mjs
// 限流退避重试工具。
//
// 飞书邮箱 API 限流（mail 域）很窄，批量/连续调用极易触发：
//   - 99991400 "failed to fetch email: request trigger frequency limit"（mail +message 详情）
//   - 1234029  "too many requests, try again later"（attachment download_url）
// 限流时连发只会雪崩：后续请求全部失败，且 drive-stream 可能返回 HTTP200+损坏字节流。
// 解法：识别限流错误 → 指数退避等待 → 重试，最多 N 次；批量调用必须逐个 + 间隔。
//
// 设计：runWithRetry 包裹任意 fn，fn 抛出的错误若匹配限流特征则退避重试，否则直接抛。
//       这样调用方无需关心限流，只管"调到成功或彻底失败"。
// （lark-cli 命令执行器见 lib/cli_helpers.mjs 的 runLarkSync/makeLarkRunner，不在本文件）

const RATE_LIMIT_CODES = ['99991400', '1234029', '11200'];
const RATE_LIMIT_MSGS = [
  'frequency limit', 'too many requests', 'rate limit',
  '触发频率', '请求过于频繁', '稍后重试', 'try again later',
];

/**
 * 判断一个错误是否是限流错误（可重试）。
 * @param {Error|{ok:false,error?:object}} errOrResp - 抛出的 Error，或 lark-cli 的 {ok:false} 响应对象
 * @returns {boolean}
 */
export function isRateLimitError(errOrResp) {
  if (!errOrResp) return false;
  // 形态1: Error 对象（execSync 抛出，message 含限流信息）
  const msg = String(errOrResp.message || errOrResp || '');
  // 形态2: lark-cli 响应对象 {ok:false, error:{code,message}}
  const code = String(errOrResp?.error?.code ?? errOrResp?.code ?? '');
  if (RATE_LIMIT_CODES.includes(code)) return true;
  const lower = msg.toLowerCase();
  return RATE_LIMIT_MSGS.some(m => lower.includes(m.toLowerCase()));
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/**
 * 带限流退避重试地执行 fn。
 *
 * @param {function} fn - 要执行的（同步或异步）函数，返回值或抛错
 * @param {object} opts
 * @param {number} opts.maxRetries - 最大重试次数（不含首次），默认 3
 * @param {number} opts.baseDelayMs - 首次退避基准毫秒，默认 5000（飞书限流窗口较宽）
 * @param {function} opts.onRetry - 重试前回调 (err, attempt, delayMs) => void，用于日志
 * @returns {Promise<any>} fn 的返回值
 * @throws {Error} 非限流错误立即抛；限流错误重试耗尽后抛最后一个
 */
export async function runWithRetry(fn, opts = {}) {
  const maxRetries = opts.maxRetries ?? 3;
  const baseDelayMs = opts.baseDelayMs ?? 5000;
  const onRetry = opts.onRetry || (() => {});

  let lastErr;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const result = await fn();
      return result;
    } catch (e) {
      lastErr = e;
      if (!isRateLimitError(e) || attempt === maxRetries) {
        throw e; // 非限流错误，或重试耗尽，直接抛
      }
      // 指数退避：5s, 10s, 20s（带一点抖动避免同步雪崩）
      const delayMs = baseDelayMs * Math.pow(2, attempt) + Math.floor(Math.random() * 500);
      onRetry(e, attempt + 1, delayMs);
      await sleep(delayMs);
    }
  }
  throw lastErr;
}
