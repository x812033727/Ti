// 助手首頁側欄(Kimi 式,PR1 骨架版):新對話聚焦 composer、工作室入口。
// 對話歷史清單(PR5)、插件(PR9)、排程(PR11)、帳號(PR12)後續接線——
// 佔位鈕以 disabled 呈現,絕不靜默失敗。
// 慣例:import 期零 DOM 觸碰,綁定集中由 app.js 呼叫 bindSidenav()。
import { $ } from "../dom.js";
import { setView } from "./dashboard.js";

export function focusComposer() {
  const input = $("#heroInput");
  if (!input) return;
  if (typeof input.focus === "function") input.focus();
  if (typeof input.select === "function") input.select();
}

export function bindSidenav() {
  const newChat = $("#homeNewChat");
  if (newChat) newChat.onclick = focusComposer;
  const studio = $("#snStudio");
  if (studio) studio.onclick = () => setView("studio");
}
