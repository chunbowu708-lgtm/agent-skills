import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { resolveRecord, matchJobDir, isWithinArchive, dateSegment } from '../../scripts/resolve_records.mjs';
import { mergeResults } from '../../scripts/merge_results.mjs';
import {
  readManifest, writeManifestAtomic, transitionRecord,
} from '../../scripts/lib/manifest.mjs';
import { sha256File, commitVerifiedFile } from '../../scripts/lib/file_identity.mjs';

function makeManifestWithRecord(rec) {
  return { schema_version: 1, batches: {}, records: { [rec.record_id]: rec } };
}

// 测试用归档根（在真实 ARCHIVE_ROOT 下建临时 fixture 目录，保证 isWithinArchive 通过；
// 2026-07-29 resolve 现在会 mkdir _暂定 子目录，需真实存在的父目录）
import { ARCHIVE_ROOT } from '../../scripts/lib/paths.mjs';
const FIXTURE_DIR = ARCHIVE_ROOT + '/_test_fixture_' + Date.now();

function makeTmpArchiveFixture() {
  // 在 ARCHIVE_ROOT 下造岗位目录 + 已收集简历
  const collected = FIXTURE_DIR + '/山海弹珠项目/美术端/特效设计师/已收集简历';
  fs.mkdirSync(collected, { recursive: true });
  return { collected };
}

// 所有测试结束后清理 fixture 目录
after(() => {
  if (fs.existsSync(FIXTURE_DIR)) fs.rmSync(FIXTURE_DIR, { recursive: true, force: true });
});

test('resolve: unique job resolves to verified with target path in _暂定 subdir', () => {
  const { collected } = makeTmpArchiveFixture();
  const jobDirs = [{ job_dir: '山海弹珠项目/美术端/特效设计师', collected_dir: collected.replace(/\\/g, '/') }];
  const rec = { record_id: 'r1', message_id: 'm1', attachment_id: 'a1', status: 'needs_resolution', errors: [], source_type: 'mail_attachment' };
  const manifest = makeManifestWithRecord(rec);
  // 注入临时 jobDirs + date，避免触碰真实归档和依赖今天日期
  const result = resolveRecord(manifest, 'r1', '张三', '特效设计师', '张三_特效设计师_5年.pdf', jobDirs, { date: '7.29' });
  assert.equal(result.status, 'resolved');
  const updated = result.manifest.records.r1;
  assert.equal(updated.status, 'verified');
  assert.equal(updated.candidate_name, '张三');
  // 2026-07-29：target_dir 必须含 _暂定 中转段（不再直接落 已收集简历 根）
  assert.ok(updated.target_dir.includes('特效设计师'), '应指向特效设计师目录');
  assert.ok(updated.target_dir.includes('7.29_暂定'), '应落到 7.29_暂定 子目录');
  // _暂定 目录应已被创建
  assert.ok(fs.existsSync(updated.target_dir), '_暂定 目录应已创建');
});

test('resolve: 目录日期段默认=记录邮件收到日（历史积压不混进操作日目录）', () => {
  const { collected } = makeTmpArchiveFixture();
  const jobDirs = [{ job_dir: '山海弹珠项目/美术端/特效设计师', collected_dir: collected.replace(/\\/g, '/') }];
  // received_at=2020-01-02（久远日期，确保≠运行日），created_at=今天：
  // 旧实现默认目录日期段=操作日（今天），历史邮件会混进今日目录
  const rec = {
    record_id: 'r9', message_id: 'm9', attachment_id: 'a9', status: 'needs_resolution',
    errors: [], source_type: 'mail_attachment',
    received_at: '2020-01-02 10:00', created_at: new Date().toISOString(),
  };
  const result = resolveRecord(makeManifestWithRecord(rec), 'r9', '陈九', '特效设计师', '陈九_特效设计师_5年.pdf', jobDirs);
  assert.equal(result.status, 'resolved');
  assert.ok(result.manifest.records.r9.target_dir.includes('1.2_暂定'),
    '目录日期段应为邮件收到日 1.2，不是操作日');
});

test('resolve: ambiguous Unity stays needs_resolution', () => {
  // 歧义测试不触发 mkdir（保持 needs_resolution），可用任意 collected_dir
  const jobDirs = [
    { job_dir: '长青工作室/技术端/Unity客户端开发工程师', collected_dir: 'F:/tmp/a/已收集简历' },
    { job_dir: '山海弹珠项目/技术端/Unity客户端开发工程师（AI-Native方向）', collected_dir: 'F:/tmp/b/已收集简历' },
  ];
  const rec = { record_id: 'r2', message_id: 'm2', attachment_id: 'a2', status: 'needs_resolution', errors: [] };
  const manifest = makeManifestWithRecord(rec);
  const result = resolveRecord(manifest, 'r2', '李四', 'Unity客户端', '李四.pdf', jobDirs);
  assert.equal(result.status, 'ambiguous');
  // 候选人/岗位写入但状态不推进
  assert.equal(result.manifest.records.r2.status, 'needs_resolution');
});

test('resolve: unknown job does not create directory', () => {
  const rec = { record_id: 'r3', status: 'needs_resolution', errors: [] };
  const manifest = makeManifestWithRecord(rec);
  const result = resolveRecord(manifest, 'r3', '王五', '不存在的岗位', '王五.pdf', []);
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

test('merge: downloaded status does not crash (A3 regression)', () => {
  // 2026-07-29 修复：旧版对 'downloaded' 状态会再次 transitionRecord→'downloaded'
  // 抛 INVALID_TRANSITION（在 try/catch 外）→ 整批中止且重跑卡死。
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'merge-'));
  const manifestPath = path.join(dir, 'manifest.json');
  const resultsDir = path.join(dir, 'results');
  fs.mkdirSync(resultsDir);

  // 记录已处于 'downloaded'（前次合并中断于此）
  let manifest = makeManifestWithRecord({
    record_id: 'rd1', message_id: 'm1', attachment_id: 'a1',
    status: 'discovered', errors: [],
    target_dir: dir, target_filename: 'resume.pdf',
  });
  manifest.records.rd1 = transitionRecord(manifest.records.rd1, 'needs_resolution');
  manifest.records.rd1 = transitionRecord(manifest.records.rd1, 'verified');
  manifest.records.rd1 = transitionRecord(manifest.records.rd1, 'downloading');
  manifest.records.rd1 = transitionRecord(manifest.records.rd1, 'downloaded');

  const targetPath = path.join(dir, 'resume.pdf');
  fs.writeFileSync(targetPath, Buffer.from('%PDF-1.7\nrecovered'));
  const sha = sha256File(targetPath);

  fs.writeFileSync(path.join(resultsDir, 'rd1.result.json'), JSON.stringify({
    record_id: 'rd1', outcome: 'committed', sha256: sha, target_path: targetPath, at: '2026-07-29',
  }));

  writeManifestAtomic(manifestPath, manifest);
  const { merged, errors } = mergeResults(manifestPath, resultsDir);
  assert.equal(merged, 1, "'downloaded' 状态记录应能继续推进到 archived");
  assert.equal(errors.length, 0);

  const finalManifest = readManifest(manifestPath);
  assert.equal(finalManifest.records.rd1.status, 'archived');
});
