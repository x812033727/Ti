// 跨模組共享的可變狀態。ES module binding 不可跨模組賦值，
// 故集中成單一 state 物件、一律以屬性讀寫（state.ws = …）。
export const state = {
  // 進行中的 WebSocket 連線（無則 null）。
  ws: null,
  sessionId: null,
  // 檔案面板/下載對接的 workspace id：一次性討論＝sessionId；專案模式＝project-<pid>
  // （多場 session 共用固定 workspace），由 session_started 的 workspace_id 提供。
  workspaceId: null,
  // 持續改良模式：迴圈內每輪討論各發自己的 done，僅「帶 improve 摘要的總結 done」才收尾。
  improveMode: false,
  // 歷史重播中：讓 handleEvent 的 done case 不會替歷史 session 補出發佈鈕。
  replaying: false,
  // 發佈設定（/api/publish/config）：是否已設定發佈 repo、是否啟用自動合併。
  publishConfigured: false,
  mergeEnabled: false,
};
