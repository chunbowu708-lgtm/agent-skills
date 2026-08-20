// scripts/lib/notifications.mjs
// 通知邮件关键词单一真相源。
//
// ⚠️ 仅用于展示打标签（scan 列表、verify 日志里的 🏷️ 标记），
// 绝不作为跳过处理的过滤条件：主题关键词（如"资料收集"）会被候选人的
// 回信继承（Re:【资料收集】公司名 + 学历/薪酬附件），按关键词丢邮件
// 会静默漏真简历。一封邮件是否相关，由 verify_mails 拉详情后按
// "有无附件/材料链接/正文提示"事实判定（fail-closed）。

export const NOTIF_RE = /奋斗食代|员工关爱|视频面试邀约|资料收集|欢迎加入|系统通知|日程提醒/;

/** 判断一封邮件是否疑似系统通知（仅打标签用，不得用于跳过处理）。 */
export function isNotification(mail) {
  return NOTIF_RE.test(mail.subject || '') || NOTIF_RE.test(mail.from || '');
}
