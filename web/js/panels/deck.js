// 啟動列（command-deck）：開始/停止鈕狀態、收合列、專案與動態流程下拉。
import { $ } from "../dom.js";
import { createProjectFlow, deleteProject, setProjectPublishRepo } from "./project.js";

export function setRunning(running) {
  $("#startBtn").disabled = running;
  $("#stopBtn").disabled = !running;
  $("#interjectInput").disabled = !running;
  $("#interjectBtn").disabled = !running;
  $("#deckStop").classList.toggle("hidden", !running); // 收合列的停止鈕只在執行中顯示
  if (!running) setDeckCollapsed(false);               // 討論結束自動展開，方便開下一場
}

// --- 啟動列收合：手機按「開始」後自動收成單列，點擊即展開 -----------------
let _mq = null;
export function mobileMq() {
  if (!_mq) _mq = window.matchMedia("(max-width: 900px)");
  return _mq;
}

export function setDeckCollapsed(collapsed) {
  document.querySelector(".command-deck").classList.toggle("collapsed", collapsed);
  if (collapsed) {
    const req = $("#requirement").value.trim();
    $("#deckSummary").textContent = req || ($("#improveChk").checked ? "♻️ 持續改良中…" : "（無需求）");
  }
}

// --- 專案（長期產品）---------------------------------------------------
export async function loadProjects() {
  const sel = $("#projectSelect");
  if (!sel) return;
  try {
    const data = await (await fetch("/api/projects")).json();
    const cur = sel.value;
    sel.innerHTML = '<option value="">（一次性討論）</option>';
    for (const p of data.projects || []) {
      const opt = document.createElement("option");
      opt.value = p.id;
      const b = p.backlog || {};
      opt.textContent = `📦 ${p.name}` + (b.pending ? `（待辦 ${b.pending}）` : "");
      sel.appendChild(opt);
    }
    const add = document.createElement("option");
    add.value = "__new__";
    add.textContent = "➕ 新增專案…";
    sel.appendChild(add);
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  } catch (e) { /* 忽略 */ }
}

// 動態流程下拉：從 /api/workflows 拉清單（含內建預設）填入啟動列選擇器。
export async function loadWorkflows() {
  const sel = $("#workflowSelect");
  if (!sel) return;
  try {
    const data = await (await fetch("/api/workflows")).json();
    const cur = sel.value;
    sel.innerHTML = '<option value="">（預設：動態優先）</option>';
    for (const w of data.workflows || []) {
      const opt = document.createElement("option");
      opt.value = w.name;
      const n = (w.stages || []).length;
      opt.textContent = `🧭 ${w.name}` + (n ? `（${n} 階段）` : "");
      opt.title = w.description || "";
      sel.appendChild(opt);
    }
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  } catch (e) { /* 忽略 */ }
}

// 啟動鈕文案隨「是否選了專案 + 是否持續改良」變化，讓「繼續專案」一目了然。
export function updateStartLabel(pending) {
  const startBtn = $("#startBtn");
  const pid = $("#projectSelect").value;
  const isProj = pid && pid !== "__new__";
  const improve = $("#improveChk").checked && isProj;
  if (improve) {
    startBtn.textContent = "♻️ 繼續改良";
    startBtn.title = "在此專案上繼續：消化改良待辦／自動找問題，持續改良直到你按停止" +
      (pending ? `（待辦 ${pending} 項）` : "（需求可留空）");
  } else if (isProj) {
    startBtn.textContent = "▶️ 繼續專案";
    startBtn.title = "用下方需求在此專案上再開一場討論（沿用專案程式碼與目標 repo）";
  } else {
    startBtn.textContent = "開始討論";
    startBtn.title = "";
  }
}

// 選擇專案後：收起「一次性 repo」欄、顯示專案的目標 repo、預設進入「繼續改良」，
// 讓「繼續一個既有專案」有明確按鈕、且設定的 repo 直接出現在啟動列。
export async function onProjectChange() {
  const pid = $("#projectSelect").value;
  const repoInput = $("#repoUrl");
  const repoTag = $("#projectRepo");
  const delBtn = $("#deckDeleteProject");
  if (pid === "__new__") { createProjectFlow(); return; }
  if (!pid) {
    // 一次性討論：還原一次性 UI
    repoInput.classList.remove("hidden");
    repoTag.classList.add("hidden");
    delBtn.classList.add("hidden");
    $("#improveChk").checked = false;
    updateStartLabel();
    return;
  }
  // 既有專案：一次性 repo 欄對專案無作用 → 收起，改顯示專案目標 repo；預設「繼續改良」
  repoInput.classList.add("hidden");
  $("#improveChk").checked = true;
  repoTag.classList.remove("hidden");
  repoTag.classList.remove("unset");
  repoTag.textContent = "🎯 載入中…";
  delBtn.classList.remove("hidden");
  delBtn.onclick = () => deleteProject(pid, pid); // 名稱載入後改用真名
  updateStartLabel();
  try {
    const d = await (await fetch(`/api/projects/${pid}`)).json();
    const p = d.project || {};
    const pending = (d.backlog || []).filter(
      (t) => t.status === "pending" || t.status === "in_progress",
    ).length;
    if (p.publish_repo) {
      repoTag.textContent = `🎯 ${p.publish_repo}`;
    } else {
      repoTag.textContent = "🎯 目標 repo 未設定（點此設定）";
      repoTag.classList.add("unset");
    }
    repoTag.onclick = () => setProjectPublishRepo(pid, p.publish_repo || "");
    delBtn.onclick = () => deleteProject(pid, p.name || pid);
    updateStartLabel(pending);
  } catch (e) {
    repoTag.textContent = "🎯 無法載入專案 repo（點此設定）";
    repoTag.classList.add("unset");
    repoTag.onclick = () => setProjectPublishRepo(pid, "");
  }
}
