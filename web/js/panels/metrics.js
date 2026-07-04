// 運維指標面板。
import { $ } from "../dom.js";

export async function openMetrics() {
  $("#metricsPanel").classList.remove("hidden");
  await refreshMetrics();
}

export function closeMetrics() { $("#metricsPanel").classList.add("hidden"); }

export async function refreshMetrics() {
  const body = $("#metricsBody");
  try {
    const m = await (await fetch("/api/metrics")).json();
    const s = m.sessions || {};
    const h = m.history || {};
    const r = h.retention || {};
    const byStatus = h.by_status || {};
    const statusLine = Object.keys(byStatus).length
      ? Object.entries(byStatus).map(([k, v]) => `${k} ${v}`).join("・")
      : "（無）";
    const cap = s.max_concurrent ? `／上限 ${s.max_concurrent}` : "（不限）";
    const pa = m.parallel || {};
    const pcfg = pa.config || {};
    const rows = [
      ["活躍場次", `${s.active ?? "?"}${cap}`],
      ["歷史場次", `${h.total ?? "?"}`],
      ["各狀態", statusLine],
      ["保留策略", `數量 ${r.max_count ? r.max_count : "不限"}・年齡 ${r.max_age_s ? r.max_age_s + "s" : "停用"}`],
      ["workspace 目錄", `${(m.workspaces || {}).count ?? "?"}`],
      ["任務並行", `${pcfg.enabled ? "開啟" : "關閉"}・支線上限 ${pcfg.lanes ?? "?"}`],
    ];
    if (pa.enabled_runs > 0) {
      rows.push(
        ["　曾並行場次", `${pa.enabled_runs}・峰值支線 ${pa.peak_lanes}`],
        ["　平均加速", `${pa.avg_speedup}×・省下約 ${pa.wall_clock_saved_s}s`],
        ["　波次／合併衝突", `${pa.total_waves} 波・${pa.merge_conflicts} 次衝突`],
      );
    }
    // 成果記分卡：成功率／輪數／一次過率／退回原因，與「近 10 場 vs 前 10 場」趨勢。
    const sc = m.scorecard || {};
    if (sc.n > 0) {
      const pct = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
      const t = sc.tasks || {};
      const rj = sc.rejects || {};
      const rejParts = [
        ["QA 退回", rj.qa_fail], ["自測失敗", rj.smoke_fail], ["客觀閘門", rj.gate_veto],
        ["異議退回", rj.critic], ["停滯收斂", rj.stall],
      ].filter(([, v]) => v > 0).map(([k, v]) => `${k} ${v}`);
      rows.push(
        ["📈 記分卡（場次）", `${sc.n} 場・成功率 ${pct(sc.completed_rate)}`],
        ["　任務", `${t.done ?? 0}/${t.total ?? 0} 完成・一次過率 ${pct(t.first_try_rate)}`],
        ["　測試通過率", pct(sc.qa_pass_rate)],
        ["　Demo 通過率", pct(sc.demo_pass_rate)],
        ["　審查通過率", pct(sc.critic_pass_rate)],
        ["　平均輪數/任務", `${sc.avg_rounds ?? "—"}`],
        ["　退回原因", rejParts.length ? rejParts.join("・") : "（無）"],
      );
      const tr = sc.trend || {};
      if ((tr.previous || {}).n > 0) {
        const a = tr.recent, b = tr.previous;
        const arrow = (recentV, prevV, lowerBetter) => {
          if (recentV == null || prevV == null || recentV === prevV) return "→";
          return (lowerBetter ? recentV < prevV : recentV > prevV) ? "↑ 進步" : "↓ 退步";
        };
        rows.push(
          ["　趨勢：成功率", `近${a.n}場 ${pct(a.completed_rate)} vs 前${b.n}場 ${pct(b.completed_rate)}（${arrow(a.completed_rate, b.completed_rate, false)}）`],
          ["　趨勢：平均輪數", `近${a.n}場 ${a.avg_rounds ?? "—"} vs 前${b.n}場 ${b.avg_rounds ?? "—"}（${arrow(a.avg_rounds, b.avg_rounds, true)}）`],
        );
      }
    }
    body.innerHTML = "";
    rows.forEach(([k, v]) => {
      const row = document.createElement("div");
      row.className = "metric-row";
      const ks = document.createElement("span");
      ks.className = "metric-k";
      ks.textContent = k;
      const vs = document.createElement("span");
      vs.className = "metric-v";
      vs.textContent = v;
      row.append(ks, vs);
      body.appendChild(row);
    });
  } catch (e) {
    body.innerHTML = `<span class="muted">讀取失敗</span>`;
  }
}
