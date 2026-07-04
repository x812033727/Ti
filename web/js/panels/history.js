// 歷史存檔 / 重播面板。
import { $, toast } from "../dom.js";
import { state } from "../state.js";
import {
  handleEvent, addSystem, clearStream, clearBoard, setPhase, scrollStream,
} from "../events-render.js";
import { setRunning } from "./deck.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { openConfirmModal } from "../components/modal.js";

export const STATUS_LABEL = {
  running: "⏳ 執行中", completed: "✅ 完成", incomplete: "⚠️ 未達標",
  stopped: "⏹ 已停止", error: "❌ 錯誤",
};

export async function openHistory() {
  const historyList = $("#historyList");
  openDrawer("#historyPanel");
  historyList.innerHTML = "<li class='muted'>載入中…</li>";
  try {
    const data = await (await fetch("/api/history")).json();
    renderHistory(data.sessions || []);
  } catch (e) {
    historyList.innerHTML = "<li class='muted'>無法載入歷史</li>";
  }
}

export function renderHistory(sessions) {
  const historyList = $("#historyList");
  if (!sessions.length) { historyList.innerHTML = "<li class='muted'>尚無歷史紀錄</li>"; return; }
  historyList.innerHTML = "";
  for (const s of sessions) {
    const li = document.createElement("li");
    const when = s.started_at ? new Date(s.started_at * 1000).toLocaleString() : "";
    li.innerHTML = `
      <button class="h-main" type="button" title="重播這場 session">
        <div class="h-req"></div>
        <div class="h-meta"><span class="h-status status-${s.status}">${STATUS_LABEL[s.status] || s.status}</span>
          <span>${s.n_events || 0} 事件</span><span>${when}</span></div>
      </button>
      ${s.status === "running" ? '<button class="h-stop" type="button" title="停止這場進行中的討論（在安全點收尾）">⏹</button>' : ""}
      <button class="h-del" type="button" title="刪除此 session（含產出檔案）">🗑</button>`;
    li.querySelector(".h-req").textContent = s.requirement || "(無需求)";
    li.querySelector(".h-main").onclick = () => replaySession(s.session_id);
    const stopB = li.querySelector(".h-stop");
    if (stopB) stopB.onclick = (e) => { e.stopPropagation(); stopSession(s.session_id); };
    li.querySelector(".h-del").onclick = (e) => {
      e.stopPropagation();
      deleteSession(s.session_id, s.status);
    };
    historyList.appendChild(li);
  }
}

export async function stopSession(sid) {
  // 與 WS 的停止同一條管線（request_stop）：頁面重整／斷線後背景續跑的討論也停得掉。
  // 停止在安全點收尾、非立即中斷，稍候再刷新列表讓狀態收斂。
  try {
    const r = await fetch(`/api/sessions/${sid}/stop`, { method: "POST" });
    if (r.ok) toast("已送出停止指令，將在安全點收尾");
    else toast("找不到進行中的目標（可能已結束，或服務曾重啟——可用專案面板的恢復）", "err");
    setTimeout(openHistory, 1500);
  } catch (e) { toast("停止失敗：" + e.message, "err"); }
}

export async function deleteSession(sid, status) {
  if (status === "running") { toast("執行中的 session 無法刪除", "err"); return; }
  if (!(await openConfirmModal({
    title: "刪除 session",
    message: "刪除此 session？產出檔案（workspace）也會一併刪除，無法復原。",
    confirmLabel: "刪除",
    danger: true,
  }))) return;
  try {
    const r = await fetch(`/api/history/${sid}`, { method: "DELETE" });
    if (r.ok) { toast("已刪除", "ok"); openHistory(); }
    else toast("刪除失敗", "err");
  } catch (e) { toast("刪除失敗", "err"); }
}

export async function cleanupCompleted() {
  if (!(await openConfirmModal({
    title: "清除已完成",
    message: "清除所有「✅ 已完成」的 session？產出檔案也會一併刪除，無法復原。",
    confirmLabel: "清除",
    danger: true,
  }))) return;
  try {
    const r = await fetch("/api/history/cleanup/completed", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    toast(`已清除 ${d.deleted ?? 0} 筆已完成 session`, "ok");
    openHistory();
  } catch (e) { toast("清除失敗", "err"); }
}

export async function replaySession(sid) {
  if (state.replaying) return;
  closeDrawer("#historyPanel");
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.close();
  let events = [];
  let requirement = "";
  try {
    const data = await (await fetch(`/api/history/${sid}/events`)).json();
    events = data.events || [];
    requirement = (data.meta && data.meta.requirement) || "";
  } catch (e) { addSystem("⚠️ 無法載入此 session"); return; }

  // 直接一次把整段歷史渲染完，畫面停在最底（最新）；要看過程往上滑即可。
  // replaying 旗標維持為 true，讓 handleEvent 的 done case 不會替歷史 session 補出發佈鈕。
  state.replaying = true;
  setRunning(false);
  clearStream();
  clearBoard();
  addSystem("📜 歷史紀錄：" + (requirement || sid));
  for (let i = 0; i < events.length; i++) handleEvent(events[i]);
  state.replaying = false;
  setPhase("📜 歷史紀錄");
  scrollStream();
}

export function closeHistory() { closeDrawer("#historyPanel"); }
