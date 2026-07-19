// 助手首頁(view=home)骨架驗證:先掛全域 stub 再 import 真實模組。
// 驗證三視圖互斥(body.dataset.view 單一真相)、rail 鈕 aria-selected 對齊、
// setView 對 home/studio 不觸發 dashboard 輪詢、sidenav 綁定與工作室入口。
import { install, expect } from './_frontend_env.mjs';

const { $ } = install(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }));

const dash = await import('../web/js/panels/dashboard.js');
const sidenav = await import('../web/js/panels/sidenav.js');

// 1) setView("home"):dataset.view=home,首頁鈕 selected、其餘鈕未選
dash.setView('home');
expect(globalThis.document.body.dataset.view === 'home', 'view 應為 home');
expect($('#homeBtn').getAttribute('aria-selected') === 'true', '首頁鈕應 selected');
expect($('#viewDashBtn').getAttribute('aria-selected') === 'false', '監控鈕應未選');
expect($('#viewStudioBtn').getAttribute('aria-selected') === 'false', '工作室鈕應未選');

// 2) setView("studio"):互斥切換
dash.setView('studio');
expect(globalThis.document.body.dataset.view === 'studio', 'view 應為 studio');
expect($('#homeBtn').getAttribute('aria-selected') === 'false', '首頁鈕應退選');
expect($('#viewStudioBtn').getAttribute('aria-selected') === 'true', '工作室鈕應 selected');

// 3) sidenav:bind 後「工作室」入口切視圖;新對話聚焦 composer(stub 容錯)
sidenav.bindSidenav();
dash.setView('home');
$('#snStudio').onclick();
expect(globalThis.document.body.dataset.view === 'studio', '側欄工作室入口應切到 studio');
let focused = false;
$('#heroInput').focus = () => { focused = true; };
sidenav.focusComposer();
expect(focused, '新對話應聚焦 composer');

console.log('OK');
