// 角色管理：列出內建＋自建角色、新增/編輯/刪除（對接 /api/roles CRUD）。
// 後端契約（studio/routes.py RoleBody）：key/name/system_prompt 必填語意、
// PUT 為整筆替換（表單以 GET 現值全量預填）、錯誤形狀 {ok:false, detail}。
import { $, toast, appendTextEl } from "../dom.js";
import { openFormModal, openConfirmModal } from "../components/modal.js";

// 角色快取：小組面板的成員選項、以及編輯時的現值預填都吃這份。
let rolesCache = [];
export function getRolesCache() { return rolesCache; }

const SOURCE_BADGE = {
  builtin: ["內建", "src-builtin"],
  override: ["覆蓋內建", "src-override"],
  file: ["自建", "src-file"],
};

// key 格式與反空殼 persona 規則（對齊 role_store.KEY_RE / _PERSONA_RE，把 422 攔在前端）
const KEY_PATTERN = /^[a-z][a-z0-9_]{1,31}$/;
const PERSONA_PATTERN = /(輸出|決議|驗證|格式|指令|決策)[:：]/;
const PERSONA_HINT = "角色專屬提示詞（不含共通守則）。至少一行需含「輸出/決議/驗證/格式/指令/決策」緊接冒號，例如「輸出格式：…」。";

export async function loadRoles() {
  const ul = $("#roleList");
  try {
    const data = await (await fetch("/api/roles")).json();
    rolesCache = data.roles || [];
  } catch (e) {
    ul.innerHTML = "<li class='muted'>無法載入角色</li>";
    return;
  }
  renderRoleList();
}

function renderRoleList() {
  const ul = $("#roleList");
  ul.innerHTML = "";
  if (!rolesCache.length) {
    ul.innerHTML = "<li class='muted'>（無角色）</li>";
    return;
  }
  for (const r of rolesCache) {
    const li = document.createElement("li");
    li.className = "team-item" + (r.in_roster ? "" : " off-roster");

    const av = appendTextEl(li, "span", "team-av", r.avatar || "🤖");
    av.setAttribute("aria-hidden", "true");

    const meta = document.createElement("div");
    meta.className = "team-meta";
    const nameRow = document.createElement("div");
    nameRow.className = "team-name-row";
    appendTextEl(nameRow, "strong", "", r.name);
    const [label, cls] = SOURCE_BADGE[r.source] || [r.source, ""];
    appendTextEl(nameRow, "span", `team-badge ${cls}`, label);
    if (!r.in_roster) appendTextEl(nameRow, "span", "team-badge off", "未在編制");
    meta.appendChild(nameRow);
    const sub = [r.key, r.title, r.model || "（預設模型）"].filter(Boolean).join("・");
    appendTextEl(meta, "div", "team-sub muted", sub);
    li.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "team-actions";
    const edit = document.createElement("button");
    edit.className = "ghost";
    edit.type = "button";
    edit.textContent = "編輯";
    edit.onclick = () => roleEditor(r);
    actions.appendChild(edit);
    if (r.source !== "builtin") {
      // file＝刪除自建；override＝刪除覆蓋檔即還原內建
      const del = document.createElement("button");
      del.className = "ghost danger";
      del.type = "button";
      del.textContent = r.source === "override" ? "還原內建" : "刪除";
      del.onclick = () => deleteRole(r);
      actions.appendChild(del);
    }
    li.appendChild(actions);
    ul.appendChild(li);
  }
}

async function roleEditor(existing) {
  const isNew = !existing;
  const r = existing || {};
  const values = await openFormModal({
    title: isNew ? "新角色" : `編輯角色：${r.name}`,
    hint: isNew
      ? "自建角色會落檔 roles/<key>.md；用內建角色的 key 則建立覆蓋檔。"
      : "編輯為整筆替換：留空的選填欄位會回到預設值。",
    fields: [
      {
        key: "key", label: "key", required: true, value: r.key || "", readOnly: !isNew,
        placeholder: "小寫英數與底線，如 designer",
        hint: isNew ? "格式：小寫字母開頭，2–32 字元的 [a-z0-9_]；建立後不可改" : "",
      },
      { key: "name", label: "名稱", required: true, value: r.name || "", placeholder: "例如：設計師" },
      { key: "avatar", label: "頭像 emoji", value: r.avatar || "🤖" },
      { key: "title", label: "職稱", value: r.title || "", placeholder: "例如：UI/UX Designer" },
      { key: "description", label: "描述", value: r.description || "", placeholder: "（選填）一句話說明這個角色" },
      {
        key: "model", label: "模型", value: r.model || "",
        placeholder: "留空＝預設（MODEL_FAST）", hint: "可填任一 provider 支援的模型名",
      },
      {
        key: "allowed_tools", label: "允許工具", value: (r.allowed_tools || ["Read", "Grep"]).join(", "),
        hint: "逗號分隔，如 Read, Grep, Write, Bash",
      },
      {
        key: "permission_mode", label: "權限模式", type: "select", value: r.permission_mode || "default",
        options: [
          { value: "default", label: "default（唯讀為主）" },
          { value: "acceptEdits", label: "acceptEdits（可寫檔）" },
        ],
      },
      { key: "tags", label: "標籤", value: (r.tags || []).join(", "), placeholder: "（選填）逗號分隔" },
      {
        key: "system_prompt", label: "角色提示詞", type: "textarea", rows: 8, required: true,
        value: r.system_prompt || "", hint: PERSONA_HINT,
      },
    ],
    submitLabel: isNew ? "建立角色" : "儲存",
    onValidate: (v) => {
      if (!KEY_PATTERN.test(v.key)) return "key 不合法：須為小寫字母開頭、2–32 字元的 [a-z0-9_]";
      if (!v.name) return "請輸入名稱";
      if (!v.system_prompt) return "請輸入角色提示詞";
      if (!PERSONA_PATTERN.test(v.system_prompt)) {
        return "角色提示詞至少一行需含「輸出/決議/驗證/格式/指令/決策」緊接冒號（反空殼 persona 規則）";
      }
      return "";
    },
  });
  if (!values) return;

  const splitList = (s) => s.split(/[,、\s]+/).map((x) => x.trim()).filter(Boolean);
  const body = {
    key: values.key,
    name: values.name,
    system_prompt: values.system_prompt,
    avatar: values.avatar || "🤖",
    title: values.title,
    model: values.model,
    allowed_tools: splitList(values.allowed_tools),
    permission_mode: values.permission_mode,
    tags: splitList(values.tags),
    description: values.description,
  };
  const url = isNew ? "/api/roles" : `/api/roles/${encodeURIComponent(values.key)}`;
  try {
    const res = await fetch(url, {
      method: isNew ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { toast(d.detail || `儲存失敗（${res.status}）`, "err"); return; }
    toast(isNew ? `角色「${values.name}」已建立` : "角色已儲存 ✓", "ok");
    await loadRoles();
  } catch (e) { toast("儲存失敗：" + e.message, "err"); }
}

async function deleteRole(r) {
  const isOverride = r.source === "override";
  const msg = isOverride
    ? `還原內建角色「${r.name}」？其覆蓋檔（roles/${r.key}.md）會被刪除。`
    : `刪除自建角色「${r.name}」？（roles/${r.key}.md 會被刪除，無法復原）`;
  if (!(await openConfirmModal({
    title: isOverride ? "還原內建角色" : "刪除角色",
    message: msg,
    confirmLabel: isOverride ? "還原內建" : "刪除",
    danger: !isOverride,
  }))) return;
  try {
    const res = await fetch(`/api/roles/${encodeURIComponent(r.key)}`, { method: "DELETE" });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { toast(d.detail || `刪除失敗（${res.status}）`, "err"); return; }
    toast(d.restored_builtin ? "已還原內建" : "角色已刪除", "ok");
    await loadRoles();
  } catch (e) { toast("刪除失敗：" + e.message, "err"); }
}

// 「＋ 新角色」的接線（由 team.js 呼叫）
export function bindRoles() {
  $("#roleNew").onclick = () => roleEditor(null);
}
