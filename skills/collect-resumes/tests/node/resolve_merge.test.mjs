import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { resolveRecord, matchJobDir, isWithinArchive } from '../../scripts/resolve_records.mjs';
import { mergeResults } from '../../scripts/merge_results.mjs';
import {
  readManifest, writeManifestAtomic, transitionRecord,
} from '../../scripts/lib/manifest.mjs';
import { sha256File, commitVerifiedFile } from '../../scripts/lib/file_identity.mjs';

function makeManifestWithRecord(rec) {
  return { schema_version: 1, batches: {}, records: { [rec.record_id]: rec } };
}

const JOB_DIRS_FIXTURE = [
  { job_dir: '山海弹珠项目/美术端/特效设计师', collected_dir: 'F:/miniwanob/data/在招岗位候选人管理/山海弹珠项目/美术端/特效设计师/已收集简历' },
  { job_dir: '长青工作室/技术端/Unity客户端开发工程师', collected_dir: 'F:/miniwanob/data/在招岗位候选人管理/长青工作室/技术端/Unity客户端开发工程师/已收集简历' },
  { job_dir: '全球发行业务/技术支持团队/中高级Unity客户端开发工程师', collected_dir: 'F:/miniwanob/data/在招岗位候选人管理/全球发行业务/技术支持团队/中高级Unity客户端开发工程师/已收集简历' },
  { job_dir: '山海弹珠项目/技术端/Unity客户端开发工程师（AI-Native方向）', collected_dir: 'F:/miniwanob/data/在招岗位候选人管理/山海弹珠项目/技术端/Unity客户端开发工程师（AI-Native方向）/已收集简历' },
];

test('resolve: unique job resolves to verified with target path', () => {
  const rec = { record_id: 'r1', message_id: 'm1', attachment_id: 'a1', status: 'needs_resolution', errors: [], source_type: 'mail_attachment' };
  const manifest = makeManifestWithRecord(rec);
  const result = resolveRecord(manifest, 'r1', '张三', '特效设计师', '张三_特效设计师_5年.pdf', JOB_DIRS_FIXTURE);
  assert.equal(result.status, 'resolved');
  assert.ok(result.detail.includes('特效设计师'));
  const updated = result.manifest.records.r1;
  assert.equal(updated.status, 'verified');
  assert.equal(updated.candidate_name, '张三');
  assert.ok(updated.target_dir.includes('特效设计师'));
});

test('resolve: ambiguous Unity stays needs_resolution', () => {
  const rec = { record_id: 'r2', message_id: 'm2', attachment_id: 'a2', status: 'needs_resolution', errors: [] };
  const manifest = makeManifestWithRecord(rec);
  const result = resolveRecord(manifest, 'r2', '李四', 'Unity客户端', '李四.pdf', JOB_DIRS_FIXTURE);
  assert.equal(result.status, 'ambiguous');
  // 候选人/岗位写入但状态不推进
  assert.equal(result.manifest.records.r2.status, 'needs_resolution');
});

test('resolve: unknown job does not create directory', () => {
  const rec = { record_id: 'r3', status: 'needs_resolution', errors: [] };
  const manifest = makeManifestWithRecord(rec);
  const result = resolveRecord(manifest, 'r3', '王五', '不存在的岗位', '王五.pdf', JOB_DIRS_FIXTURE);
  assert.equal(result.status, 'not_found');
  assert.equal(result.manifest.records.r3.status, 'needs_resolution');
});

test('merge: valid result advances record to archived', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'merge-'));
  const manifestPath = path.join(dir, 'manifest.json');
  const resultsDir = path.join(dir, 'results');
  fs.mkdirSync(resultsDir);

  // 创建一个已 verified 的 record（含 target 绑定）
  let manifest = makeManifestWithRecord({
    record_id: 'rm1', message_id: 'm1', attachment_id: 'a1',
    status: 'discovered', errors: [],
    target_dir: dir, target_filename: 'resume.pdf',
  });
  manifest.records.rm1 = transitionRecord(manifest.records.rm1, 'needs_resolution');
  manifest.records.rm1 = transitionRecord(manifest.records.rm1, 'verified');

  // 创建目标文件（模拟下载产物）
  const targetPath = path.join(dir, 'resume.pdf');
  fs.writeFileSync(targetPath, Buffer.from('%PDF-1.7\ntest content'));
  const sha = sha256File(targetPath);

  // 创建结果文件
  fs.writeFileSync(path.join(resultsDir, 'rm1.result.json'), JSON.stringify({
    record_id: 'rm1', outcome: 'committed', sha256: sha, target_path: targetPath, at: '2026-07-10',
  }));

  writeManifestAtomic(manifestPath, manifest);
  const { merged, errors } = mergeResults(manifestPath, resultsDir);
  assert.equal(merged, 1);
  assert.equal(errors.length, 0);

  const finalManifest = readManifest(manifestPath);
  assert.equal(finalManifest.records.rm1.status, 'archived');
  assert.equal(finalManifest.records.rm1.sha256, sha);
});

test('merge: hash mismatch blocks the merge', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'merge-'));
  const manifestPath = path.join(dir, 'manifest.json');
  const resultsDir = path.join(dir, 'results');
  fs.mkdirSync(resultsDir);

  let manifest = makeManifestWithRecord({ record_id: 'rm2', status: 'discovered', errors: [],
    target_dir: dir, target_filename: 'resume.pdf' });
  manifest.records.rm2 = transitionRecord(manifest.records.rm2, 'needs_resolution');
  manifest.records.rm2 = transitionRecord(manifest.records.rm2, 'verified');

  const targetPath = path.join(dir, 'resume.pdf');
  fs.writeFileSync(targetPath, Buffer.from('%PDF-1.7\nactual'));
  // 故意写错的哈希
  fs.writeFileSync(path.join(resultsDir, 'rm2.result.json'), JSON.stringify({
    record_id: 'rm2', outcome: 'committed', sha256: '0000000000000000000000000000000000000000000000000000000000000000', target_path: targetPath,
  }));

  writeManifestAtomic(manifestPath, manifest);
  const { merged, errors } = mergeResults(manifestPath, resultsDir);
  assert.equal(merged, 0);
  assert.equal(errors.length, 1);
  assert.match(errors[0].error, /哈希不一致/);
});
