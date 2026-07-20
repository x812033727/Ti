// 側欄對話清單(PR5):渲染狀態點/空清單、點擊分流(running→attach、完結→replay)、
// refresh 失敗靜默。真實模組+RecEl stub。
import { install, expect } from './_frontend_env.mjs';

let fetchPayload = { sessions: [] };
const { $ } = install(() => Promise.resolve({ ok: true, json: () => Promise.resolve(fetchPayload) }));

const sidenav = await import('../web/js/panels/sidenav.js');
const home = await import('../web/js/panels/home.js');

// 1) 空清單=引導文案
sidenav.renderSidenavHistory([]);
expect($('#snHistoryList').children.some((c) => c.className === 'sn-empty'), '空清單應顯示引導');

// 2) 渲染:狀態點分類 + 標題 textContent
sidenav.renderSidenavHistory([
  { session_id: 's1', status: 'running', requirement: '進行中的', n_events: 3 },
  { session_id: 's2', status: 'completed', requirement: '完結的', n_events: 9 },
  { session_id: 's3', status: 'error', requirement: '', n_events: 0 },
]);
const items = $('#snHistoryList').children;
expect(items.length === 3, '應渲染 3 列');
expect(items[0].children[0].className.includes('running'), 'running 應有綠點');
expect(items[2].children[0].className.includes('error'), 'error 應有紅點');
expect(items[2].children[1].textContent === '(無需求)', '空需求佔位文案');

// 3) 點擊分流:running→attachSessionInHome、完結→replaySessionInHome
//    (home.js 為靜態可 spy:動態 import 取回同一模組實例)
// module namespace 唯讀不可直接 spy → 以行為驗證:點 running 列後 attach 生效
// (state.sessionId 變為該場、subview 切 chat)。
const { state } = await import('../web/js/state.js');
state.ws = null;
items[0].onclick();
// 動態 import 的 promise 鏈需數個 microtask 才 settle:多跳幾拍(次數保守)
for (let i = 0; i < 20; i++) await Promise.resolve();
expect(state.sessionId === 's1', '點 running 列應 attach 該場(state.sessionId)');
expect($('#homeMain').dataset.subview === 'chat', 'attach 應切 chat');

// 4) refresh:fetch 失敗靜默不拋
fetchPayload = null; // json 會炸 → catch
await sidenav.refreshSidenavHistory();
console.log('OK');
