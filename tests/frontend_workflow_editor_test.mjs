// 動態流程編輯器前端驗證：載入真實 web/app.js，用記錄式 DOM + 攔截 fetch，驗證
// loadWorkflowPanel 渲染清單、renderWorkflowSelection 帶出 stages、saveWorkflow 對既有
// 流程走 PUT、對新流程走 POST，且預設流程唯讀。
import fs from 'node:fs';
import vm from 'node:vm';

class RecEl {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.className = '';
    this.textContent = '';
    this.value = '';
    this.readOnly = false;
    this.disabled = false;
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  appendChild(c) { this.children.push(c); return c; }
  prepend(c) { this.children.unshift(c); return c; }
  set innerHTML(v) { this._inner = v; if (v === '') this.children = []; }
  get innerHTML() { return this._inner || ''; }
  get options() { return this.children; } // <select> 的 options＝其子 <option>
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

const DEFAULT_WF = { name: '預設流程', description: '內建', stages: [{ type: 'clarify' }, { type: 'demo' }] };
const CUSTOM_WF = { name: '快速原型', description: '精簡', stages: [{ type: 'demo' }] };

const calls = [];
function fetchStub(url, opts = {}) {
  const method = opts.method || 'GET';
  calls.push({ url: String(url), method, body: opts.body ? JSON.parse(opts.body) : null });
  if (String(url) === '/api/workflows' && method === 'GET') {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ workflows: [DEFAULT_WF, CUSTOM_WF] }) });
  }
  // POST/PUT/DELETE → 回成功
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true, workflow: opts.body ? JSON.parse(opts.body) : {} }) });
}

const noop = () => {};
const windowObj = { addEventListener: noop, matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }), location: { protocol: 'http:', host: 'x', href: '' } };
function WebSocket() { return new RecEl('ws'); }

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
  fetch: fetchStub,
  confirm: () => true,
  console, setTimeout: noop, setInterval: noop, clearTimeout: noop, clearInterval: noop,
});

const src = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
vm.runInContext(src, ctx, { filename: 'app.js' });

function expect(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

// 1) 載入清單：兩個選項、預設被選中且唯讀
await ctx.loadWorkflowPanel();
const list = $('#workflowList');
expect(list.children.length === 2, '流程清單應有 2 個選項');
expect(list.children.map((o) => o.value).includes('快速原型'), '清單應含客製流程');
expect($('#workflowName').value === '預設流程', '預設流程應被選中');
expect($('#workflowStages').readOnly === true, '預設流程應唯讀');
expect($('#workflowSave').disabled === true, '預設流程儲存鈕應停用');

// 2) 切到客製流程：帶出 stages、可編輯
list.value = '快速原型';
ctx.renderWorkflowSelection();
expect($('#workflowStages').value === JSON.stringify(CUSTOM_WF.stages, null, 2), '應帶出客製流程的 stages JSON');
expect($('#workflowStages').readOnly === false, '客製流程應可編輯');

// 3) 儲存既有流程 → PUT /api/workflows/快速原型
calls.length = 0;
$('#workflowName').value = '快速原型';
$('#workflowDesc').value = '改版';
$('#workflowStages').value = JSON.stringify([{ type: 'wrap_up' }]);
await ctx.saveWorkflow();
const put = calls.find((c) => c.method === 'PUT');
expect(put && put.url === '/api/workflows/' + encodeURIComponent('快速原型'), '既有流程應走 PUT');
expect(put.body.stages[0].type === 'wrap_up' && put.body.description === '改版', 'PUT body 應含新 stages/description');

// 4) 新流程 → POST /api/workflows
calls.length = 0;
ctx.newWorkflow();
$('#workflowName').value = '全新流程';
$('#workflowStages').value = JSON.stringify([{ type: 'demo' }]);
await ctx.saveWorkflow();
const post = calls.find((c) => c.method === 'POST');
expect(post && post.url === '/api/workflows', '新流程應走 POST');
expect(post.body.name === '全新流程' && post.body.stages[0].type === 'demo', 'POST body 應含 name 與 stages');

// 5) 非法 JSON → 不送出（不應有 POST/PUT）
calls.length = 0;
ctx.newWorkflow();
$('#workflowName').value = '壞 JSON';
$('#workflowStages').value = '[{type: 不是合法}';
await ctx.saveWorkflow();
expect(!calls.some((c) => c.method === 'POST' || c.method === 'PUT'), '非法 JSON 不應送出請求');

console.log('OK: 動態流程編輯器 載入/切換/PUT/POST/JSON 防呆 正常');
process.exit(0);
