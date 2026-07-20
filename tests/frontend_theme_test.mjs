// 主題切換驗證：先掛全域 stub 再 import 真實 web/js/theme.js。
// 驗證三態循環（跟隨系統→淺→深）寫入 localStorage("ti-theme") 與
// documentElement.dataset.theme，且「跟隨系統」時採用系統偏好。
import { install, expect } from './_frontend_env.mjs';

const { $ } = install(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }));

// localStorage / matchMedia stub（本測試的主角）
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, v),
  removeItem: (k) => store.delete(k),
};
let prefersLight = false;
globalThis.window.matchMedia = (q) => ({
  matches: q.includes('light') ? prefersLight : !prefersLight,
  addEventListener() {},
  removeEventListener() {},
});

const mod = await import('../web/js/theme.js');
const root = globalThis.document.documentElement;

// 1) 無儲存值＋系統深色 → 跟隨系統＝深色（無 data-theme）
mod.initTheme();
expect(store.get('ti-theme') === undefined, '初始不應寫入 localStorage');
expect(root.dataset.theme === undefined, '系統深色時不應掛 data-theme');
expect($('#themeBtn').dataset.mode === 'system', '跟隨系統應標 data-mode=system');

// 2) 第一按 → 淺色
mod.toggleTheme();
expect(store.get('ti-theme') === 'light', '第一按應存 light');
expect(root.dataset.theme === 'light', '應掛 data-theme=light');
expect($('#themeBtn').dataset.mode === 'light', '淺色應標 data-mode=light');

// 3) 第二按 → 深色
mod.toggleTheme();
expect(store.get('ti-theme') === 'dark', '第二按應存 dark');
expect(root.dataset.theme === undefined, '深色應移除 data-theme');
expect($('#themeBtn').dataset.mode === 'dark', '深色應標 data-mode=dark');

// 4) 第三按 → 跟隨系統；系統偏好淺色時應套淺色
prefersLight = true;
mod.toggleTheme();
expect(store.get('ti-theme') === 'system', '第三按應存 system');
expect(root.dataset.theme === 'light', '跟隨系統＋系統偏好淺色應套 light');

// 5) localStorage 損壞（丟例外）不崩潰
globalThis.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
mod.initTheme(); // 不應 throw
mod.toggleTheme();

console.log('OK: 主題三態循環/持久化/跟隨系統/localStorage 容錯 皆正常');
process.exit(0);
