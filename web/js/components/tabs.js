// 分頁導覽：手機底部分頁（討論／成員／看板／檔案／設定）與桌面右欄分頁（看板／檔案）。
import { $ } from "../dom.js";
import { openSettings, closeSettings } from "../panels/settings.js";

// --- 手機分頁導覽：在討論／成員／看板／檔案間切換（桌機自動隱藏分頁列）----
export function setMobileView(view) {
  document.body.dataset.mv = view;
  document.querySelectorAll(".mobiletabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mv === view);
  });
  if (view === "settings") openSettings();
  else closeSettings();
}

// 分頁列與成員欄收合的接線，由入口 init 呼叫一次。
export function bindTabs() {
  document.querySelectorAll(".mobiletabs button").forEach((b) => {
    b.onclick = () => setMobileView(b.dataset.mv);
  });

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
}
