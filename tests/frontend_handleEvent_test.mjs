// 前端 handleEvent 向後相容測試：先掛全域 DOM stub，再 import 真實
// web/js/events-render.js（ES module），驗證所有已知事件（含新增 huddle /
// critic_review）不拋錯，且未知事件不崩潰。

// --- 最小 DOM stub：任何元素都是可鏈式呼叫、吸收任意操作的 Proxy ---
function makeEl() {
  const fn = function () { return makeEl(); };
  return new Proxy(fn, {
    get(t, prop) {
      if (prop === 'dataset') return {};
      if (prop === 'classList') return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
      if (prop === 'style') return {};
      if (prop === 'value') return '';
      if (prop === 'textContent') return '';
      if (prop === 'innerHTML') return '';
      if (prop === 'querySelector') return () => makeEl();
      if (prop === 'querySelectorAll') return () => [];
      if (prop === Symbol.toPrimitive) return () => '';
      return makeEl();
    },
    set() { return true; },
    apply() { return makeEl(); },
  });
}

const document = {
  querySelector: () => makeEl(),
  querySelectorAll: () => [],
  createElement: () => makeEl(),
  createTextNode: () => makeEl(),
  getElementById: () => makeEl(),
  body: makeEl(),
};
const location = { protocol: 'http:', host: 'localhost', href: '' };
function WebSocket() { return makeEl(); }
WebSocket.OPEN = 1;
const fetchStub = () => Promise.resolve({
  json: () => Promise.resolve({ files: [], sessions: [], events: [], tasks: [], fields: [] }),
  ok: true,
});

// 先掛全域 stub 再 import：模組頂層不查 DOM（repo 鐵則），import 期不會踩 stub 缺口。
Object.assign(globalThis, {
  document, location, WebSocket,
  fetch: fetchStub,
  window: { addEventListener: () => {}, matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) },
  setTimeout: () => 0, clearTimeout: () => {},
});
const mod = await import('../web/js/events-render.js');

const handleEvent = mod.handleEvent;
if (typeof handleEvent !== 'function') {
  console.error('FAIL: handleEvent 未定義');
  process.exit(1);
}

const sid = 's1';
const ev = (type, payload = {}) => ({ type, session_id: sid, ts: 1, payload });

// 1) 已知事件（含本次新增的 huddle / critic_review）全部不可拋錯
const known = [
  ev('session_started', { requirement: '需求', roster: [{ key: 'pm', name: 'PM', avatar: '🧑', title: 'x', tags: [] }] }),
  ev('phase_change', { phase: '實作', detail: 'x' }),
  ev('expert_status', { speaker: 'pm', status: 'thinking' }),
  ev('expert_message', { speaker: 'pm', name: 'PM', avatar: '🧑', text: 'hi' }),
  ev('tool_use', { speaker: 'pm', tool: 'Write', summary: 'x' }),
  ev('board_update', { columns: { todo: [{ title: 'a' }], doing: [], review: [], done: [] } }),
  ev('run_result', { passed: true, detail: 'ok', log: 'log' }),
  ev('demo_result', { label: 'Demo', command: 'py', exit_code: 0, passed: true, output: 'out' }),
  ev('demo_result', { label: 'Demo', command: 'py --bad', exit_code: 0, passed: true, output: 'out', retried_cmd: 'py', first_exit: 4 }),
  ev('git_commit', { message: 'm', hash: 'abc' }),
  ev('human_message', { text: 'hi' }),
  ev('clarify_request', { questions: [{ q: '目標平台？', assumption: '網頁版' }, { q: '要支援多人嗎？', assumption: '' }], timeout_s: 180 }),
  ev('clarify_request', { questions: [], timeout_s: 0 }),
  ev('huddle', { task_id: 1, title: 'T', participants: ['pm', 'engineer'], conclusion: '結論', limitation: false }),
  ev('huddle', { task_id: 1, title: 'T', participants: [], conclusion: '', limitation: true }),
  ev('critic_review', { gate: 'pm', passed: true, text: '放行' }),
  ev('critic_review', { gate: 'senior', passed: false, text: '異議成立' }),
  ev('task_status', { id: 1, title: 'T', status: 'done' }),
  ev('retrospective', { text: '檢討' }),
  ev('publish_result', { ok: true, detail: 'done', branch: 'b', pr_url: 'http://x' }),
  ev('done', { completed: true, stopped: false, files: [] }),
  ev('error', { message: '壞了' }),
  ev('appraisal', { provider: 'claude', model: 'claude-opus-4-8', role: 'engineer', score: 4, comment: '穩定高質量' }),
  ev('appraisal', { provider: '', model: '', role: 'qa', score: 5, comment: '' }),
];

let pass = 0, fail = 0;
for (const e of known) {
  try { handleEvent(e); pass++; }
  catch (err) { fail++; console.error(`FAIL 已知事件 ${e.type}:`, err.message); }
}

// 2) 未知/異常事件：handleEvent 必須不崩潰（switch 無 default 分支）
const unknown = [
  ev('totally_new_event_v2', { whatever: 1 }),
  ev('future_event', {}),
  ev('huddle_v2_extended', { x: 1 }),
  { type: 'no_payload_event', session_id: sid, ts: 1 }, // payload 缺失
  { type: 'weird', session_id: sid }, // 無 payload 欄位
  ev('', {}),
];
for (const e of unknown) {
  try { handleEvent(e); pass++; }
  catch (err) { fail++; console.error(`FAIL 未知事件 ${e.type}:`, err.message); }
}

console.log(`handleEvent 測試：通過 ${pass} / 失敗 ${fail}`);
process.exit(fail === 0 ? 0 : 1);
