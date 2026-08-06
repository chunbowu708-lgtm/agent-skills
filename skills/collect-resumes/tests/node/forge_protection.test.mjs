import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { mergeResults } from '../../scripts/merge_results.mjs';
import { readManifest, writeManifestAtomic, transitionRecord } from '../../scripts/lib/manifest.mjs';
import { sha256File } from '../../scripts/lib/file_identity.mjs';

function makeManifestWithRecord(rec) {
  return { schema_version: 1, batches: {}, records: { [rec.record_id]: rec } };
}

test('merge: forged target_path not matching manifest binding is rejected', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'forge-'));
  const manifestPath = path.join(dir, 'manifest.json');
  const resultsDir = path.join(dir, 'results');
  fs.mkdirSync(resultsDir);

  // 正常 record：target 指向归档内
  let manifest = makeManifestWithRecord({
    record_id: 'rf1', message_id: 'm1', attachment_id: 'a1',
    target_dir: path.join(dir, 'archive'), target_filename: 'real.pdf',
    status: 'discovered', errors: [],
  });
  manifest.records.rf1 = transitionRecord(manifest.records.rf1, 'needs_resolution');
  manifest.records.rf1 = transitionRecord(manifest.records.rf1, 'verified');

  // 伪造文件在别处
  const forgedDir = path.join(dir, 'forged');
  fs.mkdirSync(forgedDir, { recursive: true });
  const forgedPath = path.join(forgedDir, 'forged.pdf');
  fs.writeFileSync(forgedPath, Buffer.from('%PDF-1.7\nforged'));
  const forgedSha = sha256File(forgedPath);

  // 伪造结果：target_path 指向 forged，但 manifest 绑定的是 archive/real.pdf
  fs.writeFileSync(path.join(resultsDir, 'rf1.result.json'), JSON.stringify({
    record_id: 'rf1', outcome: 'committed', sha256: forgedSha, target_path: forgedPath,
  }));

  writeManifestAtomic(manifestPath, manifest);
  const { merged, errors } = mergeResults(manifestPath, resultsDir);
  assert.equal(merged, 0);
  assert.equal(errors.length, 1);
  assert.match(errors[0].error, /target_path 与 manifest 绑定不符/);
});
