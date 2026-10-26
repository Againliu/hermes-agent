/** Bot Mode's Pause / Notifications strings (`bot-pause.ts`, the bot row's
 *  menu and paused glyph). A sibling of `i18n.ts`, merged under `pause`. */

// A `type`, not an `interface`: bundles must satisfy the SDK's index-signature
// `PluginMessages`, which only type aliases do implicitly (as `BotsMessages`).
export type PauseMessages = {
  pause: string
  resume: string
  pausedBadge: string
  pausedToast: (name: string) => string
  resumedToast: (name: string) => string
  saveFailed: (name: string) => string
  notifications: string
  levelAll: string
  levelNeedsYou: string
  levelOff: string
}

const en: PauseMessages = {
  pause: 'Pause Bot',
  resume: 'Resume Bot',
  pausedBadge: 'Paused: routines and teammate messages are on hold; you can still chat',
  pausedToast: name => `${name} paused — its routines and teammate messages are on hold`,
  resumedToast: name => `${name} resumed — runs missed while paused are skipped`,
  saveFailed: name => `Couldn't change ${name}'s pause on its gateway`,
  notifications: 'Notifications',
  levelAll: 'All',
  levelNeedsYou: 'Only when it needs me',
  levelOff: 'Off'
}

const ja: PauseMessages = {
  pause: 'ボットを一時停止',
  resume: 'ボットを再開',
  pausedBadge: '一時停止中：ルーティンとチームメイトからのメッセージは保留されます。チャットは引き続き可能です',
  pausedToast: name => `${name} を一時停止しました — ルーティンとチームメイトからのメッセージは保留されます`,
  resumedToast: name => `${name} を再開しました — 一時停止中に逃した実行はスキップされます`,
  saveFailed: name => `${name} の一時停止をゲートウェイで変更できませんでした`,
  notifications: '通知',
  levelAll: 'すべて',
  levelNeedsYou: '対応が必要なときのみ',
  levelOff: 'オフ'
}

const zh: PauseMessages = {
  pause: '暂停机器人',
  resume: '恢复机器人',
  pausedBadge: '已暂停：例行任务和队友消息已搁置；你仍可与它聊天',
  pausedToast: name => `${name} 已暂停 — 其例行任务和队友消息已搁置`,
  resumedToast: name => `${name} 已恢复 — 暂停期间错过的运行将被跳过`,
  saveFailed: name => `无法在网关上更改 ${name} 的暂停状态`,
  notifications: '通知',
  levelAll: '全部',
  levelNeedsYou: '仅在需要我时',
  levelOff: '关闭'
}

const zhHant: PauseMessages = {
  pause: '暫停機器人',
  resume: '恢復機器人',
  pausedBadge: '已暫停：例行任務和隊友訊息已擱置；你仍可與它聊天',
  pausedToast: name => `${name} 已暫停 — 其例行任務和隊友訊息已擱置`,
  resumedToast: name => `${name} 已恢復 — 暫停期間錯過的執行將被略過`,
  saveFailed: name => `無法在閘道上變更 ${name} 的暫停狀態`,
  notifications: '通知',
  levelAll: '全部',
  levelNeedsYou: '僅在需要我時',
  levelOff: '關閉'
}

export const PAUSE_LOCALES = { en, ja, zh, 'zh-hant': zhHant }
