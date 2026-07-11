// Autopilot 自主迴圈面板：狀態列、backlog、額度迷你條、績效榜、動態 timeline。
import { $, appendTextEl, icon, toast } from "../dom.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { openConfirmModal } from "../components/modal.js";

// 迷你狀態：縮成一條狀態列（手機浮在分頁列上方、桌機右下小卡），輕量輪詢保持計數新鮮
let apMiniTimer = null;
function clearApMini() {
  $("#autopilotPanel").classList.remove("mini");
  $("#apMini").classList.add("hidden");
  if (apMiniTimer) { clearInterval(apMiniTimer); apMiniTimer = null; }
}

export async function openAutopilot() {
  clearApMini();
  openDrawer("#autopilotPanel");
  await refreshAutopilot();
}

export function closeAutopilot() {
  clearApMini();
  closeDrawer("#autopilotPanel");
}

export function minimizeAutopilot() {
  $("#autopilotPanel").classList.add("mini");
  $("#apMini").classList.remove("hidden");
  if (!apMiniTimer) apMiniTimer = setInterval(refreshAutopilot, 20000);
}

export async function expandAutopilot() {
  clearApMini();
  await refreshAutopilot();
}

// autopilot 目標 repo（PR 連結用）；由 /api/autopilot 回應更新，取不到時退回核心 repo。
let apRepo = "x812033727/Ti";
let apHeartbeat = {};
// 任務狀態 → sprite 圖示名（dom.js icon()）；未知狀態退回中性圓點。
export const AP_STATUS_ICON = { pending: "clock", in_progress: "refresh", merging: "merge", done: "check", failed: "x", parked: "pause" };
function statusIconEl(status) {
  const name = AP_STATUS_ICON[status];
  if (name) return icon(name, "icon sys-ic st-" + status);
  const dot = document.createElement("span");
  dot.textContent = "•";
  return dot;
}

export async function refreshAutopilot() {
  try {
    const st = await (await fetch("/api/autopilot")).json();
    if (st.repo) apRepo = st.repo;
    const c = st.counts || {};
    // 近窗完成率（後端 completion_stats：done/(done+failed)，排除 parked/pending）。
    // 舊後端無此欄時容錯不顯示；rate 為 null（無終局任務）時顯示「—」。
    const cs = st.completion || {};
    const rateStr =
      cs.rate == null ? (cs.total ? "—" : "") : `完成率 ${Math.round(cs.rate * 100)}%（近 ${cs.total}）・`;
    // 心跳：/api/autopilot 的巢狀 heartbeat 物件（autopilot 主迴圈寫 status.json：
    // state=idle/running/quota_sleep、task_id、sleep_until）；兼容頂層欄位，缺省時容錯不顯示。
    const hbObj = st.heartbeat || {};
    apHeartbeat = hbObj;
    const hbState = hbObj.state || st.state;
    const hbSleep = hbObj.sleep_until || st.sleep_until;
    let hb = "";
    if (hbState) hb += `　心跳 ${hbState}`;
    if (hbObj.task_id) hb += `（任務 #${hbObj.task_id}）`;
    if (hbSleep) hb += `（休眠至 ${new Date(hbSleep * 1000).toLocaleTimeString()}）`;
    // 調查旁路（δ）：status.json 的 sideline 子欄，執行中才有值。
    const sl = hbObj.sideline;
    if (sl && sl.task_id) hb += `　旁路 #${sl.task_id}${sl.title ? ` ${sl.title}` : ""}`;
    // 每日 PR 預算透明化（F4）：舊後端無此欄時容錯不顯示。
    const pb = st.pr_budget || {};
    const pbStr = pb.cap ? `・今日 PR ${pb.used ?? 0}/${pb.cap}` : "";
    const stateEl = $("#apState");
    stateEl.innerHTML = "";
    stateEl.appendChild(icon(st.paused ? "pause" : "play", "icon sys-ic"));
    appendTextEl(
      stateEl,
      "span",
      "",
      `${st.paused ? "已暫停" : "執行中"}　${rateStr}待辦 ${c.pending || 0}・進行中 ${c.in_progress || 0}・` +
        `完成 ${c.done || 0}・失敗 ${c.failed || 0}${c.parked ? `・停放 ${c.parked}` : ""}${pbStr}` +
        `${st.dryrun ? "　(dryrun)" : ""}${hb}`,
    );
    renderDriftBanner(st.deploy, hbObj);
    $("#apToggle").textContent = st.paused ? "恢復" : "暫停";
    $("#apToggle").dataset.paused = st.paused ? "1" : "0";
    // 派工模式雙態鈕（舊後端無 dispatch_mode 欄位時容錯視為手動）。
    const dm = st.dispatch_mode === "auto" ? "auto" : "manual";
    $("#apDispatchMode").textContent = dm === "auto" ? "派工：auto" : "派工：手動";
    $("#apDispatchMode").dataset.mode = dm;
    const miniEl = $("#apMini");
    miniEl.innerHTML = "";
    miniEl.appendChild(icon(st.paused ? "pause" : "play", "icon sys-ic"));
    appendTextEl(miniEl, "span", "", `待辦 ${c.pending || 0}・進行中 ${c.in_progress || 0}`);
    const list = await (await fetch("/api/autopilot/backlog")).json();
    const ul = $("#apBacklog");
    ul.innerHTML = "";
    (list.tasks || []).slice().reverse().forEach((t) => {
      const li = document.createElement("li");
      li.className = "ap-bl-item";
      const text = document.createElement("span");
      text.className = "ap-bl-text";
      text.appendChild(statusIconEl(t.status));
      const tspan = document.createElement("span");
      tspan.textContent = ` #${t.id} ${t.title}　[${t.source}]`;
      text.appendChild(tspan);
      li.appendChild(text);
      li.appendChild(buildTaskActions(t));
      ul.appendChild(li);
    });
  } catch (e) {
    apHeartbeat = {};
    $("#apState").textContent = "讀取失敗（autopilot 服務可能未啟動）";
    $("#apMini").textContent = "讀取失敗";
  }
  // 額度迷你條、績效榜與動態 timeline 各自容錯：任一端點失敗不影響上方狀態列。
  await refreshApQuota();
  await refreshApAppraisal();
  await refreshApActivity();
}

// --- 部署漂移橫幅（F1）：已合併 ≠ 已上線,這是完成率事故的盲點 --------------
// deploy=drift_stats（disk_head/origin_head/behind/deferred）；running_commit=行程
// 執行中的碼（status.json）。三態:磁碟落後 origin / 行程落後磁碟(未重載) / 同步隱藏。
function renderDriftBanner(deploy, heartbeat) {
  const box = $("#apDrift");
  if (!box) return;
  const d = deploy || {};
  const parts = [];
  if (Number(d.behind) > 0) {
    const defer = d.deferred && d.deferred.deferrals ? `（autodeploy 已延後 ${d.deferred.deferrals} 次）` : "";
    parts.push(`磁碟碼落後 origin ${d.behind} 個 commit${defer}`);
  }
  const running = (heartbeat || {}).running_commit || "";
  if (running && d.disk_head && !String(d.disk_head).startsWith(running)) {
    parts.push(`行程尚未重載新碼（跑 ${String(running).slice(0, 8)}，磁碟 ${String(d.disk_head).slice(0, 8)}）`);
  }
  if (!parts.length) {
    box.classList.add("hidden");
    box.textContent = "";
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = "";
  box.appendChild(icon("alert", "icon sys-ic"));
  appendTextEl(box, "span", "", `部署漂移：${parts.join("；")}——已合併的修法尚未進執行碼`);
}

// --- 一鍵分診（F3）：POST /api/autopilot/triage,回報三桶統計 -----------------
export async function triageFailedNow() {
  try {
    const r = await fetch("/api/autopilot/triage", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      toast(d.detail || "分診失敗", "err");
      return;
    }
    const s = d.stats || d;
    toast(`分診完成：重試 ${s.retried ?? 0}・復活 ${s.revived ?? 0}・歸檔 ${s.parked ?? 0}`);
  } catch (e) {
    toast("分診失敗", "err");
  }
  await refreshAutopilot();
}

// --- 績效榜（讀 /api/appraisals：per provider 平均分/樣本數/QA 通過率）--------
export async function refreshApAppraisal() {
  const box = $("#apAppraisal");
  if (!box) return;
  try {
    const data = await (await fetch("/api/appraisals")).json();
    const provs = (data.summary || {}).providers || {};
    box.innerHTML = "";
    const keys = Object.keys(provs);
    if (!keys.length) {
      appendTextEl(box, "span", "muted", "尚無考核");
      return;
    }
    // 平均分高者在前（同分按名稱穩定排序）。
    keys.sort((a, b) => (provs[b].avg_score || 0) - (provs[a].avg_score || 0) || a.localeCompare(b));
    for (const k of keys) {
      const st = provs[k] || {};
      const pass = st.pass_rate == null ? "" : `・通過率 ${Math.round(st.pass_rate * 100)}%`;
      appendTextEl(box, "div", "ap-appraisal-row", `${k}　★${st.avg_score ?? "?"}（${st.n || 0} 件${pass}）`);
    }
  } catch (e) {
    // 考核端點不可用（舊後端）時容錯：區塊留空，不影響其餘面板。
    box.textContent = "";
  }
}

// --- provider 額度迷你條（讀 /api/provider-quota，四家 5h/7d 用量）----------
export function apQuotaWindows(p) {
  // 相容三種 rate_limits 形態：單帳號 window 式、claude 多帳號（取在線帳號）、
  // antigravity bucket 式（取前兩個 bucket 當迷你條）。
  let rl = p.rate_limits;
  if (!rl && Array.isArray(p.accounts)) {
    const act = p.accounts.filter((a) => a.active && a.rate_limits)[0];
    if (act) rl = act.rate_limits;
  }
  rl = rl || {};
  const wins = [];
  if (Array.isArray(rl.buckets)) {
    rl.buckets.slice(0, 2).forEach((b) => wins.push({ label: b.label || "", pct: Number(b.used_percentage) }));
  } else {
    if (rl.five_hour) wins.push({ label: "5h", pct: Number(rl.five_hour.used_percentage) });
    if (rl.seven_day) wins.push({ label: "7d", pct: Number(rl.seven_day.used_percentage) });
    // 按模型 scoped 的專屬限額（如 Fable 週限）：與全域窗獨立顯示，PM/使用者一眼看到。
    for (const [name, mw] of Object.entries(rl.models || {})) {
      if (mw) wins.push({ label: name, pct: Number(mw.used_percentage) });
    }
  }
  return wins.filter((w) => Number.isFinite(w.pct));
}

export async function refreshApQuota() {
  const box = $("#apQuota");
  if (!box) return;
  try {
    const data = await (await fetch("/api/provider-quota")).json();
    box.innerHTML = "";
    for (const p of data.providers || []) {
      const cell = document.createElement("div");
      cell.className = "ap-quota-cell";
      appendTextEl(cell, "span", "ap-quota-name", p.label || p.key || "?");
      const wins = apQuotaWindows(p);
      if (!wins.length) appendTextEl(cell, "span", "ap-quota-win", "—");
      for (const w of wins) {
        appendTextEl(cell, "span", "ap-quota-win", w.label);
        const pct = Math.min(100, Math.max(0, w.pct));
        const bar = document.createElement("div");
        bar.className = "ap-quota-bar" + (pct >= 90 ? " crit" : pct >= 75 ? " warn" : "");
        bar.title = `${p.label || p.key} ${w.label} 已用 ${w.pct}%`;
        const fill = document.createElement("div");
        fill.className = "fill";
        fill.style.width = `${pct}%`;
        bar.appendChild(fill);
        cell.appendChild(bar);
      }
      box.appendChild(cell);
    }
  } catch (e) {
    box.textContent = "";
  }
}

// --- 工作室動態 timeline（讀 /api/autopilot/activity）----------------------
function apChip(parent, text, cls) {
  if (text) appendTextEl(parent, "span", `ap-chip${cls ? ` ${cls}` : ""}`, text);
}

export function apElapsedText(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  if (s < 60) return `${s}秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}分${String(s % 60).padStart(2, "0")}秒`;
  const h = Math.floor(m / 60);
  return `${h}小時${String(m % 60).padStart(2, "0")}分`;
}

function renderCurrentTurn(ul, heartbeat) {
  const hb = heartbeat || {};
  const expert = typeof hb.current_expert === "string" ? hb.current_expert.trim() : "";
  const rawStarted = hb.turn_started_at;
  if (rawStarted == null || rawStarted === "") return;
  const started = Number(rawStarted);
  if (!expert || !Number.isFinite(started)) return;

  const elapsed = apElapsedText(Date.now() / 1000 - started);
  const li = document.createElement("li");
  li.className = "ap-item ap-current-turn";
  const head = document.createElement("div");
  head.className = "ap-item-head";
  head.appendChild(icon("play", "icon sys-ic"));
  appendTextEl(head, "strong", "ap-item-title", `目前輪到 ${expert}`);
  appendTextEl(head, "span", "ap-item-time muted", `已跑 ${elapsed}`);
  li.appendChild(head);
  ul.appendChild(li);
}

export async function refreshApActivity(heartbeat = apHeartbeat) {
  const ul = $("#apActivity");
  if (!ul) return;
  try {
    const data = await (await fetch("/api/autopilot/activity?limit=50")).json();
    ul.innerHTML = "";
    renderCurrentTurn(ul, heartbeat);
    // 後端已按 updated_at 倒序（最新在最上）。
    for (const t of data.tasks || []) {
      const li = document.createElement("li");
      li.className = "ap-item";
      const head = document.createElement("div");
      head.className = "ap-item-head";
      head.appendChild(statusIconEl(t.status));
      appendTextEl(head, "strong", "ap-item-title", `#${t.id} ${t.title || ""}`);
      if (t.updated_at) {
        appendTextEl(head, "span", "ap-item-time muted", new Date(t.updated_at * 1000).toLocaleString());
      }
      li.appendChild(head);
      const chips = document.createElement("div");
      chips.className = "ap-item-chips";
      const tu = t.token_usage || {};
      Object.keys(tu.by_provider || {}).forEach((prov) => apChip(chips, prov, "prov"));
      Object.keys(tu.by_model || {}).forEach((model) => apChip(chips, model, ""));
      const ttftRaw = tu.ttft_s;
      if (ttftRaw != null) {
        const ttft = Number(ttftRaw);
        if (Number.isFinite(ttft)) apChip(chips, `TTFT ${ttft.toFixed(3)}s`, "ttft");
      }
      // per-task 成本（F4）：history 累計的 cost_usd，缺欄（舊後端/無計價）容錯不顯示。
      const costRaw = tu.cost_usd;
      if (costRaw != null) {
        const cost = Number(costRaw);
        if (Number.isFinite(cost) && cost > 0) apChip(chips, `$${cost.toFixed(2)}`, "cost");
      }
      if (t.pr) {
        const a = document.createElement("a");
        a.className = "ap-chip ap-pr";
        a.href = `https://github.com/${apRepo}/pull/${t.pr}`;
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = `PR #${t.pr}`;
        chips.appendChild(a);
      }
      if (chips.childNodes.length) li.appendChild(chips);
      if (t.deploy_msg) {
        const note = document.createElement("div");
        note.className = "ap-item-note";
        note.appendChild(icon("send", "icon sys-ic"));
        const nspan = document.createElement("span");
        nspan.textContent = t.deploy_msg;
        note.appendChild(nspan);
        li.appendChild(note);
      }
      if (t.note && t.note !== t.deploy_msg) {
        appendTextEl(li, "div", `ap-item-note${t.status === "failed" ? " bad" : " muted"}`, t.note);
      }
      const sc = t.scorecard;
      if (sc) {
        const demo = sc.demo_passed === true ? "・Demo ✓" : sc.demo_passed === false ? "・Demo ✗" : "";
        appendTextEl(
          li,
          "div",
          "ap-item-score muted",
          `記分卡：任務 ${sc.tasks_done || 0}/${sc.tasks_total || 0}・QA ${sc.qa_pass || 0}/${sc.qa_total || 0}${demo}`,
        );
      }
      ul.appendChild(li);
    }
  } catch (e) {
    // activity 端點不可用（舊後端）時容錯：timeline 留空，不影響其餘面板。
    ul.innerHTML = "";
  }
}

export async function toggleAutopilot() {
  const paused = $("#apToggle").dataset.paused === "1";
  await fetch(paused ? "/api/autopilot/resume" : "/api/autopilot/pause", { method: "POST" });
  await refreshAutopilot();
}

export async function toggleDispatchMode() {
  const next = $("#apDispatchMode").dataset.mode === "auto" ? "manual" : "auto";
  await fetch("/api/autopilot/dispatch-mode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: next }),
  });
  await refreshAutopilot();
}

// 看板手動操作(C1):依任務狀態渲染 retry/park/unpark 小按鈕 + P0/P1/P2 優先級 select。
// in_progress/merging 由 runner/reconciler 持有狀態機,只給改優先級(park/retry 後端也會 409)。
function buildTaskActions(t) {
  const box = document.createElement("span");
  box.className = "ap-bl-actions";
  const sel = document.createElement("select");
  sel.title = "優先級";
  [["0", "P0"], ["1", "P1"], ["2", "P2"]].forEach(([v, label]) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = label;
    if (String(t.priority ?? 1) === v) o.selected = true;
    sel.appendChild(o);
  });
  sel.addEventListener("change", () => taskAction(t.id, "priority", { priority: parseInt(sel.value, 10) }));
  box.appendChild(sel);
  const mk = (label, action, opts = {}) => {
    const b = document.createElement("button");
    b.className = "ghost ap-bl-btn";
    b.textContent = label;
    b.title = opts.title || label;
    b.addEventListener("click", async () => {
      if (opts.confirm) {
        const ok = await openConfirmModal({ title: opts.confirm, message: `#${t.id} ${t.title}`, danger: true });
        if (!ok) return;
      }
      taskAction(t.id, action);
    });
    box.appendChild(b);
  };
  if (t.status === "failed" || t.status === "parked") mk("重試", "retry", { title: "退回 pending 並歸零重試次數" });
  if (t.status === "pending" || t.status === "failed") mk("歸檔", "park", { confirm: "歸檔此任務?" });
  if (t.status === "parked") mk("取回", "unpark", { title: "取回為 pending" });
  return box;
}

export async function taskAction(id, action, extra = {}) {
  const r = await fetch(`/api/autopilot/task/${id}/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...extra }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    toast(d.detail || `${action} 失敗`, "err");
  } else {
    toast(`#${id} ${action} 完成`);
  }
  await refreshAutopilot();
}

export async function addAutopilotTask() {
  const title = $("#apTaskTitle").value.trim();
  if (!title) return;
  const detail = ($("#apTaskDetail")?.value || "").trim();
  const priority = parseInt($("#apTaskPriority")?.value ?? "1", 10);
  const type = $("#apTaskType")?.value || "improvement";
  const r = await fetch("/api/autopilot/task", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, detail, priority, type }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    toast(d.detail || "新增失敗", "err");
    return;
  }
  $("#apTaskTitle").value = "";
  if ($("#apTaskDetail")) $("#apTaskDetail").value = "";
  if ($("#apTaskPriority")) $("#apTaskPriority").value = "1";
  if ($("#apTaskType")) $("#apTaskType").value = "improvement";
  await refreshAutopilot();
}
