// 討論小組管理：列表/新增/編輯/刪除（對接 /api/groups CRUD）＋啟動列小組選擇器。
// 後端契約（studio/routes.py GroupBody）：成員 ≥2、key 須存在、mode ∈
// {round_robin, parallel}、PUT 不可改名且 role_keys/mode 皆必填；錯誤形狀 {error}。
import { $, toast, appendTextEl } from "../dom.js";
import { openFormModal, openConfirmModal } from "../components/modal.js";
import { getRolesCache } from "./roles.js";

const MODE_LABEL = { round_robin: "輪流發言", parallel: "並行" };

let groupsCache = [];

export async function loadGroups() {
  const ul = $("#groupList");
  try {
    const data = await (await fetch("/api/groups")).json();
    groupsCache = data.groups || [];
  } catch (e) {
    ul.innerHTML = "<li class='muted'>無法載入小組</li>";
    return;
  }
  renderGroupList();
  loadGroupOptions();
}

function renderGroupList() {
  const ul = $("#groupList");
  ul.innerHTML = "";
  if (!groupsCache.length) {
    ul.innerHTML = "<li class='muted'>尚無小組——「＋ 新小組」建立一組，開場時即可在啟動列指定</li>";
    return;
  }
  const byKey = new Map(getRolesCache().map((r) => [r.key, r]));
  for (const g of groupsCache) {
    const li = document.createElement("li");
    li.className = "team-item";

    const avs = (g.role_keys || []).map((k) => (byKey.get(k) || {}).avatar || "❓").join(" ");
    appendTextEl(li, "span", "team-av group", avs || "👥");

    const meta = document.createElement("div");
    meta.className = "team-meta";
    const nameRow = document.createElement("div");
    nameRow.className = "team-name-row";
    appendTextEl(nameRow, "strong", "", g.name);
    appendTextEl(nameRow, "span", "team-badge", MODE_LABEL[g.mode] || g.mode);
    meta.appendChild(nameRow);
    const names = (g.role_keys || []).map((k) => (byKey.get(k) || {}).name || k).join("、");
    appendTextEl(meta, "div", "team-sub muted", `${(g.role_keys || []).length} 位成員：${names}`);
    li.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "team-actions";
    const edit = document.createElement("button");
    edit.className = "ghost";
    edit.type = "button";
    edit.textContent = "編輯";
    edit.onclick = () => groupEditor(g);
    actions.appendChild(edit);
    const del = document.createElement("button");
    del.className = "ghost danger";
    del.type = "button";
    del.textContent = "刪除";
    del.onclick = () => deleteGroup(g);
    actions.appendChild(del);
    li.appendChild(actions);
    ul.appendChild(li);
  }
}

async function groupEditor(existing) {
  const isNew = !existing;
  const g = existing || {};
  const roleOptions = getRolesCache().map((r) => ({
    value: r.key,
    label: `${r.avatar || "🤖"} ${r.name}${r.title ? `（${r.title}）` : ""}`,
  }));
  if (roleOptions.length < 2) { toast("角色不足兩位，請先到「角色」分頁建立", "err"); return; }
  const values = await openFormModal({
    title: isNew ? "新小組" : `編輯小組：${g.name}`,
    hint: "小組限定一場討論的參與角色；開場時在啟動列的「小組」下拉指定。成員至少 2 位。",
    fields: [
      {
        key: "name", label: "小組名稱", required: true, value: g.name || "", readOnly: !isNew,
        placeholder: "例如：前端小隊", hint: isNew ? "≤64 字；建立後不可改名" : "",
      },
      { key: "role_keys", label: "成員", type: "checkboxes", value: g.role_keys || [], options: roleOptions },
      {
        key: "mode", label: "討論模式", type: "radio", value: g.mode || "round_robin",
        options: [
          { value: "round_robin", label: "輪流發言" },
          { value: "parallel", label: "並行" },
        ],
      },
    ],
    submitLabel: isNew ? "建立小組" : "儲存",
    onValidate: (v) => {
      if (!v.name) return "請輸入小組名稱";
      if ((v.role_keys || []).length < 2) return "成員至少 2 位";
      return "";
    },
  });
  if (!values) return;

  const url = isNew ? "/api/groups" : `/api/groups/${encodeURIComponent(g.name)}`;
  const body = isNew
    ? { name: values.name, role_keys: values.role_keys, mode: values.mode }
    : { role_keys: values.role_keys, mode: values.mode };
  try {
    const res = await fetch(url, {
      method: isNew ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { toast(d.error || `儲存失敗（${res.status}）`, "err"); return; }
    toast(isNew ? `小組「${values.name}」已建立` : "小組已儲存 ✓", "ok");
    await loadGroups();
  } catch (e) { toast("儲存失敗：" + e.message, "err"); }
}

async function deleteGroup(g) {
  if (!(await openConfirmModal({
    title: "刪除小組",
    message: `刪除小組「${g.name}」？（角色本身不受影響）`,
    confirmLabel: "刪除",
    danger: true,
  }))) return;
  try {
    const res = await fetch(`/api/groups/${encodeURIComponent(g.name)}`, { method: "DELETE" });
    if (!res.ok) { toast(`刪除失敗（${res.status}）`, "err"); return; }
    toast("小組已刪除", "ok");
    await loadGroups();
  } catch (e) { toast("刪除失敗：" + e.message, "err"); }
}

// 啟動列「小組」下拉：從 /api/groups 現況填入（保留目前選取）。
export async function loadGroupOptions() {
  const sel = $("#groupSelect");
  if (!sel) return;
  try {
    if (!groupsCache.length) {
      const data = await (await fetch("/api/groups")).json();
      groupsCache = data.groups || [];
    }
    const cur = sel.value;
    sel.innerHTML = '<option value="">（預設編制）</option>';
    for (const g of groupsCache) {
      const opt = document.createElement("option");
      opt.value = g.name;
      opt.textContent = `${g.name}（${(g.role_keys || []).length} 人）`;
      sel.appendChild(opt);
    }
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  } catch (e) { /* 忽略：無小組時維持預設編制 */ }
}

// 「＋ 新小組」的接線（由 team.js 呼叫）
export function bindGroups() {
  $("#groupNew").onclick = () => groupEditor(null);
}
