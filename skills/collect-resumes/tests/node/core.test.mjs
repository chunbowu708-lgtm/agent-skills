import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  attachmentRecordId,
  linkRecordId,
  transitionRecord,
  writeManifestAtomic,
} from '../../scripts/lib/manifest.mjs';
import { parseCliJson } from '../../scripts/lib/lark_mail.mjs';
import { extractLinks } from '../../scripts/lib/html_links.mjs';
import { commitVerifiedFile, detectFileType, sha256File, detectTypeFromBuffer, typeMatchesExtension } from '../../scripts/lib/file_identity.mjs';

const tempDir = () => fs.mkdtempSync(path.join(os.tmpdir(), 'collect-resumes-'));

test('record ids are stable and source-bound', () => {
  assert.equal(attachmentRecordId('m1', 'a1'), attachmentRecordId('m1', 'a1'));
  assert.notEqual(attachmentRecordId('m1', 'a1'), attachmentRecordId('m2', 'a1'));
  assert.equal(linkRecordId('m1', 'HTTPS://Example.com/a?b=1&amp;c=2'), linkRecordId('m1', 'https://example.com/a?b=1&c=2'));
});

test('state machine rejects skipped transitions', () => {
  const record = { record_id: 'r1', status: 'discovered', errors: [] };
  assert.throws(() => transitionRecord(record, 'downloaded'), /INVALID_TRANSITION/);
  assert.equal(transitionRecord(record, 'needs_resolution').status, 'needs_resolution');
});

test('atomic manifest write preserves valid JSON', () => {
  const dir = tempDir();
  const target = path.join(dir, 'manifest.json');
  writeManifestAtomic(target, { schema_version: 1, records: [] });
  assert.deepEqual(JSON.parse(fs.readFileSync(target, 'utf8')).records, []);
  assert.equal(fs.existsSync(`${target}.tmp`), false);
});

test('CLI parser fails closed on malformed or business-error JSON', () => {
  assert.throws(() => parseCliJson('not json'), /INVALID_JSON/);
  assert.throws(() => parseCliJson('{"ok":false,"error":{"message":"denied"}}'), /API_ERROR.*denied/);
  assert.equal(parseCliJson('tip: hello\n{"ok":true,"data":{"x":1}}').data.x, 1);
});

test('HTML links decode entities and use anchor context', () => {
  const links = extractLinks('<a href="https://mail.126.com/large?id=1&amp;k=2">附件</a><a href="https://portfolio.example/u">作品集</a>');
  assert.equal(links[0].url, 'https://mail.126.com/large?id=1&k=2');
  assert.equal(links[0].kind, 'large_attachment');
  assert.equal(links[1].kind, 'portfolio');
});

test('file commit is idempotent and never overwrites different content', () => {
  const dir = tempDir();
  const target = path.join(dir, 'resume.pdf');
  const first = path.join(dir, 'first.part');
  fs.writeFileSync(first, Buffer.from('%PDF-1.7\nfirst'));
  assert.equal(detectFileType(first), 'pdf');
  const committed = commitVerifiedFile(first, target, 'pdf');
  assert.equal(committed.outcome, 'committed');

  const same = path.join(dir, 'same.part');
  fs.writeFileSync(same, Buffer.from('%PDF-1.7\nfirst'));
  assert.equal(commitVerifiedFile(same, target, 'pdf').outcome, 'idempotent');

  const conflict = path.join(dir, 'conflict.part');
  fs.writeFileSync(conflict, Buffer.from('%PDF-1.7\nsecond'));
  assert.throws(() => commitVerifiedFile(conflict, target, 'pdf'), /TARGET_CONFLICT/);
  assert.equal(sha256File(target), committed.sha256);
});

test('OLE2/CFB magic bytes detected as doc (老式 .doc 下载不再被拒)', () => {
  // 老式 .doc/Excel/PPT 复合文档二进制头：D0 CF 11 E0 A1 B1 1A E1
  const ole2 = Buffer.from([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1, 0, 0, 0, 0]);
  assert.equal(detectTypeFromBuffer(ole2), 'doc');
  assert.equal(typeMatchesExtension('doc', 'doc'), true);
  // .pdf 扩展名不应匹配 doc 内容
  assert.equal(typeMatchesExtension('pdf', 'doc'), false);
  // PDF/ZIP 仍正常（回归保护）
  assert.equal(detectTypeFromBuffer(Buffer.from('%PDF-1.7 x')), 'pdf');
  assert.equal(detectTypeFromBuffer(Buffer.from('PK\x03\x04 x')), 'zip');
});
