// 監控儀表板（監控視圖首頁）：Autopilot 狀態英雄列、任務統計磁貼、
// 近 30 天結果趨勢、provider 額度、績效榜、最新動態。
// 資料全部復用既有唯讀端點；趨勢圖沿用「div bar + fill」既有手法，零外部圖表依賴。
import { $, appendTextEl, toast } from "../dom.js";
import { setMobileView } from "../components/tabs.js";
import { renderStackedTrend } from "../components/chart.js";
import { openAutopilot, apQuotaWindows, apElapsedText } from "./autopilot.js";

// 動態列的狀態中文標籤（顏色由 .dash-act-dot 表達，文字不再帶 emoji）
const ACT_STATUS_LABEL = {
  pending: "待辦", in_progress: "進行中", merging: "合併中",
  done: "完成", failed: "失敗", parked: "停放",
};

const REFRESH_MS = 30000;
let dashTimer = null;

// --- 視圖切換（首頁 home / 監控 dash / 工作室 studio）：body[data-view] 為單一真相 -------
const VIEW_BTNS = { "#homeBtn": "home", "#viewDashBtn": "dash", "#viewStudioBtn": "studio" };

// 視圖變更回呼(覆審修正):home 模組訂閱以搬回 #stream——dashboard 不 import home
// (會成環),用註冊表解耦(同 deck.onRunningChange 範式)。
const _viewCbs = [];
export function onViewChange(cb) { _viewCbs.push(cb); }

export function setView(view) {
  document.body.dataset.view = view;
  Object.entries(VIEW_BTNS).forEach(([sel, v]) => {
    const btn = $(sel);
    if (!btn) return;
    const active = v === view;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  // 手機：底部分頁與視圖對齊（切到工作室時落在討論分頁；切到監控時落在監控分頁）
  const mv = document.body.dataset.mv;
  if (view === "studio" && mv === "dash") setMobileView("discussion");
  if (view === "dash" && mv && mv !== "dash") setMobileView("dash");
  if (view === "dash") {
    refreshDashboard();
    startDashTimer();
  } else {
    stopDashTimer();
  }
  for (const cb of _viewCbs) { try { cb(view); } catch { /* 回呼不得炸視圖切換 */ } }
}

function startDashTimer() {
  if (!dashTimer) dashTimer = setInterval(() => {
    // 背景分頁不白打 API；回到前景由下一輪補上。
    if (typeof document.hidden === "boolean" && document.hidden) return;
    refreshDashboard();
  }, REFRESH_MS);
}

function stopDashTimer() {
  if (dashTimer) { clearInterval(dashTimer); dashTimer = null; }
}

// --- 英雄狀態列：狀態球 + 大字狀態 + 心跳/派工/PR 預算子行 ------------------
// 純函式：由 /api/autopilot 回應算出英雄列顯示內容（供 .mjs 測試）。
export function heroModel(st) {
  const hb = st.heartbeat || {};
  const paused = !!st.paused;
  let orb = "run";
  let title = "執行中";
  if (paused) { orb = "paused"; title = "已暫停"; }
  else if (hb.state === "quota_sleep") { orb = "sleep"; title = "額度休眠"; }
  else if (hb.state === "idle") { orb = "idle"; title = "待命中"; }
  const sub = [];
  if (hb.task_id) {
    let cur = `正在做 #${hb.task_id}`;
    if (hb.current_expert) cur += `・輪到 ${hb.current_expert}`;
    if (Number.isFinite(Number(hb.turn_started_at)) && hb.turn_started_at) {
      cur += `・已跑 ${apElapsedText(Date.now() / 1000 - Number(hb.turn_started_at))}`;
    }
    sub.push(cur);
  }
  const sl = hb.sideline;
  if (sl && sl.task_id) sub.push(`旁路 #${sl.task_id}${sl.title ? ` ${sl.title}` : ""}`);
  if (hb.sleep_until) sub.push(`休眠至 ${new Date(hb.sleep_until * 1000).toLocaleTimeString()}`);
  sub.push(`派工 ${st.dispatch_mode === "auto" ? "auto" : "手動"}`);
  const pb = st.pr_budget || {};
  if (pb.cap) sub.push(`今日 PR ${pb.used ?? 0}/${pb.cap}`);
  if (st.dryrun) sub.push("dryrun");
  return { orb, title, sub: sub.join("・"), paused };
}

// 統計磁貼：完成率 + 五態計數（供 .mjs 測試）。
export function tilesModel(st) {
  const c = st.counts || {};
  const cs = st.completion || {};
  const rate = cs.rate == null ? "—" : `${Math.round(cs.rate * 100)}%`;
  return [
    { key: "rate", label: cs.total ? `完成率（近 ${cs.total}）` : "完成率", value: rate, tone: "accent" },
    { key: "pending", label: "待辦", value: String(c.pending || 0), tone: "" },
    { key: "in_progress", label: "進行中", value: String(c.in_progress || 0), tone: "info" },
    { key: "done", label: "完成", value: String(c.done || 0), tone: "good" },
    { key: "failed", label: "失敗", value: String(c.failed || 0), tone: c.failed ? "bad" : "" },
    { key: "parked", label: "停放", value: String(c.parked || 0), tone: "" },
  ];
}

function renderHero(st) {
  const m = heroModel(st);
  $("#dashOrb").dataset.state = m.orb;
  $("#dashState").textContent = m.title;
  $("#dashSub").textContent = m.sub;
  const btn = $("#dashToggle");
  btn.disabled = false;
  btn.textContent = m.paused ? "恢復" : "暫停";
  btn.dataset.paused = m.paused ? "1" : "0";
}

function renderTiles(st) {
  const box = $("#dashTiles");
  box.innerHTML = "";
  for (const t of tilesModel(st)) {
    const tile = document.createElement("div");
    tile.className = `dash-tile${t.tone ? ` tone-${t.tone}` : ""}`;
    appendTextEl(tile, "div", "dash-tile-v", t.value);
    appendTextEl(tile, "div", "dash-tile-k", t.label);
    box.appendChild(tile);
  }
}

// 部署漂移橫幅：與 Autopilot 面板同一判定（磁碟落後 origin / 行程未重載新碼）。
function renderDrift(st) {
  const box = $("#dashDrift");
  const d = st.deploy || {};
  const hb = st.heartbeat || {};
  const parts = [];
  if (Number(d.behind) > 0) parts.push(`磁碟碼落後 origin ${d.behind} 個 commit`);
  const running = hb.running_commit || "";
  if (running && d.disk_head && !String(d.disk_head).startsWith(running)) {
    parts.push(`行程尚未重載新碼（跑 ${String(running).slice(0, 8)}，磁碟 ${String(d.disk_head).slice(0, 8)}）`);
  }
  box.classList.toggle("hidden", !parts.length);
  box.textContent = parts.length ? `部署漂移：${parts.join("；")}——已合併的修法尚未進執行碼` : "";
}

// --- 近 30 天結果趨勢（每日 done/fail 堆疊長條；缺日不補零，與洞察面板同口徑） ---
function renderTrend(data) {
  // 委派共用趨勢圖原語（components/chart.js）；合計列由原語渲染。
  renderStackedTrend($("#dashTrend"), data.buckets || [], data.totals || {});
}

// --- provider 額度（每 provider 一列：名稱 + 各窗口用量 meter）---------------
function renderQuota(data) {
  const box = $("#dashQuota");
  box.innerHTML = "";
  const provs = data.providers || [];
  if (!provs.length) { appendTextEl(box, "span", "muted", "尚無 provider 資訊"); return; }
  for (const p of provs) {
    const row = document.createElement("div");
    row.className = "dash-quota-row";
    appendTextEl(row, "div", "dash-quota-name", p.label || p.key || "?");
    const wins = document.createElement("div");
    wins.className = "dash-quota-wins";
    const list = apQuotaWindows(p);
    if (!list.length) appendTextEl(wins, "span", "muted", "—");
    for (const w of list) {
      const cell = document.createElement("div");
      cell.className = "dash-quota-win";
      appendTextEl(cell, "span", "dash-quota-label", w.label);
      const pct = Math.min(100, Math.max(0, w.pct));
      const bar = document.createElement("div");
      bar.className = "dash-meter" + (pct >= 90 ? " crit" : pct >= 75 ? " warn" : "");
      bar.title = `${p.label || p.key} ${w.label} 已用 ${w.pct}%`;
      const fill = document.createElement("div");
      fill.className = "fill";
      fill.style.width = `${pct}%`;
      bar.appendChild(fill);
      cell.appendChild(bar);
      appendTextEl(cell, "span", "dash-quota-pct", `${Math.round(w.pct)}%`);
      wins.appendChild(cell);
    }
    row.appendChild(wins);
    box.appendChild(row);
  }
}

// --- 績效榜（per provider 平均分 + 通過率）----------------------------------
function renderAppraisal(data) {
  const box = $("#dashAppraisal");
  box.innerHTML = "";
  const provs = (data.summary || {}).providers || {};
  const keys = Object.keys(provs);
  if (!keys.length) { appendTextEl(box, "span", "muted", "尚無考核"); return; }
  keys.sort((a, b) => (provs[b].avg_score || 0) - (provs[a].avg_score || 0) || a.localeCompare(b));
  for (const k of keys) {
    const st = provs[k] || {};
    const row = document.createElement("div");
    row.className = "dash-appr-row";
    appendTextEl(row, "span", "dash-appr-name", k);
    appendTextEl(row, "span", "dash-appr-score", `★${st.avg_score ?? "?"}`);
    appendTextEl(row, "span", "dash-appr-meta muted",
      `${st.n || 0} 件${st.pass_rate == null ? "" : `・通過率 ${Math.round(st.pass_rate * 100)}%`}`);
    box.appendChild(row);
  }
}

// --- 最新動態（activity 前幾筆的精簡列）------------------------------------
function renderActivity(data) {
  const ul = $("#dashActivity");
  ul.innerHTML = "";
  const tasks = (data.tasks || []).slice(0, 8);
  if (!tasks.length) { ul.innerHTML = "<li class='muted'>尚無動態</li>"; return; }
  for (const t of tasks) {
    const li = document.createElement("li");
    li.className = "dash-act-item";
    appendTextEl(li, "span", `dash-act-dot st-${t.status || "pending"}`, "");
    const body = document.createElement("div");
    body.className = "dash-act-body";
    appendTextEl(body, "div", "dash-act-title", `#${t.id} ${t.title || ""}`);
    const meta = [];
    if (t.status) meta.push(ACT_STATUS_LABEL[t.status] || t.status);
    if (t.pr) meta.push(`PR #${t.pr}`);
    if (t.updated_at) meta.push(new Date(t.updated_at * 1000).toLocaleString());
    appendTextEl(body, "div", "dash-act-meta muted", meta.join("・"));
    li.appendChild(body);
    ul.appendChild(li);
  }
}

// --- 主更新：四端點各自容錯，任一失敗不拖垮整面 ------------------------------
export async function refreshDashboard() {
  try {
    const st = await (await fetch("/api/autopilot")).json();
    renderHero(st);
    renderTiles(st);
    renderDrift(st);
  } catch (e) {
    $("#dashOrb").dataset.state = "off";
    $("#dashState").textContent = "讀不到 Autopilot 狀態";
    $("#dashSub").textContent = "autopilot 服務可能未啟動";
    $("#dashToggle").disabled = true;
  }
  try {
    renderTrend(await (await fetch("/api/autopilot/audit-trend?days=30")).json());
  } catch (e) { $("#dashTrend").textContent = "讀取失敗"; }
  try {
    renderQuota(await (await fetch("/api/provider-quota")).json());
  } catch (e) { $("#dashQuota").textContent = ""; }
  try {
    renderAppraisal(await (await fetch("/api/appraisals")).json());
  } catch (e) { $("#dashAppraisal").textContent = ""; }
  try {
    renderActivity(await (await fetch("/api/autopilot/activity?limit=8")).json());
  } catch (e) { $("#dashActivity").innerHTML = ""; }
  const upd = $("#dashUpdated");
  if (upd) upd.textContent = `更新於 ${new Date().toLocaleTimeString()}（每 30 秒自動更新）`;
}

// --- 英雄列操作：暫停/恢復、分診（與 Autopilot 面板同端點）-------------------
export async function dashToggleAutopilot() {
  const paused = $("#dashToggle").dataset.paused === "1";
  await fetch(paused ? "/api/autopilot/resume" : "/api/autopilot/pause", { method: "POST" });
  toast(paused ? "已恢復 Autopilot" : "已暫停 Autopilot");
  await refreshDashboard();
}

export async function dashTriage() {
  try {
    const r = await fetch("/api/autopilot/triage", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { toast(d.detail || "分診失敗", "err"); return; }
    const s = d.stats || d;
    toast(`分診完成：重試 ${s.retried ?? 0}・復活 ${s.revived ?? 0}・歸檔 ${s.parked ?? 0}`);
  } catch (e) { toast("分診失敗", "err"); }
  await refreshDashboard();
}

// 入口 init 呼叫一次：綁定英雄列/視圖切換的事件。
export function bindDashboard() {
  $("#viewDashBtn").onclick = () => setView("dash");
  $("#viewStudioBtn").onclick = () => setView("studio");
  $("#dashToggle").onclick = dashToggleAutopilot;
  $("#dashTriage").onclick = dashTriage;
  $("#dashRefresh").onclick = refreshDashboard;
  $("#dashNewTalk").onclick = () => { setView("studio"); $("#requirement").focus(); };
  $("#dashMore").onclick = openAutopilot;
}
