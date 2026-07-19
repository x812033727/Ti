// 插件頁(PR9):四卡總覽渲染(計數/技能清單/啟用狀態)、載入失敗優雅降級。
import { install, expect } from './_frontend_env.mjs';

let apiOk = true;
install((url) => {
  if (!apiOk) return Promise.reject(new Error('down'));
  const payloads = {
    '/api/roles': { roles: [{ key: 'pm' }, { key: 'qa' }] },
    '/api/groups': { groups: [{ name: 'g1' }] },
    '/api/workflows': { workflows: [{ name: 'w1' }, { name: 'w2' }, { name: 'w3' }] },
    '/api/skills': {
      enabled: false,
      roles: ['engineer', 'qa'],
      skills: [{ name: 'ti-shipping', description: '出貨自檢' }, { name: 'ti-investigation', description: '' }],
    },
  };
  const hit = Object.entries(payloads).find(([k]) => url.includes(k));
  return Promise.resolve({ ok: true, json: () => Promise.resolve(hit ? hit[1] : {}) });
});

const plugins = await import('../web/js/panels/plugins.js');
const { $ } = await import('../web/js/dom.js');

await plugins.renderPlugins();
const host = $('#homePlugins');
const grid = host.children.find((c) => c.className === 'plug-grid');
expect(grid && grid.children.length === 4, '應渲染四張卡');

function textOf(el) {
  let s = el.textContent || '';
  for (const c of el.children || []) s += textOf(c);
  return s;
}
const all = textOf(host);
expect(all.includes('2 位專家'), '角色計數');
expect(all.includes('3 條'), '流程計數');
expect(all.includes('未啟用'), '技能未啟用狀態');
expect(all.includes('ti-shipping') && all.includes('出貨自檢'), '技能清單含描述');
expect(all.includes('(無描述)'), '空描述佔位');

// 載入失敗:優雅降級文案,不拋
apiOk = false;
await plugins.renderPlugins();
expect(textOf($('#homePlugins')).includes('載入失敗'), '失敗應顯示引導而非白屏');

console.log('OK');
