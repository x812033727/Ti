// 升階頁(軌 D2):階段標題/條件卡 ok 態/開關列表/失敗降級。
import { install, expect } from './_frontend_env.mjs';

let apiOk = true;
install(() => apiOk
  ? Promise.resolve({ ok: true, json: () => Promise.resolve({
      stage: '3-progress',
      canaries_on: 2,
      canaries: [
        { key: 'objective_gate', label: '① 客觀驗收閘門', on: true },
        { key: 'expert_skills', label: '② 專家技能手冊', on: false },
      ],
      conditions: [
        { key: 'zero_touch', label: '零人工介入合併率 ≥90%', detail: '7 天 merged 12', ok: true },
        { key: 'slo_armed', label: 'SLO 煞車武裝', detail: '門檻 未設(=0)', ok: false },
      ],
      stage4_conditions: [
        { key: 'intent_loop_on', label: '意圖迴路運轉中', detail: '北極星 intent 驅動差距分析', ok: true },
        { key: 'autonomous_delivery', label: '意圖/排程源零人工交付 ≥1', detail: '7 天內 0 件', ok: false },
      ],
      trust: { zero_touch_rate: 0.917, merged: 12, interventions: { total: 1 }, autonomy: { autonomous_rate: 0.75 } },
    }) })
  : Promise.reject(new Error('down')));

const mod = await import('../web/js/panels/stage.js');
const { $ } = await import('../web/js/dom.js');

await mod.renderStage();
function textOf(el) { let s = el.textContent || ''; for (const c of el.children || []) s += textOf(c); return s; }
const all = textOf($('#homeStage'));
expect(all.includes('第 3 階・升級中'), '階段標題');
expect(all.includes('92%'), '零介入率百分比');
expect(all.includes('2/8 已開'), '開關計數');
expect(all.includes('運轉中') && all.includes('待開啟'), '開關狀態文案');
expect(all.includes('第 4 階(AI 原生)進度') && all.includes('意圖迴路運轉中'), '第 4 階區塊(F2)');
expect(all.includes('自主出題占比 75%'), '自主占比');
const condEls = [];
(function walk(el) { if ((el.className || '').includes('stage-cond') && !(el.className || '').includes('stage-conds')) condEls.push(el); for (const c of el.children || []) walk(c); })($('#homeStage'));
expect(condEls.length === 4 && condEls[0].className.includes('ok') && !condEls[1].className.includes('ok'), '條件卡 ok 態(2 張第 3 階+2 張第 4 階)');
expect(condEls[2].className.includes('ok') && !condEls[3].className.includes('ok'), '第 4 階條件卡 ok 態');

apiOk = false;
await mod.renderStage();
expect(textOf($('#homeStage')).includes('載入失敗'), '失敗降級');
console.log('OK');
