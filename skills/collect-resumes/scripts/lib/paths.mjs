// scripts/lib/paths.mjs
// 全部路径常量的单一真相源。所有 .mjs 脚本从此 import，不各自硬编码。
// 改路径只改这一个文件。

// ---- 工具链 ----
export const LARK_CLI = 'C:/Users/wuchunbo/AppData/Roaming/npm/lark-cli.cmd';

// ---- 数据文件 ----
export const MANIFEST = 'F:/miniwanob/notes/collection_manifest.json';
export const SCAN_ALL = 'F:/miniwanob/notes/_scan_all.json';
export const DIAG_DIR = 'F:/miniwanob/notes/_scan_diagnostics';
export const RESULTS_DIR = 'F:/miniwanob/notes/_download_results';

// ---- 归档与下载 ----
// ⚠️ ARCHIVE_ROOT 与 paths.py:13 的 ARCHIVE_ROOT 必须一致（JS/Python 无法共享文件，靠双向同步）。
// 改这里必须同步改 paths.py:13，反之亦然。
export const ARCHIVE_ROOT = 'F:/miniwanob/data/在招岗位候选人管理';
export const DOWNLOADS_DIR = 'F:/Users/wuchunbo/Downloads';
export const UNSAFE_DIR = 'F:/Users/wuchunbo/Downloads/collect-resumes-manual';

// ---- execSync 参数 ----
export const MAX_BUFFER = 50 * 1024 * 1024; // 50MB（lark-cli 分页响应可能很大）
