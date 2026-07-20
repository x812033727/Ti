// 推薦模型前端驗證：先掛全域 stub 再 import 真實 web/js/panels/settings.js，驗證
// renderSettings() 對帶 recommended 的 select 加「（推薦）」尾綴與 data-recommended，
// 且 applyRecommendedSettings() 一鍵把這些欄位填成推薦值（不動沒有推薦值的欄位）、更新 hint。

class RecEl {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this._attrs = {};
    this.className = '';
    this.textContent = '';
    this.value = '';
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  appendChild(c) {
    this.children.push(c);
    // 模擬真實 DOM：select 的 value 反映被 selected 的 option
    if (c.tag === 'option' && c.selected) this.value = c.value;
    return c;
  }
  prepend(c) { this.children.unshift(c); return c; }
  setAttribute(k, v) { this._attrs[k] = v; }
  set innerHTML(v) { this._inner = v; if (v === '') this.children = []; }
  get innerHTML() { return this._inner || ''; }
  // applyRecommendedSettings 用 settingsForm.querySelectorAll("[data-env]")：遞迴蒐集
  querySelectorAll(sel) {
    const out = [];
    const walk = (el) => {
      for (const c of el.children || []) {
        if (sel === '[data-env]' && c.dataset && c.dataset.env) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
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

const noop = () => {};
const windowObj = { addEventListener: noop, matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }), location: { protocol: 'http:', host: 'x', href: '' } };
function WebSocket() { return new RecEl('ws'); }

Object.assign(globalThis, {
  document: {
    querySelector: (s) => $(s),
    querySelectorAll: () => [],
    createElement: (t) => new RecEl(t),
    createElementNS: (_ns, t) => new RecEl(t),
    createTextNode: () => new RecEl('text'),
    getElementById: () => new RecEl('div'),
    body: new RecEl('body'),
  },
  window: windowObj, location: windowObj.location, WebSocket,
  fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
  setTimeout: noop, setInterval: noop, clearTimeout: noop, clearInterval: noop,
});

const ctx = await import('../web/js/panels/settings.js');

const FIELDS = [
  {
    env: 'TI_MODEL_ENGINEER', label: '工程師模型', kind: 'select', secret: false,
    options: ['auto', 'claude-fable-5', 'claude-sonnet-4-6'],
    placeholder: '', group: 'Claude', recommended: 'claude-fable-5', value: 'auto', set: false,
  },
  {
    env: 'TI_PROVIDER', label: '後端 Provider', kind: 'select', secret: false,
    options: ['claude', 'minimax'],
    placeholder: '', group: '一般', recommended: '', value: 'claude', set: true,
  },
];

ctx.renderSettings(FIELDS);

const inputs = new Map(FORM.querySelectorAll('[data-env]').map((el) => [el.dataset.env, el]));
const fails = [];
function check(cond, msg) { if (!cond) fails.push(msg); }

const eng = inputs.get('TI_MODEL_ENGINEER');
check(eng, '應渲染角色模型欄位');
check(eng.dataset.recommended === 'claude-fable-5', 'data-recommended 應帶推薦值');
const recOpt = eng.children.find((o) => o.value === 'claude-fable-5');
check(recOpt && recOpt.textContent.includes('（推薦）'), '推薦選項應有（推薦）尾綴');
const autoOpt = eng.children.find((o) => o.value === 'auto');
check(autoOpt && !autoOpt.textContent.includes('推薦'), '非推薦選項不該有尾綴');
check(inputs.get('TI_PROVIDER').dataset.recommended === '', '無推薦值欄位 data-recommended 為空');

// 一鍵套用：只填有推薦值的欄位
ctx.applyRecommendedSettings();
check(eng.value === 'claude-fable-5', '套用後角色模型應為推薦值');
check(inputs.get('TI_PROVIDER').value === 'claude', '無推薦值欄位不應被改動');
check($('#settingsHint').textContent.includes('已填入推薦'), 'hint 應提示已填入推薦');

// 再按一次：已是推薦配置
ctx.applyRecommendedSettings();
check($('#settingsHint').textContent.includes('已是推薦配置'), '重複套用應提示已是推薦配置');

if (fails.length) { console.error('FAIL:\n' + fails.join('\n')); process.exit(1); }
console.log('OK: 推薦尾綴、data-recommended、一鍵套用皆正常');
process.exit(0);
