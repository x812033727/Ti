// 前端模組測試共用環境：記錄式 DOM stub + 全域安裝。
// 用法：install() 掛好 globalThis 後再 await import('../web/js/…')——
// 模組頂層不查 DOM（repo 鐵則），import 期不會踩 stub 缺口。
export class RecEl {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this._attrs = {};
    this.className = '';
    this.textContent = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.readOnly = false;
    this.type = '';
    this.placeholder = '';
    this.title = '';
    this.classList = {
      _set: new Set(),
      add: (...c) => c.forEach((x) => this.classList._set.add(x)),
      remove: (...c) => c.forEach((x) => this.classList._set.delete(x)),
      toggle: (c, force) => {
        const on = force === undefined ? !this.classList._set.has(c) : force;
        if (on) this.classList._set.add(c); else this.classList._set.delete(c);
      },
      contains: (c) => this.classList._set.has(c),
    };
  }
  appendChild(c) {
    this.children.push(c);
    // 模擬真實 DOM：select 的 value 反映被 selected 的 option
    if (c.tag === 'option' && c.selected) this.value = c.value;
    return c;
  }
  prepend(c) { this.children.unshift(c); return c; }
  remove() { this._removed = true; }
  close() { this._closed = true; }
  showModal() { this._modal = true; }
  focus() {}
  setAttribute(k, v) { this._attrs[k] = v; }
  getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; }
  addEventListener(type, fn) { (this._listeners ||= {})[type] = fn; }
  set innerHTML(v) { this._inner = v; if (v === '') this.children = []; }
  get innerHTML() { return this._inner || ''; }
  get options() { return this.children; }
  // 遞迴蒐集：支援 '[data-key]' / 'tag' / '[data-env]' 三種最常用查詢
  _descendants(acc = []) {
    for (const c of this.children) { acc.push(c); if (c._descendants) c._descendants(acc); }
    return acc;
  }
  querySelectorAll(sel) {
    const all = this._descendants();
    if (sel === '[data-key]') return all.filter((e) => e.dataset && e.dataset.key);
    if (sel === '[data-env]') return all.filter((e) => e.dataset && e.dataset.env);
    return all.filter((e) => e.tag === sel);
  }
  querySelector(sel) {
    const m = /^(\w+)\[data-key="(.+)"\]$/.exec(sel) || /^\[data-key="(.+)"\]$/.exec(sel);
    if (m) {
      const key = m[2] ?? m[1];
      const tag = m[2] ? m[1] : null;
      const hit = this._descendants().find(
        (e) => e.dataset && e.dataset.key === key && (!tag || e.tag === tag),
      );
      return hit || null;
    }
    const hit = this._descendants().find((e) => e.tag === sel || (sel.startsWith('.') && String(e.className).split(' ').includes(sel.slice(1))));
    return hit || new RecEl('div');
  }
}

// 安裝全域 stub。回傳 { $, els, body, fetchCalls }：
// - els：'#id' → 單例 RecEl（跨呼叫共享，模擬固定 DOM）
// - fetchImpl(url, opts)：由測試提供；所有呼叫記進 fetchCalls
export function install(fetchImpl) {
  const els = new Map();
  const $ = (sel) => {
    if (!els.has(sel)) els.set(sel, new RecEl('stub'));
    return els.get(sel);
  };
  const body = new RecEl('body');
  const fetchCalls = [];
  const noop = () => {};
  const windowObj = {
    addEventListener: noop,
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    location: { protocol: 'http:', host: 'x', href: '' },
  };
  Object.assign(globalThis, {
    document: {
      querySelector: (s) => $(s),
      querySelectorAll: () => [],
      createElement: (t) => new RecEl(t),
      createTextNode: (t) => { const e = new RecEl('text'); e.textContent = t; return e; },
      getElementById: () => new RecEl('div'),
      body,
      documentElement: new RecEl('html'),
      activeElement: null,
      addEventListener: noop,
    },
    window: windowObj,
    location: windowObj.location,
    WebSocket: function () { return new RecEl('ws'); },
    fetch: (url, opts = {}) => {
      fetchCalls.push({ url: String(url), method: (opts.method || 'GET'), body: opts.body ? JSON.parse(opts.body) : null });
      return fetchImpl(String(url), opts);
    },
    confirm: () => true,
    setTimeout: noop, setInterval: noop, clearTimeout: noop, clearInterval: noop,
  });
  return { $, els, body, fetchCalls };
}

// 驅動 openFormModal 建出的 <dialog>：填欄位 → 送出（回傳 dialog 以便檢查）
export function driveModal(body, fill) {
  const dlg = body.children.filter((c) => c.tag === 'dialog').at(-1);
  if (!dlg) throw new Error('找不到 modal dialog');
  const inputs = new Map(dlg.querySelectorAll('[data-key]').map((e) => [e.dataset.key, e]));
  fill(inputs, dlg);
  const form = dlg.children.find((c) => c.tag === 'form');
  form.onsubmit({ preventDefault() {} });
  return dlg;
}

export function expect(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}
