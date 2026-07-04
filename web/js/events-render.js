// 事件渲染中樞：handleEvent() 把工作室事件渲染成討論串 / 看板 / 檔案面板。
// switch 分派為前後端契約（studio/events.py 的 EventType 一一對應）：
// 無 default 分支（未知事件天然被忽略）、payload 一律 `ev.payload || {}` 防呆。
import { $, toast } from "./dom.js";
import { state } from "./state.js";
import { setRunning, loadProjects } from "./panels/deck.js";

const stream = () => $("#stream");

export function setPhase(text) { $("#phase").textContent = text; }

export function scrollStream() {
  const el = stream();
  el.scrollTop = el.scrollHeight;
}

// 並行支線多欄渲染：帶 task_id 的發言/工具進各自的欄位，其餘事件照常進主時間軸。
let laneBoard = null;
const laneCols = {};

export function clearStream() {
  stream().innerHTML = "";
  laneBoard = null;
  for (const k of Object.keys(laneCols)) delete laneCols[k];
}

// 取得某支線欄位的內容容器（首次出現時建立「支線看板」與該欄）。
function laneBody(taskId) {
  if (!laneBoard) {
    laneBoard = document.createElement("div");
    laneBoard.className = "lanes-board";
    stream().appendChild(laneBoard);
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
  return p && p.task_id != null ? laneBody(p.task_id) : stream();
}

export function clearBoard() {
  document.querySelectorAll(".col .cards").forEach((c) => (c.innerHTML = ""));
}

// 專家狀態的無障礙文字（狀態點顏色的等價語意）
const EXPERT_STATUS_LABEL = { idle: "待命", thinking: "思考中", working: "工作中" };

export function renderRoster(roster) {
  const expertList = $("#expertList");
  expertList.innerHTML = "";
  for (const r of roster) {
    const el = document.createElement("div");
    el.className = "expert";
    el.dataset.key = r.key;
    el.dataset.status = "idle";
    el.setAttribute("aria-label", `${r.name}（${EXPERT_STATUS_LABEL.idle}）`);
    el.innerHTML = `
      <div class="av">${r.avatar}</div>
      <div class="meta"><div class="nm">${r.name}</div><div class="tt">${r.title}${r.provider ? " · " + r.provider : ""}</div></div>
      <div class="dot"></div>`;
    expertList.appendChild(el);
  }
}

// 動態招募：把單一新成員插入成員欄（已存在則略過，冪等）。
export function addRosterMember(r) {
  const expertList = $("#expertList");
  if (!r.key || expertList.querySelector(`.expert[data-key="${r.key}"]`)) return;
  const el = document.createElement("div");
  el.className = "expert";
  el.dataset.key = r.key;
  el.dataset.status = "idle";
  el.setAttribute("aria-label", `${r.name || r.key}（${EXPERT_STATUS_LABEL.idle}）`);
  el.innerHTML = `
      <div class="av">${r.avatar || "🆕"}</div>
      <div class="meta"><div class="nm">${r.name || r.key}</div><div class="tt">${r.title || ""}${r.provider ? " · " + r.provider : ""}</div></div>
      <div class="dot"></div>`;
  expertList.appendChild(el);
}

export function setExpertStatus(key, status) {
  const expertList = $("#expertList");
  const el = expertList.querySelector(`.expert[data-key="${key}"]`);
  if (!el) return;
  el.dataset.status = status;
  const nm = el.querySelector(".nm");
  el.setAttribute(
    "aria-label",
    `${(nm && nm.textContent) || key}（${EXPERT_STATUS_LABEL[status] || status}）`,
  );
  expertList.querySelectorAll(".expert").forEach((e) => e.classList.remove("active"));
  if (status !== "idle") el.classList.add("active");
}

export function addMessage(p) {
  const el = document.createElement("div");
  el.className = "msg" + (p.task_id != null ? " lane lane-" + (p.task_id % 6) : "");
  el.innerHTML = `
    <div class="av">${p.avatar}</div>
    <div class="body"><div class="who">${p.name}</div><div class="txt"></div></div>`;
  el.querySelector(".txt").textContent = p.text;
  sink(p).appendChild(el);
  scrollStream();
}

export function addTool(p) {
  const el = document.createElement("div");
  el.className = "tool" + (p.task_id != null ? " lane lane-" + (p.task_id % 6) : "");
  el.innerHTML = `<span class="badge">${p.tool}</span><span></span>`;
  el.querySelector("span:last-child").textContent = p.summary;
  sink(p).appendChild(el);
  scrollStream();
}

export function addSystem(text, cls) {
  const el = document.createElement("div");
  el.className = "sys" + (cls ? " " + cls : "");
  el.textContent = text;
  stream().appendChild(el);
  scrollStream();
}

export function addResult(passed, detail, log) {
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
  stream().appendChild(el);
  scrollStream();
}

export function addHuman(text) {
  const el = document.createElement("div");
  el.className = "msg human";
  el.innerHTML = `
    <div class="av">🙋</div>
    <div class="body"><div class="who">你（插話）</div><div class="txt"></div></div>`;
  el.querySelector(".txt").textContent = text;
  stream().appendChild(el);
  scrollStream();
}

export function addCommit(p) {
  const el = document.createElement("div");
  el.className = "commit";
  el.innerHTML = `<span class="hash">⎇ ${p.hash}</span><span></span>`;
  el.querySelector("span:last-child").textContent = p.message;
  stream().appendChild(el);
  scrollStream();
}

export function addDemo(p) {
  const el = document.createElement("div");
  el.className = "demo " + (p.passed ? "pass" : "fail");
  el.innerHTML = `<div class="demohead">${p.passed ? "▶️ Demo 執行成功" : "▶️ Demo 執行失敗"} <code></code></div>`;
  el.querySelector("code").textContent = p.command + "  (exit " + p.exit_code + ")";
  const pre = document.createElement("pre");
  pre.textContent = p.output || "（無輸出）";
  el.appendChild(pre);
  stream().appendChild(el);
  scrollStream();
}

export const CI_LABELS = {
  pass: ["✅ CI 通過", "pass"],
  none: ["✅ 無 CI 檢查，直接合併", "pass"],
  fail: ["❌ CI 未通過", "fail"],
  error: ["⚠️ CI 等待逾時/出錯，保留 PR 待人工", "fail"],
  merged: ["🎉 已自動合併（squash + 刪分支）", "pass"],
  merge_failed: ["❌ 合併失敗", "fail"],
  giveup: ["🛑 CI 連續失敗達上限，保留 PR 待人工", "fail"],
};

export function addCI(p) {
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
  stream().appendChild(el);
  scrollStream();
}

export function renderBoard(columns) {
  for (const [col, items] of Object.entries(columns)) {
    const wrap = document.querySelector(`.col[data-col="${col}"] .cards`);
    if (!wrap) continue;
    wrap.innerHTML = "";
    for (const it of items) {
      const c = document.createElement("div");
      c.className = "card";
      c.setAttribute("role", "listitem");
      c.textContent = it.title;
      wrap.appendChild(c);
    }
    // 欄計數 badge（欄空時留白不顯示 0）
    const badge = document.querySelector(`.col[data-col="${col}"] .col-count`);
    if (badge) badge.textContent = items.length ? String(items.length) : "";
  }
}

export async function refreshFiles() {
  const wid = state.workspaceId || state.sessionId;
  if (!wid) return;
  try {
    const res = await fetch(`/api/workspace/${wid}/files`);
    const data = await res.json();
    const list = $("#fileList");
    list.innerHTML = "";
    for (const f of data.files) {
      // 真按鈕：鍵盤可聚焦/Enter 可開，不再用 li.onclick
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "file-open";
      btn.textContent = f;
      btn.onclick = () => viewFile(f);
      li.appendChild(btn);
      list.appendChild(li);
    }
    const btn = $("#downloadBtn");
    if (btn) btn.classList.toggle("hidden", data.files.length === 0);
  } catch (e) { /* 忽略 */ }
}

export function downloadWorkspace() {
  const wid = state.workspaceId || state.sessionId;
  if (!wid) return;
  // 透過隱藏連結觸發瀏覽器下載；同源 cookie 會自動帶上（門禁啟用時）。
  const a = document.createElement("a");
  a.href = `/api/workspace/${wid}/download`;
  a.download = `workspace-${wid}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export async function viewFile(path) {
  const wid = state.workspaceId || state.sessionId;
  const res = await fetch(`/api/workspace/${wid}/file?path=${encodeURIComponent(path)}`);
  if (!res.ok) return;
  const data = await res.json();
  $("#fileView").textContent = data.content;
}

export function handleEvent(ev) {
  if (ev.session_id) state.sessionId = ev.session_id;
  const p = ev.payload || {};
  switch (ev.type) {
    case "session_started":
      state.workspaceId = p.workspace_id || ev.session_id;
      clearStream();
      renderRoster(p.roster || []);
      addSystem("🛠️ 工作室開工：" + (p.requirement || ""));
      if (p.repo_url) addSystem("📦 既有專案：" + p.repo_url);
      break;
    case "phase_change":
      setPhase(p.phase);
      addSystem(`— ${p.phase}${p.detail ? "：" + p.detail : ""} —`, "phase");
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
    case "dispatch_decision": {
      // 額度感知 per-task 派工：任務實作者暫時換綁 provider/model（log-line 樣式，比照 git_commit）。
      const target = (p.provider || "") + (p.model ? "/" + p.model : "");
      addSystem(
        `🧭 任務 #${p.task_id ?? "?"} → ${p.role || ""}@${target}` +
          (p.reason ? `（${p.reason}）` : ""),
      );
      break;
    }
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
      if (!state.replaying) $("#interjectInput").focus();
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
    case "task_result":
      // 任務執行結果事件，前端不崩潰。
      break;
    case "done":
      // 持續改良模式：迴圈內每輪討論的 done 只是「一輪結束」，迴圈總結（帶 improve）才收尾。
      if (state.improveMode && !p.improve) {
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
      if (!state.replaying && p.completed && state.publishConfigured && !p.improve) addPublishButton(state.sessionId);
      setRunning(false);
      loadProjects(); // backlog 統計可能已變，刷新專案選單
      break;
    case "error":
      addSystem("⚠️ 錯誤：" + (p.message || "未知錯誤"));
      toast(p.message || "發生錯誤", "err");
      // 持續改良模式下，單輪錯誤不終止迴圈（improver 會記 failed 並評估續跑）。
      if (!state.improveMode) setRunning(false);
      break;
    case "vote_result": {
      // 3-AI 表決：PM 無法決定時跨 provider 多數決（逐票列出，平手/降級標示）。
      addSystem(`🗳️ 表決：${p.topic || ""}`);
      (p.ballots || []).forEach((b) => {
        addSystem(`　${b.voter || "?"}（${b.provider || "?"}）→ ${b.choice || "棄權"}`);
      });
      let voteLine = `🏁 勝出：${p.winner || "（無多數共識）"}`;
      if (p.tie) voteLine += "（平手，以 PM 票定案）";
      if (p.degraded) voteLine += "（降級：可用 provider 不足兩位，PM 單票定案）";
      addSystem(voteLine);
      break;
    }
    case "appraisal": {
      // 考核：收尾檢討時 PM 對參與 AI 的績效評分（log-line 樣式，比照 dispatch_decision）。
      const who = (p.provider || p.role || "?") + (p.model ? "/" + p.model : "");
      addSystem(`📋 考核 ${who}：★${p.score ?? "?"}${p.comment ? " " + p.comment : ""}`);
      break;
    }
  }
}

// --- 發佈到 GitHub -----------------------------------------------------
export async function loadPublishConfig() {
  try {
    const cfg = await (await fetch("/api/publish/config")).json();
    state.publishConfigured = !!cfg.configured;
    state.mergeEnabled = !!cfg.merge;
  } catch (e) { state.publishConfigured = false; state.mergeEnabled = false; }
}

export function addPublishButton(sid) {
  const wrap = document.createElement("div");
  wrap.className = "publish-cta";
  const btn = document.createElement("button");
  // merge 開關開啟時，明示這顆按鈕會「發佈並合併」（合併後可再一鍵重啟）。
  btn.textContent = state.mergeEnabled ? "🚀 發佈並合併到 GitHub" : "🚀 發佈成果到 GitHub";
  btn.onclick = async () => {
    btn.disabled = true; btn.textContent = state.mergeEnabled ? "發佈並合併中…" : "發佈中…";
    try {
      const res = await (await fetch(`/api/publish/${sid}`, { method: "POST" })).json();
      // 手動發佈回傳 publisher 的 to_dict()，renderPublish 會依 outcome 顯示合併結局徽章。
      renderPublish(res);
    } catch (e) { renderPublish({ ok: false, detail: "發佈請求失敗" }); }
    btn.remove();
  };
  wrap.appendChild(btn);
  stream().appendChild(wrap);
  scrollStream();
}

// 合併結局 → 可視化徽章（對應 publisher.MergeOutcome，讓四／六種結局清楚可區分，
// 不再只看 merged=false 糊成一團）。
export const OUTCOME_BADGE = {
  merged: "✅ 已合併",
  ci_failed: "❌ CI 未過",
  blocked: "🚫 被保護擋下",
  conflict: "⚠️ 衝突／分支落後",
  timeout: "⏱️ 等待 CI 逾時",
  error: "🛑 API／網路錯誤",
};

export function renderPublish(p) {
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
  stream().appendChild(el);
  scrollStream();
}
