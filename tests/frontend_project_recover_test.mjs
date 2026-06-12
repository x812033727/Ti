// 中斷恢復按鈕前端驗證：載入真實 web/app.js，backlog 有 in_progress 任務時
// 面板應渲染「恢復」按鈕；點擊後 POST /recover，成功且尚有待辦 → 自動以
// improve 模式重啟（勾選持續改良＋建立 WebSocket）。
import fs from 'node:fs';
import vm from 'node:vm';

// --- 記錄式 DOM（同 frontend_project_panel_test 範式）---
class RecEl {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.className = '';
    this.textContent = '';
    this.value = '';
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

let interrupted = true; // recover 後翻 false，模擬後端已重置
const fixture = () => ({
  project: { id: 'p1', name: '產品X' },
  blueprint: null,
  backlog: [
    { id: 1, title: '被中斷的任務', status: interrupted ? 'in_progress' : 'pending', priority: 0, type: 'bug', source: 'user' },
  ],
  counts: interrupted ? { pending: 0, in_progress: 1 } : { pending: 1, in_progress: 0 },
});

const calls = [];
let wsCount = 0;
const noop = () => {};
const windowObj = { addEventListener: noop, location: { protocol: 'http:', host: 'x', href: '' } };
function WebSocket() { wsCount += 1; return new RecEl('ws'); }

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
  fetch: (url, opts = {}) => {
    calls.push(`${opts.method || 'GET'} ${url}`);
    if (String(url).endsWith('/recover')) {
      interrupted = false;
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ok: true, reset: 1, counts: { pending: 1, in_progress: 0 } }),
      });
    }
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(String(url).includes('/api/projects/p1') ? fixture() : {}),
    });
  },
  console, setTimeout: noop, setInterval: noop, clearTimeout: noop, clearInterval: noop,
});

const src = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
vm.runInContext(src, ctx, { filename: 'app.js' });

function expect(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg + '\nfetch 紀錄：\n' + calls.join('\n')); process.exit(1); }
}

$('#projectSelect').value = 'p1';
await ctx.refreshProjectPanel();

const findRecover = () => $('#projectBody').children.find((c) => c.id === 'projectRecover');
const btn = findRecover();
expect(btn, '有 in_progress 任務時應渲染恢復按鈕');

await btn.onclick();

expect(calls.some((c) => c === 'POST /api/projects/p1/recover'), '點擊應 POST recover');
expect($('#improveChk').checked === true, '恢復後應勾選持續改良');
expect(wsCount === 1, '恢復後應自動建立 WebSocket 重啟迴圈');
expect(!findRecover(), '任務重置回 pending 後按鈕應消失');

console.log('OK: 恢復按鈕渲染、recover API、自動重啟皆正常');
process.exit(0);
