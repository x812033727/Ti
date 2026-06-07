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

function clearStream() {
  stream.innerHTML = "";
}

function clearBoard() {
  document.querySelectorAll(".col .cards").forEach((c) => (c.innerHTML = ""));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
  el.className = "msg";
  el.innerHTML = `
    <div class="av">${p.avatar}</div>
    <div class="body"><div class="who">${p.name}</div><div class="txt"></div></div>`;
  el.querySelector(".txt").textContent = p.text;
  stream.appendChild(el);
  scrollStream();
}

function addTool(p) {
  const el = document.createElement("div");
  el.className = "tool";
  el.innerHTML = `<span class="badge">${p.tool}</span><span></span>`;
  el.querySelector("span:last-child").textContent = p.summary;
  stream.appendChild(el);
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
    const btn = $("#downloadZip");
    if (btn) {
      const hasFiles = (data.files || []).length > 0;
      btn.classList.toggle("hidden", !hasFiles);
      btn.onclick = () => {
        if (sessionId) window.location.href = `/api/workspace/${sessionId}/download`;
      };
    }
  } catch (e) { /* 忽略 */ }
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
    case "task_status":
      // 看板由 board_update 全量刷新，這裡不需額外處理
      break;
    case "retrospective":
      addSystem("📋 檢討：" + (p.text || ""));
      break;
    case "publish_result":
      renderPublish(p);
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
  if (replaying) { replaying = false; return; }   // 中止重播
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
      <div class="h-req"></div>
      <div class="h-meta"><span>${STATUS_LABEL[s.status] || s.status}</span>
        <span>${s.n_events || 0} 事件</span><span>${when}</span></div>`;
    li.querySelector(".h-req").textContent = s.requirement || "(無需求)";
    li.onclick = () => replaySession(s.session_id);
    historyList.appendChild(li);
  }
}

async function replaySession(sid) {
  if (replaying) return;
  historyPanel.classList.add("hidden");
  if (ws && ws.readyState === WebSocket.OPEN) ws.close();
  let events = [];
  try {
    const data = await (await fetch(`/api/history/${sid}/events`)).json();
    events = data.events || [];
  } catch (e) { addSystem("⚠️ 無法載入此 session"); return; }

  replaying = true;
  setRunning(false);
  stopBtn.disabled = false;          // 重播時「停止」可中止重播
  clearStream();
  clearBoard();
  addSystem("⏪ 重播 session：" + sid);
  const total = events.length;
  for (let i = 0; i < total; i++) {
    if (!replaying) { setPhase("⏪ 已中止重播"); break; }
    handleEvent(events[i]);
    setPhase(`⏪ 重播 ${i + 1}/${total}`);
    await sleep(120);
  }
  if (replaying) setPhase("⏪ 重播結束");
  replaying = false;
  stopBtn.disabled = true;
}

function closeHistory() { historyPanel.classList.add("hidden"); }

// --- 發佈到 GitHub -----------------------------------------------------
let publishConfigured = false;

async function loadPublishConfig() {
  try {
    const cfg = await (await fetch("/api/publish/config")).json();
    publishConfigured = !!cfg.configured;
  } catch (e) { publishConfigured = false; }
}

function addPublishButton(sid) {
  const wrap = document.createElement("div");
  wrap.className = "publish-cta";
  const btn = document.createElement("button");
  btn.textContent = "🚀 發佈成果到 GitHub";
  btn.onclick = async () => {
    btn.disabled = true; btn.textContent = "發佈中…";
    try {
      const res = await (await fetch(`/api/publish/${sid}`, { method: "POST" })).json();
      renderPublish(res);
    } catch (e) { renderPublish({ ok: false, detail: "發佈請求失敗" }); }
    btn.remove();
  };
  wrap.appendChild(btn);
  stream.appendChild(wrap);
  scrollStream();
}

function renderPublish(p) {
  const el = document.createElement("div");
  el.className = "publish " + (p.ok ? "ok" : "fail");
  let html = (p.ok ? "🚀 " : "⚠️ ") + (p.detail || "");
  if (p.branch) html += `　<code>${p.branch}</code>`;
  el.innerHTML = html;
  if (p.pr_url) {
    const a = document.createElement("a");
    a.href = p.pr_url; a.target = "_blank"; a.textContent = "查看 PR ↗";
    el.appendChild(document.createTextNode("　")); el.appendChild(a);
  }
  stream.appendChild(el);
  scrollStream();
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

function closeSettings() { settingsPanel.classList.add("hidden"); }

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
$("#settingsSave").onclick = saveSettings;
$("#pwSave").onclick = savePassword;
$("#historyBtn").onclick = openHistory;
$("#historyClose").onclick = closeHistory;
reqInput.addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });
interjectInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendInterject(); });

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
