// 專案面板前端驗證：載入真實 web/app.js 的 refreshProjectPanel()，用記錄式 DOM
// 實際渲染，驗證藍圖功能按 P0→P2 排序、優先級徽章、backlog 列含類型/來源。
import fs from 'node:fs';
import vm from 'node:vm';

// --- 記錄式 DOM（同 frontend_settings_render_test 範式）---
class RecEl {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.className = '';
    this.textContent = '';
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  appendChild(c) { this.children.push(c); return c; }
  prepend(c) { this.children.unshift(c); return c; }
  set innerHTML(v) { this._inner = v; if (v === '') this.children = []; }
  get innerHTML() { return this._inner || ''; }
  querySelectorAll() { return []; }
  querySelector() { return new RecEl('div'); }
  addEventListener() {}
}

const els = new Map();
function $(sel) {
  if (!els.has(sel)) els.set(sel, new RecEl('stub'));
  return els.get(sel);
}

const FIXTURE = {
  project: { id: 'p1', name: '產品X', vision: '一句願景' },
  blueprint: {
    version: 1,
    vision: '一句願景',
    users: '目標用戶',
    features: [
      { title: '加分功能', priority: 2 },
      { title: '核心功能', priority: 0, detail: '最重要' },
    ],
    milestones: [{ title: 'M1 核心可用' }],
  },
  backlog: [
    { id: 2, title: '緊急修復', status: 'pending', priority: 0, type: 'bug', source: 'user' },
    { id: 1, title: '普通改良', status: 'done', priority: 1, type: 'improvement', source: 'eval' },
  ],
  counts: { pending: 1, done: 1 },
};

const noop = () => {};
const windowObj = { addEventListener: noop, location: { protocol: 'http:', host: 'x', href: '' } };
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
  fetch: (url) =>
    Promise.resolve({
      ok: true,
      json: () => Promise.resolve(String(url).includes('/api/projects/p1') ? FIXTURE : {}),
    }),
  console, setTimeout: noop, setInterval: noop, clearTimeout: noop, clearInterval: noop,
});

const src = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
vm.runInContext(src, ctx, { filename: 'app.js' });

$('#projectSelect').value = 'p1';
await ctx.refreshProjectPanel();

const body = $('#projectBody');
const texts = body.children.map((c) => {
  const badge = c.children?.[0]?.className?.startsWith?.('prio') ? c.children[0].textContent : '';
  return badge + c.textContent;
});

function expect(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg + '\n實際渲染：\n' + texts.join('\n')); process.exit(1); }
}

expect(texts.some((t) => t.includes('產品X')), '應渲染專案名稱');
expect(texts.some((t) => t.includes('願景：一句願景')), '應渲染藍圖願景');
const p0 = texts.findIndex((t) => t.startsWith('P0') && t.includes('核心功能'));
const p2 = texts.findIndex((t) => t.startsWith('P2') && t.includes('加分功能'));
expect(p0 !== -1 && p2 !== -1 && p0 < p2, '藍圖功能應按 P0→P2 排序且帶優先級徽章');
expect(texts.some((t) => t.includes('M1 核心可用')), '應渲染里程碑');
expect(texts.some((t) => t.startsWith('P0') && t.includes('緊急修復') && t.includes('缺陷')), 'backlog 列應含優先級徽章與類型');
expect(texts.some((t) => t.includes('✅ #1 普通改良')), 'backlog 列應含狀態圖示');

console.log('OK: 專案面板渲染藍圖與排序 backlog 正常');
process.exit(0);
