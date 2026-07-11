// 設定面板（API key / provider / 模型 / GitHub token）、provider 額度、
// 重新部署重啟與登入門禁密碼。
import { $, toast, appendTextEl } from "../dom.js";
import { loadHealth, checkAuth } from "../health.js";
import { setMobileView } from "../components/tabs.js";
import { openDrawer, closeDrawer } from "../components/drawer.js";
import { openConfirmModal } from "../components/modal.js";

// --- 重新部署重啟（設定面板常駐入口）---------------------------------
// 拉取主 repo 最新 main 並自我重啟，讓合併後的新程式碼生效。後端：POST /api/redeploy。
export async function redeployNow() {
  const btn = $("#redeployBtn");
  const status = $("#redeployStatus");
  if (!(await openConfirmModal({
    title: "重新部署",
    message: "確定重新部署？服務會重啟，進行中的工作與連線會中斷。",
    confirmLabel: "重新部署",
    danger: true,
  }))) return;
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "重新部署中…";
  status.className = "redeploy-status muted";
  status.textContent = "正在拉取最新 main 並重啟…";
  try {
    const r = await (await fetch("/api/redeploy", { method: "POST" })).json();
    status.className = "redeploy-status " + (r.ok ? "ok" : "fail");
    status.textContent = r.detail || "";
  } catch (e) {
    // 重啟成功時連線會中斷，請求可能無法正常回傳——這通常代表已在重啟。
    status.className = "redeploy-status muted";
    status.textContent = "已送出重新部署，服務可能正在重啟，稍後重新整理頁面。";
  }
  btn.disabled = false;
  btn.textContent = prev;
}

// 未存變更追蹤：欄位有改動且尚未儲存時，重整／關閉分頁前由瀏覽器原生對話框提醒。
// 點「✕」關面板視為主動放棄變更（不提醒），重新開啟面板會從伺服器重新載入現值。
let settingsDirty = false;

// 設定面板的事件接線（dirty 追蹤／beforeunload 提醒／點遮罩關閉），由入口 init 呼叫一次。
export function bindSettings() {
  const settingsForm = $("#settingsForm");
  settingsForm.addEventListener("input", () => { settingsDirty = true; });
  settingsForm.addEventListener("change", () => { settingsDirty = true; });
  window.addEventListener("beforeunload", (e) => {
    if (!settingsDirty) return;
    e.preventDefault();
    e.returnValue = ""; // 舊版瀏覽器需要 returnValue 才會跳提醒
  });
  // 點卡片外的暗色遮罩即關閉設定
  const settingsPanel = $("#settingsPanel");
  settingsPanel.addEventListener("click", (e) => {
    if (e.target === settingsPanel) closeSettings();
  });
}

export async function openSettings() {
  const settingsForm = $("#settingsForm");
  const settingsQuota = $("#settingsQuota");
  openDrawer("#settingsPanel");
  settingsForm.innerHTML = "<div class='muted'>載入中…</div>";
  if (settingsQuota) settingsQuota.innerHTML = "<div class='muted'>載入 provider 狀態與額度中…</div>";
  try {
    const data = await (await fetch("/api/settings")).json();
    renderSettings(data.fields || []);
    refreshProviderQuota();
  } catch (e) {
    settingsForm.innerHTML = "<div class='muted'>無法載入設定</div>";
  }
  refreshPwStatus();
}

export async function refreshPwStatus() {
  const status = $("#pwStatus");
  const curRow = $("#pwCurrentRow");
  try {
    const s = await (await fetch("/api/auth/status")).json();
    if (s.auth_enabled) {
      status.textContent = "目前已啟用門禁。變更密碼需先輸入目前密碼。";
      curRow.classList.remove("hidden");
    } else {
      status.textContent = "目前未啟用門禁。設定一組密碼即可啟用登入保護。";
      curRow.classList.add("hidden");
    }
  } catch (e) {
    status.textContent = "";
  }
}

export async function savePassword() {
  const hint = $("#pwHint");
  const cur = $("#pwCurrent").value;
  const next = $("#pwNew").value;
  const confirm = $("#pwConfirm").value;
  if (next.length < 4) { hint.textContent = "新密碼至少 4 個字元"; return; }
  if (next !== confirm) { hint.textContent = "兩次輸入的新密碼不一致"; return; }
  hint.textContent = "變更中…";
  try {
    const res = await (await fetch("/api/auth/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: cur, new_password: next }),
    })).json();
    if (res.ok) {
      $("#pwCurrent").value = ""; $("#pwNew").value = ""; $("#pwConfirm").value = "";
      hint.textContent = "已變更，新密碼即時生效。";
      toast("存取密碼已變更", "ok");
      refreshPwStatus();
      checkAuth();   // 門禁可能剛啟用，更新登出鈕
    } else {
      hint.textContent = res.detail || "變更失敗";
    }
  } catch (e) {
    hint.textContent = "變更請求失敗";
  }
}

export function closeSettings() {
  settingsDirty = false; // 關閉＝放棄未存變更，之後重整不再提醒
  closeDrawer("#settingsPanel");
  // 若是從手機底部「設定」分頁進來的，關閉後回到討論分頁，避免留下空白畫面
  if (document.body.dataset.mv === "settings") setMobileView("discussion");
}

export function groupSettings(fields) {
  const groups = [];
  const byName = new Map();
  for (const f of fields) {
    const name = f.group || "一般";
    if (!byName.has(name)) {
      const group = { name, fields: [] };
      groups.push(group);
      byName.set(name, group);
    }
    byName.get(name).fields.push(f);
  }
  return groups;
}

function groupId(name) {
  return "settings-group-" + String(name || "general").toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

// 組級提示:Autopilot 組的旋鈕由 ti-autopilot 行程消費,寫入 .env 後 web 行程 reload 但
// autopilot 行程不會——需 restart 該服務才生效(挑任務空檔;暫停/恢復與派工模式不受影響)。
const GROUP_NOTES = {
  Autopilot: "此組寫入 .env 後需重啟 ti-autopilot 服務才對 autopilot 生效(請挑任務空檔);網頁互動討論不受影響。",
};

export function createSettingInput(f, row) {
  let input;
  if (f.kind === "select") {
    input = document.createElement("select");
    for (const opt of f.options) {
      const o = document.createElement("option");
      o.value = opt;
      o.textContent = opt + (f.recommended && opt === f.recommended ? "（推薦）" : "");
      if (opt === f.value) o.selected = true;
      input.appendChild(o);
    }
    // 現值不在清單內（如 .env 手動填過清單外的模型）：追加為選項並選取，
    // 避免開面板再存檔時被靜默改成清單第一項。
    if (f.value && !f.options.includes(f.value)) {
      const o = document.createElement("option");
      o.value = f.value; o.textContent = f.value;
      o.selected = true;
      input.appendChild(o);
    }
  } else if (f.kind === "combo") {
    // 可選可打：下拉建議來自 datalist，仍接受任意輸入（如本地模型名稱）。
    input = document.createElement("input");
    input.type = "text";
    input.value = f.value || "";
    input.placeholder = f.placeholder || "";
    const dl = document.createElement("datalist");
    dl.id = "dl-" + f.env;
    for (const opt of f.options) {
      const o = document.createElement("option");
      o.value = opt;
      dl.appendChild(o);
    }
    input.setAttribute("list", dl.id);
    row.appendChild(dl);
  } else if (f.kind === "textarea") {
    input = document.createElement("textarea");
    input.rows = 3;
    input.value = f.value || "";
    input.placeholder = f.placeholder || "";
  } else {
    input = document.createElement("input");
    input.type = f.kind === "password" ? "password" : "text";
    input.value = f.secret ? "" : (f.value || "");
    input.placeholder = f.secret && f.set
      ? "已設定（留空＝不變更）"
      : (f.placeholder || "");
  }
  input.dataset.env = f.env;
  input.dataset.secret = f.secret ? "1" : "";
  input.dataset.recommended = f.recommended || "";
  return input;
}

export function renderSettings(fields) {
  settingsDirty = false; // 重新渲染後欄位即為伺服器現值，無未存變更
  const settingsForm = $("#settingsForm");
  const settingsNav = $("#settingsNav");
  const settingsSearch = $("#settingsSearch");
  settingsForm.innerHTML = "";
  if (settingsNav) settingsNav.innerHTML = "";
  if (settingsSearch) settingsSearch.value = "";

  const groups = groupSettings(fields);
  for (const [idx, group] of groups.entries()) {
    const id = groupId(group.name);
    if (settingsNav) {
      const navBtn = document.createElement("button");
      navBtn.type = "button";
      navBtn.textContent = group.name;
      navBtn.dataset.target = id;
      if (idx === 0) navBtn.classList.add("active");
      navBtn.onclick = () => {
        const target = document.getElementById(id);
        if (target && target.scrollIntoView) target.scrollIntoView({ block: "start", behavior: "smooth" });
        settingsNav.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
        navBtn.classList.add("active");
      };
      settingsNav.appendChild(navBtn);
    }

    const section = document.createElement("section");
    section.className = "settings-section";
    section.id = id;
    section.dataset.group = group.name;
    const h = document.createElement("h3");
    const title = document.createElement("span");
    title.textContent = group.name;
    const count = document.createElement("small");
    count.textContent = `${group.fields.length} 欄`;
    h.appendChild(title);
    h.appendChild(count);
    section.appendChild(h);
    if (GROUP_NOTES[group.name]) {
      const note = document.createElement("p");
      note.className = "settings-group-note muted";
      note.textContent = GROUP_NOTES[group.name];
      section.appendChild(note);
    }
    const grid = document.createElement("div");
    grid.className = "settings-grid";
    for (const f of group.fields) {
      const row = document.createElement("label");
      row.className = "set-row";
      row.dataset.search = `${f.group || ""} ${f.label || ""} ${f.env || ""}`.toLowerCase();
      const cap = document.createElement("span");
      cap.className = "set-label";
      cap.textContent = f.label;
      row.appendChild(cap);
      const meta = document.createElement("span");
      meta.className = "set-env";
      meta.textContent = f.env;
      row.appendChild(meta);
      row.appendChild(createSettingInput(f, row));
      grid.appendChild(row);
    }
    section.appendChild(grid);
    settingsForm.appendChild(section);
  }
  filterSettings();
}

export function applyRecommendedSettings() {
  // 把所有帶推薦值的欄位（各角色模型）一鍵填入推薦配置；不自動儲存，按「儲存」才生效。
  let n = 0;
  $("#settingsForm").querySelectorAll("[data-env]").forEach((el) => {
    const rec = el.dataset.recommended;
    if (!rec) return;
    if (el.value !== rec) { el.value = rec; n += 1; }
  });
  settingsDirty = settingsDirty || n > 0;
  $("#settingsHint").textContent = n
    ? `已填入推薦配置（${n} 個欄位），按「儲存」生效。`
    : "所有欄位已是推薦配置。";
}

export function filterSettings() {
  const settingsForm = $("#settingsForm");
  const settingsNav = $("#settingsNav");
  const settingsSearch = $("#settingsSearch");
  const q = (settingsSearch && settingsSearch.value || "").trim().toLowerCase();
  settingsForm.querySelectorAll(".set-row").forEach((row) => {
    const match = !q || (row.dataset.search || "").includes(q);
    row.classList.toggle("hidden", !match);
  });
  settingsForm.querySelectorAll(".settings-section").forEach((section) => {
    const visible = Array.from(section.querySelectorAll(".set-row")).some((row) => !row.classList.contains("hidden"));
    section.classList.toggle("hidden", !visible);
    const navBtn = settingsNav && settingsNav.querySelector(`button[data-target="${section.id}"]`);
    if (navBtn) navBtn.classList.toggle("hidden", !visible);
  });
}

function providerStatusLabel(p) {
  if (p.ready) return "可用";
  if (p.status === "warn") return "需確認";
  return "未設定";
}

function fmtResetRelative(epochSec) {
  if (!epochSec) return "—";
  const diff = epochSec * 1000 - Date.now();
  if (diff <= 0) return "已重置";
  const totalMin = Math.floor(diff / 60000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h >= 1) return m ? `${h} 小時 ${m} 分後重置` : `${h} 小時後重置`;
  return `${m} 分鐘後重置`;
}

function rateLimitRow(label, win) {
  const row = document.createElement("div");
  row.className = "quota-ratelimit";
  const raw = win && Number(win.used_percentage);
  const pct = Number.isFinite(raw) ? raw : 0;
  if (pct >= 90) row.classList.add("crit");
  else if (pct >= 75) row.classList.add("warn");
  appendTextEl(row, "span", "quota-rl-label", label);
  const bar = document.createElement("div");
  bar.className = "bar";
  const fill = document.createElement("div");
  fill.className = "fill";
  fill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  bar.appendChild(fill);
  row.appendChild(bar);
  appendTextEl(row, "span", "quota-rl-pct", `${pct}%`);
  appendTextEl(row, "em", "quota-rl-reset", fmtResetRelative(win && win.reset_at));
  return row;
}

const RL_ERRORS = {
  token_missing: "找不到訂閱憑證（需 provider CLI 登入）。",
  unauthorized: "token 已過期：跑一次該 provider 討論或重新登入後重試。",
  unreachable: "暫時無法取得官方額度（稍後重試）。",
  stale_label: "此帳號的憑證快照已過期（額度不受影響）：切換到此帳號一次即會刷新。",
};

function rateLimitBlock(rl) {
  const wrap = document.createElement("div");
  wrap.className = "quota-ratelimits";
  appendTextEl(wrap, "div", "quota-rl-kicker", "訂閱額度（官方）");
  if (rl.error) {
    appendTextEl(wrap, "div", "quota-rl-note", RL_ERRORS[rl.error] || "無法取得官方額度。");
    return wrap;
  }
  if (Array.isArray(rl.buckets)) {
    // Antigravity：有數值配額畫每模型百分比條；不限量則改顯示訂閱層級
    if (rl.buckets.length) {
      for (const b of rl.buckets) wrap.appendChild(rateLimitRow(b.label, b));
    } else if (rl.tier && rl.tier.label) {
      const plan = rl.tier.unlimited ? `${rl.tier.label} · 不限量` : rl.tier.label;
      appendTextEl(wrap, "div", "quota-rl-note", `方案：${plan}`);
      if (rl.tier.paid_tier) {
        appendTextEl(wrap, "div", "quota-rl-note", `可升級：${rl.tier.paid_tier}`);
      }
    } else {
      appendTextEl(wrap, "div", "quota-rl-note", "目前無配額資料。");
    }
  } else {
    if (rl.five_hour) wrap.appendChild(rateLimitRow("5 小時", rl.five_hour));
    if (rl.seven_day) wrap.appendChild(rateLimitRow("7 天", rl.seven_day));
    if (rl.seven_day_sonnet) wrap.appendChild(rateLimitRow("7 天 · Sonnet", rl.seven_day_sonnet));
    if (rl.seven_day_opus) wrap.appendChild(rateLimitRow("7 天 · Opus", rl.seven_day_opus));
    // 按模型 scoped 的專屬限額（如 Fable 週限）：與全域窗獨立，一併列出。
    for (const [name, mw] of Object.entries(rl.models || {})) {
      if (mw) wrap.appendChild(rateLimitRow(`模型限額 · ${name}`, mw));
    }
  }
  if (rl.fetched_at) {
    appendTextEl(
      wrap,
      "div",
      "quota-rl-stamp",
      `擷取於 ${new Date(rl.fetched_at * 1000).toLocaleTimeString()}`,
    );
  }
  return wrap;
}

function accountModeRow(accounts, rotate) {
  // 帳號分配模式列（單一控制點）：「自動輪替」＋每帳號一鈕。pin 檔存在＝手動模式
  // （釘選帳號高亮、自動鈕變成可按的恢復鈕）；否則自動模式（自動鈕高亮）。
  const manual = rotate.mode === "manual";
  const row = document.createElement("div");
  row.className = "quota-account-modes";
  const autoBtn = document.createElement("button");
  autoBtn.type = "button";
  autoBtn.className = `ghost quota-mode-btn${manual ? "" : " on"}`;
  autoBtn.textContent = "🔄 自動輪替";
  if (!rotate.enabled) {
    autoBtn.disabled = true;
    autoBtn.title = ".env 已停用自動輪替（TI_CLAUDE_ROTATE=0）";
  } else if (!manual) {
    autoBtn.disabled = true; // 已在自動模式
  } else {
    autoBtn.addEventListener("click", () => setAutoMode(autoBtn));
  }
  row.appendChild(autoBtn);
  for (const a of accounts) {
    const btn = document.createElement("button");
    btn.type = "button";
    const on = manual && a.pinned;
    btn.className = `ghost quota-mode-btn${on ? " on" : ""}`;
    if (on) {
      btn.textContent = a.active ? `📌 帳號 ${a.label} · 手動` : `⏳ 帳號 ${a.label} · 排隊中`;
      btn.disabled = true;
      if (!a.active) btn.title = "等待任務空檔由 autopilot 自動切換";
    } else {
      btn.textContent = `帳號 ${a.label}`;
      btn.addEventListener("click", () => switchClaudeAccount(a.label, btn));
    }
    row.appendChild(btn);
  }
  return row;
}

function pinnedQuotaWarning(accounts, rotate) {
  // 手動模式警語：釘選帳號任一額度窗 ≥90% → 提醒會 quota_sleep、切回自動可借用另一帳號。
  if (rotate.mode !== "manual") return null;
  const pinned = accounts.find((a) => a.pinned);
  const rl = pinned && pinned.rate_limits;
  if (!rl || rl.error) return null;
  const windows = [rl.five_hour, rl.seven_day];
  const nearCap = windows.some((w) => w && Number(w.used_percentage) >= 90);
  if (!nearCap) return null;
  const div = document.createElement("div");
  div.className = "quota-rl-note quota-pin-warn";
  div.textContent =
    "⚠ 手動模式帳號額度將滿：耗盡時 autopilot 會休眠等待重置;切回自動輪替可借用另一帳號。";
  return div;
}

function claudeAccountsBlock(accounts, rotate) {
  // 模式列（自動/各帳號）＋每個 Claude 訂閱帳號一塊：標題、在線標記、官方額度條。
  const wrap = document.createElement("div");
  wrap.className = "quota-accounts";
  appendTextEl(wrap, "div", "quota-rl-kicker", "訂閱帳號");
  if (rotate) {
    wrap.appendChild(accountModeRow(accounts, rotate));
    const warn = pinnedQuotaWarning(accounts, rotate);
    if (warn) wrap.appendChild(warn);
  }
  for (const a of accounts) {
    const box = document.createElement("div");
    box.className = `quota-account${a.active ? " active" : ""}`;
    const head = document.createElement("div");
    head.className = "quota-account-head";
    const name = a.subscription ? `帳號 ${a.label} · ${a.subscription}` : `帳號 ${a.label}`;
    appendTextEl(head, "strong", "", name);
    if (a.active) {
      appendTextEl(head, "span", "quota-account-live", "● 在線");
    }
    // 無 rotate 資料的舊後端相容：非在線帳號保留原本的切換鈕。
    if (!rotate && !a.active) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost quota-account-switch";
      btn.textContent = "切換到此帳號";
      btn.addEventListener("click", () => switchClaudeAccount(a.label, btn));
      head.appendChild(btn);
    }
    box.appendChild(head);
    if (a.rate_limits) box.appendChild(rateLimitBlock(a.rate_limits));
    wrap.appendChild(box);
  }
  return wrap;
}

export async function switchClaudeAccount(label, btn) {
  if (
    !(await openConfirmModal({
      title: "切換 Claude 帳號",
      message:
        `切換到帳號 ${label} 並進入手動模式？\n\n` +
        "手動模式會暫停自動輪替（不會被政策切回），可隨時按「自動輪替」恢復。\n" +
        "切換會重啟後端服務：本面板會短暫斷線後自動重連。\n" +
        "若有互動討論或 autopilot 任務正在進行，可選擇排隊在任務空檔自動切換。",
      confirmLabel: "切換",
    }))
  )
    return;
  const origText = btn && btn.textContent;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "切換中…";
  }
  const restore = () => {
    if (btn) {
      btn.disabled = false;
      btn.textContent = origText;
    }
  };
  try {
    const resp = await fetch("/api/claude-account/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    });
    if (resp.status === 409) {
      const d = await resp.json().catch(() => ({}));
      const reasons = (d.reasons || []).join("、") || "有討論正在進行";
      restore();
      if (d.queueable) {
        await queueClaudeAccountSwitch(label, reasons, btn);
      } else {
        toast(`無法切換：${reasons}`, "error");
      }
      return;
    }
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      toast(`切換失敗：${d.error || resp.status}`, "error");
      restore();
      return;
    }
    toast(`已切換到帳號 ${label}（手動模式），服務重啟中…`, "ok");
    const settingsQuota = $("#settingsQuota");
    if (settingsQuota) {
      settingsQuota.replaceChildren();
      appendTextEl(
        settingsQuota,
        "div",
        "muted",
        `已切換到帳號 ${label}，服務重啟中…（重連後自動刷新額度）`,
      );
    }
    waitForReconnectThenRefresh();
  } catch (e) {
    toast("切換請求已送出，服務可能正在重啟，稍候重新整理頁面。", "");
  }
}

async function queueClaudeAccountSwitch(label, reasons, btn) {
  // 409 busy 的排隊路徑：寫 pin 由 autopilot 在任務空檔代切。202 的 resp.ok 為 true，
  // 一律以 body 的 queued 分流——排隊沒有重啟，不可走 waitForReconnectThenRefresh。
  if (
    !(await openConfirmModal({
      title: "帳號使用中，改為排隊切換？",
      message:
        `現在無法立即切換：${reasons}。\n\n` +
        `排隊後 autopilot 會在任務空檔自動切換到帳號 ${label} 並重啟服務，` +
        "期間進行中的任務不受影響。",
      confirmLabel: "排隊切換",
    }))
  )
    return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "排隊中…";
  }
  try {
    const resp = await fetch("/api/claude-account/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, queue: true }),
    });
    const d = await resp.json().catch(() => ({}));
    if (d.queued) {
      toast(`已排隊：將於任務空檔自動切換到帳號 ${label}（手動模式）`, "ok");
      refreshProviderQuota(); // 立即刷新顯示「排隊中」徽章（無重啟、不等重連）
      return;
    }
    if (d.restarting) {
      // 排隊送出前恰好變閒置、直接切換成功的罕見競態：走既有重啟重連路徑。
      toast(`已切換到帳號 ${label}（手動模式），服務重啟中…`, "ok");
      waitForReconnectThenRefresh();
      return;
    }
    toast(`排隊失敗：${d.error || resp.status}`, "error");
    if (btn) {
      btn.disabled = false;
      btn.textContent = `帳號 ${label}`;
    }
  } catch (e) {
    toast("排隊請求失敗，請稍後重試。", "error");
    if (btn) {
      btn.disabled = false;
      btn.textContent = `帳號 ${label}`;
    }
  }
}

async function setAutoMode(btn) {
  if (
    !(await openConfirmModal({
      title: "恢復自動輪替",
      message:
        "切回自動模式？autopilot 會依額度政策自動分配帳號（可能切換在線帳號）。\n" +
        "若有排隊中的手動切換會一併取消。",
      confirmLabel: "恢復自動",
    }))
  )
    return;
  if (btn) btn.disabled = true;
  try {
    const resp = await fetch("/api/claude-account/pin", { method: "DELETE" });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      toast(`恢復自動失敗：${d.error || resp.status}`, "error");
      if (btn) btn.disabled = false;
      return;
    }
    toast("已切回自動模式，自動輪替恢復", "ok");
    refreshProviderQuota(); // 解除釘選不重啟，直接刷新模式列
  } catch (e) {
    toast("恢復自動請求失敗，請稍後重試。", "error");
    if (btn) btn.disabled = false;
  }
}

async function waitForReconnectThenRefresh() {
  // 服務重啟需數秒；輪詢 provider-quota 直到端點恢復再刷新面板（最多約 60 秒）。
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const resp = await fetch("/api/provider-quota", { cache: "no-store" });
      if (resp.ok) {
        renderProviderQuota(await resp.json());
        return;
      }
    } catch (e) {
      // 服務還沒起來，繼續等
    }
  }
  const settingsQuota = $("#settingsQuota");
  if (settingsQuota) {
    settingsQuota.innerHTML = "<div class='muted'>服務重啟逾時，請手動重新整理頁面。</div>";
  }
}

export function renderProviderQuota(data) {
  const settingsQuota = $("#settingsQuota");
  if (!settingsQuota) return;
  const providers = data.providers || [];
  settingsQuota.innerHTML = "";

  const head = document.createElement("div");
  head.className = "quota-head";
  const titleWrap = document.createElement("div");
  appendTextEl(titleWrap, "div", "quota-kicker", "Provider 即時剩餘額度");
  appendTextEl(titleWrap, "div", "quota-title", `目前使用：${data.active_provider || "未知 provider"}`);
  // 後端 SWR：stale=true 表示回的是舊快照、背景刷新中——標註即可，下次輪詢/手動更新自然拿到新值。
  if (data.stale) appendTextEl(titleWrap, "div", "quota-stale muted", "（額度更新中…）");
  head.appendChild(titleWrap);
  settingsQuota.appendChild(head);

  const grid = document.createElement("div");
  grid.className = "quota-grid";
  for (const p of providers) {
    const models = (p.models || []).slice(0, 4).join(" · ");
    const card = document.createElement("div");
    card.className = `quota-card ${p.status || ""}${p.active ? " active" : ""}`;
    const cardHead = document.createElement("div");
    cardHead.className = "quota-card-head";
    appendTextEl(cardHead, "strong", "", p.label || p.key || "provider");
    appendTextEl(cardHead, "span", "", providerStatusLabel(p));
    card.appendChild(cardHead);
    appendTextEl(card, "div", "quota-note", (p.quota && p.quota.summary) || "");
    if (Array.isArray(p.accounts) && p.accounts.length) {
      // 多帳號（claude）：模式列（自動/手動釘選）＋逐帳號額度與在線標記，取代單一額度區。
      card.appendChild(claudeAccountsBlock(p.accounts, p.rotate));
    } else if (p.rate_limits) {
      card.appendChild(rateLimitBlock(p.rate_limits));
    }
    if (models) appendTextEl(card, "div", "quota-models", models);
    if (p.quota && p.quota.detail) appendTextEl(card, "div", "quota-detail", p.quota.detail);
    grid.appendChild(card);
  }
  settingsQuota.appendChild(grid);
}

export async function refreshProviderQuota() {
  const settingsQuota = $("#settingsQuota");
  const settingsQuotaRefresh = $("#settingsQuotaRefresh");
  if (!settingsQuota) return;
  settingsQuota.innerHTML = "<div class='muted'>更新 provider 狀態與額度中…</div>";
  if (settingsQuotaRefresh) settingsQuotaRefresh.disabled = true;
  try {
    const data = await (await fetch("/api/provider-quota")).json();
    renderProviderQuota(data);
  } catch (e) {
    settingsQuota.innerHTML = "<div class='muted'>無法載入 provider 狀態與額度</div>";
  } finally {
    if (settingsQuotaRefresh) settingsQuotaRefresh.disabled = false;
  }
}

export async function saveSettings() {
  const payload = {};
  $("#settingsForm").querySelectorAll("[data-env]").forEach((el) => {
    const val = el.value.trim();
    // 秘密欄位留空＝不變更，不送出
    if (el.dataset.secret && val === "") return;
    payload[el.dataset.env] = val;
  });
  const hint = $("#settingsHint");
  const btn = $("#settingsSave");
  hint.textContent = "儲存中…";
  btn.disabled = true; // 防連點：請求期間禁用，避免送出多筆 POST
  try {
    const res = await (await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })).json();
    if (res.ok) {
      renderSettings(res.fields || []);
      hint.textContent = "已儲存，下次討論即生效。";
      toast("設定已儲存", "ok");
      loadHealth();
    } else {
      hint.textContent = res.detail || "儲存失敗";
    }
  } catch (e) {
    hint.textContent = "儲存請求失敗";
  } finally {
    btn.disabled = false;
  }
}
