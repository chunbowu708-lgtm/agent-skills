// scripts/lib/notifications.mjs
// 通知邮件关键词单一真相源。scan_all（打标签）和 verify_mails（过滤）共用，
// 防止改一处忘另一处导致过滤不一致。

export const NOTIF_RE = /奋斗食代|员工关爱|视频面试邀约|资料收集|欢迎加入|系统通知|日程提醒/;

/** 判断一封邮件是否为系统通知（按 subject + from 匹配）。 */
export function isNotification(mail) {
  return NOTIF_RE.test(mail.subject || '') || NOTIF_RE.test(mail.from || '');
}
