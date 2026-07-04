// 任務 #3 前端驗證：模擬真實「填入 token → 按儲存」流程。
// 先掛全域 stub 再 import 真實 web/js/panels/settings.js，renderSettings() 產生欄位
// → 模擬使用者在秘密欄位輸入測試 token → 呼叫真實 saveSettings() → 攔截 fetch 取得
// POST /api/settings 的 payload 與回應 {ok:true} 後的 UI 狀態（hint 文字 + 成功 toast）。
//
// 用法：node frontend_settings_save_test.mjs <fields.json>
import fs from 'node:fs';

const fields = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const TEST_TOKEN = 'ghp_TEST_frontend_0001';

class RecEl {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.options = [];
    this.className = ''; this.textContent = ''; this._inner = '';
    this.type = ''; this.value = ''; this.placeholder = ''; this._attrs = {};
  }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this._attrs[k] = v; }
  getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; }
  remove() {}
  set innerHTML(v) { this._inner = v; if (v === '') this.children = []; }
  get innerHTML() { return this._inner; }
  _descendants(acc = []) {
    for (const c of this.children) { acc.push(c); if (c._descendants) c._descendants(acc); }
    return acc;
  }
  querySelectorAll(sel) {
    if (sel === '[data-env]') return this._descendants().filter((e) => e.dataset && e.dataset.env);
    return [];
  }
  querySelector() { return new RecEl('div'); }
  classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  addEventListener() {}
}

const singletons = new Map();
function $(sel) {
  if (!singletons.has(sel)) singletons.set(sel, new RecEl('stub'));
  return singletons.get(sel);
}
const FORM = $('#settingsForm');
const HINT = $('#settingsHint');
const TOAST = $('#toast');

const document = {
  querySelector: (s) => $(s),
  querySelectorAll: () => [],
  createElement: (t) => new RecEl(t),
  createTextNode: () => new RecEl('text'),
  getElementById: () => new RecEl('div'),
  body: new RecEl('body'),
};
const noop = () => {};

// --- fetch stub：記錄呼叫，/api/settings 回 {ok:true,...}、/api/health 回 {} ---
const calls = [];
function fetchStub(url, opts) {
  calls.push({ url, opts });
  let payload = {};
  if (opts && opts.body) { try { payload = JSON.parse(opts.body); } catch {} }
  let res;
  if (url === '/api/settings') {
    // 模擬後端：回 {ok:true} 並把秘密欄位 value 清空、set 反映是否有值
    const echo = fields.map((f) => ({
      ...f,
      value: f.secret ? '' : (payload[f.env] ?? f.value),
      set: f.secret ? (f.set || (payload[f.env] ?? '') !== '') : ((payload[f.env] ?? f.value) !== ''),
    }));
    res = { ok: true, fields: echo };
  } else if (url === '/api/health') {
    res = { ok: true, has_api_key: true, offline: false };
  } else { res = {}; }
  return Promise.resolve({ json: () => Promise.resolve(res) });
}

Object.assign(globalThis, {
  document, window: { addEventListener: noop, matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }), location: { protocol: 'http:', host: 'x', href: '' } },
  location: { protocol: 'http:', host: 'x', href: '' }, WebSocket: function () { return new RecEl('ws'); },
  fetch: fetchStub, setTimeout: (f) => 0, setInterval: () => 0,
  clearTimeout: noop, clearInterval: noop,
});
const ctx = await import('../web/js/panels/settings.js');

// --- 走查：渲染 → 使用者在 GITHUB_TOKEN 填值 → 按儲存 ---
ctx.renderSettings(fields);
const inputs = FORM.querySelectorAll('[data-env]');
const tokenInput = inputs.find((e) => e.dataset.env === 'GITHUB_TOKEN');
if (!tokenInput) { console.error('FAIL: 找不到 GITHUB_TOKEN 輸入框'); process.exit(1); }
tokenInput.value = TEST_TOKEN; // 模擬使用者鍵入

await ctx.saveSettings();

const fails = [];
const ck = (c, m) => { if (!c) fails.push(m); };

const postCall = calls.find((c) => c.url === '/api/settings' && c.opts && c.opts.method === 'POST');
ck(postCall, '未發出 POST /api/settings');
let body = {};
if (postCall) {
  ck(postCall.opts.headers['Content-Type'] === 'application/json', 'POST 缺 JSON Content-Type');
  body = JSON.parse(postCall.opts.body);
  ck(body.GITHUB_TOKEN === TEST_TOKEN, `payload 應含 GITHUB_TOKEN=${TEST_TOKEN}，實為 ${JSON.stringify(body.GITHUB_TOKEN)}`);
  // 未填的秘密欄位不應送出（留空＝不變更）
  ck(!('ANTHROPIC_API_KEY' in body), '未填的 ANTHROPIC_API_KEY 不該被送出');
  ck(!('OPENAI_API_KEY' in body), '未填的 OPENAI_API_KEY 不該被送出');
}
// UI 成功狀態
ck(HINT.textContent === '已儲存，下次討論即生效。', `hint 應顯示成功，實為「${HINT.textContent}」`);
const okToast = TOAST.children.find((c) => c.textContent === '設定已儲存');
ck(okToast, '未出現「設定已儲存」toast');

if (fails.length) { console.error('FRONTEND_SAVE_FAIL\n' + fails.map((f) => ' - ' + f).join('\n')); process.exit(1); }
console.log(`FRONTEND_SAVE_OK 送出 payload 鍵=[${Object.keys(body).join(',')}]，UI hint=「${HINT.textContent}」，toast 已觸發`);
