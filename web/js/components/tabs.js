// 分頁導覽：手機底部分頁（討論／成員／看板／檔案／設定）與桌面右欄分頁（看板／檔案）。
import { $ } from "../dom.js";
import { openSettings, closeSettings } from "../panels/settings.js";

// --- 手機分頁導覽：在討論／成員／看板／檔案間切換（桌機自動隱藏分頁列）----
export function setMobileView(view) {
  document.body.dataset.mv = view;
  // 手機分頁同時決定主視圖：監控分頁＝dash，其餘分頁＝工作室（body[data-view] 單一真相）
  document.body.dataset.view = view === "dash" ? "dash" : "studio";
  document.querySelectorAll(".mobiletabs button").forEach((b) => {
    const active = b.dataset.mv === view;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
  if (view === "settings") openSettings();
  else closeSettings();
}

// tablist 的左右鍵導航：焦點移到相鄰 tab 並觸發（循環）
function bindArrowNav(container) {
  container.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const btns = [...container.querySelectorAll("button")];
    const i = btns.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    const j = (i + (e.key === "ArrowRight" ? 1 : -1) + btns.length) % btns.length;
    btns[j].focus();
    btns[j].click();
  });
}

// 分頁列與成員欄收合的接線，由入口 init 呼叫一次。
export function bindTabs() {
  document.querySelectorAll(".mobiletabs button").forEach((b) => {
    if (!b.dataset.mv) return; // 「更多」鈕另行接線，不是 data-mv 分頁
    b.onclick = () => setMobileView(b.dataset.mv);
  });

  // 手機「更多」：把左側 rail 以 bottom sheet 形式開合；點 rail 任一鈕即收合
  const more = $("#mobileMoreBtn");
  if (more) {
    more.onclick = () => {
      const open = document.body.classList.toggle("rail-open");
      more.setAttribute("aria-expanded", open ? "true" : "false");
    };
    document.querySelectorAll(".app-rail button").forEach((b) => {
      b.addEventListener("click", () => {
        document.body.classList.remove("rail-open");
        more.setAttribute("aria-expanded", "false");
      });
    });
  }

  // --- 桌面右欄分頁（看板／檔案）與左欄成員收合 ---------------------------
  document.querySelectorAll(".rail-tabs button").forEach((b) => {
    b.onclick = () => {
      const side = document.querySelector(".side");
      if (side) side.dataset.rv = b.dataset.rt;
      document.querySelectorAll(".rail-tabs button").forEach((x) => {
        x.classList.toggle("active", x === b);
        x.setAttribute("aria-selected", x === b ? "true" : "false");
      });
    };
  });
  $("#expertsToggle").onclick = () => document.body.classList.toggle("experts-collapsed");

  // 三組 tablist 皆支援左右鍵
  document.querySelectorAll(".rail-tabs, .mobiletabs, .team-tabs").forEach(bindArrowNav);
}
