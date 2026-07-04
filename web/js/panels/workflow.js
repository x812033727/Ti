// 動態流程編輯器面板。
import { $, toast } from "../dom.js";
import { loadWorkflows } from "./deck.js";

export const WF_DEFAULT_NAME = "預設流程"; // 「載入預設範本」用的內建預設名
export const WF_RESERVED = ["預設流程", "動態優先"]; // 內建保留流程（唯讀，不可改名/刪除）
let wfCache = [];

export async function openWorkflowPanel() {
  $("#workflowPanel").classList.remove("hidden");
  await loadWorkflowPanel();
}
export function closeWorkflowPanel() { $("#workflowPanel").classList.add("hidden"); }

function setWfHint(msg) { $("#workflowHint").textContent = msg || ""; }

export async function loadWorkflowPanel(selectName) {
  const sel = $("#workflowList");
  try {
    const data = await (await fetch("/api/workflows")).json();
    wfCache = data.workflows || [];
  } catch (e) { wfCache = []; }
  const want = selectName || sel.value || (wfCache[0] && wfCache[0].name) || "";
  sel.innerHTML = "";
  for (const w of wfCache) {
    const opt = document.createElement("option");
    opt.value = w.name;
    opt.textContent = w.name + (WF_RESERVED.includes(w.name) ? "（內建）" : "");
    sel.appendChild(opt);
  }
  if ([...sel.options].some((o) => o.value === want)) sel.value = want;
  renderWorkflowSelection();
}

export function renderWorkflowSelection() {
  const name = $("#workflowList").value;
  const wf = wfCache.find((w) => w.name === name);
  const isReserved = wf && WF_RESERVED.includes(wf.name);
  $("#workflowName").value = wf ? wf.name : "";
  $("#workflowDesc").value = wf ? wf.description || "" : "";
  $("#workflowStages").value = wf ? JSON.stringify(wf.stages || [], null, 2) : "[]";
  $("#workflowName").readOnly = isReserved;
  $("#workflowDesc").readOnly = isReserved;
  $("#workflowStages").readOnly = isReserved;
  $("#workflowSave").disabled = isReserved;
  $("#workflowDelete").disabled = isReserved || !wf;
  setWfHint(isReserved ? "內建保留流程（唯讀）。可「載入預設範本」當新流程起點。" : "");
}

export function newWorkflow() {
  $("#workflowList").value = "";
  ["workflowName", "workflowDesc", "workflowStages"].forEach((id) => {
    $("#" + id).readOnly = false;
  });
  $("#workflowName").value = "";
  $("#workflowDesc").value = "";
  $("#workflowStages").value = "[\n  \n]";
  $("#workflowSave").disabled = false;
  $("#workflowDelete").disabled = true;
  setWfHint("新流程：填名稱與 stages 後儲存。");
  $("#workflowName").focus();
}

export function loadWorkflowTemplate() {
  const def = wfCache.find((w) => w.name === WF_DEFAULT_NAME);
  if (def) {
    $("#workflowStages").value = JSON.stringify(def.stages || [], null, 2);
    $("#workflowStages").readOnly = false;
    setWfHint("已載入預設流程當範本——改成你要的內容、換個名稱再儲存。");
  }
}

export async function saveWorkflow() {
  const name = $("#workflowName").value.trim();
  const description = $("#workflowDesc").value.trim();
  if (!name) { toast("請輸入流程名稱", "err"); return; }
  if (WF_RESERVED.includes(name)) { toast(`「${name}」為內建唯讀`, "err"); return; }
  let stages;
  try { stages = JSON.parse($("#workflowStages").value || "[]"); }
  catch (e) { toast("stages 不是合法 JSON：" + e.message, "err"); return; }
  if (!Array.isArray(stages)) { toast("stages 必須是 JSON 陣列", "err"); return; }
  const exists = wfCache.some((w) => w.name === name && !WF_RESERVED.includes(w.name));
  const url = exists ? `/api/workflows/${encodeURIComponent(name)}` : "/api/workflows";
  const method = exists ? "PUT" : "POST";
  const body = exists ? { description, stages } : { name, description, stages };
  try {
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { toast(`儲存失敗（${res.status}）：${data.error || ""}`, "err"); return; }
    toast("流程已儲存 ✓", "ok");
    await loadWorkflowPanel(name);
    loadWorkflows(); // 同步啟動列「動態流程」下拉
  } catch (e) { toast("儲存失敗：" + e.message, "err"); }
}

export async function deleteWorkflow() {
  const name = $("#workflowList").value;
  if (!name || WF_RESERVED.includes(name)) return;
  if (!confirm(`確定刪除流程「${name}」？`)) return;
  try {
    const res = await fetch(`/api/workflows/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!res.ok) { toast(`刪除失敗（${res.status}）`, "err"); return; }
    toast("流程已刪除 ✓", "ok");
    await loadWorkflowPanel();
    loadWorkflows();
  } catch (e) { toast("刪除失敗：" + e.message, "err"); }
}
