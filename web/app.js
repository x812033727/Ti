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
// 檔案面板/下載對接的 workspace id：一次性討論＝sessionId；專案模式＝project-<pid>
// （多場 session 共用固定 workspace），由 session_started 的 workspace_id 提供。
let workspaceId = null;
// 持續改良模式：迴圈內每輪討論各發自己的 done，僅「帶 improve 摘要的總結 done」才收尾。
let improveMode = false;

function setPhase(text) { phaseEl.textContent = text; }

function setRunning(running) {
  startBtn.disabled = running;
  stopBtn.disabled = !running;
  interjectInput.disabled = !running;
  interjectBtn.disabled = !running;
  $("#deckStop").classList.toggle("hidden", !running); // 收合列的停止鈕只在執行中顯示
  if (!running) setDeckCollapsed(false);               // 討論結束自動展開，方便開下一場
}

// --- 啟動列收合：手機按「開始」後自動收成單列，點擊即展開 -----------------
const deck = document.querySelector(".command-deck");
const MOBILE_MQ = window.matchMedia("(max-width: 900px)");
function setDeckCollapsed(collapsed) {
  deck.classList.toggle("collapsed", collapsed);
  if (collapsed) {
    const req = reqInput.value.trim();
    $("#deckSummary").textContent = req || ($("#improveChk").checked ? "♻️ 持續改良中…" : "（無需求）");
  }
}

function scrollStream() { stream.scrollTop = stream.scrollHeight; }

// 並行支線多欄渲染：帶 task_id 的發言/工具進各自的欄位，其餘事件照常進主時間軸。
let laneBoard = null;
const laneCols = {};

function clearStream() {
  stream.innerHTML = "";
  laneBoard = null;
  for (const k of Object.keys(laneCols)) delete laneCols[k];
}

// 取得某支線欄位的內容容器（首次出現時建立「支線看板」與該欄）。
function laneBody(taskId) {
  if (!laneBoard) {
    laneBoard = document.createElement("div");
    laneBoard.className = "lanes-board";
    stream.appendChild(laneBoard);
  }
  if (!laneCols[taskId]) {
    const col = document.createElement("div");
    col.className = "lane-col lane-" + (taskId % 6);
    col.innerHTML = `<div class="lane-col-head">支線 #${taskId}</div><div class="lane-col-body"></div>`;
    laneBoard.appendChild(col);
    laneCols[taskId] = col.querySelector(".lane-col-body");
  }
  return laneCols[taskId];
}

// 事件落點：帶 task_id → 對應支線欄；否則 → 主時間軸。
function sink(p) {
  return p && p.task_id != null ? laneBody(p.task_id) : stream;
}

function clearBoard() {
  document.querySelectorAll(".col .cards").forEach((c) => (c.innerHTML = ""));
}

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
      <div class="meta"><div class="nm">${r.name}</div><div class="tt">${r.title}${r.provider ? " · " + r.provider : ""}</div></div>
      <div class="dot"></div>`;
    expertList.appendChild(el);
  }
}

// 動態招募：把單一新成員插入成員欄（已存在則略過，冪等）。
function addRosterMember(r) {
  if (!r.key || expertList.querySelector(`.expert[data-key="${r.key}"]`)) return;
  const el = document.createElement("div");
  el.className = "expert";
  el.dataset.key = r.key;
  el.dataset.status = "idle";
  el.innerHTML = `
      <div class="av">${r.avatar || "🆕"}</div>
      <div class="meta"><div class="nm">${r.name || r.key}</div><div class="tt">${r.title || ""}${r.provider ? " · " + r.provider : ""}</div></div>
      <div class="dot"></div>`;
  expertList.appendChild(el);
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
  el.className = "msg" + (p.task_id != null ? " lane lane-" + (p.task_id % 6) : "");
  el.innerHTML = `
    <div class="av">${p.avatar}</div>
    <div class="body"><div class="who">${p.name}</div><div class="txt"></div></div>`;
  el.querySelector(".txt").textContent = p.text;
  sink(p).appendChild(el);
  scrollStream();
}

function addTool(p) {
  const el = document.createElement("div");
  el.className = "tool" + (p.task_id != null ? " lane lane-" + (p.task_id % 6) : "");
  el.innerHTML = `<span class="badge">${p.tool}</span><span></span>`;
  el.querySelector("span:last-child").textContent = p.summary;
  sink(p).appendChild(el);
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

const CI_LABELS = {
  pass: ["✅ CI 通過", "pass"],
  none: ["✅ 無 CI 檢查，直接合併", "pass"],
  fail: ["❌ CI 未通過", "fail"],
  error: ["⚠️ CI 等待逾時/出錯，保留 PR 待人工", "fail"],
  merged: ["🎉 已自動合併（squash + 刪分支）", "pass"],
  merge_failed: ["❌ 合併失敗", "fail"],
  giveup: ["🛑 CI 連續失敗達上限，保留 PR 待人工", "fail"],
};

function addCI(p) {
  const [label, cls] = CI_LABELS[p.state] || [p.state || "CI", "fail"];
  const round = p.attempt && p.rounds ? `（第 ${p.attempt}/${p.rounds} 輪）` : "";
  const el = document.createElement("div");
  el.className = "ci result " + cls;
  el.textContent = "🔁 " + label + round;
  if (p.detail) {
    const det = document.createElement("details");
    det.className = "log";
    det.innerHTML = "<summary>查看詳情</summary>";
    const pre = document.createElement("pre");
    pre.textContent = p.detail;
    det.appendChild(pre);
    el.appendChild(det);
  }
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
  const wid = workspaceId || sessionId;
  if (!wid) return;
  try {
    const res = await fetch(`/api/workspace/${wid}/files`);
    const data = await res.json();
    const list = $("#fileList");
    list.innerHTML = "";
    for (const f of data.files) {
      const li = document.createElement("li");
      li.textContent = f;
      li.onclick = () => viewFile(f);
      list.appendChild(li);
    }
    const btn = $("#downloadBtn");
    if (btn) btn.classList.toggle("hidden", data.files.length === 0);
  } catch (e) { /* 忽略 */ }
}

function downloadWorkspace() {
  const wid = workspaceId || sessionId;
  if (!wid) return;
  // 透過隱藏連結觸發瀏覽器下載；同源 cookie 會自動帶上（門禁啟用時）。
  const a = document.createElement("a");
  a.href = `/api/workspace/${wid}/download`;
  a.download = `workspace-${wid}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function viewFile(path) {
  const wid = workspaceId || sessionId;
  const res = await fetch(`/api/workspace/${wid}/file?path=${encodeURIComponent(path)}`);
  if (!res.ok) return;
  const data = await res.json();
  $("#fileView").textContent = data.content;
}

function handleEvent(ev) {
  if (ev.session_id) sessionId = ev.session_id;
  const p = ev.payload || {};
  switch (ev.type) {
    case "session_started":
      workspaceId = p.workspace_id || ev.session_id;
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
    case "expert_joined":
      addRosterMember(p);
      addSystem(
        `👤 PM 招募「${p.name || p.key}」加入（${p.reason || "招募"}${p.provider ? "・" + p.provider : ""}）`,
      );
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
    case "clarify_request": {
      // PM 的需求澄清提問：逐題渲染並引導用插話框回答（逾時自動按假設續行）。
      addSystem("❓ PM 想先跟你確認需求（在下方插話框回答，一則訊息回答全部即可）：");
      (p.questions || []).forEach((q, i) => {
        addSystem(`${i + 1}. ${q.q}` + (q.assumption ? `（未回覆時假設：${q.assumption}）` : ""));
      });
      if (p.timeout_s) addSystem(`⏳ ${Math.round(p.timeout_s)} 秒內未回覆，將按 PM 的預設假設繼續。`);
      if (!replaying) interjectInput.focus();
      break;
    }
    case "workflow_plan": {
      // 動態流程定義快照：本場採用的 workflow 名稱與 stage 序列（開場廣播、重播亦經此）。
      const stages = p.stages || [];
      addSystem(
        `🧭 動態流程：${p.name || "預設流程"}（${stages.length} 階段）` +
          (stages.length ? "　" + stages.map((s) => s.name || s.type).join(" → ") : ""),
      );
      break;
    }
    case "agenda_plan": {
      // 拆解結果快照：議程子題＋主責分派（含硬驗證修正紀錄），重播歷史時也會經此渲染。
      const items = p.agenda || [];
      addSystem(`📋 議程拆解：${items.length} 個子題`);
      items.forEach((a, i) => {
        let line = `${i + 1}. ${a.title || ""}`;
        if (a.description) line += `｜${a.description}`;
        if (a.assignee) line += `（主責: ${a.assignee}）`;
        if (a.criteria) line += `｜【準則】${a.criteria}`;
        addSystem(line);
      });
      (p.corrections || []).forEach((c) => {
        addSystem(`↩️ 分派修正：子題 ${c.index + 1} 的「負責: ${c.given || "（缺漏）"}」→ ${c.assigned}`);
      });
      break;
    }
    case "conclusion": {
      // 結論彙整快照：一場討論收斂後產出 CONCLUSION.md，渲染四段摘要（重播時亦經此）。
      addSystem("📝 結論彙整：已產出 CONCLUSION.md");
      const s = p.summary || {};
      const sections = [
        ["共識", s.consensus],
        ["分歧", s.disagreements],
        ["未決事項", s.open_questions],
        ["後續行動", s.actions],
      ];
      sections.forEach(([label, items]) => {
        const list = items || [];
        if (list.length) addSystem(`【${label}】` + list.join("；"));
      });
      break;
    }
    case "critic_review":
      if (p.passed) {
        addSystem("🔍 異議檢查放行（" + (p.gate || "") + " 視角）");
      } else {
        addSystem("🛑 異議檢查退回（" + (p.gate || "") + " 視角）：" + (p.text || ""));
      }
      break;
    case "huddle":
      if (p.limitation) {
        addSystem("🚧 已知限制：任務「" + (p.title || "") + "」huddle 後仍未通過。");
      } else {
        const who = (p.participants || []).join("、");
        addSystem("🧩 卡關討論：任務「" + (p.title || "") + "」召集 " + who + " 找替代方案。");
        if (p.conclusion) addSystem(p.conclusion);
      }
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
    case "ci_result":
      addCI(p);
      break;
    case "token_usage":
      // 統計事件由後端 history/meta 聚合，前端即時串流不用顯示。
      break;
    case "done":
      // 持續改良模式：迴圈內每輪討論的 done 只是「一輪結束」，迴圈總結（帶 improve）才收尾。
      if (improveMode && !p.improve) {
        addSystem(p.completed ? "✅ 本輪改良完成，繼續下一輪…" : "⚠️ 本輪未達標，迴圈將評估是否續跑…");
        refreshFiles();
        break;
      }
      if (p.improve) {
        const s = p.improve;
        setPhase(p.stopped ? "⏹ 已停止" : "♻️ 持續改良結束");
        addSystem(`♻️ 持續改良結束：共 ${s.cycles} 輪（完成 ${s.done}、未達標 ${s.failed}）。`);
      } else {
        setPhase(p.stopped ? "⏹ 已停止" : (p.completed ? "✅ 已完成" : "⚠️ 結束（未完全達標）"));
        addSystem(p.stopped ? "已依指示停止。" : (p.completed ? "🎉 專案完成！" : "專案結束，仍有未達標項目。"));
      }
      refreshFiles();
      if (!replaying && p.completed && publishConfigured && !p.improve) addPublishButton(sessionId);
      setRunning(false);
      loadProjects(); // backlog 統計可能已變，刷新專案選單
      break;
    case "error":
      addSystem("⚠️ 錯誤：" + (p.message || "未知錯誤"));
      toast(p.message || "發生錯誤", "err");
      // 持續改良模式下，單輪錯誤不終止迴圈（improver 會記 failed 並評估續跑）。
      if (!improveMode) setRunning(false);
      break;
  }
}

function start() {
  const requirement = reqInput.value.trim();
  const projectId = $("#projectSelect").value;
  const improve = $("#improveChk").checked && !!projectId;
  // 需求必填；唯「專案 + 持續改良」可留空（任務由專案 backlog／找問題供給）。
  if (!requirement && !improve) { reqInput.focus(); return; }
  const repoUrl = projectId ? "" : $("#repoUrl").value.trim();
  replaying = false;
  improveMode = improve;
  workspaceId = null;
  closeHistory();
  setRunning(true);
  if (MOBILE_MQ.matches) setDeckCollapsed(true); // 手機：開始後收合啟動列，騰出討論空間
  setPhase("連線中…");

  const payload = { requirement, repo_url: repoUrl };
  if (projectId) payload.project_id = projectId;
  if (improve) payload.mode = "improve";
  const workflowName = ($("#workflowSelect") || {}).value || "";
  if (workflowName) payload.workflow = workflowName;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => ws.send(JSON.stringify(payload));
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
  ws.onerror = () => { addSystem("⚠️ 連線發生錯誤"); toast("WebSocket 連線錯誤", "err"); setRunning(false); };
  // 連線關閉＝後端收尾（總結 done 已送）或斷線；無論何者，恢復可重新開始的狀態。
  ws.onclose = () => setRunning(false);
}

// --- 專案（長期產品）---------------------------------------------------
async function loadProjects() {
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
async function loadWorkflows() {
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
function updateStartLabel(pending) {
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
async function onProjectChange() {
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

async function createProjectFlow() {
  const name = (prompt("專案名稱（例如：無人機地面站）") || "").trim();
  if (!name) { $("#projectSelect").value = ""; onProjectChange(); return; }
  const vision = (prompt("一句話產品願景（選填，會持續提醒團隊方向）") || "").trim();
  try {
    const res = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, vision }),
    });
    const data = await res.json();
    if (!res.ok) { toast(data.error || "建立失敗", "err"); return; }
    await loadProjects();
    $("#projectSelect").value = data.project.id;
    toast(`專案「${name}」已建立`, "ok");
    onProjectChange(); // 顯示新專案的目標 repo 狀態（未設定→提示設定）
  } catch (e) { toast("建立專案失敗", "err"); }
}

function sendInterject() {
  const text = interjectInput.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "interject", text }));
  interjectInput.value = "";
}

function stop() {
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
      <div class="h-main">
        <div class="h-req"></div>
        <div class="h-meta"><span class="h-status status-${s.status}">${STATUS_LABEL[s.status] || s.status}</span>
          <span>${s.n_events || 0} 事件</span><span>${when}</span></div>
      </div>
      ${s.status === "running" ? '<button class="h-stop" title="停止這場進行中的討論（在安全點收尾）">⏹</button>' : ""}
      <button class="h-del" title="刪除此 session（含產出檔案）">🗑</button>`;
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

async function stopSession(sid) {
  // 與 WS 的停止同一條管線（request_stop）：頁面重整／斷線後背景續跑的討論也停得掉。
  // 停止在安全點收尾、非立即中斷，稍候再刷新列表讓狀態收斂。
  try {
    const r = await fetch(`/api/sessions/${sid}/stop`, { method: "POST" });
    if (r.ok) toast("已送出停止指令，將在安全點收尾");
    else toast("找不到進行中的目標（可能已結束，或服務曾重啟——可用專案面板的恢復）", "err");
    setTimeout(openHistory, 1500);
  } catch (e) { toast("停止失敗：" + e.message, "err"); }
}

async function deleteSession(sid, status) {
  if (status === "running") { toast("執行中的 session 無法刪除", "err"); return; }
  if (!confirm("刪除此 session？產出檔案（workspace）也會一併刪除，無法復原。")) return;
  try {
    const r = await fetch(`/api/history/${sid}`, { method: "DELETE" });
    if (r.ok) { toast("已刪除", "ok"); openHistory(); }
    else toast("刪除失敗", "err");
  } catch (e) { toast("刪除失敗", "err"); }
}

async function cleanupCompleted() {
  if (!confirm("清除所有「✅ 已完成」的 session？產出檔案也會一併刪除，無法復原。")) return;
  try {
    const r = await fetch("/api/history/cleanup/completed", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    toast(`已清除 ${d.deleted ?? 0} 筆已完成 session`, "ok");
    openHistory();
  } catch (e) { toast("清除失敗", "err"); }
}

async function replaySession(sid) {
  if (replaying) return;
  historyPanel.classList.add("hidden");
  if (ws && ws.readyState === WebSocket.OPEN) ws.close();
  let events = [];
  let requirement = "";
  try {
    const data = await (await fetch(`/api/history/${sid}/events`)).json();
    events = data.events || [];
    requirement = (data.meta && data.meta.requirement) || "";
  } catch (e) { addSystem("⚠️ 無法載入此 session"); return; }

  // 直接一次把整段歷史渲染完，畫面停在最底（最新）；要看過程往上滑即可。
  // replaying 旗標維持為 true，讓 handleEvent 的 done case 不會替歷史 session 補出發佈鈕。
  replaying = true;
  setRunning(false);
  clearStream();
  clearBoard();
  addSystem("📜 歷史紀錄：" + (requirement || sid));
  for (let i = 0; i < events.length; i++) handleEvent(events[i]);
  replaying = false;
  setPhase("📜 歷史紀錄");
  scrollStream();
}

function closeHistory() { historyPanel.classList.add("hidden"); }

// --- Autopilot 自主迴圈 ------------------------------------------------
const autopilotPanel = $("#autopilotPanel");

// 迷你狀態：縮成一條狀態列（手機浮在分頁列上方、桌機右下小卡），輕量輪詢保持計數新鮮
let apMiniTimer = null;
function clearApMini() {
  autopilotPanel.classList.remove("mini");
  $("#apMini").classList.add("hidden");
  if (apMiniTimer) { clearInterval(apMiniTimer); apMiniTimer = null; }
}

async function openAutopilot() {
  clearApMini();
  autopilotPanel.classList.remove("hidden");
  await refreshAutopilot();
}

function closeAutopilot() {
  clearApMini();
  autopilotPanel.classList.add("hidden");
}

function minimizeAutopilot() {
  autopilotPanel.classList.add("mini");
  $("#apMini").classList.remove("hidden");
  if (!apMiniTimer) apMiniTimer = setInterval(refreshAutopilot, 20000);
}

async function expandAutopilot() {
  clearApMini();
  await refreshAutopilot();
}

async function refreshAutopilot() {
  try {
    const st = await (await fetch("/api/autopilot")).json();
    const c = st.counts || {};
    $("#apState").textContent =
      `${st.paused ? "⏸ 已暫停" : "▶ 執行中"}　待辦 ${c.pending || 0}・進行中 ${c.in_progress || 0}・` +
      `完成 ${c.done || 0}・失敗 ${c.failed || 0}${st.dryrun ? "　(dryrun)" : ""}`;
    $("#apToggle").textContent = st.paused ? "恢復" : "暫停";
    $("#apToggle").dataset.paused = st.paused ? "1" : "0";
    $("#apMini").textContent = `${st.paused ? "⏸" : "▶"} 待辦 ${c.pending || 0}・進行中 ${c.in_progress || 0}`;
    const list = await (await fetch("/api/autopilot/backlog")).json();
    const ul = $("#apBacklog");
    ul.innerHTML = "";
    (list.tasks || []).slice().reverse().forEach((t) => {
      const li = document.createElement("li");
      const icon = { pending: "🕓", in_progress: "⚙️", done: "✅", failed: "❌" }[t.status] || "•";
      li.textContent = `${icon} #${t.id} ${t.title}　[${t.source}]`;
      ul.appendChild(li);
    });
  } catch (e) {
    $("#apState").textContent = "讀取失敗（autopilot 服務可能未啟動）";
    $("#apMini").textContent = "讀取失敗";
  }
}

async function toggleAutopilot() {
  const paused = $("#apToggle").dataset.paused === "1";
  await fetch(paused ? "/api/autopilot/resume" : "/api/autopilot/pause", { method: "POST" });
  await refreshAutopilot();
}

async function addAutopilotTask() {
  const title = $("#apTaskTitle").value.trim();
  if (!title) return;
  await fetch("/api/autopilot/task", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  $("#apTaskTitle").value = "";
  await refreshAutopilot();
}

// --- 專案面板（藍圖 + 改良待辦）-----------------------------------------
const projectPanel = $("#projectPanel");
const PRIO_LABEL = ["P0", "P1", "P2"];
const TYPE_LABEL = { feature: "功能", bug: "缺陷", improvement: "改良" };

async function openProjectPanel() {
  const pid = $("#projectSelect").value;
  if (!pid || pid === "__new__") { toast("先在上方選擇一個專案", ""); return; }
  projectPanel.classList.remove("hidden");
  await refreshProjectPanel();
}

function closeProjectPanel() { projectPanel.classList.add("hidden"); }

function projLine(text, cls) {
  const div = document.createElement("div");
  if (cls) div.className = cls;
  div.textContent = text;
  return div;
}

async function refreshProjectPanel() {
  const body = $("#projectBody");
  const pid = $("#projectSelect").value;
  if (!pid || pid === "__new__") return;
  body.innerHTML = "<span class='muted'>載入中…</span>";
  try {
    const d = await (await fetch(`/api/projects/${pid}`)).json();
    body.innerHTML = "";
    const p = d.project || {};
    body.appendChild(projLine(`📦 ${p.name || pid}`, "proj-name"));

    // 目標 repo＝工作基底＋發佈目標：workspace 全新時下一場討論先 clone 它當基底
    // （專家在你指定的程式碼上修改），成果推分支開 PR；已同源則每場快轉到遠端 base。
    const repoRow = projLine(
      `目標 repo（工作基底＋發佈）：${p.publish_repo || "未設定（從零自建，無法開 PR）"}`,
      "muted",
    );
    const repoBtn = document.createElement("button");
    repoBtn.id = "projectPublishRepo";
    repoBtn.className = "ghost";
    repoBtn.textContent = "設定";
    repoBtn.title =
      "owner/repo；workspace 全新時下一場討論以該 repo 程式碼為工作基底；" +
      "不存在且 owner 是 token 使用者時會自動建立私有 repo，留空＝清除";
    repoBtn.onclick = () => setProjectPublishRepo(pid, p.publish_repo || "");
    repoRow.appendChild(repoBtn);
    body.appendChild(repoRow);

    // 藍圖卡片（有藍圖才顯示；raw 藍圖只提示看 BLUEPRINT.md）
    const bp = d.blueprint;
    if (bp && (bp.features || []).length) {
      if (bp.vision) body.appendChild(projLine(`願景：${bp.vision}`));
      if (bp.users) body.appendChild(projLine(`目標用戶：${bp.users}`));
      body.appendChild(projLine("核心功能：", "muted"));
      const feats = (bp.features || []).slice().sort((a, b) => (a.priority ?? 1) - (b.priority ?? 1));
      for (const f of feats) {
        const li = projLine(`${f.title}${f.detail ? " — " + f.detail : ""}`, "proj-feature");
        const tag = document.createElement("span");
        tag.className = `prio prio-${f.priority ?? 1}`;
        tag.textContent = PRIO_LABEL[f.priority ?? 1] || "P1";
        li.prepend(tag);
        body.appendChild(li);
      }
      if ((bp.milestones || []).length) {
        body.appendChild(projLine("里程碑：" + bp.milestones.map((m) => m.title).join("；"), "muted"));
      }
    } else if (bp && bp.raw) {
      body.appendChild(projLine("藍圖以原文保存（見 workspace 的 BLUEPRINT.md）", "muted"));
    } else if (p.vision) {
      body.appendChild(projLine(`願景：${p.vision}`));
      body.appendChild(projLine("（尚無結構化藍圖；開啟 TI_BLUEPRINT 後啟動持續改良即會生成）", "muted"));
    }

    // backlog（後端已按 priority→建立時間排序）
    body.appendChild(projLine("改良待辦（依消化順序）：", "muted"));
    const tasks = d.backlog || [];
    if (!tasks.length) body.appendChild(projLine("（空）", "muted"));
    for (const t of tasks) {
      const icon = { pending: "🕓", in_progress: "⚙️", done: "✅", failed: "❌" }[t.status] || "•";
      const typ = TYPE_LABEL[t.type] || TYPE_LABEL.improvement;
      const li = projLine(`${icon} #${t.id} ${t.title}　[${typ}・${t.source}]`, "proj-task");
      const tag = document.createElement("span");
      tag.className = `prio prio-${t.priority ?? 1}`;
      tag.textContent = PRIO_LABEL[t.priority ?? 1] || "P1";
      li.prepend(tag);
      body.appendChild(li);
    }

    // 中斷恢復：有任務卡在 ⚙️ 進行中時顯示。正常運行中按下會被後端 409 擋掉（無害），
    // 真正中斷（服務重啟／行程被殺）時則重置殘留並自動重啟持續改良。
    if (tasks.some((t) => t.status === "in_progress")) {
      const btn = document.createElement("button");
      btn.id = "projectRecover";
      btn.className = "ghost";
      btn.textContent = "🛟 恢復中斷的改良";
      btn.title = "服務重啟或行程中斷後：把卡在進行中的任務重置回待辦，並重新啟動持續改良迴圈";
      btn.onclick = () => recoverProject(pid);
      body.appendChild(btn);
    }

    // 專案層級操作：進行中可一鍵停止（同 WS stop 管線，斷線後也停得掉）；刪除整個專案。
    const actions = document.createElement("div");
    actions.className = "proj-actions";
    if (d.active) {
      const stopB = document.createElement("button");
      stopB.id = "projectStop";
      stopB.className = "ghost";
      stopB.textContent = "⏹ 停止執行";
      stopB.title = "對這個專案進行中的討論／持續改良迴圈送停止指令（在安全點收尾）";
      stopB.onclick = () => stopProject(pid);
      actions.appendChild(stopB);
    }
    const delB = document.createElement("button");
    delB.id = "projectDelete";
    delB.className = "ghost danger";
    delB.textContent = "🗑 刪除專案";
    delB.title = "刪除專案 meta、改良待辦、藍圖與 workspace 程式碼（歷史紀錄保留）；進行中需先停止";
    delB.onclick = () => deleteProject(pid, p.name || pid);
    actions.appendChild(delB);
    body.appendChild(actions);
  } catch (e) {
    body.innerHTML = "<span class='muted'>無法載入專案</span>";
  }
}

async function stopProject(pid) {
  try {
    const r = await fetch(`/api/sessions/${pid}/stop`, { method: "POST" });
    if (r.ok) toast("已送出停止指令，將在安全點收尾");
    else toast("沒有進行中的討論", "err");
    setTimeout(refreshProjectPanel, 1500);
  } catch (e) { toast("停止失敗：" + e.message, "err"); }
}

async function deleteProject(pid, name) {
  if (!confirm(
    `刪除專案「${name}」？\n` +
    "專案 meta、改良待辦、藍圖與 workspace 程式碼會一併刪除，無法復原。\n" +
    "（歷史紀錄保留，可在歷史面板個別刪除）"
  )) return;
  try {
    const res = await fetch(`/api/projects/${pid}`, { method: "DELETE" });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { toast(d.error || "刪除失敗", "err"); return; }
    toast("專案已刪除", "ok");
    closeProjectPanel();
    $("#projectSelect").value = "";
    await loadProjects();
    onProjectChange(); // 還原啟動列（收起 repo 標籤／刪除鈕、回到一次性討論）
  } catch (e) { toast("刪除失敗：" + e.message, "err"); }
}

async function setProjectPublishRepo(pid, current) {
  const v = prompt(
    "目標 repo（owner/repo，留空＝清除）\n" +
      "設定後：workspace 全新 → 下一場討論自動以該 repo 的程式碼為工作基底（專家改你的 repo，不另起爐灶）；" +
      "每場開始會同步遠端 base；成果推分支並開 PR。\n" +
      "repo 不存在且 owner 是你的 token 使用者時會自動建立私有 repo。",
    current,
  );
  if (v === null) return; // 取消
  try {
    const res = await fetch(`/api/projects/${pid}/publish-repo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo: v.trim() }),
    });
    const d = await res.json();
    if (!res.ok) { toast(d.error || "設定失敗", "err"); return; }
    if (d.warning) {
      toast(d.warning, "err");
    } else {
      toast(v.trim() ? `目標 repo 已設為 ${v.trim()}` : "已清除目標 repo");
    }
    await refreshProjectPanel();
    // 若該專案正選在啟動列，同步更新啟動列上的目標 repo 標籤
    if ($("#projectSelect").value === pid) {
      const repoTag = $("#projectRepo");
      if (v.trim()) {
        repoTag.textContent = `🎯 ${v.trim()}`;
        repoTag.classList.remove("unset");
      } else {
        repoTag.textContent = "🎯 目標 repo 未設定（點此設定）";
        repoTag.classList.add("unset");
      }
      repoTag.onclick = () => setProjectPublishRepo(pid, v.trim());
    }
  } catch (e) {
    toast("設定失敗：" + e.message, "err");
  }
}

async function recoverProject(pid) {
  try {
    const res = await fetch(`/api/projects/${pid}/recover`, { method: "POST" });
    const d = await res.json();
    if (!res.ok) { toast(d.error || "恢復失敗", "err"); return; }
    toast(d.reset ? `已重置 ${d.reset} 個中斷任務` : "沒有中斷殘留");
    await refreshProjectPanel();
    // 還有待辦且目前沒有討論在跑 → 以既有 improve 流程自動重啟（事件即時串流到本頁）。
    if (((d.counts || {}).pending || 0) > 0 && !startBtn.disabled) {
      $("#projectSelect").value = pid;
      $("#improveChk").checked = true;
      closeProjectPanel();
      start();
    }
  } catch (e) {
    toast("恢復失敗：" + e.message, "err");
  }
}

// --- 運維指標 ----------------------------------------------------------
const metricsPanel = $("#metricsPanel");

async function openMetrics() {
  metricsPanel.classList.remove("hidden");
  await refreshMetrics();
}

function closeMetrics() { metricsPanel.classList.add("hidden"); }

async function refreshMetrics() {
  const body = $("#metricsBody");
  try {
    const m = await (await fetch("/api/metrics")).json();
    const s = m.sessions || {};
    const h = m.history || {};
    const r = h.retention || {};
    const byStatus = h.by_status || {};
    const statusLine = Object.keys(byStatus).length
      ? Object.entries(byStatus).map(([k, v]) => `${k} ${v}`).join("・")
      : "（無）";
    const cap = s.max_concurrent ? `／上限 ${s.max_concurrent}` : "（不限）";
    const pa = m.parallel || {};
    const pcfg = pa.config || {};
    const rows = [
      ["活躍場次", `${s.active ?? "?"}${cap}`],
      ["歷史場次", `${h.total ?? "?"}`],
      ["各狀態", statusLine],
      ["保留策略", `數量 ${r.max_count ? r.max_count : "不限"}・年齡 ${r.max_age_s ? r.max_age_s + "s" : "停用"}`],
      ["workspace 目錄", `${(m.workspaces || {}).count ?? "?"}`],
      ["任務並行", `${pcfg.enabled ? "開啟" : "關閉"}・支線上限 ${pcfg.lanes ?? "?"}`],
    ];
    if (pa.enabled_runs > 0) {
      rows.push(
        ["　曾並行場次", `${pa.enabled_runs}・峰值支線 ${pa.peak_lanes}`],
        ["　平均加速", `${pa.avg_speedup}×・省下約 ${pa.wall_clock_saved_s}s`],
        ["　波次／合併衝突", `${pa.total_waves} 波・${pa.merge_conflicts} 次衝突`],
      );
    }
    // 成果記分卡：成功率／輪數／一次過率／退回原因，與「近 10 場 vs 前 10 場」趨勢。
    const sc = m.scorecard || {};
    if (sc.n > 0) {
      const pct = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
      const t = sc.tasks || {};
      const rj = sc.rejects || {};
      const rejParts = [
        ["QA 退回", rj.qa_fail], ["自測失敗", rj.smoke_fail], ["客觀閘門", rj.gate_veto],
        ["異議退回", rj.critic], ["停滯收斂", rj.stall],
      ].filter(([, v]) => v > 0).map(([k, v]) => `${k} ${v}`);
      rows.push(
        ["📈 記分卡（場次）", `${sc.n} 場・成功率 ${pct(sc.completed_rate)}`],
        ["　任務", `${t.done ?? 0}/${t.total ?? 0} 完成・一次過率 ${pct(t.first_try_rate)}`],
        ["　測試通過率", pct(sc.qa_pass_rate)],
        ["　Demo 通過率", pct(sc.demo_pass_rate)],
        ["　審查通過率", pct(sc.critic_pass_rate)],
        ["　平均輪數/任務", `${sc.avg_rounds ?? "—"}`],
        ["　退回原因", rejParts.length ? rejParts.join("・") : "（無）"],
      );
      const tr = sc.trend || {};
      if ((tr.previous || {}).n > 0) {
        const a = tr.recent, b = tr.previous;
        const arrow = (recentV, prevV, lowerBetter) => {
          if (recentV == null || prevV == null || recentV === prevV) return "→";
          return (lowerBetter ? recentV < prevV : recentV > prevV) ? "↑ 進步" : "↓ 退步";
        };
        rows.push(
          ["　趨勢：成功率", `近${a.n}場 ${pct(a.completed_rate)} vs 前${b.n}場 ${pct(b.completed_rate)}（${arrow(a.completed_rate, b.completed_rate, false)}）`],
          ["　趨勢：平均輪數", `近${a.n}場 ${a.avg_rounds ?? "—"} vs 前${b.n}場 ${b.avg_rounds ?? "—"}（${arrow(a.avg_rounds, b.avg_rounds, true)}）`],
        );
      }
    }
    body.innerHTML = "";
    rows.forEach(([k, v]) => {
      const row = document.createElement("div");
      row.className = "metric-row";
      const ks = document.createElement("span");
      ks.className = "metric-k";
      ks.textContent = k;
      const vs = document.createElement("span");
      vs.className = "metric-v";
      vs.textContent = v;
      row.append(ks, vs);
      body.appendChild(row);
    });
  } catch (e) {
    body.innerHTML = `<span class="muted">讀取失敗</span>`;
  }
}

// --- 動態流程編輯器 ----------------------------------------------------
const workflowPanel = $("#workflowPanel");
const WF_DEFAULT_NAME = "預設流程"; // 「載入預設範本」用的內建預設名
const WF_RESERVED = ["預設流程", "動態優先"]; // 內建保留流程（唯讀，不可改名/刪除）
let wfCache = [];

async function openWorkflowPanel() {
  workflowPanel.classList.remove("hidden");
  await loadWorkflowPanel();
}
function closeWorkflowPanel() { workflowPanel.classList.add("hidden"); }

function setWfHint(msg) { $("#workflowHint").textContent = msg || ""; }

async function loadWorkflowPanel(selectName) {
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

function renderWorkflowSelection() {
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

function newWorkflow() {
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

function loadWorkflowTemplate() {
  const def = wfCache.find((w) => w.name === WF_DEFAULT_NAME);
  if (def) {
    $("#workflowStages").value = JSON.stringify(def.stages || [], null, 2);
    $("#workflowStages").readOnly = false;
    setWfHint("已載入預設流程當範本——改成你要的內容、換個名稱再儲存。");
  }
}

async function saveWorkflow() {
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

async function deleteWorkflow() {
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

// --- 發佈到 GitHub -----------------------------------------------------
let publishConfigured = false;
let mergeEnabled = false;

async function loadPublishConfig() {
  try {
    const cfg = await (await fetch("/api/publish/config")).json();
    publishConfigured = !!cfg.configured;
    mergeEnabled = !!cfg.merge;
  } catch (e) { publishConfigured = false; mergeEnabled = false; }
}

function addPublishButton(sid) {
  const wrap = document.createElement("div");
  wrap.className = "publish-cta";
  const btn = document.createElement("button");
  // merge 開關開啟時，明示這顆按鈕會「發佈並合併」（合併後可再一鍵重啟）。
  btn.textContent = mergeEnabled ? "🚀 發佈並合併到 GitHub" : "🚀 發佈成果到 GitHub";
  btn.onclick = async () => {
    btn.disabled = true; btn.textContent = mergeEnabled ? "發佈並合併中…" : "發佈中…";
    try {
      const res = await (await fetch(`/api/publish/${sid}`, { method: "POST" })).json();
      // 手動發佈回傳 publisher 的 to_dict()，renderPublish 會依 outcome 顯示合併結局徽章。
      renderPublish(res);
    } catch (e) { renderPublish({ ok: false, detail: "發佈請求失敗" }); }
    btn.remove();
  };
  wrap.appendChild(btn);
  stream.appendChild(wrap);
  scrollStream();
}

// 合併結局 → 可視化徽章（對應 publisher.MergeOutcome，讓四／六種結局清楚可區分，
// 不再只看 merged=false 糊成一團）。
const OUTCOME_BADGE = {
  merged: "✅ 已合併",
  ci_failed: "❌ CI 未過",
  blocked: "🚫 被保護擋下",
  conflict: "⚠️ 衝突／分支落後",
  timeout: "⏱️ 等待 CI 逾時",
  error: "🛑 API／網路錯誤",
};

function renderPublish(p) {
  const el = document.createElement("div");
  el.className = "publish " + (p.ok ? "ok" : "fail");
  let html = (p.ok ? "🚀 " : "⚠️ ") + (p.detail || "");
  if (p.branch) html += `　<code>${p.branch}</code>`;
  // 優先依 outcome 顯示明確徽章；無 outcome（未嘗試合併）時退回舊的 merged 判斷。
  const badge = p.outcome ? OUTCOME_BADGE[p.outcome] : (p.merged ? OUTCOME_BADGE.merged : "");
  if (badge) html += `　<span class="merge-outcome ${p.outcome || (p.merged ? "merged" : "")}">${badge}</span>`;
  el.innerHTML = html;
  if (p.pr_url) {
    const a = document.createElement("a");
    a.href = p.pr_url; a.target = "_blank"; a.textContent = "查看 PR ↗";
    el.appendChild(document.createTextNode("　")); el.appendChild(a);
  }
  stream.appendChild(el);
  scrollStream();
}

// --- 重新部署重啟（設定面板常駐入口）---------------------------------
// 拉取主 repo 最新 main 並自我重啟，讓合併後的新程式碼生效。後端：POST /api/redeploy。
async function redeployNow() {
  const btn = $("#redeployBtn");
  const status = $("#redeployStatus");
  if (!confirm("確定重新部署？服務會重啟，進行中的工作與連線會中斷。")) return;
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "重新部署中…";
  status.className = "redeploy-status muted";
  status.textContent = "正在拉取最新 main 並重啟…";
  try {
    const r = await (await fetch("/api/redeploy", { method: "POST" })).json();
    status.className = "redeploy-status " + (r.ok ? "ok" : "fail");
    status.textContent = (r.ok ? "♻️ " : "⚠️ ") + (r.detail || "");
  } catch (e) {
    // 重啟成功時連線會中斷，請求可能無法正常回傳——這通常代表已在重啟。
    status.className = "redeploy-status muted";
    status.textContent = "♻️ 已送出重新部署，服務可能正在重啟，稍後重新整理頁面。";
  }
  btn.disabled = false;
  btn.textContent = prev;
}

// --- 設定（API key / provider / 模型 / GitHub token）-------------------
const settingsPanel = $("#settingsPanel");
const settingsForm = $("#settingsForm");
const settingsNav = $("#settingsNav");
const settingsSearch = $("#settingsSearch");
const settingsQuota = $("#settingsQuota");
const settingsQuotaRefresh = $("#settingsQuotaRefresh");

// 未存變更追蹤：欄位有改動且尚未儲存時，重整／關閉分頁前由瀏覽器原生對話框提醒。
// 點「✕」關面板視為主動放棄變更（不提醒），重新開啟面板會從伺服器重新載入現值。
let settingsDirty = false;
settingsForm.addEventListener("input", () => { settingsDirty = true; });
settingsForm.addEventListener("change", () => { settingsDirty = true; });
window.addEventListener("beforeunload", (e) => {
  if (!settingsDirty) return;
  e.preventDefault();
  e.returnValue = ""; // 舊版瀏覽器需要 returnValue 才會跳提醒
});

async function openSettings() {
  settingsPanel.classList.remove("hidden");
  settingsForm.innerHTML = "<div class='muted'>載入中…</div>";
  if (settingsQuota) settingsQuota.innerHTML = "<div class='muted'>載入 provider 狀態與額度中…</div>";
  try {
    const data = await (await fetch("/api/settings")).json();
    renderSettings(data.fields || []);
    refreshProviderQuota();
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

function closeSettings() {
  settingsDirty = false; // 關閉＝放棄未存變更，之後重整不再提醒
  settingsPanel.classList.add("hidden");
  // 若是從手機底部「設定」分頁進來的，關閉後回到討論分頁，避免留下空白畫面
  if (document.body.dataset.mv === "settings") setMobileView("discussion");
}

function groupSettings(fields) {
  const groups = [];
  const byName = new Map();
  for (const f of fields) {
    const name = f.group || "一般";
    if (!byName.has(name)) {
      const group = { name, fields: [] };
      groups.push(group);
      byName.set(name, group);
    }
    byName.get(name).fields.push(f);
  }
  return groups;
}

function groupId(name) {
  return "settings-group-" + String(name || "general").toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

function createSettingInput(f, row) {
  let input;
  if (f.kind === "select") {
    input = document.createElement("select");
    for (const opt of f.options) {
      const o = document.createElement("option");
      o.value = opt;
      o.textContent = opt + (f.recommended && opt === f.recommended ? "\uff08\u63a8\u85a6\uff09" : "");
      if (opt === f.value) o.selected = true;
      input.appendChild(o);
    }
    // 現值不在清單內（如 .env 手動填過清單外的模型）：追加為選項並選取，
    // 避免開面板再存檔時被靜默改成清單第一項。
    if (f.value && !f.options.includes(f.value)) {
      const o = document.createElement("option");
      o.value = f.value; o.textContent = f.value;
      o.selected = true;
      input.appendChild(o);
    }
  } else if (f.kind === "combo") {
    // 可選可打：下拉建議來自 datalist，仍接受任意輸入（如本地模型名稱）。
    input = document.createElement("input");
    input.type = "text";
    input.value = f.value || "";
    input.placeholder = f.placeholder || "";
    const dl = document.createElement("datalist");
    dl.id = "dl-" + f.env;
    for (const opt of f.options) {
      const o = document.createElement("option");
      o.value = opt;
      dl.appendChild(o);
    }
    input.setAttribute("list", dl.id);
    row.appendChild(dl);
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
  input.dataset.recommended = f.recommended || "";
  return input;
}

function renderSettings(fields) {
  settingsDirty = false; // 重新渲染後欄位即為伺服器現值，無未存變更
  settingsForm.innerHTML = "";
  if (settingsNav) settingsNav.innerHTML = "";
  if (settingsSearch) settingsSearch.value = "";

  const groups = groupSettings(fields);
  for (const [idx, group] of groups.entries()) {
    const id = groupId(group.name);
    if (settingsNav) {
      const navBtn = document.createElement("button");
      navBtn.type = "button";
      navBtn.textContent = group.name;
      navBtn.dataset.target = id;
      if (idx === 0) navBtn.classList.add("active");
      navBtn.onclick = () => {
        const target = document.getElementById(id);
        if (target && target.scrollIntoView) target.scrollIntoView({ block: "start", behavior: "smooth" });
        settingsNav.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
        navBtn.classList.add("active");
      };
      settingsNav.appendChild(navBtn);
    }

    const section = document.createElement("section");
    section.className = "settings-section";
    section.id = id;
    section.dataset.group = group.name;
    const h = document.createElement("h3");
    const title = document.createElement("span");
    title.textContent = group.name;
    const count = document.createElement("small");
    count.textContent = `${group.fields.length} 欄`;
    h.appendChild(title);
    h.appendChild(count);
    section.appendChild(h);
    const grid = document.createElement("div");
    grid.className = "settings-grid";
    for (const f of group.fields) {
      const row = document.createElement("label");
      row.className = "set-row";
      row.dataset.search = `${f.group || ""} ${f.label || ""} ${f.env || ""}`.toLowerCase();
      const cap = document.createElement("span");
      cap.className = "set-label";
      cap.textContent = f.label;
      row.appendChild(cap);
      const meta = document.createElement("span");
      meta.className = "set-env";
      meta.textContent = f.env;
      row.appendChild(meta);
      row.appendChild(createSettingInput(f, row));
      grid.appendChild(row);
    }
    section.appendChild(grid);
    settingsForm.appendChild(section);
  }
  filterSettings();
}

function applyRecommendedSettings() {
  // 把所有帶推薦值的欄位（各角色模型）一鍵填入推薦配置；不自動儲存，按「儲存」才生效。
  let n = 0;
  settingsForm.querySelectorAll("[data-env]").forEach((el) => {
    const rec = el.dataset.recommended;
    if (!rec) return;
    if (el.value !== rec) { el.value = rec; n += 1; }
  });
  settingsDirty = settingsDirty || n > 0;
  $("#settingsHint").textContent = n
    ? `已填入推薦配置（${n} 個欄位），按「儲存」生效。`
    : "所有欄位已是推薦配置。";
}

function filterSettings() {
  const q = (settingsSearch && settingsSearch.value || "").trim().toLowerCase();
  settingsForm.querySelectorAll(".set-row").forEach((row) => {
    const match = !q || (row.dataset.search || "").includes(q);
    row.classList.toggle("hidden", !match);
  });
  settingsForm.querySelectorAll(".settings-section").forEach((section) => {
    const visible = Array.from(section.querySelectorAll(".set-row")).some((row) => !row.classList.contains("hidden"));
    section.classList.toggle("hidden", !visible);
    const navBtn = settingsNav && settingsNav.querySelector(`button[data-target="${section.id}"]`);
    if (navBtn) navBtn.classList.toggle("hidden", !visible);
  });
}

function fmtInt(n) {
  const x = Number(n || 0);
  return Number.isFinite(x) ? x.toLocaleString() : "0";
}


function providerStatusLabel(p) {
  if (p.ready) return "可用";
  if (p.status === "warn") return "需確認";
  return "未設定";
}

function appendTextEl(parent, tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = text;
  parent.appendChild(el);
  return el;
}

function fmtResetRelative(epochSec) {
  if (!epochSec) return "—";
  const diff = epochSec * 1000 - Date.now();
  if (diff <= 0) return "已重置";
  const totalMin = Math.floor(diff / 60000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h >= 1) return m ? `${h} 小時 ${m} 分後重置` : `${h} 小時後重置`;
  return `${m} 分鐘後重置`;
}

function rateLimitRow(label, win) {
  const row = document.createElement("div");
  row.className = "quota-ratelimit";
  const raw = win && Number(win.used_percentage);
  const pct = Number.isFinite(raw) ? raw : 0;
  if (pct >= 90) row.classList.add("crit");
  else if (pct >= 75) row.classList.add("warn");
  appendTextEl(row, "span", "quota-rl-label", label);
  const bar = document.createElement("div");
  bar.className = "bar";
  const fill = document.createElement("div");
  fill.className = "fill";
  fill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  bar.appendChild(fill);
  row.appendChild(bar);
  appendTextEl(row, "span", "quota-rl-pct", `${pct}%`);
  appendTextEl(row, "em", "quota-rl-reset", fmtResetRelative(win && win.reset_at));
  return row;
}

const RL_ERRORS = {
  token_missing: "找不到訂閱憑證（需 provider CLI 登入）。",
  unauthorized: "token 已過期：跑一次該 provider 討論或重新登入後重試。",
  unreachable: "暫時無法取得官方額度（稍後重試）。",
};

function rateLimitBlock(rl) {
  const wrap = document.createElement("div");
  wrap.className = "quota-ratelimits";
  appendTextEl(wrap, "div", "quota-rl-kicker", "訂閱額度（官方）");
  if (rl.error) {
    appendTextEl(wrap, "div", "quota-rl-note", RL_ERRORS[rl.error] || "無法取得官方額度。");
    return wrap;
  }
  if (Array.isArray(rl.buckets)) {
    // Antigravity：有數值配額畫每模型百分比條；不限量則改顯示訂閱層級
    if (rl.buckets.length) {
      for (const b of rl.buckets) wrap.appendChild(rateLimitRow(b.label, b));
    } else if (rl.tier && rl.tier.label) {
      const plan = rl.tier.unlimited ? `${rl.tier.label} · 不限量` : rl.tier.label;
      appendTextEl(wrap, "div", "quota-rl-note", `方案：${plan}`);
      if (rl.tier.paid_tier) {
        appendTextEl(wrap, "div", "quota-rl-note", `可升級：${rl.tier.paid_tier}`);
      }
    } else {
      appendTextEl(wrap, "div", "quota-rl-note", "目前無配額資料。");
    }
  } else {
    if (rl.five_hour) wrap.appendChild(rateLimitRow("5 小時", rl.five_hour));
    if (rl.seven_day) wrap.appendChild(rateLimitRow("7 天", rl.seven_day));
    if (rl.seven_day_sonnet) wrap.appendChild(rateLimitRow("7 天 · Sonnet", rl.seven_day_sonnet));
    if (rl.seven_day_opus) wrap.appendChild(rateLimitRow("7 天 · Opus", rl.seven_day_opus));
  }
  if (rl.fetched_at) {
    appendTextEl(
      wrap,
      "div",
      "quota-rl-stamp",
      `擷取於 ${new Date(rl.fetched_at * 1000).toLocaleTimeString()}`,
    );
  }
  return wrap;
}

function claudeAccountsBlock(accounts) {
  // 每個 Claude 訂閱帳號一塊：標題（帳號 + 訂閱類型）、在線標記或切換鈕、官方額度條。
  const wrap = document.createElement("div");
  wrap.className = "quota-accounts";
  appendTextEl(wrap, "div", "quota-rl-kicker", "訂閱帳號（可切換）");
  for (const a of accounts) {
    const box = document.createElement("div");
    box.className = `quota-account${a.active ? " active" : ""}`;
    const head = document.createElement("div");
    head.className = "quota-account-head";
    const name = a.subscription ? `帳號 ${a.label} · ${a.subscription}` : `帳號 ${a.label}`;
    appendTextEl(head, "strong", "", name);
    if (a.active) {
      appendTextEl(head, "span", "quota-account-live", "● 在線");
    } else {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost quota-account-switch";
      btn.textContent = "切換到此帳號";
      btn.addEventListener("click", () => switchClaudeAccount(a.label, btn));
      head.appendChild(btn);
    }
    box.appendChild(head);
    if (a.rate_limits) box.appendChild(rateLimitBlock(a.rate_limits));
    wrap.appendChild(box);
  }
  return wrap;
}

async function switchClaudeAccount(label, btn) {
  if (
    !confirm(
      `切換到帳號 ${label}？\n\n` +
        "這會重啟後端服務：本面板會短暫斷線後自動重連。\n" +
        "若有互動討論或 autopilot 任務正在進行，會被擋下、不會切換。",
    )
  )
    return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "切換中…";
  }
  try {
    const resp = await fetch("/api/claude-account/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    });
    if (resp.status === 409) {
      const d = await resp.json().catch(() => ({}));
      toast(`無法切換：${(d.reasons || []).join("、") || "有討論正在進行"}`, "error");
      if (btn) {
        btn.disabled = false;
        btn.textContent = "切換到此帳號";
      }
      return;
    }
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      toast(`切換失敗：${d.error || resp.status}`, "error");
      if (btn) {
        btn.disabled = false;
        btn.textContent = "切換到此帳號";
      }
      return;
    }
    toast(`已切換到帳號 ${label}，服務重啟中…`, "ok");
    if (settingsQuota) {
      settingsQuota.innerHTML = `<div class='muted'>已切換到帳號 ${label}，服務重啟中…（重連後自動刷新額度）</div>`;
    }
    waitForReconnectThenRefresh();
  } catch (e) {
    toast("切換請求已送出，服務可能正在重啟，稍候重新整理頁面。", "");
  }
}

async function waitForReconnectThenRefresh() {
  // 服務重啟需數秒；輪詢 provider-quota 直到端點恢復再刷新面板（最多約 60 秒）。
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const resp = await fetch("/api/provider-quota", { cache: "no-store" });
      if (resp.ok) {
        renderProviderQuota(await resp.json());
        return;
      }
    } catch (e) {
      // 服務還沒起來，繼續等
    }
  }
  if (settingsQuota) {
    settingsQuota.innerHTML = "<div class='muted'>服務重啟逾時，請手動重新整理頁面。</div>";
  }
}

function renderProviderQuota(data) {
  if (!settingsQuota) return;
  const providers = data.providers || [];
  settingsQuota.innerHTML = "";

  const head = document.createElement("div");
  head.className = "quota-head";
  const titleWrap = document.createElement("div");
  appendTextEl(titleWrap, "div", "quota-kicker", "Provider 即時剩餘額度");
  appendTextEl(titleWrap, "div", "quota-title", `目前使用：${data.active_provider || "未知 provider"}`);
  head.appendChild(titleWrap);
  settingsQuota.appendChild(head);

  const grid = document.createElement("div");
  grid.className = "quota-grid";
  for (const p of providers) {
    const models = (p.models || []).slice(0, 4).join(" · ");
    const card = document.createElement("div");
    card.className = `quota-card ${p.status || ""}${p.active ? " active" : ""}`;
    const cardHead = document.createElement("div");
    cardHead.className = "quota-card-head";
    appendTextEl(cardHead, "strong", "", p.label || p.key || "provider");
    appendTextEl(cardHead, "span", "", providerStatusLabel(p));
    card.appendChild(cardHead);
    appendTextEl(card, "div", "quota-note", (p.quota && p.quota.summary) || "");
    if (Array.isArray(p.accounts) && p.accounts.length) {
      // 多帳號（claude）：逐帳號顯示額度＋在線標記＋切換鈕，取代單一額度區。
      card.appendChild(claudeAccountsBlock(p.accounts));
    } else if (p.rate_limits) {
      card.appendChild(rateLimitBlock(p.rate_limits));
    }
    if (models) appendTextEl(card, "div", "quota-models", models);
    if (p.quota && p.quota.detail) appendTextEl(card, "div", "quota-detail", p.quota.detail);
    grid.appendChild(card);
  }
  settingsQuota.appendChild(grid);
}

async function refreshProviderQuota() {
  if (!settingsQuota) return;
  settingsQuota.innerHTML = "<div class='muted'>更新 provider 狀態與額度中…</div>";
  if (settingsQuotaRefresh) settingsQuotaRefresh.disabled = true;
  try {
    const data = await (await fetch("/api/provider-quota")).json();
    renderProviderQuota(data);
  } catch (e) {
    settingsQuota.innerHTML = "<div class='muted'>無法載入 provider 狀態與額度</div>";
  } finally {
    if (settingsQuotaRefresh) settingsQuotaRefresh.disabled = false;
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
  const btn = $("#settingsSave");
  hint.textContent = "儲存中…";
  btn.disabled = true; // 防連點：請求期間禁用，避免送出多筆 POST
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
  } finally {
    btn.disabled = false;
  }
}

startBtn.onclick = start;
stopBtn.onclick = stop;
interjectBtn.onclick = sendInterject;
$("#settingsBtn").onclick = openSettings;
$("#settingsClose").onclick = closeSettings;
// 點卡片外的暗色遮罩即關閉設定
settingsPanel.addEventListener("click", (e) => {
  if (e.target === settingsPanel) closeSettings();
});
$("#settingsSave").onclick = saveSettings;
$("#settingsRecommend").onclick = applyRecommendedSettings;
if (settingsSearch) settingsSearch.addEventListener("input", filterSettings);
if (settingsQuotaRefresh) settingsQuotaRefresh.onclick = refreshProviderQuota;
$("#pwSave").onclick = savePassword;
$("#redeployBtn").onclick = redeployNow;
$("#downloadBtn").onclick = downloadWorkspace;
$("#historyBtn").onclick = openHistory;
$("#historyClose").onclick = closeHistory;
$("#historyCleanup").onclick = cleanupCompleted;
$("#autopilotBtn").onclick = openAutopilot;
// head 內按鈕需 stopPropagation：迷你狀態下整條標題列可點擊展開
$("#autopilotClose").onclick = (e) => { e.stopPropagation(); closeAutopilot(); };
$("#autopilotMin").onclick = (e) => { e.stopPropagation(); minimizeAutopilot(); };
$("#autopilotHead").onclick = () => {
  if (autopilotPanel.classList.contains("mini")) expandAutopilot();
};
$("#apToggle").onclick = toggleAutopilot;
$("#apAddBtn").onclick = addAutopilotTask;
$("#deckBar").onclick = () => setDeckCollapsed(false);
$("#deckStop").onclick = (e) => { e.stopPropagation(); stop(); };
$("#metricsBtn").onclick = openMetrics;
$("#metricsClose").onclick = closeMetrics;
$("#metricsRefresh").onclick = refreshMetrics;
$("#workflowBtn").onclick = openWorkflowPanel;
$("#workflowClose").onclick = closeWorkflowPanel;
$("#workflowRefresh").onclick = () => loadWorkflowPanel();
$("#workflowList").addEventListener("change", renderWorkflowSelection);
$("#workflowNew").onclick = newWorkflow;
$("#workflowTemplate").onclick = loadWorkflowTemplate;
$("#workflowSave").onclick = saveWorkflow;
$("#workflowDelete").onclick = deleteWorkflow;
$("#projectBtn").onclick = openProjectPanel;
$("#projectClose").onclick = closeProjectPanel;
$("#projectRefresh").onclick = refreshProjectPanel;
$("#projectSelect").addEventListener("change", onProjectChange);
$("#improveChk").addEventListener("change", () => updateStartLabel());
reqInput.addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });
interjectInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendInterject(); });

// --- 手機分頁導覽：在討論／成員／看板／檔案間切換（桌機自動隱藏分頁列）----
function setMobileView(view) {
  document.body.dataset.mv = view;
  document.querySelectorAll(".mobiletabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mv === view);
  });
  if (view === "settings") openSettings();
  else closeSettings();
}
document.querySelectorAll(".mobiletabs button").forEach((b) => {
  b.onclick = () => setMobileView(b.dataset.mv);
});
setMobileView("discussion");

// --- 桌面右欄分頁（看板／檔案）與左欄成員收合 ---------------------------
document.querySelectorAll(".rail-tabs button").forEach((b) => {
  b.onclick = () => {
    const side = document.querySelector(".side");
    if (side) side.dataset.rv = b.dataset.rt;
    document.querySelectorAll(".rail-tabs button").forEach((x) =>
      x.classList.toggle("active", x === b));
  };
});
$("#expertsToggle").onclick = () => document.body.classList.toggle("experts-collapsed");

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
  loadProjects();
  loadWorkflows();
}

init();
