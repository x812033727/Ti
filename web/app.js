// Ti Studio 前端入口（ES module）：載入各模組、集中事件綁定、初始化。
// 職責分工見 web/js/：dom.js（工具）、state.js（跨模組狀態）、events-render.js
//（事件渲染中樞 handleEvent）、ws.js（連線）、panels/*（各面板）、components/*（共用元件）。
import { $ } from "./js/dom.js";
import { initTheme, toggleTheme } from "./js/theme.js";
import { downloadWorkspace, loadPublishConfig } from "./js/events-render.js";
import { start, stop, sendInterject } from "./js/ws.js";
import { loadHealth, checkAuth } from "./js/health.js";
import { setMobileView, bindTabs } from "./js/components/tabs.js";
import { bindDrawers } from "./js/components/drawer.js";
import {
  setDeckCollapsed, loadProjects, loadWorkflows, updateStartLabel, onProjectChange,
} from "./js/panels/deck.js";
import { openHistory, closeHistory, cleanupCompleted } from "./js/panels/history.js";
import {
  openAutopilot, closeAutopilot, minimizeAutopilot, expandAutopilot,
  toggleAutopilot, addAutopilotTask,
} from "./js/panels/autopilot.js";
import { openProjectPanel, closeProjectPanel, refreshProjectPanel } from "./js/panels/project.js";
import { bindTeam } from "./js/panels/team.js";
import { loadGroupOptions } from "./js/panels/groups.js";
import { openMetrics, closeMetrics, refreshMetrics } from "./js/panels/metrics.js";
import {
  openWorkflowPanel, closeWorkflowPanel, loadWorkflowPanel, renderWorkflowSelection,
  newWorkflow, loadWorkflowTemplate, saveWorkflow, deleteWorkflow,
} from "./js/panels/workflow.js";
import {
  bindSettings, openSettings, closeSettings, saveSettings, applyRecommendedSettings,
  filterSettings, refreshProviderQuota, savePassword, redeployNow,
} from "./js/panels/settings.js";

// --- 事件綁定（集中接線；module script 於 DOM 解析完才執行，可直接查元素）----
$("#startBtn").onclick = start;
$("#stopBtn").onclick = stop;
$("#interjectBtn").onclick = sendInterject;
$("#settingsBtn").onclick = openSettings;
$("#settingsClose").onclick = closeSettings;
$("#settingsSave").onclick = saveSettings;
$("#settingsRecommend").onclick = applyRecommendedSettings;
$("#settingsSearch").addEventListener("input", filterSettings);
$("#settingsQuotaRefresh").onclick = refreshProviderQuota;
$("#pwSave").onclick = savePassword;
$("#redeployBtn").onclick = redeployNow;
$("#downloadBtn").onclick = downloadWorkspace;
$("#historyBtn").onclick = openHistory;
$("#historyClose").onclick = closeHistory;
$("#historyCleanup").onclick = cleanupCompleted;
$("#autopilotBtn").onclick = openAutopilot;
// head 內按鈕需 stopPropagation：迷你狀態下整條標題列可點擊展開
$("#autopilotClose").onclick = (e) => { e.stopPropagation(); closeAutopilot(); };
$("#autopilotMin").onclick = (e) => { e.stopPropagation(); minimizeAutopilot(); };
$("#autopilotHead").onclick = () => {
  if ($("#autopilotPanel").classList.contains("mini")) expandAutopilot();
};
$("#apToggle").onclick = toggleAutopilot;
$("#apAddBtn").onclick = addAutopilotTask;
$("#deckBar").onclick = () => setDeckCollapsed(false);
$("#deckStop").onclick = (e) => { e.stopPropagation(); stop(); };
$("#themeBtn").onclick = toggleTheme;
$("#metricsBtn").onclick = openMetrics;
$("#metricsClose").onclick = closeMetrics;
$("#metricsRefresh").onclick = refreshMetrics;
$("#workflowBtn").onclick = openWorkflowPanel;
$("#workflowClose").onclick = closeWorkflowPanel;
$("#workflowRefresh").onclick = () => loadWorkflowPanel();
$("#workflowList").addEventListener("change", renderWorkflowSelection);
$("#workflowNew").onclick = newWorkflow;
$("#workflowTemplate").onclick = loadWorkflowTemplate;
$("#workflowSave").onclick = saveWorkflow;
$("#workflowDelete").onclick = deleteWorkflow;
$("#projectBtn").onclick = openProjectPanel;
$("#projectClose").onclick = closeProjectPanel;
$("#projectRefresh").onclick = refreshProjectPanel;
$("#projectSelect").addEventListener("change", onProjectChange);
$("#improveChk").addEventListener("change", () => updateStartLabel());
$("#requirement").addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });
$("#interjectInput").addEventListener("keydown", (e) => { if (e.key === "Enter") sendInterject(); });

bindSettings();
bindTabs();
bindDrawers();
bindTeam();
initTheme();
setMobileView("discussion");

async function init() {
  if (!(await checkAuth())) return;
  loadPublishConfig();
  loadHealth();
  loadProjects();
  loadWorkflows();
  loadGroupOptions();
}

init();
