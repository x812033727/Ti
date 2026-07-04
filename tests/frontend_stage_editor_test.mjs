// workflow 結構化 stage 卡片編輯器驗證：先掛全域 stub 再 import
// 真實 web/js/panels/workflow.js。驗證卡片渲染、卡片操作即時序列化回
// #workflowStages（單一真相）、build 自動補 task_pipeline、JSON 壞值顯示提示。
import { install, expect } from './_frontend_env.mjs';

const WF = {
  name: '測試流程',
  description: '',
  stages: [
    { type: 'discuss', roles: ['pm'], gate: [{ role: 'pm', verdict: 'pm_done' }] },
    { type: 'build', task_pipeline: [{ type: 'implement', assignee: 'engineer' }] },
  ],
};
const { $ } = install((url, opts = {}) => {
  if (url === '/api/workflows') return Promise.resolve({ ok: true, json: () => Promise.resolve({ workflows: [WF] }) });
  if (url === '/api/roles') {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ roles: [
      { key: 'pm', name: '專案經理', avatar: '🧭' }, { key: 'engineer', name: '工程師', avatar: '🛠' },
    ] }) });
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
});

const mod = await import('../web/js/panels/workflow.js');

// 1) 載入後渲染卡片：兩張 session 卡＋一顆「＋ 加階段」
await mod.loadWorkflowPanel();
await new Promise((r) => setImmediate(r)); // 等 roles 選項載完
mod.renderStageCards();
const box = $('#wfStageCards');
const cards = box.children.filter((c) => String(c.className).startsWith('wf-card'));
expect(cards.length === 2, `應渲染 2 張階段卡，實為 ${cards.length}`);
const addBtn = box.children.find((c) => c.tag === 'button');
expect(addBtn && addBtn.textContent === '＋ 加階段', '應有「＋ 加階段」按鈕');
// build 卡應含子階段區
const buildCard = cards[1];
const sub = buildCard._descendants().find((c) => c.className === 'wf-subpipeline');
expect(sub, 'build 卡應含 task_pipeline 子階段區');
expect(sub._descendants().some((c) => c.textContent === '＋ 子階段'), '子階段區應可再加');

// 2) 「＋ 加階段」→ textarea JSON 即時多一個 discuss（單一真相寫回）
addBtn.onclick();
let stages = JSON.parse($('#workflowStages').value);
expect(stages.length === 3 && stages[2].type === 'discuss', `加階段後 JSON 應 3 段，實為 ${JSON.stringify(stages)}`);

// 3) 把第三段 type 改成 build → 自動補空 task_pipeline
const cards2 = $('#wfStageCards').children.filter((c) => String(c.className).startsWith('wf-card'));
const typeSel = cards2[2]._descendants().find((c) => c.tag === 'select');
typeSel.value = 'build';
typeSel.onchange();
stages = JSON.parse($('#workflowStages').value);
expect(Array.isArray(stages[2].task_pipeline), 'type 改 build 應自動補 task_pipeline');

// 4) 卡片刪除 → JSON 同步移除
const cards3 = $('#wfStageCards').children.filter((c) => String(c.className).startsWith('wf-card'));
const delBtn = cards3[2]._descendants().find((c) => c.tag === 'button' && c.textContent === '✕' && c.title === '刪除此階段');
delBtn.onclick();
stages = JSON.parse($('#workflowStages').value);
expect(stages.length === 2, '刪卡後 JSON 應回 2 段');

// 5) 原文壞 JSON → 卡片區顯示提示不崩潰
$('#workflowStages').value = '[{壞掉';
mod.renderStageCards();
const warn = $('#wfStageCards').children.find((c) => c.className === 'wf-json-error');
expect(warn && warn.textContent.includes('JSON 無法解析'), '壞 JSON 應顯示提示');

console.log('OK: 卡片渲染/加階段寫回/build 補 pipeline/刪卡同步/壞 JSON 提示 皆正常');
process.exit(0);
