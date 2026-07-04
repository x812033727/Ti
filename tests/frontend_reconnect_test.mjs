// WS 斷線重連純邏輯驗證：先掛全域 stub 再 import 真實 web/js/ws.js（ES module），
// 驗 computeReconnectDelay 的退避/封頂、trackSocketEvent 的計數起算與收尾判定、
// attach_ok 校準（bindSocket 路徑）、onclose 的重連分流（進行中→排程重連；收尾→不重連）。

// --- 記錄式 DOM（同 frontend_project_recover_test 範式）---
class RecEl {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.className = '';
    this.textContent = '';
    this.value = '';
    this.checked = false;
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  appendChild(c) { this.children.push(c); return c; }
  prepend(c) { this.children.unshift(c); return c; }
  set innerHTML(v) { this._inner = v; if (v === '') this.children = []; }
  get innerHTML() { return this._inner || ''; }
  querySelectorAll() { return []; }
  querySelector() { return new RecEl('div'); }
  addEventListener() {}
  focus() {}
}

const els = new Map();
function $(sel) {
  if (!els.has(sel)) els.set(sel, new RecEl('stub'));
  return els.get(sel);
}

const noop = () => {};
const timers = []; // 捕捉 setTimeout 排程（重連延遲）
const sockets = [];
function WebSocket() { const s = new RecEl('ws'); s.sent = []; s.send = (m) => s.sent.push(m); sockets.push(s); return s; }
WebSocket.OPEN = 1;

const realSetTimeout = globalThis.setTimeout;
Object.assign(globalThis, {
  document: {
    querySelector: (s) => $(s),
    querySelectorAll: () => [],
    createElement: (t) => new RecEl(t),
    createTextNode: () => new RecEl('text'),
    getElementById: () => new RecEl('div'),
    body: new RecEl('body'),
    documentElement: new RecEl('html'),
  },
  window: {
    addEventListener: noop,
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    location: { protocol: 'http:', host: 'x', href: '' },
  },
  location: { protocol: 'http:', host: 'x', href: '' },
  WebSocket,
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearTimeout: noop,
});

const ws = await import('../web/js/ws.js');
const { state } = await import('../web/js/state.js');
globalThis.setTimeout = realSetTimeout; // 之後的非重連計時不再攔截

function expect(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

// --- computeReconnectDelay：指數遞增、封頂 15s、equal-jitter 落點 ---
for (let n = 0; n < 12; n++) {
  const base = Math.min(1000 * 2 ** n, 15000);
  for (let i = 0; i < 20; i++) {
    const d = ws.computeReconnectDelay(n);
    expect(d >= base / 2 && d <= base, `delay(${n}) 應落在 [${base / 2}, ${base}]，實得 ${d}`);
  }
}

// --- trackSocketEvent：session_started 起算、逐筆計數、done 判 terminal ---
expect(ws.getReconnectState().counting === false, '初始不起算');
expect(ws.trackSocketEvent({ type: 'phase_change', payload: {} }) === false, '起算前事件非 terminal');
expect(ws.getReconnectState().eventCount === 0, '起算前不計數（未入檔的準備事件）');

expect(ws.trackSocketEvent({ type: 'session_started', payload: {} }) === false, 'session_started 非 terminal');
expect(ws.getReconnectState().counting === true, 'session_started 後起算');
expect(ws.getReconnectState().eventCount === 1, 'session_started 本身計 1（入檔第一筆）');
ws.trackSocketEvent({ type: 'expert_message', payload: {} });
ws.trackSocketEvent({ type: 'tool_use', payload: {} });
expect(ws.getReconnectState().eventCount === 3, '之後逐筆累加');
expect(ws.trackSocketEvent({ type: 'done', payload: { completed: true } }) === true, 'done 為 terminal（非 improve 模式）');

// --- bindSocket：attach_ok 校準計數、onclose 分流 ---
const sock = new WebSocket();
ws.bindSocket(sock);
sock.onmessage({ data: JSON.stringify({ type: 'attach_ok', payload: { session_id: 's1', cursor: 42 } }) });
expect(ws.getReconnectState().eventCount === 42, 'attach_ok 應以伺服器權威計數校準 eventCount');
expect(ws.getReconnectState().reconnectAttempts === 0, 'attach_ok 應歸零重試計數');

// 進行中斷線 → 排程重連（sessionId 由 handleEvent 設定：餵一筆帶 session_id 的事件）
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
sock.onmessage({ data: JSON.stringify({ type: 'phase_change', session_id: 's1', payload: { phase: 'x' } }) });
const before = timers.length;
sock.onclose();
expect(timers.length === before + 1, '進行中（counting 且未 done）斷線應排程重連');
expect(ws.getReconnectState().reconnectAttempts === 1, '重連次數 +1');

// 排程觸發 → 開新 socket 並送 attach 首訊息
const nSockets = sockets.length;
timers[timers.length - 1].fn();
expect(sockets.length === nSockets + 1, '重連應開新 WebSocket');
const re = sockets[sockets.length - 1];
re.onopen();
const first = JSON.parse(re.sent[0]);
expect(first.attach === 's1', '重連首訊息應帶 attach=sessionId');
expect(typeof first.cursor === 'number', '重連首訊息應帶 cursor 計數');
expect(state.ws === re, '重連後 state.ws 指向新 socket（插話/停止走新連線）');

// 正常收尾（done）後 onclose 不重連
re.onmessage({ data: JSON.stringify({ type: 'done', session_id: 's1', payload: { completed: true } }) });
const before2 = timers.length;
re.onclose();
expect(timers.length === before2, '收尾 done 後斷線不應再排程重連');

// stopReconnect：主動導航（重播）後 onclose 不重連
ws.trackSocketEvent({ type: 'session_started', payload: {} }); // 重新起算模擬進行中
ws.stopReconnect();
const before3 = timers.length;
sock.onclose();
expect(timers.length === before3, 'stopReconnect 後斷線不應排程重連');

// 未知事件型別不崩
ws.trackSocketEvent({ type: 'no_such_type' });

console.log('OK: 重連延遲、計數起算/校準、onclose 分流皆正常');
process.exit(0);
