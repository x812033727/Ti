// 小組管理面板驗證：先掛全域 stub 再 import 真實 web/js/panels/groups.js（含 roles 快取）。
// 驗證列表渲染、成員 <2 前端擋下不送、POST/PUT/DELETE 路徑與 body、
// 啟動列 groupSelect 選項、start() 開場 payload 帶 group。
import { install, driveModal, expect } from './_frontend_env.mjs';

const ROLES = [
  { key: 'pm', name: '專案經理', avatar: '🧭', source: 'builtin', in_roster: true, allowed_tools: [], tags: [], system_prompt: '決議: x', permission_mode: 'default', title: '', model: '', description: '' },
  { key: 'engineer', name: '工程師', avatar: '🛠', source: 'builtin', in_roster: true, allowed_tools: [], tags: [], system_prompt: '輸出: x', permission_mode: 'default', title: '', model: '', description: '' },
  { key: 'qa', name: '驗證工程師', avatar: '🔬', source: 'builtin', in_roster: true, allowed_tools: [], tags: [], system_prompt: '驗證: x', permission_mode: 'default', title: '', model: '', description: '' },
];
let groups = [{ name: '前端小隊', role_keys: ['pm', 'engineer'], mode: 'parallel' }];

const wsSent = [];
const { $, body, fetchCalls } = install((url, opts = {}) => {
  const method = opts.method || 'GET';
  if (url === '/api/roles') return Promise.resolve({ ok: true, json: () => Promise.resolve({ roles: ROLES }) });
  if (url === '/api/groups' && method === 'GET') {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ groups }) });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ group: {} }) });
});
// start() 用的 WebSocket stub：記錄 send 內容
globalThis.WebSocket = function () {
  return { send: (d) => wsSent.push(JSON.parse(d)), set onopen(fn) { fn(); }, get onopen() { return null; } };
};
globalThis.WebSocket.OPEN = 1;

const rolesMod = await import('../web/js/panels/roles.js');
const mod = await import('../web/js/panels/groups.js');

// 1) 列表渲染（成員名來自角色快取）
await rolesMod.loadRoles();
await mod.loadGroups();
const items = $('#groupList').children;
expect(items.length === 1, `小組列表應 1 列，實為 ${items.length}`);
const text = items[0]._descendants().map((c) => c.textContent).join('|');
expect(text.includes('前端小隊') && text.includes('並行'), '應渲染名稱與 mode 徽章');
expect(text.includes('專案經理') && text.includes('工程師'), '成員名應由角色快取解出');

// 2) 啟動列 groupSelect 選項
const opts = $('#groupSelect').children.map((o) => o.value);
expect(opts.includes('前端小隊'), 'groupSelect 應含小組選項');

// 3) 成員 <2 前端擋下
mod.bindGroups();
const p1 = $('#groupNew').onclick();
await new Promise((r) => setImmediate(r));
fetchCalls.length = 0;
const dlg = driveModal(body, (inputs, d) => {
  inputs.get('name').value = '獨腳戲';
  const boxes = d._descendants().filter((c) => c.tag === 'input' && c.type === 'checkbox');
  boxes[0].checked = true; // 只勾 1 位
});
expect(!fetchCalls.some((c) => c.method === 'POST'), '成員 <2 不應送出');
const err = dlg._descendants().find((c) => c.className === 'form-modal-error');
expect(err && err.textContent.includes('至少 2'), '應提示成員至少 2 位');
dlg._descendants().find((c) => c.tag === 'button' && c.textContent === '取消').onclick();
await p1;

// 4) 合法新增 → POST body {name, role_keys, mode}
const p2 = $('#groupNew').onclick();
await new Promise((r) => setImmediate(r));
fetchCalls.length = 0;
driveModal(body, (inputs, d) => {
  inputs.get('name').value = '全端小隊';
  const boxes = d._descendants().filter((c) => c.tag === 'input' && c.type === 'checkbox');
  boxes[0].checked = true;
  boxes[2].checked = true;
  const radios = d._descendants().filter((c) => c.tag === 'input' && c.type === 'radio');
  radios.forEach((r) => { r.checked = r.value === 'parallel'; });
});
await p2;
const post = fetchCalls.find((c) => c.method === 'POST');
expect(post && post.url === '/api/groups', '新增小組應 POST /api/groups');
expect(post.body.name === '全端小隊' && post.body.mode === 'parallel', 'POST body 應含 name/mode');
expect(JSON.stringify(post.body.role_keys) === JSON.stringify(['pm', 'qa']), `role_keys 應為勾選成員，實為 ${JSON.stringify(post.body.role_keys)}`);

// 5) start() 開場 payload 帶 group（後端 ws.py 契約）
const ws = await import('../web/js/ws.js');
$('#requirement').value = '做個東西';
$('#projectSelect').value = '';
$('#repoUrl').value = '';
$('#workflowSelect').value = '';
$('#groupSelect').value = '前端小隊';
ws.start();
expect(wsSent.length === 1, 'start() 應送出開場 payload');
expect(wsSent[0].group === '前端小隊', `payload 應帶 group，實為 ${JSON.stringify(wsSent[0])}`);

console.log('OK: 小組列表/成員下限先驗/POST body/groupSelect/開場 group 皆正常');
process.exit(0);
