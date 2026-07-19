// 專案面板（藍圖 + 改良待辦）與專案 CRUD（建立/刪除/目標 repo/中斷恢復）。
import { $, icon, toast } from "../dom.js";
import { openFormModal, openConfirmModal } from "../components/modal.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { loadProjects, onProjectChange } from "./deck.js";
import { start } from "../ws.js";

export const PRIO_LABEL = ["P0", "P1", "P2"];
export const TYPE_LABEL = { feature: "功能", bug: "缺陷", improvement: "改良" };

export async function createProjectFlow() {
  // 表單 modal 一次收齊（取代先前的連環 prompt）；目標 repo 選填、可日後再設。
  const values = await openFormModal({
    title: "新增專案",
    hint: "長期專案：程式碼與改良任務跨場次累積；選「一次性討論」則每次從零開始。",
    fields: [
      { key: "name", label: "專案名稱", required: true, placeholder: "例如：無人機地面站" },
      {
        key: "vision", label: "一句話產品願景", type: "textarea", rows: 2,
        placeholder: "（選填）會持續提醒團隊方向",
      },
      {
        key: "repo", label: "目標 repo", placeholder: "（選填）owner/repo，可日後在專案面板設定",
        hint: "工作基底＋發佈目標：下一場討論以該 repo 程式碼為基底，成果推分支開 PR；" +
          "repo 不存在且 owner 是 token 使用者時會自動建立私有 repo",
      },
    ],
    submitLabel: "建立專案",
    onValidate: (v) => (v.name ? "" : "請輸入專案名稱"),
  });
  if (!values) { $("#projectSelect").value = ""; onProjectChange(); return; }
  const { name, vision, repo } = values;
  try {
    const res = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, vision }),
    });
    const data = await res.json();
    if (!res.ok) { toast(data.error || "建立失敗", "err"); return; }
    if (repo) {
      // 同一張表單順手設定目標 repo；失敗不擋建立流程，提示可稍後再設。
      try {
        const r2 = await fetch(`/api/projects/${data.project.id}/publish-repo`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo }),
        });
        const d2 = await r2.json().catch(() => ({}));
        if (!r2.ok) toast(d2.error || "目標 repo 設定失敗（可稍後在專案面板設定）", "err");
        else if (d2.warning) toast(d2.warning, "err");
      } catch (e) { toast("目標 repo 設定失敗（可稍後在專案面板設定）", "err"); }
    }
    await loadProjects();
    $("#projectSelect").value = data.project.id;
    toast(`專案「${name}」已建立`, "ok");
    onProjectChange(); // 顯示新專案的目標 repo 狀態（未設定→提示設定）
  } catch (e) { toast("建立專案失敗", "err"); }
}

export async function openProjectPanel() {
  const pid = $("#projectSelect").value;
  if (!pid || pid === "__new__") { toast("先在上方選擇一個專案", ""); return; }
  openDrawer("#projectPanel");
  await refreshProjectPanel();
}

export function closeProjectPanel() { closeDrawer("#projectPanel"); }

function projLine(text, cls, iconName) {
  const div = document.createElement("div");
  if (cls) div.className = cls;
  if (iconName) div.appendChild(icon(iconName, "icon sys-ic"));
  const span = document.createElement("span");
  span.textContent = text;
  div.appendChild(span);
  return div;
}

export async function refreshProjectPanel() {
  const body = $("#projectBody");
  const pid = $("#projectSelect").value;
  if (!pid || pid === "__new__") return;
  body.innerHTML = "<span class='muted'>載入中…</span>";
  try {
    const d = await (await fetch(`/api/projects/${pid}`)).json();
    body.innerHTML = "";
    const p = d.project || {};
    body.appendChild(projLine(p.name || pid, "proj-name", "box"));

    // 目標 repo＝工作基底＋發佈目標：workspace 全新時下一場討論先 clone 它當基底
    // （專家在你指定的程式碼上修改），成果推分支開 PR；已同源則每場快轉到遠端 base。
    const repoRow = projLine(
      `目標 repo（工作基底＋發佈）：${p.publish_repo || "未設定（從零自建，無法開 PR）"}`,
      "muted",
    );
    const repoBtn = document.createElement("button");
    repoBtn.id = "projectPublishRepo";
    repoBtn.className = "ghost";
    repoBtn.textContent = "設定";
    repoBtn.title =
      "owner/repo；workspace 全新時下一場討論以該 repo 程式碼為工作基底；" +
      "不存在且 owner 是 token 使用者時會自動建立私有 repo，留空＝清除";
    repoBtn.onclick = () => setProjectPublishRepo(pid, p.publish_repo || "");
    repoRow.appendChild(repoBtn);
    body.appendChild(repoRow);

    // 常駐意圖(第 4 階 B3):可隨時更新的北極星指令;TI_INTENT_LOOP 開啟後,
    // 持續改良的「找問題」會先對照意圖做差距分析。
    const intentRow = projLine(`常駐意圖：${p.intent || "（未設定）"}`, "muted");
    const intentBtn = document.createElement("button");
    intentBtn.id = "projectIntent";
    intentBtn.className = "ghost";
    intentBtn.textContent = "設定";
    intentBtn.title = "一句話北極星指令；TI_INTENT_LOOP 開啟後找問題先做意圖差距分析；留空＝清除";
    intentBtn.onclick = () => setProjectIntent(pid, p.intent || "");
    intentRow.appendChild(intentBtn);
    body.appendChild(intentRow);

    // 藍圖卡片（有藍圖才顯示；raw 藍圖只提示看 BLUEPRINT.md）
    const bp = d.blueprint;
    if (bp && (bp.features || []).length) {
      if (bp.vision) body.appendChild(projLine(`願景：${bp.vision}`));
      if (bp.users) body.appendChild(projLine(`目標用戶：${bp.users}`));
      body.appendChild(projLine("核心功能：", "muted"));
      const feats = (bp.features || []).slice().sort((a, b) => (a.priority ?? 1) - (b.priority ?? 1));
      for (const f of feats) {
        const li = projLine(`${f.title}${f.detail ? " — " + f.detail : ""}`, "proj-feature");
        const tag = document.createElement("span");
        tag.className = `prio prio-${f.priority ?? 1}`;
        tag.textContent = PRIO_LABEL[f.priority ?? 1] || "P1";
        li.prepend(tag);
        body.appendChild(li);
      }
      if ((bp.milestones || []).length) {
        body.appendChild(projLine("里程碑：" + bp.milestones.map((m) => m.title).join("；"), "muted"));
      }
    } else if (bp && bp.raw) {
      body.appendChild(projLine("藍圖以原文保存（見 workspace 的 BLUEPRINT.md）", "muted"));
    } else if (p.vision) {
      body.appendChild(projLine(`願景：${p.vision}`));
      body.appendChild(projLine("（尚無結構化藍圖；開啟 TI_BLUEPRINT 後啟動持續改良即會生成）", "muted"));
    }

    // backlog（後端已按 priority→建立時間排序）
    body.appendChild(projLine("改良待辦（依消化順序）：", "muted"));
    const tasks = d.backlog || [];
    if (!tasks.length) body.appendChild(projLine("（空）", "muted"));
    for (const t of tasks) {
      const ic = { pending: "clock", in_progress: "refresh", done: "check", failed: "x" }[t.status];
      const typ = TYPE_LABEL[t.type] || TYPE_LABEL.improvement;
      const li = projLine(`#${t.id} ${t.title}　[${typ}・${t.source}]`, "proj-task", ic);
      const tag = document.createElement("span");
      tag.className = `prio prio-${t.priority ?? 1}`;
      tag.textContent = PRIO_LABEL[t.priority ?? 1] || "P1";
      li.prepend(tag);
      body.appendChild(li);
    }

    // 中斷恢復：有任務卡在「進行中」時顯示。正常運行中按下會被後端 409 擋掉（無害），
    // 真正中斷（服務重啟／行程被殺）時則重置殘留並自動重啟持續改良。
    if (tasks.some((t) => t.status === "in_progress")) {
      const btn = document.createElement("button");
      btn.id = "projectRecover";
      btn.className = "ghost";
      btn.textContent = "恢復中斷的改良";
      btn.title = "服務重啟或行程中斷後：把卡在進行中的任務重置回待辦，並重新啟動持續改良迴圈";
      btn.onclick = () => recoverProject(pid);
      body.appendChild(btn);
    }

    // 專案層級操作：進行中可一鍵停止（同 WS stop 管線，斷線後也停得掉）；刪除整個專案。
    const actions = document.createElement("div");
    actions.className = "proj-actions";
    if (d.active) {
      const stopB = document.createElement("button");
      stopB.id = "projectStop";
      stopB.className = "ghost";
      stopB.textContent = "停止執行";
      stopB.title = "對這個專案進行中的討論／持續改良迴圈送停止指令（在安全點收尾）";
      stopB.onclick = () => stopProject(pid);
      actions.appendChild(stopB);
    }
    const delB = document.createElement("button");
    delB.id = "projectDelete";
    delB.className = "ghost danger";
    delB.textContent = "刪除專案";
    delB.title = "刪除專案 meta、改良待辦、藍圖與 workspace 程式碼（歷史紀錄保留）；進行中需先停止";
    delB.onclick = () => deleteProject(pid, p.name || pid);
    actions.appendChild(delB);
    body.appendChild(actions);
  } catch (e) {
    body.innerHTML = "<span class='muted'>無法載入專案</span>";
  }
}

export async function stopProject(pid) {
  try {
    const r = await fetch(`/api/sessions/${pid}/stop`, { method: "POST" });
    if (r.ok) toast("已送出停止指令，將在安全點收尾");
    else toast("沒有進行中的討論", "err");
    setTimeout(refreshProjectPanel, 1500);
  } catch (e) { toast("停止失敗：" + e.message, "err"); }
}

export async function deleteProject(pid, name) {
  if (!(await openConfirmModal({
    title: "刪除專案",
    message:
      `刪除專案「${name}」？\n` +
      "專案 meta、改良待辦、藍圖與 workspace 程式碼會一併刪除，無法復原。\n" +
      "（歷史紀錄保留，可在歷史面板個別刪除）",
    confirmLabel: "刪除專案",
    danger: true,
  }))) return;
  try {
    const res = await fetch(`/api/projects/${pid}`, { method: "DELETE" });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { toast(d.error || "刪除失敗", "err"); return; }
    toast("專案已刪除", "ok");
    closeProjectPanel();
    $("#projectSelect").value = "";
    await loadProjects();
    onProjectChange(); // 還原啟動列（收起 repo 標籤／刪除鈕、回到一次性討論）
  } catch (e) { toast("刪除失敗：" + e.message, "err"); }
}

export async function setProjectIntent(pid, current) {
  const values = await openFormModal({
    title: "設定常駐意圖",
    hint: "一句話北極星指令（例如「把結帳流程做到可正式收費」）。TI_INTENT_LOOP 開啟後，" +
      "持續改良的「找問題」會先對照意圖做差距分析，優先補離意圖最近的缺口。留空＝清除。",
    fields: [
      { key: "intent", label: "常駐意圖", value: current, placeholder: "（留空＝清除）" },
    ],
    submitLabel: "儲存",
  });
  if (values === null) return; // 取消
  try {
    const res = await fetch(`/api/projects/${pid}/intent`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ intent: values.intent }),
    });
    const d = await res.json();
    if (!res.ok) { toast(d.detail || "設定失敗", "err"); return; }
    toast(values.intent ? "常駐意圖已更新" : "已清除常駐意圖");
    await refreshProjectPanel();
  } catch (e) { toast("設定失敗：" + e.message, "err"); }
}

export async function setProjectPublishRepo(pid, current) {
  const values = await openFormModal({
    title: "設定目標 repo",
    hint: "設定後：workspace 全新 → 下一場討論自動以該 repo 的程式碼為工作基底" +
      "（專家改你的 repo，不另起爐灶）；每場開始會同步遠端 base；成果推分支並開 PR。" +
      "repo 不存在且 owner 是你的 token 使用者時會自動建立私有 repo。",
    fields: [
      { key: "repo", label: "目標 repo", value: current, placeholder: "owner/repo（留空＝清除）" },
    ],
    submitLabel: "儲存",
  });
  if (values === null) return; // 取消
  const v = values.repo;
  try {
    const res = await fetch(`/api/projects/${pid}/publish-repo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo: v }),
    });
    const d = await res.json();
    if (!res.ok) { toast(d.error || "設定失敗", "err"); return; }
    if (d.warning) {
      toast(d.warning, "err");
    } else {
      toast(v ? `目標 repo 已設為 ${v}` : "已清除目標 repo");
    }
    await refreshProjectPanel();
    // 若該專案正選在啟動列，同步更新啟動列上的目標 repo 標籤
    if ($("#projectSelect").value === pid) {
      const repoTag = $("#projectRepo");
      repoTag.innerHTML = "";
      repoTag.appendChild(icon("target", "icon sys-ic"));
      const span = document.createElement("span");
      span.textContent = v || "目標 repo 未設定（點此設定）";
      repoTag.appendChild(span);
      repoTag.classList.toggle("unset", !v);
      repoTag.onclick = () => setProjectPublishRepo(pid, v);
    }
  } catch (e) {
    toast("設定失敗：" + e.message, "err");
  }
}

export async function recoverProject(pid) {
  try {
    const res = await fetch(`/api/projects/${pid}/recover`, { method: "POST" });
    const d = await res.json();
    if (!res.ok) { toast(d.error || "恢復失敗", "err"); return; }
    toast(d.reset ? `已重置 ${d.reset} 個中斷任務` : "沒有中斷殘留");
    await refreshProjectPanel();
    // 還有待辦且目前沒有討論在跑 → 以既有 improve 流程自動重啟（事件即時串流到本頁）。
    if (((d.counts || {}).pending || 0) > 0 && !$("#startBtn").disabled) {
      $("#projectSelect").value = pid;
      $("#improveChk").checked = true;
      closeProjectPanel();
      start();
    }
  } catch (e) {
    toast("恢復失敗：" + e.message, "err");
  }
}
