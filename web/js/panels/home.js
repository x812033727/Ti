// 助手首頁對話編排(Kimi 化 PR4):hero composer → 既有 ws 管線,直播在 #homeChat。
//
// 核心手法=#stream 重掛(reparent)不複製:events-render/ws/replay 只認 #stream 這一個
// 節點,home 對話時把它搬進 #homeChat、進工作室視圖時搬回原位——串流/重連/重播零改動。
// 契約 id 不動:開始前把 hero 輸入同步進 #requirement(值的單一來源),再呼叫既有 start()。
import { $, toast } from "../dom.js";
import { start, stop, sendInterject } from "../ws.js";
import { state } from "../state.js";
import { onRunningChange } from "./deck.js";
import { focusComposer } from "./sidenav.js";

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

export function setSubview(name) {
  const main = $("#homeMain");
  if (main) main.dataset.subview = name;
  const chat = $("#homeChat");
  if (chat) chat.classList.toggle("hidden", name !== "chat");
}

export function resetToHero() {
  setSubview("hero");
  const input = $("#heroInput");
  if (input) input.value = "";
  focusComposer();
}

export function heroStart() {
  const input = $("#heroInput");
  const text = (input?.value || "").trim();
  if (!text) { focusComposer(); return; }
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    toast("目前已有進行中的討論——先停止它,或到工作室查看", "err");
    return;
  }
  $("#requirement").value = text; // 契約 id=值的單一來源(工作室測試/流程依賴)
  setSubview("chat");
  moveStreamHome();
  start(); // 專案/流程/小組沿用工作室啟動列現值;ws 拒絕(併發滿/互斥)由既有 error 事件呈現
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
  const send = $("#heroSend");
  if (send) send.disabled = running;
  for (const sel of ["#heroInterject", "#heroInterjectBtn", "#heroStopBtn"]) {
    const el = $(sel);
    if (el) el.disabled = !running;
  }
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
  onRunningChange(setHomeRunning);
}
