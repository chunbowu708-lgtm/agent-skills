// dates.mjs 回归测试：「邮件何时收到」判定的单一真相源。
// 防回归：received_at 缺失时的 fallback 链、快照日期格式解析、M.DD 派生。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  parseMailDate, parseDateArg, sameLocalDay, mdd, recordDate,
} from '../../scripts/lib/dates.mjs';

test('parseMailDate: 快照 "2026-08-14 18:50" 与 ISO 都按本地日解析', () => {
  const a = parseMailDate('2026-08-14 18:50');
  const b = parseMailDate('2026-08-14T02:00:00Z');
  assert.ok(a, '快照格式应可解析');
  assert.ok(sameLocalDay(a, b), '同一本地日（假定本地时区 UTC+8，02:00Z=10:00 本地）');
  assert.equal(parseMailDate(''), null);
  assert.equal(parseMailDate('garbage'), null);
  assert.equal(parseMailDate(null), null);
});

test('parseDateArg: "8.14" 补当年，"2026-08-14" 直接解析', () => {
  const a = parseDateArg('8.14');
  assert.equal(a.getMonth(), 7);
  assert.equal(a.getDate(), 14);
  assert.equal(parseDateArg('2026-08-14').getDate(), 14);
  assert.equal(parseDateArg('abc'), null);
});

test('recordDate: received_at 优先，缺失 fallback created_at，再缺 fallback 今天', () => {
  // received_at 存在 → 用邮件时间（created_at 是入库时间，不得覆盖）
  assert.equal(mdd(recordDate({ received_at: '2026-07-14 10:00', created_at: '2026-08-14T02:00:00Z' })), '7.14');
  // received_at 缺失 → created_at（当日处理当日邮件时≈收到日）
  assert.equal(mdd(recordDate({ created_at: '2026-08-13T02:00:00Z' })), '8.13');
  // 都没有 → 今天（不返回 null，保证目录日期段总能生成）
  assert.equal(mdd(recordDate({})), mdd(new Date()));
});

test('sameLocalDay: 跨日/同日判定', () => {
  assert.ok(sameLocalDay(new Date(2026, 7, 14, 23, 59), new Date(2026, 7, 14, 0, 1)));
  assert.ok(!sameLocalDay(new Date(2026, 7, 14, 23, 59), new Date(2026, 7, 15, 0, 1)));
  assert.ok(!sameLocalDay(null, new Date()));
});
