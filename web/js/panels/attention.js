// 「需要你」例外收件匣(軌 F1):按例外監控的單一入口——澄清待答票(可直接答覆)、
// 停放任務+原因、近 7 天紅色事件。資料=GET /api/autopilot/attention;答覆走既有
// task action unpark+note 契約(答案成為 agent 續跑的指示)。import 期零 DOM。
import { $, appendTextEl, toast } from "../dom.js";

export const EVENT_LABEL = {
  task_failed: "任務失敗",
  loop_stall: "迴圈停滯",
  quota_exhausted: "額度耗盡",
  watchdog_paused: "看門狗暫停",
  slo_brake: "SLO 自動煞車",
  deploy_verify_failed: "部署驗證失敗",
  clarify_pending: "澄清待答",
};

async function answerClarify(taskId, note) {
  const r = await fetch(`/api/autopilot/task/${taskId}/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "unpark", note }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    toast(d.detail || "答覆失敗", "err");
    return false;
  }
  toast(`#${taskId} 已答覆並取回佇列`);
  return true;
}

function renderClarifySection(host, tickets) {
  appendTextEl(host, "h3", "stage-sec", `澄清待答(${tickets.length})`);
  if (!tickets.length) {
    appendTextEl(host, "p", "muted", "沒有待答的問題——agent 目前都看得懂。");
    return;
  }
  for (const t of tickets) {
    const card = document.createElement("div");
    card.className = "att-card clarify";
    appendTextEl(card, "div", "att-title", `#${t.id} ${t.title || ""}`);
    appendTextEl(card, "p", "att-question", t.clarify || "");
    const row = document.createElement("div");
    row.className = "att-answer";
    const input = document.createElement("textarea");
    input.className = "att-input";
    input.rows = 2;
    input.placeholder = "你的答覆(會成為 agent 續跑的指示)…";
    const btn = document.createElement("button");
    btn.className = "primary att-send";
    btn.textContent = "答覆並取回";
    btn.onclick = async () => {
      const note = input.value.trim();
      if (!note) { input.focus(); return; }
      btn.disabled = true;
      const ok = await answerClarify(t.id, note);
      if (ok) await renderAttention();
      else btn.disabled = false;
    };
    row.appendChild(input);
    row.appendChild(btn);
    card.appendChild(row);
    host.appendChild(card);
  }
}

function renderParkedSection(host, parked) {
  appendTextEl(host, "h3", "stage-sec", `停放中(${parked.length})`);
  if (!parked.length) {
    appendTextEl(host, "p", "muted", "沒有停放的任務。");
    return;
  }
  for (const t of parked) {
    const card = document.createElement("div");
    card.className = "att-card";
    const head = document.createElement("div");
    head.className = "att-head";
    appendTextEl(head, "div", "att-title", `#${t.id} ${t.title || ""}`);
    const btn = document.createElement("button");
    btn.className = "ghost att-unpark";
    btn.textContent = "取回";
    btn.title = "取回為 pending,讓 agent 續跑";
    btn.onclick = async () => {
      btn.disabled = true;
      const ok = await answerClarify(t.id, "");
      if (ok) await renderAttention();
      else btn.disabled = false;
    };
    head.appendChild(btn);
    card.appendChild(head);
    if (t.note) appendTextEl(card, "p", "muted att-note", t.note);
    host.appendChild(card);
  }
}

function renderEventsSection(host, events) {
  appendTextEl(host, "h3", "stage-sec", "紅色事件(7 天)");
  if (!events.length) {
    appendTextEl(host, "p", "muted", "一片安靜——沒有需要注意的事件。");
    return;
  }
  const list = document.createElement("div");
  list.className = "att-events";
  for (const e of events) {
    const row = document.createElement("div");
    row.className = "att-event";
    appendTextEl(row, "span", "att-ev-kind", EVENT_LABEL[e.kind] || e.kind);
    appendTextEl(row, "span", "att-ev-title", e.title || "");
    const when = e.ts ? new Date(e.ts * 1000).toLocaleString() : "";
    appendTextEl(row, "span", "att-ev-time muted", when);
    list.appendChild(row);
  }
  host.appendChild(list);
}

export function updateBadge(count) {
  const badge = $("#snAttentionBadge");
  if (!badge) return;
  badge.textContent = count > 99 ? "99+" : String(count);
  badge.classList.toggle("hidden", !count);
}

// 側欄 badge 輕量刷新(home 載入時呼叫);失敗靜默——badge 是輔助不是真相。
export async function refreshAttentionBadge() {
  try {
    const d = await (await fetch("/api/autopilot/attention")).json();
    updateBadge(d.pending_clarify || 0);
  } catch { /* 靜默 */ }
}

export async function renderAttention() {
  const host = $("#homeAttention");
  if (!host) return;
  host.innerHTML = "";
  appendTextEl(host, "h2", "home-sub-title", "需要你");
  let d = null;
  try {
    d = await (await fetch("/api/autopilot/attention")).json();
  } catch {
    appendTextEl(host, "p", "muted", "載入失敗——稍後再試。");
    return;
  }
  appendTextEl(host, "p", "muted", "只有這裡列出的事需要你——其餘一切 agent 自己處理。");
  renderClarifySection(host, d.clarify || []);
  renderParkedSection(host, d.parked || []);
  renderEventsSection(host, d.events || []);
  updateBadge(d.pending_clarify || 0);
}

export function openAttention() {
  import("./home.js").then((m) => m.setSubview("attention"));
  renderAttention();
}
