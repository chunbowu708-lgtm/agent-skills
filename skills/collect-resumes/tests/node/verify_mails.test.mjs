import test from 'node:test';
import assert from 'node:assert/strict';
import { runVerify, runVerifyParallel, filterPending, backfillReceivedAt, filterByDate } from '../../scripts/verify_mails.mjs';
import { readManifest } from '../../scripts/lib/manifest.mjs';

function makeDetailRunner(responses) {
  // responses: { [message_id]: { attachments?, body_html?, error? } }
  return (cmd) => {
    const midMatch = cmd.match(/--message-id "([^"]+)"/);
    const mid = midMatch ? midMatch[1] : '';
    const r = responses[mid];
    if (!r || r.error) {
      return JSON.stringify({ ok: false, error: { message: r?.error || 'not found' } });
    }
    return JSON.stringify({ ok: true, data: { message_id: mid, attachments: r.attachments || [], body_html: r.body_html || '' } });
  };
}

test('attachments become manifest records with stable IDs', async () => {
  const candidates = [{ message_id: 'm1', subject: '张三简历', date: '2026-07-10' }];
  const runner = makeDetailRunner({
    m1: { attachments: [{ id: 'att1', filename: '张三.pdf' }] },
  });
  const { manifest, failed } = await runVerify(candidates, runner, null);
  const ids = Object.keys(manifest.records);
  assert.equal(ids.length, 1);
  assert.equal(manifest.records[ids[0]].source_type, 'mail_attachment');
  assert.equal(manifest.records[ids[0]].status, 'needs_resolution');
  assert.equal(manifest.records[ids[0]].original_filename, '张三.pdf');
  assert.equal(failed.length, 0);
  // 核查成功写增量标记（processed）
  assert.ok(manifest.processed?.m1, 'success path should mark processed');
});

test('body links become records (126.com + portfolio no longer dropped)', async () => {
  const candidates = [{ message_id: 'm2', subject: '李四作品', date: '2026-07-10' }];
  const runner = makeDetailRunner({
    m2: {
      body_html: '<a href="https://mail.126.com/large?id=1&amp;k=2">我的作品</a><a href="https://artstation.com/u">作品集</a>',
    },
  });
  const { manifest } = await runVerify(candidates, runner, null);
  const recs = Object.values(manifest.records);
  assert.equal(recs.length, 2);
  const kinds = recs.map(r => r.link_kind).sort();
  assert.deepEqual(kinds, ['large_attachment', 'portfolio']);
});

test('detail fetch failure blocks the email, does not record zero attachments', async () => {
  const candidates = [{ message_id: 'm3', subject: '王五', date: '2026-07-10' }];
  const runner = makeDetailRunner({ m3: { error: 'message not found' } });
  const { manifest, failed } = await runVerify(candidates, runner, null);
  const recs = Object.values(manifest.records);
  assert.equal(recs.length, 1);
  assert.equal(recs[0].status, 'blocked');
  assert.match(recs[0].errors[0].code, /DETAIL_FETCH_FAILED/);
  assert.equal(failed.length, 1);
  // 失败不写 processed 标记（下次增量运行会重试）
  assert.ok(!manifest.processed?.m3, 'failed fetch must NOT mark processed');
});

test('body hints material but no attachment and no link → blocked', async () => {
  const candidates = [{ message_id: 'm4', subject: '赵六', date: '2026-07-10' }];
  const runner = makeDetailRunner({
    m4: { body_html: '<p>附件是我的简历，请查收</p>' }, // 提示关键词但无附件无链接
  });
  const { manifest, failed } = await runVerify(candidates, runner, null);
  const blocked = Object.values(manifest.records).filter(r => r.status === 'blocked');
  assert.equal(blocked.length, 1);
  assert.match(blocked[0].errors[0].code, /MATERIAL_HINT_NO_SOURCE/);
});

test('incremental: does not drop existing records from previous manifest', async () => {
  const prevManifest = {
    schema_version: 1,
    batches: {},
    records: { existing_rec: { record_id: 'existing_rec', status: 'validated', errors: [] } },
  };
  const candidates = [{ message_id: 'm5', subject: '新候选人', date: '2026-07-10' }];
  const runner = makeDetailRunner({ m5: { attachments: [{ id: 'a1', filename: '新.pdf' }] } });
  const { manifest } = await runVerify(candidates, runner, prevManifest);
  assert.ok(manifest.records.existing_rec, '旧记录不丢失');
  assert.ok(Object.keys(manifest.records).length >= 2);
});

test('parallel verify produces same records as serial (D1 consistency)', async () => {
  // 2026-07-29 缺陷④：并行化后结果必须与串行一致（record 集合相同）
  const candidates = [
    { message_id: 'p1', subject: '甲', date: '2026-07-29' },
    { message_id: 'p2', subject: '乙', date: '2026-07-29' },
    { message_id: 'p3', subject: '丙失败', date: '2026-07-29' },
  ];
  const responses = {
    p1: { attachments: [{ id: 'a1', filename: '甲.pdf' }] },
    p2: { body_html: '<a href="https://mail.126.com/large?id=1">作品</a>' },
    p3: { error: 'message not found' },
  };
  const syncRunner = makeDetailRunner(responses);
  // 异步 runner（包一层 Promise）
  const asyncRunner = async (cmd) => syncRunner(cmd);

  const serialResult = await runVerify(candidates, syncRunner, null);
  const parallelResult = await runVerifyParallel(candidates, asyncRunner, null, 2);

  // record 集合应完全一致（record_id 相同）
  const serialIds = Object.keys(serialResult.manifest.records).sort();
  const parallelIds = Object.keys(parallelResult.manifest.records).sort();
  assert.deepEqual(serialIds, parallelIds, '并行与串行应产出相同 record 集合');
  assert.equal(parallelResult.failed.length, 1, '失败邮件数应一致');
});

// ---- 增量过滤 filterPending ----

test('filterPending: 疑似通知邮件不因主题关键词被跳过（资料收集回信含真简历）', () => {
  const manifest = { records: {}, processed: {} };
  const candidates = [
    { message_id: 'n1', subject: '薪酬学历资料收集-林盛烁', from: '249941679@qq.com' },
    { message_id: 'n2', subject: '【资料收集】深圳市迷你玩科技有限公司', from: '13422047985@163.com' },
  ];
  const { pending, skipped } = filterPending(candidates, manifest);
  assert.equal(pending.length, 2, '通知样主题也必须核查（由详情事实决定相关性）');
  assert.equal(skipped.length, 0);
});

test('filterPending: processed 标记的邮件跳过，新邮件待核查', () => {
  const manifest = { records: {}, processed: { done1: '2026-08-14T00:00:00Z' } };
  const { pending, skipped } = filterPending(
    [{ message_id: 'done1' }, { message_id: 'new1' }], manifest);
  assert.deepEqual(pending.map(m => m.message_id), ['new1']);
  assert.deepEqual(skipped.map(m => m.message_id), ['done1']);
});

test('filterPending: 已有稳定 records 的邮件跳过（旧 manifest 无 processed 兼容）', () => {
  const manifest = { records: { r1: { record_id: 'r1', message_id: 'old1', status: 'archived' } }, processed: {} };
  const { pending, skipped } = filterPending(
    [{ message_id: 'old1' }, { message_id: 'fresh' }], manifest);
  assert.deepEqual(pending.map(m => m.message_id), ['fresh']);
  assert.deepEqual(skipped.map(m => m.message_id), ['old1']);
});

test('filterPending: 详情拉取曾失败（mail_detail blocked）的邮件保留重试', () => {
  const manifest = {
    records: {
      r1: { record_id: 'r1', message_id: 'bad1', source_type: 'mail_detail', status: 'blocked', errors: [{ code: 'DETAIL_FETCH_FAILED' }] },
    },
    processed: {},
  };
  const { pending } = filterPending([{ message_id: 'bad1' }], manifest);
  assert.equal(pending.length, 1, 'blocked 详情记录必须重试（自愈）');
});

test('runVerify 成功路径自愈历史 detail blocked 记录', async () => {
  const prev = await runVerify(
    [{ message_id: 'm9', subject: '甲', date: '2026-08-14' }],
    makeDetailRunner({ m9: { error: 'rate limited' } }), null);
  // 第二轮：拉取成功 → 历史 blocked 应被自愈为 excluded
  const { manifest } = await runVerify(
    [{ message_id: 'm9', subject: '甲', date: '2026-08-14' }],
    makeDetailRunner({ m9: { attachments: [{ id: 'a1', filename: '甲.pdf' }] } }),
    prev.manifest);
  const detailRecs = Object.values(manifest.records).filter(r => r.source_type === 'mail_detail');
  assert.equal(detailRecs.length, 1);
  assert.equal(detailRecs[0].status, 'excluded');
  assert.match(detailRecs[0].exclude_reason.code, /DETAIL_FETCH_RECOVERED/);
});

// ---- received_at 存量回填（backfillReceivedAt）----

test('backfillReceivedAt: 存量记录按快照邮件时间补齐，幂等且不碰已有值', () => {
  const manifest = {
    records: {
      r1: { record_id: 'r1', message_id: 'm1', received_at: null, created_at: '2026-08-14T02:00:00Z' },
      r2: { record_id: 'r2', message_id: 'm2', received_at: '2026-07-01 10:00' },
      r3: { record_id: 'r3', message_id: 'gone', received_at: null },  // 快照里没有的邮件
    },
  };
  const snapshot = [
    { message_id: 'm1', date: '2026-07-14 09:00' },
    { message_id: 'm2', date: '2026-08-14 09:00' },
  ];
  const { manifest: out, count } = backfillReceivedAt(manifest, snapshot);
  assert.equal(count, 1, '只有 r1 被补齐');
  assert.equal(out.records.r1.received_at, '2026-07-14 09:00');
  assert.equal(out.records.r2.received_at, '2026-07-01 10:00', '已有 received_at 不被覆盖');
  assert.equal(out.records.r3.received_at, null, '快照缺失的保持 null（走 created_at fallback）');
  // 幂等：再跑一次 count=0
  const again = backfillReceivedAt(out, snapshot);
  assert.equal(again.count, 0);
});

// ---- --date 按收到日过滤（filterByDate）----

test('filterByDate: "8.14" 与 "2026-08-14" 都能命中快照日期（startsWith 旧实现漏 "8.14"）', () => {
  const messages = [
    { message_id: 'a', date: '2026-08-14 18:50' },
    { message_id: 'b', date: '2026-08-13 09:00' },
  ];
  assert.deepEqual(filterByDate(messages, '8.14').map(m => m.message_id), ['a']);
  assert.deepEqual(filterByDate(messages, '2026-08-14').map(m => m.message_id), ['a']);
  assert.deepEqual(filterByDate(messages, '8.13').map(m => m.message_id), ['b']);
  assert.equal(filterByDate(messages, undefined).length, 2, '无 --date 不过滤');
  assert.equal(filterByDate(messages, '乱写').length, 2, '解析失败不过滤（不静默清空）');
});
