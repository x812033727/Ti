// 洞察面板(D1/D2):audit 趨勢 / 教訓庫瀏覽 / 調查結論 三分頁 drawer + 週報 modal。
// 趨勢圖沿用 ap-quota-bar 的「div bar + fill」既有手法,零外部圖表依賴。
import { $, appendTextEl, toast } from "../dom.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";

export function openInsights() {
  openDrawer("#insightsPanel");
  switchTab("trend");
  loadTrend();
}

export function closeInsights() { closeDrawer("#insightsPanel"); }

function switchTab(which) {
  $("#insTrend").classList.toggle("hidden", which !== "trend");
  $("#insLessons").classList.toggle("hidden", which !== "lessons");
  $("#insInvestigations").classList.toggle("hidden", which !== "inv");
  document.querySelectorAll(".insights-tabs button").forEach((b) => {
    const active = b.dataset.it === which;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
  if (which === "lessons") loadLessons();
  if (which === "inv") loadInvestigations();
}

async function loadTrend() {
  const box = $("#insTrendBody");
  box.innerHTML = "<p class='muted'>載入中…</p>";
  try {
    const d = await (await fetch("/api/autopilot/audit-trend?days=30")).json();
    box.innerHTML = "";
    const totals = d.totals || {};
    appendTextEl(
      box, "p", "muted",
      `近 ${d.days} 天:成功 ${totals.ok ?? 0}・失敗 ${totals.fail ?? 0}・完成率 ${totals.rate != null ? Math.round(totals.rate * 100) + "%" : "—"}`
    );
    const chart = document.createElement("div");
    chart.className = "ins-trend-chart";
    const maxN = Math.max(1, ...(d.buckets || []).map((b) => b.ok + b.fail));
    (d.buckets || []).forEach((b) => {
      const col = document.createElement("div");
      col.className = "ins-trend-col";
      const detail = Object.entries(b.outcomes || {}).map(([k, v]) => `${k}:${v}`).join(" ");
      col.title = `${b.date}  成功 ${b.ok}/失敗 ${b.fail}  完成率 ${b.rate != null ? Math.round(b.rate * 100) + "%" : "—"}\n${detail}`;
      const bar = document.createElement("div");
      bar.className = "ins-trend-bar";
      const okDiv = document.createElement("div");
      okDiv.className = "ins-trend-ok";
      okDiv.style.height = `${(b.ok / maxN) * 100}%`;
      const failDiv = document.createElement("div");
      failDiv.className = "ins-trend-fail";
      failDiv.style.height = `${(b.fail / maxN) * 100}%`;
      bar.appendChild(failDiv);
      bar.appendChild(okDiv);
      col.appendChild(bar);
      appendTextEl(col, "span", "ins-trend-date", b.date.slice(5));
      chart.appendChild(col);
    });
    box.appendChild(chart);
    if (!(d.buckets || []).length) appendTextEl(box, "p", "muted", "尚無 audit 紀錄");
  } catch (e) {
    box.textContent = "讀取失敗";
  }
}

let lessonsTimer = null;

async function loadLessons() {
  const q = ($("#insLessonsSearch")?.value || "").trim();
  const ul = $("#insLessonsList");
  try {
    const d = await (await fetch(`/api/lessons?q=${encodeURIComponent(q)}&limit=100`)).json();
    ul.innerHTML = "";
    (d.lessons || []).forEach((it) => {
      const li = document.createElement("li");
      appendTextEl(li, "div", "ins-lesson-text", it.text || "");
      appendTextEl(
        li, "div", "muted ins-lesson-meta",
        `${it.source || ""}・${it.scope || "global"}${it.requirement ? "・" + String(it.requirement).slice(0, 40) : ""}`
      );
      ul.appendChild(li);
    });
    if (!(d.lessons || []).length) ul.innerHTML = "<li class='muted'>無符合的教訓</li>";
  } catch (e) {
    ul.innerHTML = "<li class='muted'>讀取失敗</li>";
  }
}

async function loadInvestigations() {
  const ul = $("#insInvList");
  try {
    const d = await (await fetch("/api/autopilot/investigations?limit=100")).json();
    ul.innerHTML = "";
    (d.investigations || []).forEach((it) => {
      const li = document.createElement("li");
      appendTextEl(li, "div", "", `#${it.task_id} ${it.title}`);
      appendTextEl(li, "div", "ins-lesson-text", it.note || "");
      const dur = it.duration_s != null ? `・${Math.round(it.duration_s)}s` : "";
      appendTextEl(li, "div", "muted ins-lesson-meta", `${it.status}${it.outcome ? "・" + it.outcome : ""}${dur}`);
      ul.appendChild(li);
    });
    if (!(d.investigations || []).length) ul.innerHTML = "<li class='muted'>尚無調查結論</li>";
  } catch (e) {
    ul.innerHTML = "<li class='muted'>讀取失敗</li>";
  }
}

async function showDigest() {
  try {
    const d = await (await fetch("/api/autopilot/digest?days=7")).json();
    const dlg = document.createElement("dialog");
    dlg.className = "form-modal glass digest-modal";
    const pre = document.createElement("pre");
    pre.className = "digest-md";
    pre.textContent = d.markdown || "(空)";
    const actions = document.createElement("div");
    actions.className = "form-modal-actions";
    const copy = document.createElement("button");
    copy.textContent = "複製";
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(d.markdown || ""); toast("已複製"); } catch { toast("複製失敗", "err"); }
    };
    const close = document.createElement("button");
    close.className = "ghost";
    close.textContent = "關閉";
    close.onclick = () => { dlg.close(); dlg.remove(); };
    actions.appendChild(copy);
    actions.appendChild(close);
    dlg.appendChild(pre);
    dlg.appendChild(actions);
    document.body.appendChild(dlg);
    dlg.showModal();
  } catch (e) {
    toast("週報產生失敗", "err");
  }
}

export function bindInsights() {
  $("#insightsBtn").onclick = openInsights;
  $("#insightsClose").onclick = closeInsights;
  document.querySelectorAll(".insights-tabs button").forEach((b) => {
    b.onclick = () => switchTab(b.dataset.it);
  });
  const search = $("#insLessonsSearch");
  if (search) {
    search.addEventListener("input", () => {
      clearTimeout(lessonsTimer);
      lessonsTimer = setTimeout(loadLessons, 300);
    });
  }
  const digestBtn = $("#insDigestBtn");
  if (digestBtn) digestBtn.onclick = showDigest;
}
