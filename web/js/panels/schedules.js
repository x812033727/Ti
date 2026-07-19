// 排程任務頁(Kimi 化 PR11):列表/建立/編輯/啟停/刪除。
// 資料=PR10 後端 /api/schedules*;寫入是 admin 面(401/403 明確提示不靜默)。
// import 期零 DOM;綁定由 sidenav 導流、app.js 集中。
import { $, appendTextEl, toast, icon } from "../dom.js";
import { openFormModal, openConfirmModal } from "../components/modal.js";

export const REC_LABEL = {
  daily: (r) => `每日 ${r.time}(UTC)`,
  weekly: (r) => `每週${"一二三四五六日"[r.weekday ?? 0]} ${r.time}(UTC)`,
  interval_hours: (r) => `每 ${r.hours} 小時`,
};

// 純函式(供 .mjs 測試):表單值 → recurrence 物件。
export function recurrenceFromForm(v) {
  if (v.kind === "weekly") return { kind: "weekly", time: v.time || "08:00", weekday: Number(v.weekday || 0) };
  if (v.kind === "interval_hours") return { kind: "interval_hours", hours: Number(v.hours || 24) };
  return { kind: "daily", time: v.time || "08:00" };
}

function scheduleFields(s = {}) {
  const rec = s.recurrence || {};
  return [
    { key: "title", label: "任務標題", value: s.title || "", placeholder: "例:產出每週營運摘要" },
    { key: "detail", label: "細節(交給 agent 的說明)", type: "textarea", value: s.detail || "" },
    {
      key: "kind", label: "頻率", type: "radio", value: rec.kind || "daily",
      options: [
        { value: "daily", label: "每日" },
        { value: "weekly", label: "每週" },
        { value: "interval_hours", label: "每 N 小時" },
      ],
    },
    { key: "time", label: "時刻 HH:MM(UTC;每日/每週用)", value: rec.time || "08:00" },
    {
      key: "weekday", label: "星期幾(每週用)", type: "select", value: String(rec.weekday ?? 0),
      options: ["0", "1", "2", "3", "4", "5", "6"].map((v, i) => ({ value: v, label: "週" + "一二三四五六日"[i] })),
    },
    { key: "hours", label: "間隔小時(每 N 小時用)", value: String(rec.hours || 24) },
  ];
}

async function submitSchedule(method, url, values) {
  const body = {
    title: values.title,
    detail: values.detail,
    recurrence: recurrenceFromForm(values),
  };
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    toast(
      r.status === 401 || r.status === 403 ? "需要登入(或本機管理權限)才能改排程" : d.detail || "儲存失敗",
      "err",
    );
    return false;
  }
  return true;
}

export async function createScheduleFlow() {
  const values = await openFormModal({
    title: "新增排程任務",
    hint: "到點時把任務排進 agent 佇列(執行/合併/部署走既有 autopilot 流程);前一次還沒消化完會自動跳過,不堆積。",
    fields: scheduleFields(),
    submitLabel: "建立",
  });
  if (values === null) return;
  if (await submitSchedule("POST", "/api/schedules", values)) {
    toast("排程已建立");
    renderSchedules();
  }
}

export async function editScheduleFlow(s) {
  const values = await openFormModal({
    title: "編輯排程",
    fields: scheduleFields(s),
    submitLabel: "儲存",
  });
  if (values === null) return;
  if (await submitSchedule("PUT", `/api/schedules/${s.id}`, values)) {
    toast("已更新");
    renderSchedules();
  }
}

async function toggleSchedule(s) {
  const r = await fetch(`/api/schedules/${s.id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: !s.enabled }),
  });
  if (!r.ok) toast("需要登入(或本機管理權限)才能改排程", "err");
  renderSchedules();
}

async function deleteScheduleFlow(s) {
  if (!(await openConfirmModal({
    title: "刪除排程",
    message: `刪除「${s.title}」?已入列的任務不受影響。`,
    confirmLabel: "刪除",
    danger: true,
  }))) return;
  const r = await fetch(`/api/schedules/${s.id}`, { method: "DELETE" });
  if (!r.ok) toast("需要登入(或本機管理權限)才能改排程", "err");
  renderSchedules();
}

export async function renderSchedules() {
  const host = $("#homeSchedules");
  if (!host) return;
  host.innerHTML = "";
  appendTextEl(host, "h2", "home-sub-title", "排程任務");
  appendTextEl(host, "p", "muted", "定期交辦給 agent 的例行工作——到點自動排進佇列執行。");
  const addBtn = document.createElement("button");
  addBtn.id = "schedAdd";
  addBtn.className = "hero-send";
  addBtn.textContent = "新增排程";
  addBtn.onclick = createScheduleFlow;
  host.appendChild(addBtn);

  let items = [];
  try {
    const data = await (await fetch("/api/schedules")).json();
    items = data.schedules || [];
  } catch {
    appendTextEl(host, "p", "muted", "載入失敗——稍後再試。");
    return;
  }
  const list = document.createElement("div");
  list.className = "sched-list";
  host.appendChild(list);
  if (!items.length) {
    appendTextEl(list, "p", "muted", "還沒有排程——「新增排程」建立第一個(例:每天早上產出營運摘要)。");
    return;
  }
  for (const s of items) {
    const row = document.createElement("div");
    row.className = "sched-row" + (s.enabled ? "" : " off");
    const info = document.createElement("div");
    info.className = "sched-info";
    appendTextEl(info, "div", "sched-title", s.title);
    const rec = s.recurrence || {};
    const fmt = REC_LABEL[rec.kind];
    appendTextEl(info, "div", "sched-meta muted", (fmt ? fmt(rec) : "?") + (s.enabled ? "" : "・已停用"));
    row.appendChild(info);
    const onoff = document.createElement("button");
    onoff.className = "ghost";
    onoff.textContent = s.enabled ? "停用" : "啟用";
    onoff.onclick = () => toggleSchedule(s);
    row.appendChild(onoff);
    const edit = document.createElement("button");
    edit.className = "ghost";
    edit.textContent = "編輯";
    edit.onclick = () => editScheduleFlow(s);
    row.appendChild(edit);
    const del = document.createElement("button");
    del.className = "ghost icon-btn";
    del.title = "刪除";
    del.appendChild(icon("trash", "icon"));
    del.onclick = () => deleteScheduleFlow(s);
    row.appendChild(del);
    list.appendChild(row);
  }
}

export function openSchedules() {
  import("./home.js").then((m) => m.setSubview("schedules"));
  renderSchedules();
}
