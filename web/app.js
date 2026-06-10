// Ti Studio 前端：連 WebSocket，把工作室事件渲染成討論串 / 看板 / 檔案面板。

const $ = (sel) => document.querySelector(sel);
const stream = $("#stream");
const expertList = $("#expertList");
const phaseEl = $("#phase");
const startBtn = $("#startBtn");
const stopBtn = $("#stopBtn");
const reqInput = $("#requirement");
const interjectInput = $("#interjectInput");
const interjectBtn = $("#interjectBtn");

let ws = null;
let sessionId = null;

function setPhase(text) { phaseEl.textContent = text; }

function setRunning(running) {
  startBtn.disabled = running;
  stopBtn.disabled = !running;
  interjectInput.disabled = !running;
  interjectBtn.disabled = !running;
}

function scrollStream() { stream.scrollTop = stream.scrollHeight; }

// 並行支線多欄渲染：帶 task_id 的發言/工具進各自的欄位，其餘事件照常進主時間軸。
let laneBoard = null;
const laneCols = {};

function clearStream() {
  stream.innerHTML = "";
  laneBoard = null;
  for (const k of Object.keys(laneCols)) delete laneCols[k];
}

// 取得某支線欄位的內容容器（首次出現時建立「支線看板」與該欄）。
function laneBody(taskId) {
  if (!laneBoard) {
    laneBoard = document.createElement("div");
    laneBoard.className = "lanes-board";
    stream.appendChild(laneBoard);
  }
  if (!laneCols[taskId]) {
    const col = document.createElement("div");
    col.className = "lane-col lane-" + (taskId % 6);
    col.innerHTML = `<div class="lane-col-head">支線 #${taskId}</div><div class="lane-col-body"></div>`;
    laneBoard.appendChild(col);
    laneCols[taskId] = col.querySelector(".lane-col-body");
  }
  return laneCols[taskId];
}

// 事件落點：帶 task_id → 對應支線欄；否則 → 主時間軸。
function sink(p) {
  return p && p.task_id != null ? laneBody(p.task_id) : stream;
}

function clearBoard() {
  document.querySelectorAll(".col .cards").forEach((c) => (c.innerHTML = ""));
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function renderRoster(roster) {
  expertList.innerHTML = "";
  for (const r of roster) {
    const el = document.createElement("div");
    el.className = "expert";
    el.dataset.key = r.key;
    el.dataset.status = "idle";
    el.innerHTML = `
      <div class="av">${r.avatar}</div>
      <div class="meta"><div class="nm">${r.name}</div><div class="tt">${r.title}</div></div>
      <div class="dot"></div>`;
    expertList.appendChild(el);
  }
}

function setExpertStatus(key, status) {
  const el = expertList.querySelector(`.expert[data-key="${key}"]`);
  if (!el) return;
  el.dataset.status = status;
  expertList.querySelectorAll(".expert").forEach((e) => e.classList.remove("active"));
  if (status !== "idle") el.classList.add("active");
}

function addMessage(p) {
  const el = document.createElement("div");
  el.className = "msg" + (p.task_id != null ? " lane lane-" + (p.task_id % 6) : "");
  el.innerHTML = `
    <div class="av">${p.avatar}</div>
    <div class="body"><div class="who">${p.name}</div><div class="txt"></div></div>`;
  el.querySelector(".txt").textContent = p.text;
  sink(p).appendChild(el);
  scrollStream();
}

function addTool(p) {
  const el = document.createElement("div");
  el.className = "tool" + (p.task_id != null ? " lane lane-" + (p.task_id % 6) : "");
  el.innerHTML = `<span class="badge">${p.tool}</span><span></span>`;
  el.querySelector("span:last-child").textContent = p.summary;
  sink(p).appendChild(el);
  scrollStream();
}

function addSystem(text) {
  const el = document.createElement("div");
  el.className = "sys";
  el.textContent = text;
  stream.appendChild(el);
  scrollStream();
}

function addResult(passed, detail, log) {
  const el = document.createElement("div");
  el.className = "result " + (passed ? "pass" : "fail");
  el.textContent = (passed ? "✅ " : "❌ ") + detail;
  if (log) {
    const det = document.createElement("details");
    det.className = "log";
    det.innerHTML = "<summary>查看 log</summary>";
    const pre = document.createElement("pre");
    pre.textContent = log;
    det.appendChild(pre);
    el.appendChild(det);
  }
  stream.appendChild(el);
  scrollStream();
}

function addHuman(text) {
  const el = document.createElement("div");
  el.className = "msg human";
  el.innerHTML = `
    <div class="av">🙋</div>
    <div class="body"><div class="who">你（插話）</div><div class="txt"></div></div>`;
  el.querySelector(".txt").textContent = text;
  stream.appendChild(el);
  scrollStream();
}

function addCommit(p) {
  const el = document.createElement("div");
  el.className = "commit";
  el.innerHTML = `<span class="hash">⎇ ${p.hash}</span><span></span>`;
  el.querySelector("span:last-child").textContent = p.message;
  stream.appendChild(el);
  scrollStream();
}

function addDemo(p) {
  const el = document.createElement("div");
  el.className = "demo " + (p.passed ? "pass" : "fail");
  el.innerHTML = `<div class="demohead">${p.passed ? "▶️ Demo 執行成功" : "▶️ Demo 執行失敗"} <code></code></div>`;
  el.querySelector("code").textContent = p.command + "  (exit " + p.exit_code + ")";
  const pre = document.createElement("pre");
  pre.textContent = p.output || "（無輸出）";
  el.appendChild(pre);
  stream.appendChild(el);
  scrollStream();
}

const CI_LABELS = {
  pass: ["✅ CI 通過", "pass"],
  none: ["✅ 無 CI 檢查，直接合併", "pass"],
  fail: ["❌ CI 未通過", "fail"],
  error: ["⚠️ CI 等待逾時/出錯，保留 PR 待人工", "fail"],
  merged: ["🎉 已自動合併（squash + 刪分支）", "pass"],
  merge_failed: ["❌ 合併失敗", "fail"],
  giveup: ["🛑 CI 連續失敗達上限，保留 PR 待人工", "fail"],
};

function addCI(p) {
  const [label, cls] = CI_LABELS[p.state] || [p.state || "CI", "fail"];
  const round = p.attempt && p.rounds ? `（第 ${p.attempt}/${p.rounds} 輪）` : "";
  const el = document.createElement("div");
  el.className = "ci result " + cls;
  el.textContent = "🔁 " + label + round;
  if (p.detail) {
    const det = document.createElement("details");
    det.className = "log";
    det.innerHTML = "<summary>查看詳情</summary>";
    const pre = document.createElement("pre");
    pre.textContent = p.detail;
    det.appendChild(pre);
    el.appendChild(det);
  }
  stream.appendChild(el);
  scrollStream();
}

function renderBoard(columns) {
  for (const [col, items] of Object.entries(columns)) {
    const wrap = document.querySelector(`.col[data-col="${col}"] .cards`);
    if (!wrap) continue;
    wrap.innerHTML = "";
    for (const it of items) {
      const c = document.createElement("div");
      c.className = "card";
      c.textContent = it.title;
      wrap.appendChild(c);
    }
  }
}

async function refreshFiles() {
  if (!sessionId) return;
  try {
    const res = await fetch(`/api/workspace/${sessionId}/files`);
    const data = await res.json();
    const list = $("#fileList");
    list.innerHTML = "";
    for (const f of data.files) {
      const li = document.createElement("li");
      li.textContent = f;
      li.onclick = () => viewFile(f);
      list.appendChild(li);
    }
    const btn = $("#downloadBtn");
    if (btn) btn.classList.toggle("hidden", data.files.length === 0);
  } catch (e) { /* 忽略 */ }
}

function downloadWorkspace() {
  if (!sessionId) return;
  // 透過隱藏連結觸發瀏覽器下載；同源 cookie 會自動帶上（門禁啟用時）。
  const a = document.createElement("a");
  a.href = `/api/workspace/${sessionId}/download`;
  a.download = `workspace-${sessionId}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function viewFile(path) {
  const res = await fetch(`/api/workspace/${sessionId}/file?path=${encodeURIComponent(path)}`);
  if (!res.ok) return;
  const data = await res.json();
  $("#fileView").textContent = data.content;
}

function handleEvent(ev) {
  if (ev.session_id) sessionId = ev.session_id;
  const p = ev.payload || {};
  switch (ev.type) {
    case "session_started":
      clearStream();
      renderRoster(p.roster || []);
      addSystem("🛠️ 工作室開工：" + (p.requirement || ""));
      if (p.repo_url) addSystem("📦 既有專案：" + p.repo_url);
      break;
    case "phase_change":
      setPhase(p.phase);
      addSystem(`— ${p.phase}${p.detail ? "：" + p.detail : ""} —`);
      break;
    case "expert_status":
      setExpertStatus(p.speaker, p.status);
      break;
    case "expert_message":
      addMessage(p);
      break;
    case "tool_use":
      addTool(p);
      refreshFiles();
      break;
    case "board_update":
      renderBoard(p.columns || {});
      break;
    case "run_result":
      addResult(p.passed, p.detail, p.log);
      break;
    case "demo_result":
      addDemo(p);
      refreshFiles();
      break;
    case "git_commit":
      addCommit(p);
      break;
    case "human_message":
      addHuman(p.text);
      break;
    case "critic_review":
      if (p.passed) {
        addSystem("🔍 異議檢查放行（" + (p.gate || "") + " 視角）");
      } else {
        addSystem("🛑 異議檢查退回（" + (p.gate || "") + " 視角）：" + (p.text || ""));
      }
      break;
    case "huddle":
      if (p.limitation) {
        addSystem("🚧 已知限制：任務「" + (p.title || "") + "」huddle 後仍未通過。");
      } else {
        const who = (p.participants || []).join("、");
        addSystem("🧩 卡關討論：任務「" + (p.title || "") + "」召集 " + who + " 找替代方案。");
        if (p.conclusion) addSystem(p.conclusion);
      }
      break;
    case "task_status":
      // 看板由 board_update 全量刷新，這裡不需額外處理
      break;
    case "retrospective":
      addSystem("📋 檢討：" + (p.text || ""));
      break;
    case "publish_result":
      renderPublish(p);
      break;
    case "ci_result":
      addCI(p);
      break;
    case "done":
      setPhase(p.stopped ? "⏹ 已停止" : (p.completed ? "✅ 已完成" : "⚠️ 結束（未完全達標）"));
      addSystem(p.stopped ? "已依指示停止。" : (p.completed ? "🎉 專案完成！" : "專案結束，仍有未達標項目。"));
      refreshFiles();
      if (!replaying && p.completed && publishConfigured) addPublishButton(sessionId);
      setRunning(false);
      break;
    case "error":
      addSystem("⚠️ 錯誤：" + (p.message || "未知錯誤"));
      toast(p.message || "發生錯誤", "err");
      setRunning(false);
      break;
  }
}

function start() {
  const requirement = reqInput.value.trim();
  if (!requirement) { reqInput.focus(); return; }
  const repoUrl = $("#repoUrl").value.trim();
  replaying = false;
  closeHistory();
  setRunning(true);
  setPhase("連線中…");

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => ws.send(JSON.stringify({ requirement, repo_url: repoUrl }));
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
  ws.onerror = () => { addSystem("⚠️ 連線發生錯誤"); toast("WebSocket 連線錯誤", "err"); setRunning(false); };
}

function sendInterject() {
  const text = interjectInput.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "interject", text }));
  interjectInput.value = "";
}

function stop() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" }));
  stopBtn.disabled = true;
  toast("已送出停止指令", "");
}

// --- 歷史存檔 / 重播 ---------------------------------------------------
const historyPanel = $("#historyPanel");
const historyList = $("#historyList");
let replaying = false;

const STATUS_LABEL = {
  running: "⏳ 執行中", completed: "✅ 完成", incomplete: "⚠️ 未達標",
  stopped: "⏹ 已停止", error: "❌ 錯誤",
};

async function openHistory() {
  historyPanel.classList.remove("hidden");
  historyList.innerHTML = "<li class='muted'>載入中…</li>";
  try {
    const data = await (await fetch("/api/history")).json();
    renderHistory(data.sessions || []);
  } catch (e) {
    historyList.innerHTML = "<li class='muted'>無法載入歷史</li>";
  }
}

function renderHistory(sessions) {
  if (!sessions.length) { historyList.innerHTML = "<li class='muted'>尚無歷史紀錄</li>"; return; }
  historyList.innerHTML = "";
  for (const s of sessions) {
    const li = document.createElement("li");
    const when = s.started_at ? new Date(s.started_at * 1000).toLocaleString() : "";
    li.innerHTML = `
      <div class="h-main">
        <div class="h-req"></div>
        <div class="h-meta"><span class="h-status status-${s.status}">${STATUS_LABEL[s.status] || s.status}</span>
          <span>${s.n_events || 0} 事件</span><span>${when}</span></div>
      </div>
      <button class="h-del" title="刪除此 session（含產出檔案）">🗑</button>`;
    li.querySelector(".h-req").textContent = s.requirement || "(無需求)";
    li.querySelector(".h-main").onclick = () => replaySession(s.session_id);
    li.querySelector(".h-del").onclick = (e) => {
      e.stopPropagation();
      deleteSession(s.session_id, s.status);
    };
    historyList.appendChild(li);
  }
}

async function deleteSession(sid, status) {
  if (status === "running") { toast("執行中的 session 無法刪除", "err"); return; }
  if (!confirm("刪除此 session？產出檔案（workspace）也會一併刪除，無法復原。")) return;
  try {
    const r = await fetch(`/api/history/${sid}`, { method: "DELETE" });
    if (r.ok) { toast("已刪除", "ok"); openHistory(); }
    else toast("刪除失敗", "err");
  } catch (e) { toast("刪除失敗", "err"); }
}

async function cleanupCompleted() {
  if (!confirm("清除所有「✅ 已完成」的 session？產出檔案也會一併刪除，無法復原。")) return;
  try {
    const r = await fetch("/api/history/cleanup/completed", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    toast(`已清除 ${d.deleted ?? 0} 筆已完成 session`, "ok");
    openHistory();
  } catch (e) { toast("清除失敗", "err"); }
}

async function replaySession(sid) {
  if (replaying) return;
  historyPanel.classList.add("hidden");
  if (ws && ws.readyState === WebSocket.OPEN) ws.close();
  let events = [];
  let requirement = "";
  try {
    const data = await (await fetch(`/api/history/${sid}/events`)).json();
    events = data.events || [];
    requirement = (data.meta && data.meta.requirement) || "";
  } catch (e) { addSystem("⚠️ 無法載入此 session"); return; }

  // 直接一次把整段歷史渲染完，畫面停在最底（最新）；要看過程往上滑即可。
  // replaying 旗標維持為 true，讓 handleEvent 的 done case 不會替歷史 session 補出發佈鈕。
  replaying = true;
  setRunning(false);
  clearStream();
  clearBoard();
  addSystem("📜 歷史紀錄：" + (requirement || sid));
  for (let i = 0; i < events.length; i++) handleEvent(events[i]);
  replaying = false;
  setPhase("📜 歷史紀錄");
  scrollStream();
}

function closeHistory() { historyPanel.classList.add("hidden"); }

// --- Autopilot 自主迴圈 ------------------------------------------------
const autopilotPanel = $("#autopilotPanel");

async function openAutopilot() {
  autopilotPanel.classList.remove("hidden");
  await refreshAutopilot();
}

function closeAutopilot() { autopilotPanel.classList.add("hidden"); }

async function refreshAutopilot() {
  try {
    const st = await (await fetch("/api/autopilot")).json();
    const c = st.counts || {};
    $("#apState").textContent =
      `${st.paused ? "⏸ 已暫停" : "▶ 執行中"}　待辦 ${c.pending || 0}・進行中 ${c.in_progress || 0}・` +
      `完成 ${c.done || 0}・失敗 ${c.failed || 0}${st.dryrun ? "　(dryrun)" : ""}`;
    $("#apToggle").textContent = st.paused ? "恢復" : "暫停";
    $("#apToggle").dataset.paused = st.paused ? "1" : "0";
    const list = await (await fetch("/api/autopilot/backlog")).json();
    const ul = $("#apBacklog");
    ul.innerHTML = "";
    (list.tasks || []).slice().reverse().forEach((t) => {
      const li = document.createElement("li");
      const icon = { pending: "🕓", in_progress: "⚙️", done: "✅", failed: "❌" }[t.status] || "•";
      li.textContent = `${icon} #${t.id} ${t.title}　[${t.source}]`;
      ul.appendChild(li);
    });
  } catch (e) {
    $("#apState").textContent = "讀取失敗（autopilot 服務可能未啟動）";
  }
}

async function toggleAutopilot() {
  const paused = $("#apToggle").dataset.paused === "1";
  await fetch(paused ? "/api/autopilot/resume" : "/api/autopilot/pause", { method: "POST" });
  await refreshAutopilot();
}

async function addAutopilotTask() {
  const title = $("#apTaskTitle").value.trim();
  if (!title) return;
  await fetch("/api/autopilot/task", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  $("#apTaskTitle").value = "";
  await refreshAutopilot();
}

// --- 運維指標 ----------------------------------------------------------
const metricsPanel = $("#metricsPanel");

async function openMetrics() {
  metricsPanel.classList.remove("hidden");
  await refreshMetrics();
}

function closeMetrics() { metricsPanel.classList.add("hidden"); }

async function refreshMetrics() {
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

// --- 發佈到 GitHub -----------------------------------------------------
let publishConfigured = false;
let mergeEnabled = false;

async function loadPublishConfig() {
  try {
    const cfg = await (await fetch("/api/publish/config")).json();
    publishConfigured = !!cfg.configured;
    mergeEnabled = !!cfg.merge;
  } catch (e) { publishConfigured = false; mergeEnabled = false; }
}

function addPublishButton(sid) {
  const wrap = document.createElement("div");
  wrap.className = "publish-cta";
  const btn = document.createElement("button");
  // merge 開關開啟時，明示這顆按鈕會「發佈並合併」（合併後可再一鍵重啟）。
  btn.textContent = mergeEnabled ? "🚀 發佈並合併到 GitHub" : "🚀 發佈成果到 GitHub";
  btn.onclick = async () => {
    btn.disabled = true; btn.textContent = mergeEnabled ? "發佈並合併中…" : "發佈中…";
    try {
      const res = await (await fetch(`/api/publish/${sid}`, { method: "POST" })).json();
      // 手動發佈回傳 publisher 的 to_dict()，renderPublish 會依 outcome 顯示合併結局徽章。
      renderPublish(res);
    } catch (e) { renderPublish({ ok: false, detail: "發佈請求失敗" }); }
    btn.remove();
  };
  wrap.appendChild(btn);
  stream.appendChild(wrap);
  scrollStream();
}

// 合併結局 → 可視化徽章（對應 publisher.MergeOutcome，讓四／六種結局清楚可區分，
// 不再只看 merged=false 糊成一團）。
const OUTCOME_BADGE = {
  merged: "✅ 已合併",
  ci_failed: "❌ CI 未過",
  blocked: "🚫 被保護擋下",
  conflict: "⚠️ 衝突／分支落後",
  timeout: "⏱️ 等待 CI 逾時",
  error: "🛑 API／網路錯誤",
};

function renderPublish(p) {
  const el = document.createElement("div");
  el.className = "publish " + (p.ok ? "ok" : "fail");
  let html = (p.ok ? "🚀 " : "⚠️ ") + (p.detail || "");
  if (p.branch) html += `　<code>${p.branch}</code>`;
  // 優先依 outcome 顯示明確徽章；無 outcome（未嘗試合併）時退回舊的 merged 判斷。
  const badge = p.outcome ? OUTCOME_BADGE[p.outcome] : (p.merged ? OUTCOME_BADGE.merged : "");
  if (badge) html += `　<span class="merge-outcome ${p.outcome || (p.merged ? "merged" : "")}">${badge}</span>`;
  el.innerHTML = html;
  if (p.pr_url) {
    const a = document.createElement("a");
    a.href = p.pr_url; a.target = "_blank"; a.textContent = "查看 PR ↗";
    el.appendChild(document.createTextNode("　")); el.appendChild(a);
  }
  stream.appendChild(el);
  scrollStream();
}

// --- 重新部署重啟（設定面板常駐入口）---------------------------------
// 拉取主 repo 最新 main 並自我重啟，讓合併後的新程式碼生效。後端：POST /api/redeploy。
async function redeployNow() {
  const btn = $("#redeployBtn");
  const status = $("#redeployStatus");
  if (!confirm("確定重新部署？服務會重啟，進行中的工作與連線會中斷。")) return;
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "重新部署中…";
  status.className = "redeploy-status muted";
  status.textContent = "正在拉取最新 main 並重啟…";
  try {
    const r = await (await fetch("/api/redeploy", { method: "POST" })).json();
    status.className = "redeploy-status " + (r.ok ? "ok" : "fail");
    status.textContent = (r.ok ? "♻️ " : "⚠️ ") + (r.detail || "");
  } catch (e) {
    // 重啟成功時連線會中斷，請求可能無法正常回傳——這通常代表已在重啟。
    status.className = "redeploy-status muted";
    status.textContent = "♻️ 已送出重新部署，服務可能正在重啟，稍後重新整理頁面。";
  }
  btn.disabled = false;
  btn.textContent = prev;
}

// --- 設定（API key / provider / 模型 / GitHub token）-------------------
const settingsPanel = $("#settingsPanel");
const settingsForm = $("#settingsForm");

async function openSettings() {
  settingsPanel.classList.remove("hidden");
  settingsForm.innerHTML = "<div class='muted'>載入中…</div>";
  try {
    const data = await (await fetch("/api/settings")).json();
    renderSettings(data.fields || []);
  } catch (e) {
    settingsForm.innerHTML = "<div class='muted'>無法載入設定</div>";
  }
  refreshPwStatus();
}

async function refreshPwStatus() {
  const status = $("#pwStatus");
  const curRow = $("#pwCurrentRow");
  try {
    const s = await (await fetch("/api/auth/status")).json();
    if (s.auth_enabled) {
      status.textContent = "目前已啟用門禁。變更密碼需先輸入目前密碼。";
      curRow.classList.remove("hidden");
    } else {
      status.textContent = "目前未啟用門禁。設定一組密碼即可啟用登入保護。";
      curRow.classList.add("hidden");
    }
  } catch (e) {
    status.textContent = "";
  }
}

async function savePassword() {
  const hint = $("#pwHint");
  const cur = $("#pwCurrent").value;
  const next = $("#pwNew").value;
  const confirm = $("#pwConfirm").value;
  if (next.length < 4) { hint.textContent = "新密碼至少 4 個字元"; return; }
  if (next !== confirm) { hint.textContent = "兩次輸入的新密碼不一致"; return; }
  hint.textContent = "變更中…";
  try {
    const res = await (await fetch("/api/auth/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: cur, new_password: next }),
    })).json();
    if (res.ok) {
      $("#pwCurrent").value = ""; $("#pwNew").value = ""; $("#pwConfirm").value = "";
      hint.textContent = "已變更，新密碼即時生效。";
      toast("存取密碼已變更", "ok");
      refreshPwStatus();
      checkAuth();   // 門禁可能剛啟用，更新登出鈕
    } else {
      hint.textContent = res.detail || "變更失敗";
    }
  } catch (e) {
    hint.textContent = "變更請求失敗";
  }
}

function closeSettings() {
  settingsPanel.classList.add("hidden");
  // 若是從手機底部「設定」分頁進來的，關閉後回到討論分頁，避免留下空白畫面
  if (document.body.dataset.mv === "settings") setMobileView("discussion");
}

function renderSettings(fields) {
  settingsForm.innerHTML = "";
  let lastGroup = null;
  for (const f of fields) {
    if (f.group && f.group !== lastGroup) {
      const h = document.createElement("h3");
      h.textContent = f.group;
      settingsForm.appendChild(h);
      lastGroup = f.group;
    }
    const row = document.createElement("label");
    row.className = "set-row";
    const cap = document.createElement("span");
    cap.className = "set-label";
    cap.textContent = f.label;
    row.appendChild(cap);

    let input;
    if (f.kind === "select") {
      input = document.createElement("select");
      for (const opt of f.options) {
        const o = document.createElement("option");
        o.value = opt; o.textContent = opt;
        if (opt === f.value) o.selected = true;
        input.appendChild(o);
      }
    } else {
      input = document.createElement("input");
      input.type = f.kind === "password" ? "password" : "text";
      input.value = f.secret ? "" : (f.value || "");
      input.placeholder = f.secret && f.set
        ? "已設定（留空＝不變更）"
        : (f.placeholder || "");
    }
    input.dataset.env = f.env;
    input.dataset.secret = f.secret ? "1" : "";
    row.appendChild(input);
    settingsForm.appendChild(row);
  }
}

async function saveSettings() {
  const payload = {};
  settingsForm.querySelectorAll("[data-env]").forEach((el) => {
    const val = el.value.trim();
    // 秘密欄位留空＝不變更，不送出
    if (el.dataset.secret && val === "") return;
    payload[el.dataset.env] = val;
  });
  const hint = $("#settingsHint");
  hint.textContent = "儲存中…";
  try {
    const res = await (await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })).json();
    if (res.ok) {
      renderSettings(res.fields || []);
      hint.textContent = "已儲存，下次討論即生效。";
      toast("設定已儲存", "ok");
      loadHealth();
    } else {
      hint.textContent = res.detail || "儲存失敗";
    }
  } catch (e) {
    hint.textContent = "儲存請求失敗";
  }
}

startBtn.onclick = start;
stopBtn.onclick = stop;
interjectBtn.onclick = sendInterject;
$("#settingsBtn").onclick = openSettings;
$("#settingsClose").onclick = closeSettings;
// 點卡片外的暗色遮罩即關閉設定
settingsPanel.addEventListener("click", (e) => {
  if (e.target === settingsPanel) closeSettings();
});
$("#settingsSave").onclick = saveSettings;
$("#pwSave").onclick = savePassword;
$("#redeployBtn").onclick = redeployNow;
$("#downloadBtn").onclick = downloadWorkspace;
$("#historyBtn").onclick = openHistory;
$("#historyClose").onclick = closeHistory;
$("#historyCleanup").onclick = cleanupCompleted;
$("#autopilotBtn").onclick = openAutopilot;
$("#autopilotClose").onclick = closeAutopilot;
$("#apToggle").onclick = toggleAutopilot;
$("#apAddBtn").onclick = addAutopilotTask;
$("#metricsBtn").onclick = openMetrics;
$("#metricsClose").onclick = closeMetrics;
$("#metricsRefresh").onclick = refreshMetrics;
reqInput.addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });
interjectInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendInterject(); });

// --- 手機分頁導覽：在討論／成員／看板／檔案間切換（桌機自動隱藏分頁列）----
function setMobileView(view) {
  document.body.dataset.mv = view;
  document.querySelectorAll(".mobiletabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mv === view);
  });
  if (view === "settings") openSettings();
  else closeSettings();
}
document.querySelectorAll(".mobiletabs button").forEach((b) => {
  b.onclick = () => setMobileView(b.dataset.mv);
});
setMobileView("discussion");

async function loadHealth() {
  try {
    const h = await (await fetch("/api/health")).json();
    if (h.offline) {
      $("#offlineBadge").classList.remove("hidden");
      reqInput.placeholder = "離線示範模式：輸入任意需求即可看完整流程（不需 API 金鑰）";
    } else if (!h.has_api_key) {
      toast("未設定 ANTHROPIC_API_KEY；可用 TI_OFFLINE=1 啟動離線示範", "err");
    }
  } catch (e) { /* 忽略 */ }
}

// --- 登入 / 門禁 -------------------------------------------------------
async function checkAuth() {
  try {
    const s = await (await fetch("/api/auth/status")).json();
    if (s.auth_enabled && !s.authed) {
      location.href = "/login";
      return false;
    }
    if (s.auth_enabled) {
      const btn = $("#logoutBtn");
      btn.classList.remove("hidden");
      btn.onclick = async () => {
        try { await fetch("/api/logout", { method: "POST" }); } catch (e) { /* 忽略 */ }
        location.href = "/login";
      };
    }
  } catch (e) { /* 忽略：門禁狀態無法取得時不阻擋 */ }
  return true;
}

async function init() {
  if (!(await checkAuth())) return;
  loadPublishConfig();
  loadHealth();
}

init();
