// WebSocket 生命週期：開場（送需求）、插話、停止。
import { $, toast } from "./dom.js";
import { state } from "./state.js";
import { handleEvent, setPhase, addSystem } from "./events-render.js";
import { setRunning, setDeckCollapsed, mobileMq } from "./panels/deck.js";
import { closeHistory } from "./panels/history.js";

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
  state.ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
  state.ws.onerror = () => { addSystem("⚠️ 連線發生錯誤"); toast("WebSocket 連線錯誤", "err"); setRunning(false); };
  // 連線關閉＝後端收尾（總結 done 已送）或斷線；無論何者，恢復可重新開始的狀態。
  state.ws.onclose = () => setRunning(false);
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
