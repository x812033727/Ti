// 助手首頁側欄(Kimi 式):新對話、對話歷史清單(PR5)、工作室入口。
// 歷史資料=既有 /api/history;點擊:進行中→attach 直播、已結束→重播,皆灌入 home chat。
// 慣例:import 期零 DOM 觸碰,綁定集中由 app.js 呼叫 bindSidenav()。
import { $, appendTextEl } from "../dom.js";
import { setView } from "./dashboard.js";
import { toggleTheme } from "../theme.js";
import { openSettings } from "./settings.js";
import { STATUS_LABEL } from "./history.js";

export function focusComposer() {
  const input = $("#heroInput");
  if (!input) return;
  if (typeof input.focus === "function") input.focus();
  if (typeof input.select === "function") input.select();
}

// 由新到舊渲染側欄會話列(資料端已排序)。
export function renderSidenavHistory(sessions) {
  const list = $("#snHistoryList");
  if (!list) return;
  list.innerHTML = "";
  if (!sessions.length) {
    const li = document.createElement("li");
    li.className = "sn-empty";
    li.textContent = "還沒有對話——從上面開始第一個。";
    list.appendChild(li);
    return;
  }
  for (const s of sessions) {
    const li = document.createElement("li");
    li.className = "sn-item";
    li.title = `${STATUS_LABEL[s.status] || s.status}・${s.n_events || 0} 事件`;
    const dot = document.createElement("span");
    dot.className =
      "sn-dot" + (s.status === "running" ? " running" : s.status === "error" ? " error" : "");
    li.appendChild(dot);
    appendTextEl(li, "span", "sn-title", s.requirement || "(無需求)");
    li.onclick = () => {
      // 動態 import 防循環(home.js import 本模組)
      import("./home.js").then((m) =>
        s.status === "running"
          ? m.attachSessionInHome(s.session_id)
          : m.replaySessionInHome(s.session_id),
      );
    };
    list.appendChild(li);
  }
}

export async function refreshSidenavHistory() {
  try {
    const data = await (await fetch("/api/history")).json();
    renderSidenavHistory(data.sessions || []);
  } catch {
    /* 側欄清單載入失敗不打擾:hero 仍可用,下次進 home 再試 */
  }
}

export function bindSidenav() {
  const newChat = $("#homeNewChat");
  if (newChat) newChat.onclick = () => import("./home.js").then((m) => m.resetToHero());
  const studio = $("#snStudio");
  if (studio) studio.onclick = () => setView("studio");
  const plugins = $("#snPlugins");
  if (plugins) plugins.onclick = () => import("./plugins.js").then((m) => m.openPlugins());
  const sched = $("#snSchedules");
  if (sched) sched.onclick = () => import("./schedules.js").then((m) => m.openSchedules());
  const stage = $("#snStage");
  if (stage) stage.onclick = () => import("./stage.js").then((m) => m.openStage());
  // 帳號選單(PR12):向上彈出;主題/密碼走既有機制,登出同 header 登出鈕語意。
  const acc = $("#snAccount");
  const menu = $("#snAccountMenu");
  if (acc && menu) acc.onclick = () => menu.classList.toggle("hidden");
  const accTheme = $("#accTheme");
  if (accTheme) accTheme.onclick = toggleTheme;
  const accPw = $("#accPassword");
  if (accPw) accPw.onclick = () => { menu?.classList.add("hidden"); openSettings(); };
  const accOut = $("#accLogout");
  if (accOut) accOut.onclick = async () => {
    try { await fetch("/api/logout", { method: "POST" }); } catch { /* 斷線也照導 */ }
    location.href = "/login";
  };
}
