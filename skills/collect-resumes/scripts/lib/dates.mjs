// scripts/lib/dates.mjs
// 「邮件何时收到」判定的单一真相源。
//
// 事实链：邮件真实收到时间只存在于扫描快照的 m.date（"2026-08-14 18:50"），
// verify_mails 建记录时写入 record.received_at。created_at（首次入库）/ updated_at
// （状态推进）是处理时间戳，会随后续运行漂移，禁止用于"何时收到"判定。
// collect 的下载 scope、resolve 的目录日期段、verify 的 --date 过滤都从这里派生，
// 不允许各自读时间戳字段。

/**
 * 解析邮件/记录里的日期字符串（快照 "2026-08-14 18:50" 或 ISO 均可，按本地时区）。
 * @returns {Date|null} 无法解析返回 null
 */
export function parseMailDate(s) {
  if (!s) return null;
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * 解析 CLI --date 参数："8.14"（补当年）或 "2026-08-14"。
 * @returns {Date|null}
 */
export function parseDateArg(s) {
  if (!s) return null;
  const m = /^(\d{1,2})\.(\d{1,2})$/.exec(String(s).trim());
  if (m) {
    const now = new Date();
    return new Date(now.getFullYear(), parseInt(m[1], 10) - 1, parseInt(m[2], 10));
  }
  return parseMailDate(s);
}

/** 两个 Date 是否同一本地日。任一为 null 返回 false。 */
export function sameLocalDay(a, b) {
  if (!a || !b) return false;
  return a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** Date → 归档日期段名 {M.DD}（如 8.14）。 */
export function mdd(d) {
  return `${d.getMonth() + 1}.${d.getDate()}`;
}

/**
 * 记录的"收到日"：received_at（邮件时间）→ created_at（首次入库，当日处理当日
 * 邮件时≈收到日）→ 都没有按今天。目录日期段与下载 scope 共用此 fallback 链。
 */
export function recordDate(rec) {
  return parseMailDate(rec?.received_at) || parseMailDate(rec?.created_at) || new Date();
}
