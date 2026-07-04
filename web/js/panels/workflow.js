// 動態流程編輯器面板。
import { $, toast } from "../dom.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { openConfirmModal } from "../components/modal.js";
import { loadWorkflows } from "./deck.js";

export const WF_DEFAULT_NAME = "預設流程"; // 「載入預設範本」用的內建預設名
export const WF_RESERVED = ["預設流程", "動態優先", "快速模式"]; // 內建保留流程（唯讀，不可改名/刪除）
let wfCache = [];

export async function openWorkflowPanel() {
  openDrawer("#workflowPanel");
  await loadWorkflowPanel();
}
export function closeWorkflowPanel() { closeDrawer("#workflowPanel"); }

function setWfHint(msg) { $("#workflowHint").textContent = msg || ""; }

export async function loadWorkflowPanel(selectName) {
  const sel = $("#workflowList");
  try {
    const data = await (await fetch("/api/workflows")).json();
    wfCache = data.workflows || [];
  } catch (e) { wfCache = []; }
  loadWfRoles(); // 卡片編輯器的角色選項（非同步補上即可，不擋面板開啟）
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
  renderStageCards();
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
  renderStageCards();
  $("#workflowName").focus();
}

export function loadWorkflowTemplate() {
  const def = wfCache.find((w) => w.name === WF_DEFAULT_NAME);
  if (def) {
    $("#workflowStages").value = JSON.stringify(def.stages || [], null, 2);
    $("#workflowStages").readOnly = false;
    setWfHint("已載入預設流程當範本——改成你要的內容、換個名稱再儲存。");
    renderStageCards();
  }
}

// --- 結構化 stage 卡片編輯器 --------------------------------------------
// 單一真相＝ #workflowStages 的 JSON 原文：卡片的每次變更即時序列化回 textarea，
// saveWorkflow() 照舊只讀 textarea（儲存管線零改動）；「{} JSON」可切回原文直接編輯。
// 白名單對齊 studio/workflow.py（STAGE_TYPES / TASK_STAGE_TYPES / STAGE_MODES / VERDICTS）。

const STAGE_TYPE_OPTS = [
  ["clarify", "clarify（需求澄清）"],
  ["research", "research（技術研究）"],
  ["decompose", "decompose（議程拆解）"],
  ["discuss", "discuss（分組討論）"],
  ["build", "build（逐任務實作）"],
  ["integrate", "integrate（整合驗證）"],
  ["demo", "demo（實際執行）"],
  ["wrap_up", "wrap_up（驗收/檢討）"],
  ["publish", "publish（發佈）"],
  ["dynamic", "dynamic（PM 動態決定）"],
];
const TASK_TYPE_OPTS = [
  ["implement", "implement（實作）"],
  ["review", "review（審查）"],
  ["gate", "gate（客觀閘門）"],
  ["dynamic", "dynamic（PM 動態決定）"],
];
const MODE_OPTS = [
  ["", "（預設）"],
  ["round_robin", "round_robin（輪流發言）"],
  ["parallel", "parallel（並行）"],
  ["single", "single（單人）"],
];
const VERDICT_OPTS = [
  ["qa_passed", "qa_passed（驗證通過）"],
  ["senior_approved", "senior_approved（高工核可）"],
  ["security_approved", "security_approved（資安核可）"],
  ["critic_blocks", "critic_blocks（異議阻擋）"],
  ["pm_done", "pm_done（PM 判定完成）"],
];

let wfRolesOpts = []; // [{key, name, avatar}]（卡片的角色選項）
let wfJsonMode = false;

async function loadWfRoles() {
  try {
    const d = await (await fetch("/api/roles")).json();
    wfRolesOpts = (d.roles || []).map((r) => ({ key: r.key, name: r.name, avatar: r.avatar }));
  } catch (e) { wfRolesOpts = []; }
}

function readDraft() {
  try {
    const v = JSON.parse($("#workflowStages").value || "[]");
    return Array.isArray(v) ? { stages: v } : { error: "stages 必須是 JSON 陣列" };
  } catch (e) { return { error: e.message }; }
}

function syncDraft(stages) {
  $("#workflowStages").value = JSON.stringify(stages, null, 2);
  renderStageCards();
}

// 設欄位：等於預設值就整鍵移除，讓序列化後的 JSON 保持精簡
function setField(obj, key, val, def) {
  if (val === def || val == null || (Array.isArray(val) && !val.length)) delete obj[key];
  else obj[key] = val;
}

function mkSelect(opts, value, onchange, disabled) {
  const sel = document.createElement("select");
  for (const [v, label] of opts) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = label;
    if (v === value) o.selected = true;
    sel.appendChild(o);
  }
  // 現值不在白名單（手改 JSON 產生）：追加顯示，避免被靜默改掉
  if (value && !opts.some(([v]) => v === value)) {
    const o = document.createElement("option");
    o.value = value; o.textContent = value; o.selected = true;
    sel.appendChild(o);
  }
  sel.onchange = () => onchange(sel.value);
  sel.disabled = !!disabled;
  return sel;
}

function mkText(value, placeholder, onchange, disabled) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value || "";
  input.placeholder = placeholder || "";
  input.onchange = () => onchange(input.value.trim());
  input.disabled = !!disabled;
  return input;
}

function mkLabeled(text, control) {
  const wrap = document.createElement("label");
  wrap.className = "wf-field";
  const cap = document.createElement("span");
  cap.textContent = text;
  wrap.appendChild(cap); wrap.appendChild(control);
  return wrap;
}

function roleSelectOpts() {
  return [["", "（不指定）"], ...wfRolesOpts.map((r) => [r.key, `${r.avatar} ${r.name}`])];
}

// 單一 stage 卡片；taskLevel＝內嵌於 build.task_pipeline 的子階段
function stageCard(stages, i, ro, taskLevel) {
  const s = stages[i];
  const card = document.createElement("div");
  card.className = "wf-card" + (taskLevel ? " task" : "");

  // 標題列：序號＋type＋排序/刪除
  const head = document.createElement("div");
  head.className = "wf-card-head";
  const idx = document.createElement("span");
  idx.className = "wf-card-idx";
  idx.textContent = taskLevel ? `任務 ${i + 1}` : `階段 ${i + 1}`;
  head.appendChild(idx);
  head.appendChild(mkSelect(taskLevel ? TASK_TYPE_OPTS : STAGE_TYPE_OPTS, s.type, (v) => {
    s.type = v;
    if (v === "build" && !Array.isArray(s.task_pipeline)) s.task_pipeline = [];
    syncDraft(rootStages());
  }, ro));
  const tools = document.createElement("div");
  tools.className = "wf-card-tools";
  const mv = (delta) => {
    const j = i + delta;
    if (j < 0 || j >= stages.length) return;
    [stages[i], stages[j]] = [stages[j], stages[i]];
    syncDraft(rootStages());
  };
  const up = document.createElement("button");
  up.type = "button"; up.className = "ghost"; up.textContent = "↑"; up.title = "上移";
  up.disabled = ro || i === 0; up.onclick = () => mv(-1);
  const down = document.createElement("button");
  down.type = "button"; down.className = "ghost"; down.textContent = "↓"; down.title = "下移";
  down.disabled = ro || i === stages.length - 1; down.onclick = () => mv(1);
  const del = document.createElement("button");
  del.type = "button"; del.className = "ghost danger"; del.textContent = "✕"; del.title = "刪除此階段";
  del.disabled = ro;
  del.onclick = () => { stages.splice(i, 1); syncDraft(rootStages()); };
  tools.appendChild(up); tools.appendChild(down); tools.appendChild(del);
  head.appendChild(tools);
  card.appendChild(head);

  // 基本欄位
  const grid = document.createElement("div");
  grid.className = "wf-card-grid";
  grid.appendChild(mkLabeled("顯示名", mkText(s.name, "（選填）", (v) => { setField(s, "name", v, ""); syncDraft(rootStages()); }, ro)));
  grid.appendChild(mkLabeled("主責", mkSelect(roleSelectOpts(), s.assignee || "", (v) => { setField(s, "assignee", v, ""); syncDraft(rootStages()); }, ro)));
  grid.appendChild(mkLabeled("模式", mkSelect(MODE_OPTS, s.mode || "", (v) => { setField(s, "mode", v, ""); syncDraft(rootStages()); }, ro)));
  const rounds = document.createElement("input");
  rounds.type = "number"; rounds.min = "0"; rounds.value = s.max_rounds || 0;
  rounds.title = "0＝用系統預設輪數";
  rounds.onchange = () => { setField(s, "max_rounds", Math.max(0, Number(rounds.value) || 0), 0); syncDraft(rootStages()); };
  rounds.disabled = ro;
  grid.appendChild(mkLabeled("輪數上限", rounds));
  if (!taskLevel) {
    grid.appendChild(mkLabeled("條件 when", mkText(s.when, "has:security / flag:TI_BLUEPRINT", (v) => { setField(s, "when", v, ""); syncDraft(rootStages()); }, ro)));
  }
  const optWrap = document.createElement("label");
  optWrap.className = "wf-field wf-check";
  const opt = document.createElement("input");
  opt.type = "checkbox"; opt.checked = !!s.optional; opt.disabled = ro;
  opt.onchange = () => { setField(s, "optional", opt.checked, false); syncDraft(rootStages()); };
  const optCap = document.createElement("span");
  optCap.textContent = "選配（失敗不擋流程）";
  optWrap.appendChild(opt); optWrap.appendChild(optCap);
  grid.appendChild(optWrap);
  if (s.type === "dynamic") {
    const budget = document.createElement("input");
    budget.type = "number"; budget.min = "0"; budget.value = s.budget || 0;
    budget.title = "dynamic step 的步數預算（0＝系統預設）";
    budget.onchange = () => { setField(s, "budget", Math.max(0, Number(budget.value) || 0), 0); syncDraft(rootStages()); };
    budget.disabled = ro;
    grid.appendChild(mkLabeled("步數預算", budget));
    grid.appendChild(mkLabeled("後備主責", mkSelect(roleSelectOpts(), s.fallback || "", (v) => { setField(s, "fallback", v, ""); syncDraft(rootStages()); }, ro)));
  }
  card.appendChild(grid);

  // 參與角色（多選）
  const rolesRow = document.createElement("div");
  rolesRow.className = "wf-roles";
  appendCap(rolesRow, "參與角色");
  for (const r of wfRolesOpts) {
    const item = document.createElement("label");
    item.className = "wf-role-chip";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = (s.roles || []).includes(r.key);
    cb.disabled = ro;
    cb.onchange = () => {
      const cur = new Set(s.roles || []);
      if (cb.checked) cur.add(r.key); else cur.delete(r.key);
      // 依角色選項順序輸出，序列化結果穩定
      setField(s, "roles", wfRolesOpts.map((x) => x.key).filter((k) => cur.has(k)), null);
      syncDraft(rootStages());
    };
    const cap = document.createElement("span");
    cap.textContent = `${r.avatar} ${r.name}`;
    item.appendChild(cb); item.appendChild(cap);
    rolesRow.appendChild(item);
  }
  card.appendChild(rolesRow);

  // 閘門
  const gates = document.createElement("div");
  gates.className = "wf-gates";
  appendCap(gates, "收斂閘門");
  (s.gate || []).forEach((g, gi) => {
    const row = document.createElement("div");
    row.className = "wf-gate-row";
    row.appendChild(mkSelect(roleSelectOpts().slice(1), g.role, (v) => { g.role = v; syncDraft(rootStages()); }, ro));
    row.appendChild(mkSelect(VERDICT_OPTS, g.verdict, (v) => { g.verdict = v; syncDraft(rootStages()); }, ro));
    const optLabel = document.createElement("label");
    optLabel.className = "wf-check";
    const gcb = document.createElement("input");
    gcb.type = "checkbox"; gcb.checked = !!g.optional; gcb.disabled = ro;
    gcb.onchange = () => { if (gcb.checked) g.optional = true; else delete g.optional; syncDraft(rootStages()); };
    const gcap = document.createElement("span");
    gcap.textContent = "角色缺席可略過";
    optLabel.appendChild(gcb); optLabel.appendChild(gcap);
    row.appendChild(optLabel);
    const gdel = document.createElement("button");
    gdel.type = "button"; gdel.className = "ghost danger"; gdel.textContent = "✕"; gdel.disabled = ro;
    gdel.onclick = () => { s.gate.splice(gi, 1); if (!s.gate.length) delete s.gate; syncDraft(rootStages()); };
    row.appendChild(gdel);
    gates.appendChild(row);
  });
  const addGate = document.createElement("button");
  addGate.type = "button"; addGate.className = "ghost wf-add"; addGate.textContent = "＋ 閘門"; addGate.disabled = ro;
  addGate.onclick = () => {
    if (!Array.isArray(s.gate)) s.gate = [];
    s.gate.push({ role: (wfRolesOpts[0] || {}).key || "qa", verdict: "qa_passed" });
    syncDraft(rootStages());
  };
  gates.appendChild(addGate);
  card.appendChild(gates);

  // build：內嵌 task_pipeline 子階段
  if (s.type === "build") {
    const sub = document.createElement("div");
    sub.className = "wf-subpipeline";
    appendCap(sub, "task_pipeline（每個任務依序走）");
    const list = Array.isArray(s.task_pipeline) ? s.task_pipeline : (s.task_pipeline = []);
    list.forEach((_, ti) => sub.appendChild(stageCard(list, ti, ro, true)));
    const addTask = document.createElement("button");
    addTask.type = "button"; addTask.className = "ghost wf-add"; addTask.textContent = "＋ 子階段"; addTask.disabled = ro;
    addTask.onclick = () => { list.push({ type: "implement" }); syncDraft(rootStages()); };
    sub.appendChild(addTask);
    card.appendChild(sub);
  }
  return card;
}

function appendCap(parent, text) {
  const cap = document.createElement("div");
  cap.className = "wf-cap muted";
  cap.textContent = text;
  parent.appendChild(cap);
}

// 目前 draft 的根陣列（stageCard 內的變更共用同一份參照）
let _rootStages = [];
function rootStages() { return _rootStages; }

export function renderStageCards() {
  const box = $("#wfStageCards");
  if (!box) return;
  if (wfJsonMode) { box.innerHTML = ""; return; } // JSON 模式：卡片區收起
  const { stages, error } = readDraft();
  box.innerHTML = "";
  if (error) {
    const warn = document.createElement("div");
    warn.className = "wf-json-error";
    warn.textContent = "JSON 無法解析（" + error + "）——請切到 {} JSON 修正";
    box.appendChild(warn);
    return;
  }
  _rootStages = stages;
  const ro = !!$("#workflowStages").readOnly;
  stages.forEach((_, i) => box.appendChild(stageCard(stages, i, ro)));
  const add = document.createElement("button");
  add.type = "button";
  add.className = "ghost wf-add";
  add.textContent = "＋ 加階段";
  add.disabled = ro;
  add.onclick = () => { stages.push({ type: "discuss" }); syncDraft(stages); };
  box.appendChild(add);
}

// 「{} JSON」切換：卡片 ↔ 原文（原文有錯時卡片區顯示提示、不擋切換）
export function toggleWfJsonMode() {
  wfJsonMode = !wfJsonMode;
  $("#workflowPanel").classList.toggle("wf-json-mode", wfJsonMode);
  $("#wfModeToggle").textContent = wfJsonMode ? "🧩 卡片" : "{} JSON";
  renderStageCards();
}

// 編輯器接線（由入口 init 呼叫一次）：JSON 原文手改後，切回卡片或存檔前都以原文為準
export function bindWorkflowEditor() {
  $("#wfModeToggle").onclick = toggleWfJsonMode;
  $("#workflowStages").addEventListener("change", () => { if (!wfJsonMode) renderStageCards(); });
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
  if (!(await openConfirmModal({
    title: "刪除流程",
    message: `確定刪除流程「${name}」？`,
    confirmLabel: "刪除",
    danger: true,
  }))) return;
  try {
    const res = await fetch(`/api/workflows/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!res.ok) { toast(`刪除失敗（${res.status}）`, "err"); return; }
    toast("流程已刪除 ✓", "ok");
    await loadWorkflowPanel();
    loadWorkflows();
  } catch (e) { toast("刪除失敗：" + e.message, "err"); }
}
