// 浮層 drawer / 設定面板的統一開關：集中 Esc 關閉、開啟焦點移入、關閉焦點還原。
// 各面板的 open*/close* 仍保留自己的資料載入邏輯，只把顯示切換委派到這裡。
import { $ } from "../dom.js";

// 開啟中的面板堆疊（後開的先關）：{ sel, trigger }
const stack = [];

export function openDrawer(sel) {
  const el = $(sel);
  if (!el) return;
  // 單 drawer 政策：開新面板前先關其他開啟中的 drawer，終結 drawer 疊 drawer。
  // 例外：縮成右下小卡的 autopilot（.mini）與非 drawer 面板（設定 modal）不動。
  for (const d of [...stack]) {
    if (d.sel === sel) continue;
    const other = $(d.sel);
    if (!other || !other.classList.contains("drawer") || other.classList.contains("mini")) continue;
    closeDrawer(d.sel);
  }
  if (!stack.some((d) => d.sel === sel)) {
    stack.push({ sel, trigger: document.activeElement });
  }
  el.classList.remove("hidden");
  // 焦點移入面板（第一個可聚焦元素，通常是標題列按鈕），鍵盤使用者才進得來
  const first = el.querySelector("button, [href], input, select, textarea");
  if (first && first.focus) first.focus();
}

export function closeDrawer(sel) {
  const el = $(sel);
  if (!el) return;
  el.classList.add("hidden");
  const i = stack.findIndex((d) => d.sel === sel);
  if (i >= 0) {
    const [d] = stack.splice(i, 1);
    // 關閉後把焦點還給觸發鈕（存在且仍在文件中才還）
    if (d.trigger && d.trigger.focus) d.trigger.focus();
  }
}

// Esc 關閉最上層面板；由入口 init 呼叫一次。
export function bindDrawers() {
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !stack.length) return;
    // modal <dialog> 開啟時，Esc 屬於 dialog（原生 cancel 關 modal）——
    // keydown 仍會冒泡到 document，這裡必須讓步，否則一次 Esc 連底下面板一起關。
    if (document.querySelector("dialog[open]")) return;
    closeDrawer(stack[stack.length - 1].sel);
  });
}
