// scripts/lib/html_links.mjs
// 正文链接提取：HTML 实体解码、href 提取、URL 规范化、来源分类。

import { normalizeUrl } from './manifest.mjs';

const LARGE_ATTACHMENT_HOSTS = [
  'wx.mail.qq.com/ftn',
  'mail.126.com',
  '126.com',
  'mail.163.com/large',
  '163.com/large',
  'dashi.163.com',       // 网易邮箱大师云附件
  'dashi.163.com/html/cloud-attachment-download',
];
const CLOUD_DISK_HOSTS = ['pan.baidu.com', 'aliyundrive', 'alipan.com', 'cloud.189.cn'];
const PORTFOLIO_HOSTS = ['artstation.com', 'behance.net', 'zcool.com.cn', 'dribbble.com'];
const PORTFOLIO_TEXT_RE = /作品集|作品|portfolio|个人站|主页|homepage|behance|artstation/i;

/**
 * 从 HTML 中提取所有链接，返回 [{ url, text, kind }]。
 * kind: 'large_attachment' | 'cloud_disk' | 'portfolio' | 'unknown'
 *
 * @param {string} html - body_html 原文
 * @returns {Array<{url:string, text:string, kind:string}>}
 */
export function extractLinks(html) {
  if (!html) return [];
  // 先做全局实体 decode（href 属性值里 &amp; 很常见）
  const decoded = decodeHtmlEntities(html);
  const results = [];
  const seen = new Set();
  // 匹配 <a ... href="...">锚文本</a>
  const anchorRe = /<a\s+[^>]*href\s*=\s*["']?([^"'\s>]+)["']?[^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = anchorRe.exec(decoded)) !== null) {
    const rawUrl = m[1];
    const text = stripTags(m[2] || '').trim();
    // 跳过 mailto/javascript/空
    if (!rawUrl || /^(mailto:|javascript:|#)/i.test(rawUrl)) continue;
    try {
      const normalized = normalizeUrl(rawUrl);
      if (seen.has(normalized)) continue;
      seen.add(normalized);
      results.push({ url: normalized, text, kind: classifyLink(normalized, text) });
    } catch {
      // URL 解析失败仍记录为 unknown，不静默丢
      const fallback = rawUrl.toLowerCase();
      if (!seen.has(fallback)) {
        seen.add(fallback);
        results.push({ url: rawUrl, text, kind: classifyLink(rawUrl, text) });
      }
    }
  }
  return results;
}

/**
 * 分类单个链接。先按域名，再按锚文本。
 */
export function classifyLink(url, text = '') {
  const lower = (url || '').toLowerCase();
  const combined = `${lower} ${text}`;
  if (LARGE_ATTACHMENT_HOSTS.some(h => lower.includes(h))) return 'large_attachment';
  if (CLOUD_DISK_HOSTS.some(h => lower.includes(h))) return 'cloud_disk';
  if (PORTFOLIO_HOSTS.some(h => lower.includes(h))) return 'portfolio';
  if (PORTFOLIO_TEXT_RE.test(combined)) return 'portfolio';
  return 'unknown';
}

/**
 * 是否为"作品/附件材料类"链接（需要人工或浏览器下载）。
 */
export function isMaterialLink(link) {
  return link.kind !== 'unknown' || /作品|附件|简历|portfolio|artstation|resume/i.test(`${link.url} ${link.text}`);
}

function decodeHtmlEntities(s) {
  return s
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ');
}

function stripTags(s) {
  return s.replace(/<[^>]*>/g, '');
}
