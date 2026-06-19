// 任務 #2 前端驗證：載入真實 web/app.js 的 renderSettings()，用記錄式 DOM
// 實際渲染欄位，驗證 token／API key 欄位 → password 型態、不預填明文、
// 未設定時 placeholder 用 f.placeholder、已設定秘密欄位顯示「已設定（留空＝不變更）」。
//
// 用法：node frontend_settings_render_test.mjs <fields.json>
// fields.json 為後端 /api/settings 回傳的 fields 陣列（真實資料）。
import fs from 'node:fs';
import vm from 'node:vm';

const fieldsPath = process.argv[2];
const fields = JSON.parse(fs.readFileSync(fieldsPath, 'utf8'));

// --- 記錄式 DOM：真的把 type/value/placeholder/textContent/children 存下來供斷言 ---
class RecEl {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this._attrs = {};
    this.options = [];
    this.className = '';
    this.textContent = '';
    this.innerHTML = '';
    // app.js 頂層即會呼叫 setMobileView → closeSettings → classList.add（#84 手機分頁）
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  // 手機分頁導覽（#84）於 app.js 載入時即呼叫 setMobileView → open/closeSettings 操作
  // classList；stub 一律 no-op（與 save harness 對齊），避免載入期 TypeError。
  classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  appendChild(c) {
    if (this.innerHTML === '') {} // 不清空既有 children
    this.children.push(c);
    return c;
  }
  setAttribute(k, v) { this._attrs[k] = v; }
  getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; }
  set innerHTML(v) { this._inner = v; if (v === '') this.children = []; }
  get innerHTML() { return this._inner || ''; }
  querySelectorAll() { return []; }
  querySelector() { return new RecEl('div'); }
  addEventListener() {}
}

const FORM = new RecEl('form');
const els = new Map();
function $(sel) {
  if (sel === '#settingsForm') return FORM;
  if (!els.has(sel)) els.set(sel, new RecEl('stub'));
  return els.get(sel);
}

const document = {
  querySelector: (s) => $(s),
  querySelectorAll: () => [],
  createElement: (t) => new RecEl(t),
  createTextNode: () => new RecEl('text'),
  getElementById: () => new RecEl('div'),
  body: new RecEl('body'),
};
const noop = () => {};
const windowObj = { addEventListener: noop, matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }), location: { protocol: 'http:', host: 'x', href: '' } };
function WebSocket() { return new RecEl('ws'); }

const ctx = vm.createContext({
  document, window: windowObj, location: windowObj.location, WebSocket,
  fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
  console, setTimeout: noop, setInterval: noop, clearTimeout: noop, clearInterval: noop,
});

const src = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
vm.runInContext(src, ctx, { filename: 'app.js' });

// renderSettings 是 function 宣告 → 已掛到 context 全域；FORM 是它寫入的目標
ctx.renderSettings(fields);

// 收集渲染後的 input/select 列（依 data-env 對應回欄位）
const rendered = new Map();
function walkRows(el, currentRow = null) {
  const row = el.className === 'set-row' ? el : currentRow;
  for (const c of el.children || []) {
    if (c.dataset && c.dataset.env) rendered.set(c.dataset.env, { row, input: c });
    walkRows(c, row);
  }
}
walkRows(FORM);

const fails = [];
function check(cond, msg) { if (!cond) fails.push(msg); }

// 找出秘密 token 類欄位（驗收 #2 明確點名 GITHUB_TOKEN / ANTHROPIC_API_KEY）
const mustHave = ['GITHUB_TOKEN', 'ANTHROPIC_API_KEY'];
for (const env of mustHave) {
  const f = fields.find((x) => x.env === env);
  check(f, `欄位清單缺少 ${env}`);
  if (!f) continue;
  const r = rendered.get(env);
  check(r, `${env} 未被渲染出 input`);
  if (!r) continue;
  check(r.input.tag === 'input', `${env} 應為 input，實為 ${r.input.tag}`);
  check(r.input.type === 'password', `${env} type 應為 password，實為 ${r.input.type}`);
  check(r.input.value === '', `${env} 不該預填明文，value 實為 ${JSON.stringify(r.input.value)}`);
  check(r.input.dataset.secret === '1', `${env} dataset.secret 應為 '1'`);
  // label 文字應出現在該 row 子節點
  const labelText = r.row.children.map((c) => c.textContent).join('|');
  check(labelText.includes(f.label), `${env} label 未渲染（期望含「${f.label}」）`);
  // 未設定 → placeholder 用 f.placeholder；已設定 → 提示文字
  const expectPh = (f.secret && f.set) ? '已設定（留空＝不變更）' : (f.placeholder || '');
  check(r.input.placeholder === expectPh,
    `${env} placeholder 應為「${expectPh}」，實為「${r.input.placeholder}」`);
}

// 全體秘密欄位通則：一律 password 型態、value 空
for (const f of fields.filter((x) => x.secret)) {
  const r = rendered.get(f.env);
  if (!r) { fails.push(`秘密欄位 ${f.env} 未渲染`); continue; }
  check(r.input.type === 'password', `秘密欄位 ${f.env} 非 password 型態`);
  check(r.input.value === '', `秘密欄位 ${f.env} 預填了明文`);
}

if (fails.length) {
  console.error('FRONTEND_FAIL\n' + fails.map((f) => ' - ' + f).join('\n'));
  process.exit(1);
}
console.log(`FRONTEND_OK 已渲染 ${rendered.size} 欄位，秘密欄位 ${fields.filter((x) => x.secret).length} 個全為 password 且未預填明文`);
