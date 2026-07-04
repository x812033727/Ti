// 角色管理面板驗證：先掛全域 stub 再 import 真實 web/js/panels/roles.js。
// 驗證列表渲染（來源徽章/未在編制/builtin 無刪除鈕）、新增走 POST 且 payload 含
// RoleBody 全欄位、前端反空殼 persona 先驗、422 detail 顯示為 toast。
import { install, driveModal, expect } from './_frontend_env.mjs';

const FIXTURE = [
  { key: 'pm', name: '專案經理', avatar: '🧭', title: 'PM', model: '', allowed_tools: ['Read'],
    permission_mode: 'default', tags: [], description: '', source: 'builtin', in_roster: true,
    system_prompt: '決議: 給出結論' },
  { key: 'engineer', name: '工程師', avatar: '🛠', title: 'Eng', model: 'claude-fable-5',
    allowed_tools: ['Read', 'Write'], permission_mode: 'acceptEdits', tags: [], description: '',
    source: 'override', in_roster: true, system_prompt: '輸出格式: code' },
  { key: 'designer', name: '設計師', avatar: '🎨', title: '', model: '', allowed_tools: ['Read'],
    permission_mode: 'default', tags: [], description: '', source: 'file', in_roster: false,
    system_prompt: '輸出格式: 條列' },
];

let failNext = null; // 設成 {status, detail} 讓下一個寫入請求失敗
const { $, body, fetchCalls } = install((url, opts = {}) => {
  const method = opts.method || 'GET';
  if (url === '/api/roles' && method === 'GET') {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ roles: FIXTURE }) });
  }
  if (failNext) {
    const f = failNext; failNext = null;
    return Promise.resolve({ ok: false, status: f.status, json: () => Promise.resolve({ ok: false, detail: f.detail }) });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
});

const mod = await import('../web/js/panels/roles.js');

// 1) 列表渲染
await mod.loadRoles();
const items = $('#roleList').children;
expect(items.length === 3, `角色列表應 3 列，實為 ${items.length}`);
const texts = items.map((li) => li._descendants().map((c) => c.textContent).join('|'));
expect(texts[0].includes('內建'), 'builtin 應有「內建」徽章');
expect(texts[1].includes('覆蓋內建'), 'override 應有「覆蓋內建」徽章');
expect(texts[2].includes('自建') && texts[2].includes('未在編制'), 'file 角色應有「自建」與「未在編制」');
const btnTexts = (li) => li._descendants().filter((c) => c.tag === 'button').map((b) => b.textContent);
expect(!btnTexts(items[0]).some((t) => t.includes('刪除') || t.includes('還原')), 'builtin 不應有刪除/還原鈕');
expect(btnTexts(items[1]).includes('還原內建'), 'override 的刪除鈕文案應為「還原內建」');
expect(btnTexts(items[2]).includes('刪除'), 'file 角色應有刪除鈕');

// 2) 新增角色：開 modal → 填表 → 送出 → POST /api/roles 全欄位
mod.bindRoles();
const newP = $('#roleNew').onclick(); // roleEditor(null) → Promise
await new Promise((r) => setTimeout(r, 0) || setImmediate(r));
fetchCalls.length = 0;
driveModal(body, (inputs) => {
  inputs.get('key').value = 'critic';
  inputs.get('name').value = '評論家';
  inputs.get('title').value = 'Critic';
  inputs.get('model').value = 'gpt-5.5';
  inputs.get('allowed_tools').value = 'Read, Grep, Bash';
  inputs.get('tags').value = 'review';
  inputs.get('system_prompt').value = '你是評論家。\n決議: 核可/退回';
});
await newP;
const post = fetchCalls.find((c) => c.method === 'POST');
expect(post && post.url === '/api/roles', '新增角色應 POST /api/roles');
expect(post.body.key === 'critic' && post.body.name === '評論家', 'payload 應含 key/name');
expect(JSON.stringify(post.body.allowed_tools) === JSON.stringify(['Read', 'Grep', 'Bash']), 'allowed_tools 應解析為陣列');
expect(post.body.permission_mode === 'default', 'permission_mode 預設 default');
expect(post.body.system_prompt.includes('決議: 核可/退回'), 'payload 應含 system_prompt');

// 3) persona 反空殼先驗：缺「關鍵詞:」的提示詞應被前端擋下（不送請求）
const p3 = $('#roleNew').onclick();
await new Promise((r) => setImmediate(r));
fetchCalls.length = 0;
const dlg = driveModal(body, (inputs) => {
  inputs.get('key').value = 'empty_shell';
  inputs.get('name').value = '空殼';
  inputs.get('system_prompt').value = '我什麼都行';
});
expect(!fetchCalls.some((c) => c.method === 'POST'), 'persona 不合規不應送出');
const err = dlg._descendants().find((c) => c.className === 'form-modal-error');
expect(err && err.textContent.includes('反空殼'), '應顯示反空殼規則錯誤');
// 取消收尾（讓 Promise 結束）
dlg._descendants().find((c) => c.tag === 'button' && c.textContent === '取消').onclick();
await p3;

// 4) 後端 422 detail → toast
const p4 = $('#roleNew').onclick();
await new Promise((r) => setImmediate(r));
failNext = { status: 422, detail: 'key 不合法（後端擋下）' };
driveModal(body, (inputs) => {
  inputs.get('key').value = 'okkey';
  inputs.get('name').value = 'X';
  inputs.get('system_prompt').value = '輸出: y';
});
await p4;
const toasts = $('#toast').children.map((c) => c.textContent);
expect(toasts.some((t) => t.includes('後端擋下')), `422 detail 應顯示為 toast，實為 ${JSON.stringify(toasts)}`);

console.log('OK: 角色列表徽章/builtin 保護/POST payload/persona 先驗/422 顯示 皆正常');
process.exit(0);
