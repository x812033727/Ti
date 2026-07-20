// 「需要你」收件匣(軌 F1):澄清票答覆走 unpark+note/停放取回/事件過濾呈現/badge/失敗降級。
import { install, expect } from './_frontend_env.mjs';

let apiOk = true;
let attention = {
  pending_clarify: 1,
  clarify: [{ id: 7, title: '歧義任務', clarify: '要部署到哪個環境?', updated_at: 100 }],
  parked: [{ id: 8, title: '等外部依賴', note: '上游 API 未就緒', updated_at: 90 }],
  events: [{ kind: 'task_failed', title: '任務失敗一則', ts: 1700000000 }],
};
const env = install((url, opts = {}) => {
  if (!apiOk) return Promise.reject(new Error('down'));
  if (url.includes('/action')) return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
  return Promise.resolve({ ok: true, json: () => Promise.resolve(attention) });
});

const mod = await import('../web/js/panels/attention.js');
const { $ } = await import('../web/js/dom.js');

function textOf(el) { let s = el.textContent || ''; for (const c of el.children || []) s += textOf(c); return s; }
function walk(el, pred, acc = []) { if (pred(el)) acc.push(el); for (const c of el.children || []) walk(c, pred, acc); return acc; }

await mod.renderAttention();
const host = $('#homeAttention');
const all = textOf(host);
expect(all.includes('澄清待答(1)'), '澄清區標題含數');
expect(all.includes('要部署到哪個環境?'), '澄清問題全文');
expect(all.includes('上游 API 未就緒'), '停放原因');
expect(all.includes('任務失敗') && !all.includes('daily_digest'), '事件標籤');
const badge = $('#snAttentionBadge');
expect(badge.textContent === '1' && !badge.classList.contains('hidden'), 'badge 顯示待答數');

// 答覆流:填 textarea → 按鈕 → POST unpark+note
const inputs = walk(host, (e) => e.tag === 'textarea');
expect(inputs.length === 1, '一張澄清票一個答覆框');
inputs[0].value = '部署到 staging';
const sendBtns = walk(host, (e) => (e.className || '').includes('att-send'));
attention = { pending_clarify: 0, clarify: [], parked: [], events: [] };
await sendBtns[0].onclick();
const post = env.fetchCalls.find((c) => c.url.includes('/api/autopilot/task/7/action'));
expect(!!post && post.method === 'POST', '答覆走 task action');
expect(post.body.action === 'unpark' && post.body.note === '部署到 staging', 'unpark+note 契約');
expect($('#snAttentionBadge').classList.contains('hidden'), '清空後 badge 隱藏');

// 空答不送出
await mod.renderAttention(); // 空狀態
expect(textOf($('#homeAttention')).includes('沒有待答的問題'), '空狀態文案');

// 失敗降級
apiOk = false;
await mod.renderAttention();
expect(textOf($('#homeAttention')).includes('載入失敗'), '失敗降級');
console.log('OK');
