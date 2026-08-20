// scripts/lib/cli_helpers.mjs
// 入口脚本共用工具：参数解析、直跑判定、lark-cli 执行器。
// 所有入口脚本从此 import，不各自复制（复制 = 变异 = bug）。

import { execSync, exec } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { runWithRetry } from './retry.mjs';
import { MAX_BUFFER } from './paths.mjs';

/** 取命令行参数值（--name value）。不存在返回 undefined。 */
export function getArg(args, name) {
  const i = args.indexOf(name);
  return i !== -1 ? args[i + 1] : undefined;
}

/**
 * 判断当前模块是否被直接运行（node xxx.mjs）而非被 import（测试）。
 * ⚠️ 必须传调用方自己的 import.meta.url——本函数在共享模块里，无参时
 * import.meta.url 会解析成 cli_helpers.mjs 自身，导致入口脚本永不执行。
 * @param {string} moduleUrl - 调用方的 import.meta.url
 */
export function isDirectRun(moduleUrl) {
  if (!moduleUrl || !process.argv[1]) return false;
  return fileURLToPath(moduleUrl).replace(/\\/g, '/') === path.resolve(process.argv[1]).replace(/\\/g, '/');
}

/**
 * 同步执行 lark-cli 命令。失败时把 stderr 拼进 message，让 isRateLimitError 能识别限流 JSON。
 * @returns {string} stdout
 */
export function runLarkSync(cmd) {
  try {
    return execSync(cmd, { encoding: 'utf8', maxBuffer: MAX_BUFFER });
  } catch (e) {
    const enriched = new Error((e.message || '') + ' ' + (e.stderr || ''));
    enriched.stderr = e.stderr || '';
    enriched.stdout = e.stdout || '';
    throw enriched;
  }
}

/**
 * 构造异步 lark-cli 执行器（限流时指数退避重试，供并发场景使用）。
 * @param {object} opts - 透传 runWithRetry（onRetry/maxRetries/baseDelayMs）
 * @returns {function(cmd: string): Promise<string>} stdout
 */
export function makeLarkRunner(opts = {}) {
  const execAsync = promisify(exec);
  return (cmd) => runWithRetry(async () => {
    try {
      const { stdout } = await execAsync(cmd, { encoding: 'utf8', maxBuffer: MAX_BUFFER });
      return stdout;
    } catch (e) {
      const enriched = new Error((e.message || '') + ' ' + (e.stderr || ''));
      enriched.stderr = e.stderr || '';
      throw enriched;
    }
  }, opts);
}
