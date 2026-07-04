// WS 斷線重連純邏輯驗證：載入真實 web/app.js，驗 computeReconnectDelay 的退避/封頂、
// trackSocketEvent 的計數起算與收尾判定、attach_ok 校準（bindSocket 路徑）、
// onclose 的重連分流（進行中→排程重連；正常收尾→不重連）。
import fs from 'node:fs';
import vm from 'node:vm';

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

const windowObj = {
  addEventListener: noop,
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  location: { protocol: 'http:', host: 'x', href: '' },
};

const ctx = vm.createContext({
  document: {
    querySelector: (s) => $(s),
    querySelectorAll: () => [],
    createElement: (t) => new RecEl(t),
    createTextNode: () => new RecEl('text'),
    getElementById: () => new RecEl('div'),
    body: new RecEl('body'),
  },
  window: windowObj, location: windowObj.location, WebSocket,
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  console,
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearTimeout: noop, setInterval: noop, clearInterval: noop,
});

const src = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
vm.runInContext(src, ctx, { filename: 'app.js' });

function expect(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

// --- computeReconnectDelay：指數遞增、封頂 15s、equal-jitter 落點 ---
for (let n = 0; n < 12; n++) {
  const base = Math.min(1000 * 2 ** n, 15000);
  for (let i = 0; i < 20; i++) {
    const d = ctx.computeReconnectDelay(n);
    expect(d >= base / 2 && d <= base, `delay(${n}) 應落在 [${base / 2}, ${base}]，實得 ${d}`);
  }
}
expect(ctx.computeReconnectDelay(10) <= 15000, '大 n 應封頂 15s');

// --- trackSocketEvent：session_started 起算、逐筆計數、done 判 terminal ---
expect(ctx.getReconnectState().counting === false, '初始不起算');
expect(ctx.trackSocketEvent({ type: 'phase_change', payload: {} }) === false, '起算前事件非 terminal');
expect(ctx.getReconnectState().eventCount === 0, '起算前不計數（未入檔的準備事件）');

expect(ctx.trackSocketEvent({ type: 'session_started', payload: {} }) === false, 'session_started 非 terminal');
expect(ctx.getReconnectState().counting === true, 'session_started 後起算');
expect(ctx.getReconnectState().eventCount === 1, 'session_started 本身計 1（入檔第一筆）');
ctx.trackSocketEvent({ type: 'expert_message', payload: {} });
ctx.trackSocketEvent({ type: 'tool_use', payload: {} });
expect(ctx.getReconnectState().eventCount === 3, '之後逐筆累加');
expect(ctx.trackSocketEvent({ type: 'done', payload: { completed: true } }) === true, 'done 為 terminal（非 improve 模式）');

// --- bindSocket：attach_ok 校準計數、onclose 分流 ---
const sock = new WebSocket();
ctx.bindSocket(sock);
sock.onmessage({ data: JSON.stringify({ type: 'attach_ok', payload: { session_id: 's1', cursor: 42 } }) });
expect(ctx.getReconnectState().eventCount === 42, 'attach_ok 應以伺服器權威計數校準 eventCount');
expect(ctx.getReconnectState().reconnectAttempts === 0, 'attach_ok 應歸零重試計數');

// 進行中斷線 → 排程重連（sessionId 由 handleEvent 設定：餵一筆帶 session_id 的事件）
sock.onmessage({ data: JSON.stringify({ type: 'phase_change', session_id: 's1', payload: { phase: 'x' } }) });
const before = timers.length;
sock.onclose();
expect(timers.length === before + 1, '進行中（counting 且未 done）斷線應排程重連');
expect(ctx.getReconnectState().reconnectAttempts === 1, '重連次數 +1');

// 排程觸發 → 開新 socket 並送 attach 首訊息
const nSockets = sockets.length;
timers[timers.length - 1].fn();
expect(sockets.length === nSockets + 1, '重連應開新 WebSocket');
const re = sockets[sockets.length - 1];
re.onopen();
const first = JSON.parse(re.sent[0]);
expect(first.attach === 's1', '重連首訊息應帶 attach=sessionId');
expect(typeof first.cursor === 'number', '重連首訊息應帶 cursor 計數');

// 正常收尾（done）後 onclose 不重連
re.onmessage({ data: JSON.stringify({ type: 'done', session_id: 's1', payload: { completed: true } }) });
const before2 = timers.length;
re.onclose();
expect(timers.length === before2, '收尾 done 後斷線不應再排程重連');

// 未知事件型別不崩
ctx.trackSocketEvent({ type: 'no_such_type' });

console.log('OK: 重連延遲、計數起算/校準、onclose 分流皆正常');
process.exit(0);
