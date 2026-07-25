// 「需要你」收件匣(軌 F1):澄清票答覆走 unpark+note/停放取回/事件過濾呈現/badge/失敗降級。
import { install, expect } from './_frontend_env.mjs';

let apiOk = true;
let attention = {
  pending_clarify: 2,
  pending_admission_blocked: 1,
  clarify: [
    { id: 7, title: '歧義任務', clarify: '要部署到哪個環境?', updated_at: 100 },
    {
      id: 9,
      title: '契約缺 target',
      clarify: '要修改哪個檔案?',
      updated_at: 99,
      admission: {
        outcome: 'needs_clarification',
        overridable: true,
        scope_hash: 'a'.repeat(64),
        recommendation: '建議鎖定 studio/backlog.py',
      },
    },
  ],
  admission_blocked: [{
    id: 10,
    title: '外部寫入被阻擋',
    admission: {
      outcome: 'blocked',
      reasons: ['external_write_not_authorized'],
      recommendation: '不要繞過治理閘',
    },
  }],
  parked: [{ id: 8, title: '等外部依賴', note: '上游 API 未就緒', updated_at: 90 }],
  events: [{ kind: 'task_failed', title: '任務失敗一則', ts: 1700000000 }],
  deploy: { remote: 'da1646d6138e', reason: 'governance_evidence_required', deferrals: 42, first_deferred_at: 1784000000 },
  policy_blocked: [{ id: 653, title: '檢討 #504 治理層', note: '自治政策在 deploy 前拒絕：all_verdicts_must_approve', updated_at: 95 }],
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
expect(all.includes('澄清待答(2)'), '澄清區標題含數');
expect(all.includes('要部署到哪個環境?'), '澄清問題全文');
expect(all.includes('建議鎖定 studio/backlog.py'), 'admission 建議');
expect(all.includes('准入阻擋(1)') && all.includes('external_write_not_authorized'), '准入阻擋獨立呈現');
expect(all.includes('上游 API 未就緒'), '停放原因');
expect(all.includes('任務失敗') && !all.includes('daily_digest'), '事件標籤');
expect(all.includes('main 已前進到 da1646d6138e'), '部署漂移卡標題');
expect(all.includes('納管部署需審查證據'), '延後原因人話');
expect(all.includes('已延後 42 輪'), '延後輪數');
expect(all.includes('政策攔下(1)'), '政策攔下區標題含數');
expect(all.includes('all_verdicts_must_approve'), '政策攔下原因全文');
const badge = $('#snAttentionBadge');
expect(badge.textContent === '5' && !badge.classList.contains('hidden'), 'badge=澄清+准入阻擋+政策攔下+漂移卡');

// Admission 澄清走 scope-bound override，不沿用 generic unpark。
const inputs = walk(host, (e) => e.tag === 'textarea');
expect(inputs.length === 2, '每張可答澄清票一個答覆框');
inputs[1].value = '接受建議的低風險預設';
const initialBtns = walk(host, (e) => (e.className || '').includes('att-send'));
await initialBtns[1].onclick();
const overridePost = env.fetchCalls.find((c) => c.url.includes('/api/autopilot/task/9/admission-override'));
expect(!!overridePost && overridePost.method === 'POST', 'admission 答覆走 override endpoint');
expect(overridePost.body.scope_hash === 'a'.repeat(64), 'override 帶目前 scope hash');
expect(overridePost.body.reason === '接受建議的低風險預設', 'override 帶人工理由');

// Legacy 答覆流仍維持 POST unpark+note。
inputs[0].value = '部署到 staging';
attention = { pending_clarify: 0, pending_admission_blocked: 0, clarify: [], admission_blocked: [], parked: [], events: [] };
await initialBtns[0].onclick();
const post = env.fetchCalls.find((c) => c.url.includes('/api/autopilot/task/7/action'));
expect(!!post && post.method === 'POST', '答覆走 task action');
expect(post.body.action === 'unpark' && post.body.note === '部署到 staging', 'unpark+note 契約');
expect($('#snAttentionBadge').classList.contains('hidden'), '清空後 badge 隱藏');

// 空答不送出
await mod.renderAttention(); // 空狀態
expect(textOf($('#homeAttention')).includes('沒有待答的問題'), '空狀態文案');
expect(textOf($('#homeAttention')).includes('沒有等待中的部署'), '無漂移空狀態');
expect(textOf($('#homeAttention')).includes('沒有被自治政策攔下的任務'), '無政策攔下空狀態');

// 失敗降級
apiOk = false;
await mod.renderAttention();
expect(textOf($('#homeAttention')).includes('載入失敗'), '失敗降級');
console.log('OK');
