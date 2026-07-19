// 升階頁(軌 D2):你的工作室正在升級——自治階段/八開關/宣告條件/信任數字。
// 資料=GET /api/autopilot/stage(D1);純唯讀呈現。import 期零 DOM。
import { $, appendTextEl } from "../dom.js";

export const STAGE_LABEL = {
  "2": { title: "第 2 階・指揮官", sub: "多分身並行,自動測試把關——正在累積升階的信任數字。" },
  "3-progress": { title: "第 3 階・升級中", sub: "監督式自治的開關逐一點亮;你負責餵背景,不再逐件驗收。" },
  "3-ready": { title: "第 3 階・待宣告", sub: "條件快照全綠——連續維持 14 天即可宣告監督式自治。" },
  "4-progress": { title: "第 4 階・AI 原生進行式", sub: "意圖驅動、按例外監控——你出意圖,工作室自己出題交付。" },
};

function fmtPct(v) {
  return v == null ? "—" : Math.round(v * 100) + "%";
}

export async function renderStage() {
  const host = $("#homeStage");
  if (!host) return;
  host.innerHTML = "";
  appendTextEl(host, "h2", "home-sub-title", "升階");
  let d = null;
  try {
    d = await (await fetch("/api/autopilot/stage")).json();
  } catch {
    appendTextEl(host, "p", "muted", "載入失敗——稍後再試。");
    return;
  }
  const meta = STAGE_LABEL[d.stage] || { title: d.stage, sub: "" };
  const heroEl = document.createElement("div");
  heroEl.className = "stage-hero";
  appendTextEl(heroEl, "div", "stage-title", meta.title);
  appendTextEl(heroEl, "p", "muted", meta.sub);
  host.appendChild(heroEl);

  // 宣告條件四卡
  appendTextEl(host, "h3", "stage-sec", "第 3 階宣告條件(7 天快照)");
  const condGrid = document.createElement("div");
  condGrid.className = "stage-conds";
  for (const c of d.conditions || []) {
    const card = document.createElement("div");
    card.className = "stage-cond" + (c.ok ? " ok" : "");
    appendTextEl(card, "div", "sc-dot", c.ok ? "✓" : "…");
    const body = document.createElement("div");
    appendTextEl(body, "div", "sc-label", c.label);
    appendTextEl(body, "div", "sc-detail muted", c.detail || "");
    card.appendChild(body);
    condGrid.appendChild(card);
  }
  host.appendChild(condGrid);

  // 宣告 streak 進度(D5):條件全綠連續 N/14 天
  if (typeof d.streak === "number") {
    const target = d.streak_target || 14;
    const wrap = document.createElement("div");
    wrap.className = "stage-streak";
    appendTextEl(wrap, "span", "muted", `條件全綠連續 ${d.streak}/${target} 天`);
    const bar = document.createElement("div");
    bar.className = "streak-bar";
    const fill = document.createElement("div");
    fill.className = "streak-fill";
    fill.style.width = Math.min(100, Math.round((d.streak / target) * 100)) + "%";
    bar.appendChild(fill);
    wrap.appendChild(bar);
    host.appendChild(wrap);
  }

  // 第 4 階條件卡(F2):意圖→自產→零人工交付
  if (Array.isArray(d.stage4_conditions) && d.stage4_conditions.length) {
    appendTextEl(host, "h3", "stage-sec", "第 4 階(AI 原生)進度");
    const s4Grid = document.createElement("div");
    s4Grid.className = "stage-conds";
    for (const c of d.stage4_conditions) {
      const card = document.createElement("div");
      card.className = "stage-cond" + (c.ok ? " ok" : "");
      appendTextEl(card, "div", "sc-dot", c.ok ? "✓" : "…");
      const body = document.createElement("div");
      appendTextEl(body, "div", "sc-label", c.label);
      appendTextEl(body, "div", "sc-detail muted", c.detail || "");
      card.appendChild(body);
      s4Grid.appendChild(card);
    }
    host.appendChild(s4Grid);
  }

  // 信任數字一行
  const tr = d.trust || {};
  appendTextEl(
    host,
    "p",
    "stage-trust muted",
    `零介入合併率 ${fmtPct(tr.zero_touch_rate)}・7 天 merged ${tr.merged ?? "—"}・人工介入 ${(tr.interventions || {}).total ?? "—"} 次・自主出題占比 ${fmtPct((tr.autonomy || {}).autonomous_rate)}`,
  );

  // 八開關
  appendTextEl(host, "h3", "stage-sec", `自治開關(${d.canaries_on ?? 0}/8 已開,依觀察窗逐一點亮)`);
  const list = document.createElement("div");
  list.className = "stage-canaries";
  for (const c of d.canaries || []) {
    const row = document.createElement("div");
    row.className = "stage-canary" + (c.on ? " on" : "");
    appendTextEl(row, "span", "cn-dot", c.on ? "●" : "○");
    appendTextEl(row, "span", "cn-label", c.label);
    appendTextEl(row, "span", "cn-state muted", c.on ? "運轉中" : "待開啟");
    list.appendChild(row);
  }
  host.appendChild(list);
  appendTextEl(host, "p", "muted", "開關由觀察窗紀律控制(一次一個、各約 7 天);異常會推播到你的手機。");
}

export function openStage() {
  import("./home.js").then((m) => m.setSubview("stage"));
  renderStage();
}
