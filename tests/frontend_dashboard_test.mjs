// 監控儀表板驗證：先掛全域 stub 再 import 真實 web/js/panels/dashboard.js。
// 驗證 heroModel/tilesModel 純函式、refreshDashboard 端點覆蓋與渲染、setView 視圖切換。
import { install, expect } from './_frontend_env.mjs';

const RESP = {
  '/api/autopilot': {
    paused: true,
    counts: { pending: 142, in_progress: 1, done: 187, failed: 16, parked: 61 },
    completion: { rate: 0.94, total: 50 },
    dispatch_mode: 'auto',
    pr_budget: { used: 3, cap: 12 },
    deploy: { disk_head: '80fcf5ba', behind: 0 },
    heartbeat: { state: 'running', task_id: 260, running_commit: '9e991412' },
  },
  '/api/autopilot/audit-trend?days=30': {
    days: 30,
    buckets: [
      { date: '2026-07-09', ok: 5, fail: 1, rate: 0.833 },
      { date: '2026-07-10', ok: 8, fail: 0, rate: 1 },
    ],
    totals: { ok: 13, fail: 1, rate: 0.929 },
  },
  '/api/provider-quota': {
    providers: [
      { key: 'claude', label: 'Claude', rate_limits: { five_hour: { used_percentage: 42 }, seven_day: { used_percentage: 91 } } },
    ],
  },
  '/api/appraisals': { summary: { providers: { codex: { avg_score: 4.45, n: 143, pass_rate: 0.73 } } } },
  '/api/autopilot/activity?limit=8': {
    tasks: [{ id: 571, title: '在 main 驗收', status: 'done', pr: 416, updated_at: 1799999999 }],
  },
};

const { $, fetchCalls } = install((url) => {
  const data = RESP[url];
  return Promise.resolve({ ok: !!data, json: () => Promise.resolve(data || {}) });
});

const mod = await import('../web/js/panels/dashboard.js');

// 1) heroModel：暫停優先於心跳 running；子行帶任務/派工/PR 預算
let m = mod.heroModel(RESP['/api/autopilot']);
expect(m.orb === 'paused' && m.title === '已暫停', '暫停狀態應為 paused/已暫停');
expect(m.sub.includes('#260') && m.sub.includes('派工 auto') && m.sub.includes('今日 PR 3/12'),
  '子行應含任務/派工/PR 預算：' + m.sub);
m = mod.heroModel({ paused: false, heartbeat: { state: 'quota_sleep' } });
expect(m.orb === 'sleep' && m.title === '額度休眠', 'quota_sleep 應為額度休眠');
m = mod.heroModel({ paused: false, heartbeat: { state: 'idle' } });
expect(m.orb === 'idle' && m.title === '待命中', 'idle 應為待命中');
m = mod.heroModel({ paused: false, heartbeat: { state: 'running' } });
expect(m.orb === 'run' && m.title === '執行中', 'running 應為執行中');

// 2) tilesModel：完成率與五態計數
const tiles = mod.tilesModel(RESP['/api/autopilot']);
expect(tiles[0].value === '94%' && tiles[0].label.includes('50'), '完成率磁貼應為 94%（近 50）');
expect(tiles.find((t) => t.key === 'pending').value === '142', '待辦磁貼應為 142');
expect(tiles.find((t) => t.key === 'failed').tone === 'bad', '失敗>0 應標 bad tone');

// 3) refreshDashboard：打齊五個唯讀端點並渲染
await mod.refreshDashboard();
const urls = fetchCalls.map((c) => c.url);
for (const u of Object.keys(RESP)) {
  expect(urls.includes(u), `應呼叫 ${u}`);
}
expect($('#dashState').textContent === '已暫停', '英雄列應顯示已暫停');
expect($('#dashOrb').dataset.state === 'paused', '狀態球應為 paused');
expect($('#dashToggle').textContent === '恢復', '暫停時主鍵應顯示「恢復」');
// 部署漂移：行程 9e991412 ≠ 磁碟 80fcf5ba → 橫幅露出
expect(!$('#dashDrift').classList.contains('hidden'), '漂移橫幅應露出');
expect($('#dashDrift').textContent.includes('9e991412'), '漂移橫幅應含行程 commit');
// 趨勢：兩天各一根（fail+ok 兩段）
const chart = $('#dashTrend').children.find((c) => c.className === 'trend-chart');
expect(chart && chart.children.length === 2, '趨勢圖應有 2 根日柱');
// 額度：91% 應標 crit
const quotaHtml = JSON.stringify($('#dashQuota').children, (k, v) => (k === 'parentNode' ? undefined : v));
expect(quotaHtml.includes('crit'), '7d 91% 應標 crit');
// 動態列
expect($('#dashActivity').children.length === 1, '動態應有 1 筆');

// 4) setView：body[data-view] 切換 + 切換鈕 active 對齊
mod.setView('studio');
expect(globalThis.document.body.dataset.view === 'studio', 'setView(studio) 應設 data-view');
mod.setView('dash');
expect(globalThis.document.body.dataset.view === 'dash', 'setView(dash) 應設 data-view');

console.log('OK: 儀表板 heroModel/tilesModel/端點覆蓋/漂移橫幅/視圖切換 皆正常');
process.exit(0);
