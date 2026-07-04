// WebSocket 生命週期：開場（送需求）、插話、停止、斷線自動重連（attach 重掛）。
import { $, toast } from "./dom.js";
import { state } from "./state.js";
import { handleEvent, setPhase, addSystem } from "./events-render.js";
import { setRunning, setDeckCollapsed, mobileMq } from "./panels/deck.js";
import { closeHistory, replaySession } from "./panels/history.js";

// --- 斷線自動重連（attach 重掛）----------------------------------------
// 後端斷線後討論仍在背景續跑、事件照寫 history；前端以 {attach, cursor} 重新訂閱，
// 伺服器補放第 cursor 筆之後的已錯過事件再接回 live。cursor＝已收到的「入檔」事件數
// （從 session_started 起算，與後端 JSONL 行數對齊；attach_ok 會回權威計數校準）。
let eventCount = 0;
let counting = false;      // 看到 session_started 才起算（之前的準備類事件不入檔）
let sawDone = false;       // 收到收尾 done＝正常結束，不重連
let reconnectAttempts = 0;
let reconnectTimer = null;
const RECONNECT_MAX = 8;
const RECONNECT_CAP_MS = 15000;

// 指數退避＋抖動（equal-jitter），封頂 15 秒。純函式，供 .mjs 測試。
export function computeReconnectDelay(n) {
  const base = Math.min(1000 * 2 ** n, RECONNECT_CAP_MS);
  return base / 2 + Math.random() * (base / 2);
}

// 事件計數與收尾判定；回傳 true＝本場已正常收尾（不需重連）。純邏輯，供 .mjs 測試。
export function trackSocketEvent(ev) {
  if (ev.type === "session_started") { counting = true; eventCount = 0; }
  if (counting) eventCount += 1;
  if (ev.type === "done") {
    const p = ev.payload || {};
    if (!state.improveMode || p.improve) return true;
  }
  return false;
}

// 重連狀態快照（供 .mjs 測試觀察 module-local 變數）。
export function getReconnectState() {
  return { eventCount, counting, sawDone, reconnectAttempts };
}

export function stopReconnect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  counting = false; // 停止起算＝onclose 不再觸發重連（重播/手動導航時用）
}

function scheduleReconnect() {
  reconnectAttempts += 1;
  const n = reconnectAttempts;
  const delay = computeReconnectDelay(n - 1);
  setPhase("🔌 重連中…");
  addSystem(`⚠️ 連線中斷，${Math.max(1, Math.round(delay / 1000))} 秒後第 ${n} 次重連…`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const sock = new WebSocket(`${proto}://${location.host}/ws`);
    sock.onopen = () => sock.send(JSON.stringify({ attach: state.sessionId, cursor: eventCount }));
    bindSocket(sock);
    state.ws = sock;
  }, delay);
}

// 統一掛 socket 事件（start 與 attach 重連共用）。
export function bindSocket(sock) {
  sock.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    // attach 專屬訊息不入檔、不進 handleEvent：ok＝以伺服器權威計數校準後接 live。
    if (ev.type === "attach_ok") {
      const p = ev.payload || {};
      eventCount = p.cursor || 0;
      reconnectAttempts = 0;
      addSystem("🔌 已重新連上進行中的討論");
      setPhase("已重新連上");
      return;
    }
    if (ev.type === "error" && (ev.payload || {}).code === "attach_unavailable") {
      addSystem("ℹ️ " + ((ev.payload || {}).message || "該場討論已結束"));
      stopReconnect();
      if (state.sessionId) replaySession(state.sessionId); else setRunning(false);
      return;
    }
    if (trackSocketEvent(ev)) sawDone = true;
    handleEvent(ev);
  };
  sock.onerror = () => { addSystem("⚠️ 連線發生錯誤"); toast("WebSocket 連線錯誤", "err"); };
  // 連線關閉＝後端收尾（done 已送）或斷線：討論進行中即自動重連（背景仍在跑），
  // 正常收尾/重播/持續改良模式維持原行為。
  sock.onclose = () => {
    if (state.replaying || state.improveMode || !state.sessionId || !counting || sawDone) {
      setRunning(false);
      return;
    }
    if (reconnectAttempts >= RECONNECT_MAX) {
      addSystem("⛔ 重連失敗；討論仍在背景進行，稍後可從歷史列表重播此場");
      toast("重連失敗", "err");
      setRunning(false);
      return;
    }
    scheduleReconnect();
  };
}

export function start() {
  const reqInput = $("#requirement");
  const requirement = reqInput.value.trim();
  const projectId = $("#projectSelect").value;
  const improve = $("#improveChk").checked && !!projectId;
  // 需求必填；唯「專案 + 持續改良」可留空（任務由專案 backlog／找問題供給）。
  if (!requirement && !improve) { reqInput.focus(); return; }
  const repoUrl = projectId ? "" : $("#repoUrl").value.trim();
  state.replaying = false;
  state.improveMode = improve;
  state.workspaceId = null;
  // 重置重連狀態機（新場從零起算）。
  stopReconnect();
  eventCount = 0;
  sawDone = false;
  reconnectAttempts = 0;
  closeHistory();
  setRunning(true);
  if (mobileMq().matches) setDeckCollapsed(true); // 手機：開始後收合啟動列，騰出討論空間
  setPhase("連線中…");

  const payload = { requirement, repo_url: repoUrl };
  if (projectId) payload.project_id = projectId;
  if (improve) payload.mode = "improve";
  const workflowName = ($("#workflowSelect") || {}).value || "";
  if (workflowName) payload.workflow = workflowName;
  // 討論小組：限定本場參與角色（後端 ws.py 已支援 group 參數，找不到會回 error 事件）
  const groupName = ($("#groupSelect") || {}).value || "";
  if (groupName) payload.group = groupName;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.onopen = () => state.ws.send(JSON.stringify(payload));
  bindSocket(state.ws); // onmessage/onerror/onclose 統一掛載（含斷線自動重連）
}

export function sendInterject() {
  const interjectInput = $("#interjectInput");
  const text = interjectInput.value.trim();
  if (!text || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.ws.send(JSON.stringify({ type: "interject", text }));
  interjectInput.value = "";
}

export function stop() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify({ type: "stop" }));
  $("#stopBtn").disabled = true;
  toast("已送出停止指令", "");
}
