// 排程任務頁(PR11):recurrenceFromForm 純函式、列表渲染(頻率文案/停用態)、
// 建立表單 POST payload、403 明確提示。真實模組+RecEl stub+driveModal。
import { install, expect, driveModal } from './_frontend_env.mjs';

let schedules = [
  { id: 'a1', title: '每週營運摘要', enabled: true, recurrence: { kind: 'weekly', time: '09:00', weekday: 0 } },
  { id: 'a2', title: '每日健檢', enabled: false, recurrence: { kind: 'daily', time: '08:00' } },
];
let writeStatus = 200;
const { $, body, fetchCalls } = install((url, opts = {}) => {
  const method = opts.method || 'GET';
  if (url.includes('/api/schedules') && method !== 'GET') {
    return Promise.resolve({ ok: writeStatus === 200, status: writeStatus, json: () => Promise.resolve({}) });
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ schedules }) });
});

const mod = await import('../web/js/panels/schedules.js');

// 1) recurrenceFromForm
expect(mod.recurrenceFromForm({ kind: 'daily', time: '07:30' }).time === '07:30', 'daily 帶時刻');
const w = mod.recurrenceFromForm({ kind: 'weekly', time: '09:00', weekday: '3' });
expect(w.weekday === 3, 'weekly weekday 轉數字');
expect(mod.recurrenceFromForm({ kind: 'interval_hours', hours: '6' }).hours === 6, 'interval 轉數字');

// 2) 列表渲染
await mod.renderSchedules();
function textOf(el) { let s = el.textContent || ''; for (const c of el.children || []) s += textOf(c); return s; }
const all = textOf($('#homeSchedules'));
expect(all.includes('每週一 09:00'), '週頻率文案');
expect(all.includes('每日 08:00'), '日頻率文案');
expect(all.includes('已停用'), '停用標示');

// 3) 建立流程:表單 → POST payload 正確
const p = mod.createScheduleFlow();
await new Promise((r) => queueMicrotask(r));
driveModal(body, (inputs) => {
  inputs.get('title').value = '新排程';
  inputs.get('detail').value = '做點事';
  inputs.get('time').value = '10:00';
});
await p;
const post = fetchCalls.find((c) => c.url === '/api/schedules' && c.method === 'POST');
expect(!!post, '應 POST /api/schedules');
expect(post.body.title === '新排程' && post.body.recurrence.kind === 'daily' && post.body.recurrence.time === '10:00',
  'payload 含 recurrence(預設 daily+表單時刻)');

// 4) 403 → 明確提示(不拋)
writeStatus = 403;
const p2 = mod.createScheduleFlow();
await new Promise((r) => queueMicrotask(r));
driveModal(body, (inputs) => { inputs.get('title').value = 'x'; });
await p2;

console.log('OK');
