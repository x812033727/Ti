// 助手首頁對話編排(Kimi 化 PR4):hero composer → 既有 ws 管線,直播在 #homeChat。
//
// 核心手法=#stream 重掛(reparent)不複製:events-render/ws/replay 只認 #stream 這一個
// 節點,home 對話時把它搬進 #homeChat、進工作室視圖時搬回原位——串流/重連/重播零改動。
// 契約 id 不動:開始前把 hero 輸入同步進 #requirement(值的單一來源),再呼叫既有 start()。
import { $, toast } from "../dom.js";
import { start, stop, sendInterject, bindSocket, resetForAttach } from "../ws.js";
import { state } from "../state.js";
import { clearStream, clearBoard } from "../events-render.js";
import { onRunningChange, setRunning } from "./deck.js";
import { onViewChange } from "./dashboard.js";
import { focusComposer, refreshSidenavHistory } from "./sidenav.js";
import { replaySession } from "./history.js";
import { createTaskCard, startTaskCardPolling } from "../components/taskcard.js";
import { appendTextEl } from "../dom.js";

let _origParent = null; // #stream 原位(工作室 .discussion 內)的還原錨
let _origNext = null;

export function moveStreamHome() {
  const stream = $("#stream");
  const dest = $("#homeChatStream");
  if (!stream || !dest || !stream.parentNode || stream.parentNode === dest) return;
  _origParent = stream.parentNode;
  _origNext = stream.nextSibling || null;
  dest.appendChild(stream);
}

export function moveStreamBack() {
  const stream = $("#stream");
  if (!stream || !_origParent || stream.parentNode === _origParent) return;
  if (typeof _origParent.insertBefore === "function") {
    _origParent.insertBefore(stream, _origNext);
  } else {
    _origParent.appendChild(stream);
  }
}

const SUBVIEWS = { chat: "#homeChat", plugins: "#homePlugins", schedules: "#homeSchedules", stage: "#homeStage" };

export function setSubview(name) {
  const main = $("#homeMain");
  if (main) main.dataset.subview = name;
  for (const [k, sel] of Object.entries(SUBVIEWS)) {
    const el = $(sel);
    if (el) el.classList.toggle("hidden", name !== k);
  }
}

export function resetToHero() {
  setSubview("hero");
  const input = $("#heroInput");
  if (input) input.value = "";
  focusComposer();
}

// composer 模式(PR6):chat=多專家討論(直播)、task=交辦 agent(背景 autopilot 佇列)。
let _heroMode = "chat";

export function setHeroMode(mode) {
  _heroMode = mode === "task" || mode === "quick" ? mode : "chat";
  const map = { "#heroModeChat": "chat", "#heroModeTask": "task", "#heroModeQuick": "quick" };
  for (const [sel, m] of Object.entries(map)) {
    const btn = $(sel);
    if (!btn) continue;
    const on = m === _heroMode;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  }
  const send = $("#heroSend");
  if (send) {
    const label = send.querySelector("span");
    if (label) label.textContent = { task: "交辦", quick: "快答" }[_heroMode] || "開始";
  }
}

export function getHeroMode() { return _heroMode; }

// 交辦 agent:POST 既有 autopilot 任務端點;成功→對話區插任務卡片並輪詢進度。
export async function heroDispatchTask(text) {
  const title = text.split("\n")[0].slice(0, 120);
  setSubview("chat");
  moveStreamHome();
  try {
    const r = await fetch("/api/autopilot/task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, detail: text, priority: 1, type: "improvement" }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const why = r.status === 401 || r.status === 403
        ? "需要登入(或本機管理權限)才能交辦"
        : d.detail || "交辦失敗";
      toast(why, "err");
      return null;
    }
    const card = createTaskCard(d.task.id, title);
    $("#homeChatStream").appendChild(card);
    startTaskCardPolling(card);
    const input = $("#heroInput");
    if (input) input.value = "";
    setTimeout(refreshSidenavHistory, 1200);
    return d.task;
  } catch (e) {
    toast("交辦失敗:" + e.message, "err");
    return null;
  }
}

export function heroStart() {
  const input = $("#heroInput");
  const text = (input?.value || "").trim();
  if (!text) { focusComposer(); return; }
  if (_heroMode === "task") { heroDispatchTask(text); return; }
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    toast("目前已有進行中的討論——先停止它,或到工作室查看", "err");
    return;
  }
  $("#requirement").value = text; // 契約 id=值的單一來源(工作室測試/流程依賴)
  setSubview("chat");
  moveStreamHome();
  // 快答模式(PR13):暫借 workflowSelect 指到內建「快答」流程(單專家一輪),start() 讀完即還原
  const wfSel = $("#workflowSelect");
  const prevWf = _heroMode === "quick" && wfSel ? wfSel.value : null;
  if (prevWf !== null) wfSel.value = "快答";
  start(); // 專案/流程/小組沿用工作室啟動列現值;ws 拒絕(併發滿/互斥)由既有 error 事件呈現
  if (prevWf !== null) wfSel.value = prevWf;
  setTimeout(refreshSidenavHistory, 1200); // 新場入列後刷新側欄(session_started 落檔約需一拍)
}

// 側欄點「進行中」會話:以 attach 訂閱直播(cursor=0=補放全程再接 live)。
export function attachSessionInHome(sid) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.close();
  resetForAttach(); // 含 sawDone 歸零:殘留 true 會讓本場斷線被誤判已收尾而不重連
  state.replaying = false;
  state.improveMode = false;
  state.sessionId = sid;
  setSubview("chat");
  moveStreamHome();
  clearStream();
  clearBoard();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(`${proto}://${location.host}/ws`);
  sock.onopen = () => sock.send(JSON.stringify({ attach: sid, cursor: 0 }));
  bindSocket(sock);
  state.ws = sock;
  setRunning(true); // home 視圖不跳走(deck 已豁免);啟用插話/停止
}

// 側欄點「已結束」會話:重播灌入 home chat(replaySession 渲染 #stream,已先 reparent)。
export async function replaySessionInHome(sid) {
  setSubview("chat");
  moveStreamHome();
  await replaySession(sid, { view: "home" });
}

export function homeInterject() {
  const src = $("#heroInterject");
  const text = (src?.value || "").trim();
  if (!text) return;
  $("#interjectInput").value = text; // 走既有 sendInterject 契約 id
  sendInterject();
  src.value = "";
}

export function setHomeRunning(running) {
  if (!running) refreshSidenavHistory(); // 收尾後刷新側欄狀態點
  const send = $("#heroSend");
  if (send) send.disabled = running;
  for (const sel of ["#heroInterject", "#heroInterjectBtn", "#heroStopBtn"]) {
    const el = $(sel);
    if (el) el.disabled = !running;
  }
}


// --- 工作室脈搏+靈感區(PR12):hero 下方的「這間工作室正在活著」訊號 ---------
// 脈搏=一行即時狀態(/api/autopilot);靈感=最近合併成果+佇列頭建議(activity),
// 點建議卡=帶入 composer 交辦模式。載入失敗一律優雅隱藏,不打擾 hero。
export async function refreshHomeExtras() {
  const pulse = $("#heroPulse");
  const inspire = $("#heroInspire");
  if (!pulse || !inspire) return;
  try {
    const st = await (await fetch("/api/autopilot")).json();
    const c = st.counts || st.backlog || {};
    const hb = st.heartbeat || {};
    const runState = st.paused ? "已暫停" : hb.state === "running" ? "執行中" : "待命";
    pulse.textContent = `工作室脈搏:${runState}・待辦 ${c.pending ?? "?"}・完成 ${c.done ?? "?"}`;
  } catch { pulse.textContent = ""; }
  try {
    const data = await (await fetch("/api/autopilot/activity?limit=12")).json();
    const tasks = data.tasks || [];
    inspire.innerHTML = "";
    const merged = tasks.filter((t) => t.status === "done" && t.pr).slice(0, 3);
    const suggest = tasks.filter((t) => t.status === "pending").slice(0, 3);
    for (const t of merged) {
      const cardEl = document.createElement("div");
      cardEl.className = "inspire-card done";
      appendTextEl(cardEl, "div", "ic-tag ok", `已出貨・PR #${t.pr}`);
      appendTextEl(cardEl, "div", "ic-title", t.title);
      inspire.appendChild(cardEl);
    }
    for (const t of suggest) {
      const cardEl = document.createElement("div");
      cardEl.className = "inspire-card idea";
      appendTextEl(cardEl, "div", "ic-tag", "佇列中・點擊改派或加碼");
      appendTextEl(cardEl, "div", "ic-title", t.title);
      cardEl.onclick = () => {
        const input = $("#heroInput");
        if (input) input.value = t.title;
        setHeroMode("task");
        focusComposer();
      };
      inspire.appendChild(cardEl);
    }
  } catch { inspire.innerHTML = ""; }
}

export function bindHome() {
  const send = $("#heroSend");
  if (send) send.onclick = heroStart;
  const input = $("#heroInput");
  if (input) {
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault?.(); heroStart(); }
    });
  }
  const ij = $("#heroInterjectBtn");
  if (ij) ij.onclick = homeInterject;
  const ijInput = $("#heroInterject");
  if (ijInput) ijInput.addEventListener("keydown", (e) => { if (e.key === "Enter") homeInterject(); });
  const stopBtn = $("#heroStopBtn");
  if (stopBtn) stopBtn.onclick = stop;
  const mc = $("#heroModeChat");
  if (mc) mc.onclick = () => setHeroMode("chat");
  const mt = $("#heroModeTask");
  if (mt) mt.onclick = () => setHeroMode("task");
  const mq = $("#heroModeQuick");
  if (mq) mq.onclick = () => setHeroMode("quick");
  onRunningChange(setHomeRunning);
  // 覆審修正:離開 home 必搬回 #stream(否則工作室討論區永遠空白=單向陷阱);
  // 回到 home 且停在 chat 子頁則搬回來。
  onViewChange((view) => {
    if (view !== "home") moveStreamBack();
    else if (($("#homeMain") || {}).dataset?.subview === "chat") moveStreamHome();
  });
}
