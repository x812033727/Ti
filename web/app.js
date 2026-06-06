// Ti Studio 前端：連 WebSocket，把工作室事件渲染成討論串 / 看板 / 檔案面板。

const $ = (sel) => document.querySelector(sel);
const stream = $("#stream");
const expertList = $("#expertList");
const phaseEl = $("#phase");
const startBtn = $("#startBtn");
const reqInput = $("#requirement");

let ws = null;
let sessionId = null;

function setPhase(text) { phaseEl.textContent = text; }

function scrollStream() { stream.scrollTop = stream.scrollHeight; }

function clearStream() {
  stream.innerHTML = "";
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

function addResult(passed, detail) {
  const el = document.createElement("div");
  el.className = "result " + (passed ? "pass" : "fail");
  el.textContent = (passed ? "✅ " : "❌ ") + detail;
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
      addResult(p.passed, p.detail);
      break;
    case "retrospective":
      addSystem("📋 檢討：" + (p.text || ""));
      break;
    case "done":
      setPhase(p.completed ? "✅ 已完成" : "⚠️ 結束（未完全達標）");
      addSystem(p.completed ? "🎉 專案完成！" : "專案結束，仍有未達標項目。");
      refreshFiles();
      startBtn.disabled = false;
      break;
    case "error":
      addSystem("⚠️ 錯誤：" + (p.message || "未知錯誤"));
      startBtn.disabled = false;
      break;
  }
}

function start() {
  const requirement = reqInput.value.trim();
  if (!requirement) { reqInput.focus(); return; }
  startBtn.disabled = true;
  setPhase("連線中…");

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => ws.send(JSON.stringify({ requirement }));
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
  ws.onerror = () => { addSystem("⚠️ 連線發生錯誤"); startBtn.disabled = false; };
  ws.onclose = () => { setPhase((t => t)(phaseEl.textContent)); };
}

startBtn.onclick = start;
reqInput.addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });
