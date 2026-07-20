// 洞察面板(D1/D2):audit 趨勢 / 教訓庫瀏覽 / 調查結論 三分頁 drawer + 週報 modal。
// 趨勢圖委派共用原語 components/chart.js（與儀表板同一套 .trend-*）。
import { $, appendTextEl, toast } from "../dom.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { renderStackedTrend } from "../components/chart.js";

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
    // 委派共用趨勢圖原語：合計已在上方摘要列呈現，不重複渲染；
    // tooltip 附各 outcome 分佈（僅洞察面板有此明細）。
    const chartWrap = document.createElement("div");
    renderStackedTrend(chartWrap, d.buckets || [], totals, {
      totalsLine: false,
      detail: (b) => Object.entries(b.outcomes || {}).map(([k, v]) => `${k}:${v}`).join(" "),
    });
    box.appendChild(chartWrap);
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
    let shown = d.markdown || "";
    // 歷史（F6）：autopilot 每日落盤的 digest 檔清單；選了就載入該日內容（「即時」=現算）。
    const sel = document.createElement("select");
    sel.className = "digest-history";
    try {
      const hist = await (await fetch("/api/autopilot/digests")).json();
      const items = hist.digests || [];
      const live = document.createElement("option");
      live.value = "";
      live.textContent = "即時（近 7 天）";
      sel.appendChild(live);
      items.forEach((it) => {
        const o = document.createElement("option");
        o.value = it.name;
        o.textContent = it.name.replace(/^digest-|\.md$/g, "");
        sel.appendChild(o);
      });
      sel.onchange = async () => {
        try {
          if (!sel.value) {
            const fresh = await (await fetch("/api/autopilot/digest?days=7")).json();
            shown = fresh.markdown || "";
          } else {
            const one = await (await fetch(`/api/autopilot/digests/${encodeURIComponent(sel.value)}`)).json();
            shown = one.markdown || "";
          }
          pre.textContent = shown || "(空)";
        } catch { toast("載入失敗", "err"); }
      };
      if (items.length <= 0) sel.classList.add("hidden");
    } catch { sel.classList.add("hidden"); }
    const actions = document.createElement("div");
    actions.className = "form-modal-actions";
    actions.appendChild(sel);
    const copy = document.createElement("button");
    copy.textContent = "複製";
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(shown); toast("已複製"); } catch { toast("複製失敗", "err"); }
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
