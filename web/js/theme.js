// 主題切換：三態循環「跟隨系統 → 淺色 → 深色」，localStorage("ti-theme") 持久化。
// index.html / login.html 的 <head> inline script 已在樣式載入前套用初值（防 FOUC）；
// 這裡負責執行期切換與「跟隨系統」時的系統偏好監聽。
import { $ } from "./dom.js";

const KEY = "ti-theme";
const ORDER = ["system", "light", "dark"];
const LABEL = { system: "主題：跟隨系統", light: "主題：淺色", dark: "主題：深色" };

export function storedTheme() {
  try {
    const v = localStorage.getItem(KEY);
    return ORDER.includes(v) ? v : "system";
  } catch (e) { return "system"; }
}

function systemPrefersLight() {
  try { return window.matchMedia("(prefers-color-scheme: light)").matches; }
  catch (e) { return false; }
}

export function applyTheme(mode) {
  const light = mode === "light" || (mode === "system" && systemPrefersLight());
  if (light) document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
  const btn = $("#themeBtn");
  if (btn) {
    // 圖示由 CSS 依 data-mode 切換（按鈕內含三顆 SVG，不能用 textContent 蓋掉）
    btn.dataset.mode = mode;
    btn.title = LABEL[mode];
    btn.setAttribute("aria-label", LABEL[mode]);
  }
}

export function toggleTheme() {
  const next = ORDER[(ORDER.indexOf(storedTheme()) + 1) % ORDER.length];
  try { localStorage.setItem(KEY, next); } catch (e) { /* 私密模式等：僅本次生效 */ }
  applyTheme(next);
}

export function initTheme() {
  applyTheme(storedTheme());
  // 「跟隨系統」時，系統深淺切換即時反映
  try {
    window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
      if (storedTheme() === "system") applyTheme("system");
    });
  } catch (e) { /* 舊瀏覽器無 addEventListener：略過即時跟隨 */ }
}
