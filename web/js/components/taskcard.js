// 交辦任務卡片(Kimi 化 PR6):在 home 對話區呈現背景 agent 任務的即時進度。
// 資料=既有 GET /api/autopilot/activity(backlog×history join);5 秒輪詢,終局即停。
// 卡片一律 createElement/textContent(零 innerHTML 模型內容)。
import { $, appendTextEl, icon } from "../dom.js";

export const TC_STATUS = {
  pending: { label: "排隊中", cls: "wait" },
  in_progress: { label: "執行中", cls: "run" },
  merging: { label: "合併中", cls: "run" },
  done: { label: "已完成", cls: "ok" },
  failed: { label: "失敗", cls: "bad" },
  parked: { label: "待處理(停放)", cls: "wait" },
};
const TERMINAL = new Set(["done", "failed", "parked"]);
const POLL_MS = 5000;

// 純函式(供 .mjs 測試):由 activity 任務列算卡片顯示模型。
export function taskCardModel(task) {
  const st = TC_STATUS[task?.status] || { label: task?.status || "未知", cls: "wait" };
  const bits = [];
  if (task?.pr) bits.push(`PR #${task.pr}`);
  if (task?.note) bits.push(String(task.note).slice(0, 120));
  return {
    label: st.label,
    cls: st.cls,
    sub: bits.join("・"),
    terminal: TERMINAL.has(task?.status),
    liveSid: task?.status === "in_progress" ? task?.session_id || null : null,
  };
}

export function createTaskCard(taskId, title) {
  const card = document.createElement("div");
  card.className = "taskcard";
  card.dataset.taskId = String(taskId);
  const head = document.createElement("div");
  head.className = "tc-head";
  head.appendChild(icon("bot", "icon"));
  appendTextEl(head, "span", "tc-title", title);
  appendTextEl(head, "span", "tc-status wait", "排隊中");
  card.appendChild(head);
  appendTextEl(card, "div", "tc-sub muted", `任務 #${taskId}・agent 會在背景完成,期間你可以繼續交辦或討論`);
  const live = document.createElement("button");
  live.className = "ghost tc-live hidden";
  live.textContent = "觀看直播";
  card.appendChild(live);
  return card;
}

export function updateTaskCard(card, task) {
  const m = taskCardModel(task);
  const st = card.querySelector(".tc-status");
  if (st) { st.textContent = m.label; st.className = "tc-status " + m.cls; }
  const sub = card.querySelector(".tc-sub");
  if (sub && m.sub) sub.textContent = m.sub;
  const live = card.querySelector(".tc-live");
  if (live) {
    live.classList.toggle("hidden", !m.liveSid);
    if (m.liveSid) {
      live.onclick = () =>
        import("../panels/home.js").then((mod) => mod.attachSessionInHome(m.liveSid));
    }
  }
  return m.terminal;
}

// 5 秒輪詢 activity 更新卡片;終局即停。背景分頁不打 API。
export function startTaskCardPolling(card) {
  const id = Number(card.dataset.taskId);
  const timer = setInterval(async () => {
    if (typeof document.hidden === "boolean" && document.hidden) return;
    try {
      const data = await (await fetch("/api/autopilot/activity?limit=50")).json();
      const task = (data.tasks || []).find((x) => x.id === id);
      if (task && updateTaskCard(card, task)) clearInterval(timer);
    } catch {
      /* 單次輪詢失敗忽略,下一拍再試 */
    }
  }, POLL_MS);
  return timer;
}
