// 交辦任務卡片(PR6):taskCardModel 純函式、卡片建立/更新、交辦流程(fetch mock)、
// 401 降級提示。真實模組+RecEl stub。
import { install, expect } from './_frontend_env.mjs';

let postResp = { ok: true, status: 200, json: () => Promise.resolve({ ok: true, task: { id: 42 } }) };
const { $, fetchCalls } = install((url, opts = {}) => {
  if (url.includes('/api/autopilot/task') && (opts.method || 'GET') === 'POST') return Promise.resolve(postResp);
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ sessions: [], tasks: [] }) });
});

const tc = await import('../web/js/components/taskcard.js');
const home = await import('../web/js/panels/home.js');

// 1) taskCardModel:狀態映射/終局/直播 sid
let m = tc.taskCardModel({ status: 'pending' });
expect(m.label === '排隊中' && !m.terminal && !m.liveSid, 'pending 模型');
m = tc.taskCardModel({ status: 'in_progress', session_id: 'ap1', pr: 7, note: 'x' });
expect(m.liveSid === 'ap1' && m.sub.includes('PR #7'), 'in_progress 帶直播 sid 與 PR');
m = tc.taskCardModel({ status: 'done' });
expect(m.terminal && m.cls === 'ok', 'done=終局');
m = tc.taskCardModel({ status: 'failed' });
expect(m.terminal && m.cls === 'bad', 'failed=終局');

// 2) 卡片建立與更新
const card = tc.createTaskCard(42, '做每週報表');
expect(card.dataset.taskId === '42', '卡片帶任務 id');
const done = tc.updateTaskCard(card, { status: 'in_progress', session_id: 'ap9' });
expect(done === false, '非終局回 false(輪詢續跑)');
expect(tc.updateTaskCard(card, { status: 'done' }) === true, '終局回 true(輪詢停)');

// 3) 交辦模式:POST 正確 payload、成功插卡片
home.setHeroMode('task');
expect(home.getHeroMode() === 'task', '模式切換');
$('#heroInput').value = '做每週營運報表\n包含完成率與額度';
home.heroStart();
for (let i = 0; i < 20; i++) await Promise.resolve();
const post = fetchCalls.find((c) => c.url.includes('/api/autopilot/task') && c.method === 'POST');
expect(!!post, '應 POST 交辦端點');
expect(post.body.title === '做每週營運報表' && post.body.detail.includes('完成率'), 'title=首行,detail=全文');
expect($('#homeChatStream').children.some((c) => c.className === 'taskcard'), '成功後插入任務卡片');

// 4) 401 → 明確提示不靜默(卡片不新增)
const before = $('#homeChatStream').children.length;
postResp = { ok: false, status: 401, json: () => Promise.resolve({}) };
$('#heroInput').value = '再交辦一個';
home.heroStart();
for (let i = 0; i < 20; i++) await Promise.resolve();
expect($('#homeChatStream').children.length === before, '401 不得新增卡片(有 toast 提示)');

console.log('OK');
