// 插件頁(Kimi 化 PR9):角色/群組/流程/技能的卡片式總覽。
// 零複製邏輯:前三者的管理入口=開既有面板(team/workflow drawer),本頁只做總覽與導流;
// 技能=GET /api/skills 唯讀清單。import 期零 DOM;綁定由 app.js 集中。
import { $, appendTextEl, icon } from "../dom.js";
import { openTeamPanel } from "./team.js";
import { openWorkflowPanel } from "./workflow.js";

function card(iconName, title, desc) {
  const el = document.createElement("div");
  el.className = "plug-card";
  const head = document.createElement("div");
  head.className = "plug-head";
  head.appendChild(icon(iconName, "icon"));
  appendTextEl(head, "span", "plug-title", title);
  el.appendChild(head);
  appendTextEl(el, "p", "plug-desc muted", desc);
  return el;
}

function manageBtn(label, onClick) {
  const b = document.createElement("button");
  b.className = "ghost plug-manage";
  b.textContent = label;
  b.onclick = onClick;
  return b;
}

export async function renderPlugins() {
  const host = $("#homePlugins");
  if (!host) return;
  host.innerHTML = "";
  appendTextEl(host, "h2", "home-sub-title", "插件");
  appendTextEl(
    host,
    "p",
    "muted",
    "組成你工作室的零件:誰來做(角色/群組)、怎麼做(流程)、帶什麼手冊(技能)。",
  );
  const grid = document.createElement("div");
  grid.className = "plug-grid";
  host.appendChild(grid);

  // 角色/群組/流程:計數總覽+管理入口(開既有面板)
  let roles = [], groups = [], workflows = [], skills = null;
  try {
    const [r, g, w, s] = await Promise.all([
      fetch("/api/roles").then((x) => x.json()),
      fetch("/api/groups").then((x) => x.json()),
      fetch("/api/workflows").then((x) => x.json()),
      fetch("/api/skills").then((x) => x.json()),
    ]);
    roles = r.roles || [];
    groups = g.groups || [];
    workflows = w.workflows || [];
    skills = s;
  } catch {
    appendTextEl(host, "p", "muted", "載入失敗——稍後再試,或直接從左側 rail 開啟各面板。");
    return;
  }

  const cRoles = card("users", "角色", `${roles.length} 位專家:每位有自己的職責、模型與工具權限。`);
  cRoles.appendChild(manageBtn("管理角色與群組", openTeamPanel));
  grid.appendChild(cRoles);

  const cGroups = card("users", "討論群組", `${groups.length} 組:限定一場討論的參與陣容。`);
  cGroups.appendChild(manageBtn("管理角色與群組", openTeamPanel));
  grid.appendChild(cGroups);

  const cWf = card("route", "流程", `${workflows.length} 條:一場討論怎麼走(階段/把關/表決)。`);
  cWf.appendChild(manageBtn("管理流程", openWorkflowPanel));
  grid.appendChild(cWf);

  const cSk = card(
    "bulb",
    "技能",
    skills?.enabled
      ? `已啟用(適用:${(skills.roles || []).join("、")})`
      : "未啟用(TI_EXPERT_SKILLS;設定面板可開)",
  );
  const ul = document.createElement("ul");
  ul.className = "plug-skills";
  for (const s of skills?.skills || []) {
    const li = document.createElement("li");
    appendTextEl(li, "strong", "", s.name);
    appendTextEl(li, "span", "muted", " — " + (s.description || "(無描述)"));
    ul.appendChild(li);
  }
  cSk.appendChild(ul);
  grid.appendChild(cSk);
}

export function openPlugins() {
  import("./home.js").then((m) => m.setSubview("plugins"));
  renderPlugins();
}
