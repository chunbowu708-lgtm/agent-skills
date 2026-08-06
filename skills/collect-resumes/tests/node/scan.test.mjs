import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { runScan } from '../../scripts/scan_all.mjs';

// 伪造分页 runner：按页返回预设响应
function makePageRunner(pages) {
  let call = 0;
  return (_cmd) => {
    const p = pages[Math.min(call, pages.length - 1)];
    call++;
    return typeof p === 'string' ? p : JSON.stringify(p);
  };
}

test('complete scan returns all messages when has_more=false', () => {
  const runner = makePageRunner([
    { ok: true, has_more: true, page_token: 'tok2', messages: [{ message_id: 'm1', subject: 'A', from: 'x', date: '2026-07-10' }] },
    { ok: true, has_more: false, page_token: '', messages: [{ message_id: 'm2', subject: 'B', from: 'y', date: '2026-07-10' }] },
  ]);
  const result = runScan(runner);
  assert.equal(result.complete, true);
  assert.equal(result.messages.length, 2);
});

test('API business error (ok=false) throws, does not return partial silently', () => {
  const runner = makePageRunner([{ ok: false, error: { message: 'access denied' } }]);
  assert.throws(() => runScan(runner), /API_ERROR.*denied/);
});

test('malformed JSON on page 2 throws (fail-closed)', () => {
  const runner = makePageRunner([
    { ok: true, has_more: true, page_token: 'tok2', messages: [{ message_id: 'm1', subject: 'A' }] },
    'THIS IS NOT JSON {{{',
  ]);
  assert.throws(() => runScan(runner), /INVALID_JSON/);
});

test('empty page_token with has_more=true returns incomplete', () => {
  const runner = makePageRunner([
    { ok: true, has_more: true, page_token: '', messages: [{ message_id: 'm1' }] },
  ]);
  const result = runScan(runner);
  assert.equal(result.complete, false);
  assert.match(result.error, /page_token 为空/);
  assert.equal(result.messages.length, 1);
});

test('duplicate page_token returns incomplete', () => {
  const runner = makePageRunner([
    { ok: true, has_more: true, page_token: 'same', messages: [{ message_id: 'm1' }] },
    { ok: true, has_more: true, page_token: 'same', messages: [{ message_id: 'm2' }] },
  ]);
  const result = runScan(runner);
  assert.equal(result.complete, false);
  assert.match(result.error, /page_token 与上一页相同/);
});

test('deduplicates by message_id across pages', () => {
  const runner = makePageRunner([
    { ok: true, has_more: true, page_token: 't2', messages: [{ message_id: 'm1', subject: 'A' }] },
    { ok: true, has_more: false, messages: [{ message_id: 'm1', subject: 'A dup' }] },
  ]);
  const result = runScan(runner);
  assert.equal(result.messages.length, 1);
});
