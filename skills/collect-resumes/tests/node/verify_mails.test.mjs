import test from 'node:test';
import assert from 'node:assert/strict';
import { runVerify } from '../../scripts/verify_mails.mjs';
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

test('attachments become manifest records with stable IDs', () => {
  const candidates = [{ message_id: 'm1', subject: '张三简历', date: '2026-07-10' }];
  const runner = makeDetailRunner({
    m1: { attachments: [{ id: 'att1', filename: '张三.pdf' }] },
  });
  const { manifest, failed } = runVerify(candidates, runner, null);
  const ids = Object.keys(manifest.records);
  assert.equal(ids.length, 1);
  assert.equal(manifest.records[ids[0]].source_type, 'mail_attachment');
  assert.equal(manifest.records[ids[0]].status, 'needs_resolution');
  assert.equal(manifest.records[ids[0]].original_filename, '张三.pdf');
  assert.equal(failed.length, 0);
});

test('body links become records (126.com + portfolio no longer dropped)', () => {
  const candidates = [{ message_id: 'm2', subject: '李四作品', date: '2026-07-10' }];
  const runner = makeDetailRunner({
    m2: {
      body_html: '<a href="https://mail.126.com/large?id=1&amp;k=2">我的作品</a><a href="https://artstation.com/u">作品集</a>',
    },
  });
  const { manifest } = runVerify(candidates, runner, null);
  const recs = Object.values(manifest.records);
  assert.equal(recs.length, 2);
  const kinds = recs.map(r => r.link_kind).sort();
  assert.deepEqual(kinds, ['large_attachment', 'portfolio']);
});

test('detail fetch failure blocks the email, does not record zero attachments', () => {
  const candidates = [{ message_id: 'm3', subject: '王五', date: '2026-07-10' }];
  const runner = makeDetailRunner({ m3: { error: 'message not found' } });
  const { manifest, failed } = runVerify(candidates, runner, null);
  const recs = Object.values(manifest.records);
  assert.equal(recs.length, 1);
  assert.equal(recs[0].status, 'blocked');
  assert.match(recs[0].errors[0].code, /DETAIL_FETCH_FAILED/);
  assert.equal(failed.length, 1);
});

test('body hints material but no attachment and no link → blocked', () => {
  const candidates = [{ message_id: 'm4', subject: '赵六', date: '2026-07-10' }];
  const runner = makeDetailRunner({
    m4: { body_html: '<p>附件是我的简历，请查收</p>' }, // 提示关键词但无附件无链接
  });
  const { manifest, failed } = runVerify(candidates, runner, null);
  const blocked = Object.values(manifest.records).filter(r => r.status === 'blocked');
  assert.equal(blocked.length, 1);
  assert.match(blocked[0].errors[0].code, /MATERIAL_HINT_NO_SOURCE/);
});

test('incremental: does not drop existing records from previous manifest', () => {
  const prevManifest = {
    schema_version: 1,
    batches: {},
    records: { existing_rec: { record_id: 'existing_rec', status: 'validated', errors: [] } },
  };
  const candidates = [{ message_id: 'm5', subject: '新候选人', date: '2026-07-10' }];
  const runner = makeDetailRunner({ m5: { attachments: [{ id: 'a1', filename: '新.pdf' }] } });
  const { manifest } = runVerify(candidates, runner, prevManifest);
  assert.ok(manifest.records.existing_rec, '旧记录不丢失');
  assert.ok(Object.keys(manifest.records).length >= 2);
});
