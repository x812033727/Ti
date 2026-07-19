// 帳號選單+靈感區(PR12):選單開合/登出導向、脈搏文案、建議卡點擊帶入交辦。
import { install, expect } from './_frontend_env.mjs';

const { $, fetchCalls } = install((url) => {
  if (url.includes('/api/autopilot/activity')) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ tasks: [
      { id: 1, status: 'done', pr: 99, title: '已出貨的功能' },
      { id: 2, status: 'pending', title: '建議做的事' },
    ] }) });
  }
  if (url.includes('/api/autopilot')) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ paused: false, counts: { pending: 4, done: 12 }, heartbeat: { state: 'running' } }) });
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
});

const sidenav = await import('../web/js/panels/sidenav.js');
const home = await import('../web/js/panels/home.js');

// 1) 選單開合 + 登出打 /api/logout
sidenav.bindSidenav();
const menu = $('#snAccountMenu');
menu.classList.add('hidden');
$('#snAccount').onclick();
expect(!menu.classList.contains('hidden'), '點帳號應展開選單');
await $('#accLogout').onclick();
expect(fetchCalls.some((c) => c.url === '/api/logout' && c.method === 'POST'), '登出應打 /api/logout');

// 2) 脈搏+靈感渲染
await home.refreshHomeExtras();
expect($('#heroPulse').textContent.includes('執行中') && $('#heroPulse').textContent.includes('待辦 4'), '脈搏一行狀態');
const cards = $('#heroInspire').children;
expect(cards.length === 2, '已出貨+建議各一卡');
function textOf(el) { let s = el.textContent || ''; for (const c of el.children || []) s += textOf(c); return s; }
expect(textOf(cards[0]).includes('PR #99'), '出貨卡帶 PR 號');

// 3) 建議卡點擊 → 帶入 composer 交辦模式
cards[1].onclick();
expect($('#heroInput').value === '建議做的事', '建議卡應填入 composer');
expect(home.getHeroMode() === 'task', '並切到交辦模式');

console.log('OK');
