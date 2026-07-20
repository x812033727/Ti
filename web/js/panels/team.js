// 團隊面板外殼：角色／小組兩個分頁的 drawer 開關與切換。
import { $ } from "../dom.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { loadRoles, bindRoles } from "./roles.js";
import { loadGroups, bindGroups } from "./groups.js";

export async function openTeamPanel() {
  openDrawer("#teamPanel");
  await refreshTeamPanel();
}

export function closeTeamPanel() { closeDrawer("#teamPanel"); }

export async function refreshTeamPanel() {
  // 角色先載（小組列表的成員名/頭像吃角色快取）
  await loadRoles();
  await loadGroups();
}

function switchTab(which) {
  $("#teamRoles").classList.toggle("hidden", which !== "roles");
  $("#teamGroups").classList.toggle("hidden", which !== "groups");
  document.querySelectorAll(".team-tabs button").forEach((b) => {
    const active = b.dataset.tt === which;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
}

export function bindTeam() {
  $("#teamBtn").onclick = openTeamPanel;
  $("#teamClose").onclick = closeTeamPanel;
  $("#teamRefresh").onclick = refreshTeamPanel;
  document.querySelectorAll(".team-tabs button").forEach((b) => {
    b.onclick = () => switchTab(b.dataset.tt);
  });
  bindRoles();
  bindGroups();
}
