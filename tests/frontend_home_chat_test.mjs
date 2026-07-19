// 助手首頁對話(PR4):hero composer → 既有 ws 管線。
// 驗:heroStart 同步 #requirement(契約 id=值的單一來源)、subview 切 chat、
// #stream reparent 進 homeChat 並可復位、running 狀態同步鎖 composer、
// 已有進行中討論時第二次 heroStart 被擋。
import { install, expect } from './_frontend_env.mjs';

const { $ } = install(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }));

const home = await import('../web/js/panels/home.js');
const deck = await import('../web/js/panels/deck.js');
const { state } = await import('../web/js/state.js');

// 1) heroStart:同步 requirement、切 chat、開 ws
$('#heroInput').value = ' 做一個天氣頁 ';
home.heroStart();
expect($('#requirement').value === '做一個天氣頁', 'hero 輸入應同步進 #requirement(trim)');
expect($('#homeMain').dataset.subview === 'chat', 'subview 應切到 chat');
expect(state.ws !== null, '應建立 WebSocket(start 已呼叫)');

// 2) 已有進行中討論 → 第二次擋下(stub 環境 readyState===OPEN 恆真)
const prevWs = state.ws;
$('#heroInput').value = '再開一場';
home.heroStart();
expect(state.ws === prevWs, '進行中不得再開新場');

// 3) reparent:#stream 搬進 homeChatStream、可復位
const stream = $('#stream');
const orig = { children: [stream], insertBefore(el) { this.children.push(el); } };
stream.parentNode = orig;
stream.nextSibling = null;
home.moveStreamHome();
expect($('#homeChatStream').children.includes(stream), 'stream 應搬進 homeChat');
stream.parentNode = $('#homeChatStream');
home.moveStreamBack();
expect(orig.children.filter((c) => c === stream).length === 2 || orig.children.at(-1) === stream,
  'stream 應可復位回原容器');

// 4) running 同步:home composer 鎖定/解鎖
home.bindHome();
deck.setRunning(true);
expect($('#heroSend').disabled === true, '執行中 composer 應鎖定');
expect($('#heroInterject').disabled === false, '執行中插話應可用');
deck.setRunning(false);
expect($('#heroSend').disabled === false, '結束後 composer 解鎖');
expect($('#heroStopBtn').disabled === true, '結束後停止鈕鎖定');

// 5) resetToHero:清空+回 hero
$('#heroInput').value = '殘字';
home.resetToHero();
expect($('#homeMain').dataset.subview === 'hero' && $('#heroInput').value === '', '新對話應清空回 hero');



// 6) 覆審修正回歸:離開 home 自動搬回 #stream(單向陷阱);attach 重置 sawDone
const dash = await import('../web/js/panels/dashboard.js');
const ws = await import('../web/js/ws.js');
{
  const stream2 = $('#stream');
  const orig2 = { children: [stream2], insertBefore(el) { this.children.push(el); } };
  stream2.parentNode = orig2;
  stream2.nextSibling = null;
  home.moveStreamHome();
  stream2.parentNode = $('#homeChatStream');
  dash.setView('studio'); // 觸發 onViewChange → moveStreamBack
  expect(orig2.children.at(-1) === stream2, '切離 home 應自動搬回 #stream');
}
{
  // 模擬前一場正常收尾(走真實 bindSocket 路徑把 sawDone 置 true),attach 後必須歸零
  const sock = { onmessage: null, onerror: null, onclose: null };
  ws.bindSocket(sock);
  sock.onmessage({ data: JSON.stringify({ type: 'session_started', payload: {} }) });
  sock.onmessage({ data: JSON.stringify({ type: 'done', payload: {} }) });
  expect(ws.getReconnectState().sawDone === true, '前置:done 應標記 sawDone');
  home.attachSessionInHome('s9');
  expect(ws.getReconnectState().sawDone === false, 'attach 必須歸零 sawDone(否則斷線不重連)');
}

console.log('OK');
