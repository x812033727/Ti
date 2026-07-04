// 建立專案 modal 驗證：先掛全域 stub 再 import 真實 web/js/panels/project.js。
// 驗證表單送出 → POST /api/projects {name, vision}、有填目標 repo 時接著
// POST publish-repo、建立後選中新專案；取消 → 還原下拉且不送請求。
import { install, driveModal, expect } from './_frontend_env.mjs';

const { $, body, fetchCalls } = install((url, opts = {}) => {
  const method = opts.method || 'GET';
  if (url === '/api/projects' && method === 'POST') {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, project: { id: 'p9' } }) });
  }
  if (url === '/api/projects') {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ projects: [{ id: 'p9', name: '新品' }] }) });
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
});

const mod = await import('../web/js/panels/project.js');

// 1) 完整填寫（含 repo）→ POST /api/projects + POST publish-repo
const p1 = mod.createProjectFlow();
await new Promise((r) => setImmediate(r));
driveModal(body, (inputs) => {
  inputs.get('name').value = '新品';
  inputs.get('vision').value = '一句願景';
  inputs.get('repo').value = 'me/newrepo';
});
await p1;
const post = fetchCalls.find((c) => c.method === 'POST' && c.url === '/api/projects');
expect(post, '應 POST /api/projects');
expect(post.body.name === '新品' && post.body.vision === '一句願景', `payload 應含 name/vision，實為 ${JSON.stringify(post.body)}`);
const repoPost = fetchCalls.find((c) => c.method === 'POST' && c.url === '/api/projects/p9/publish-repo');
expect(repoPost && repoPost.body.repo === 'me/newrepo', '有填 repo 應接著 POST publish-repo');
expect($('#projectSelect').value === 'p9', '建立後應選中新專案');

// 2) 取消 → 還原下拉、不送請求
$('#projectSelect').value = '__new__';
fetchCalls.length = 0;
const p2 = mod.createProjectFlow();
await new Promise((r) => setImmediate(r));
const dlg = body.children.filter((c) => c.tag === 'dialog').at(-1);
dlg._descendants().find((c) => c.tag === 'button' && c.textContent === '取消').onclick();
await p2;
expect(!fetchCalls.some((c) => c.method === 'POST'), '取消不應送出 POST');
expect($('#projectSelect').value === '', '取消應還原下拉為（一次性討論）');

console.log('OK: 建立專案 modal 的 POST payload/repo 順手設定/取消還原 皆正常');
process.exit(0);
