// 通用表單 modal：取代原生 prompt()/裸 JSON 輸入的結構化表單。
// 用原生 <dialog>（免費取得置頂層、Esc 取消、焦點圈禁與 ::backdrop）。
//
// openFormModal({ title, hint, fields, submitLabel, onValidate }) → Promise<值物件|null（取消）>
// fields 每項：{ key, label, type, value, placeholder, required, pattern, hint,
//               options: [{value,label,hint?}]（select/radio/checkboxes 用）, rows（textarea 用） }
// type ∈ "text" | "textarea" | "select" | "radio" | "checkboxes"；未給＝text。
// onValidate(values) 回傳錯誤字串＝擋下送出並顯示；回傳空值＝放行。
import { $, icon } from "../dom.js";

export function openFormModal({ title, hint, fields, submitLabel = "確定", onValidate }) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "form-modal glass";
    dlg.setAttribute("aria-label", title || "表單");

    const form = document.createElement("form");
    form.noValidate = false;

    // 標題列
    const head = document.createElement("div");
    head.className = "form-modal-head";
    const h = document.createElement("h2");
    h.textContent = title || "";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "ghost";
    closeBtn.appendChild(icon("x"));
    closeBtn.setAttribute("aria-label", "取消並關閉");
    head.appendChild(h); head.appendChild(closeBtn);
    form.appendChild(head);

    if (hint) {
      const p = document.createElement("p");
      p.className = "muted form-modal-hint";
      p.textContent = hint;
      form.appendChild(p);
    }

    // 欄位
    const getters = new Map(); // key → () => 現值
    const body = document.createElement("div");
    body.className = "form-modal-body";
    for (const f of fields || []) {
      body.appendChild(buildField(f, getters));
    }
    form.appendChild(body);

    // 錯誤列 + 動作列
    const err = document.createElement("div");
    err.className = "form-modal-error";
    err.setAttribute("role", "alert");
    form.appendChild(err);

    const foot = document.createElement("div");
    foot.className = "form-modal-foot";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "ghost";
    cancel.textContent = "取消";
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "form-modal-submit";
    submit.textContent = submitLabel;
    foot.appendChild(cancel); foot.appendChild(submit);
    form.appendChild(foot);
    dlg.appendChild(form);

    const finish = (val) => {
      if (dlg.close) dlg.close();
      dlg.remove();
      resolve(val);
    };
    closeBtn.onclick = () => finish(null);
    cancel.onclick = () => finish(null);
    // Esc（dialog 原生 cancel 事件）與點 backdrop 皆視為取消
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); finish(null); });
    dlg.addEventListener("click", (e) => { if (e.target === dlg) finish(null); });
    form.onsubmit = (e) => {
      e.preventDefault();
      const values = {};
      for (const [key, get] of getters) values[key] = get();
      const problem = onValidate && onValidate(values);
      if (problem) { err.textContent = problem; return; }
      finish(values);
    };

    document.body.appendChild(dlg);
    if (dlg.showModal) dlg.showModal();
    const first = form.querySelector("input, textarea, select");
    if (first && first.focus) first.focus();
  });
}

// 確認對話框（取代原生 confirm）：Esc/backdrop/取消 → false，確認 → true。
// message 支援多行（\n 保留換行）；danger 時確認鈕紅色。
export function openConfirmModal({ title, message, confirmLabel = "確定", danger = false }) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "form-modal confirm-modal glass";
    dlg.setAttribute("role", "alertdialog");
    dlg.setAttribute("aria-label", title || "確認");

    const head = document.createElement("div");
    head.className = "form-modal-head";
    const h = document.createElement("h2");
    h.textContent = title || "確認";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "ghost";
    closeBtn.appendChild(icon("x"));
    closeBtn.setAttribute("aria-label", "取消並關閉");
    head.appendChild(h); head.appendChild(closeBtn);
    dlg.appendChild(head);

    const body = document.createElement("p");
    body.className = "confirm-modal-msg";
    body.textContent = message || "";
    dlg.appendChild(body);

    const foot = document.createElement("div");
    foot.className = "form-modal-foot";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "ghost";
    cancel.textContent = "取消";
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "form-modal-submit" + (danger ? " danger" : "");
    ok.textContent = confirmLabel;
    foot.appendChild(cancel); foot.appendChild(ok);
    dlg.appendChild(foot);

    const finish = (val) => {
      if (dlg.close) dlg.close();
      dlg.remove();
      resolve(val);
    };
    closeBtn.onclick = () => finish(false);
    cancel.onclick = () => finish(false);
    ok.onclick = () => finish(true);
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); finish(false); });
    dlg.addEventListener("click", (e) => { if (e.target === dlg) finish(false); });

    document.body.appendChild(dlg);
    if (dlg.showModal) dlg.showModal();
    // 預設焦點在「取消」：破壞性操作不該一個 Enter 就過
    if (cancel.focus) cancel.focus();
  });
}

function buildField(f, getters) {
  const row = document.createElement("label");
  row.className = "form-modal-row";
  const cap = document.createElement("span");
  cap.className = "form-modal-label";
  cap.textContent = f.label + (f.required ? " *" : "");
  row.appendChild(cap);

  if (f.type === "textarea") {
    const ta = document.createElement("textarea");
    ta.rows = f.rows || 5;
    ta.value = f.value || "";
    ta.placeholder = f.placeholder || "";
    ta.required = !!f.required;
    ta.dataset.key = f.key;
    row.appendChild(ta);
    getters.set(f.key, () => ta.value.trim());
  } else if (f.type === "select") {
    const sel = document.createElement("select");
    sel.dataset.key = f.key;
    for (const o of f.options || []) {
      const opt = document.createElement("option");
      opt.value = o.value;
      opt.textContent = o.label ?? o.value;
      if (o.value === f.value) opt.selected = true;
      sel.appendChild(opt);
    }
    row.appendChild(sel);
    getters.set(f.key, () => sel.value);
  } else if (f.type === "radio" || f.type === "checkboxes") {
    // radio＝單選一組；checkboxes＝多選（回傳勾選值陣列）
    const group = document.createElement("div");
    group.className = "form-modal-choices";
    group.setAttribute("role", f.type === "radio" ? "radiogroup" : "group");
    const name = `fm-${f.key}`;
    const boxes = [];
    for (const o of f.options || []) {
      const item = document.createElement("label");
      item.className = "form-modal-choice";
      const input = document.createElement("input");
      input.type = f.type === "radio" ? "radio" : "checkbox";
      input.name = name;
      input.value = o.value;
      if (f.type === "radio") input.checked = o.value === f.value;
      else input.checked = Array.isArray(f.value) && f.value.includes(o.value);
      const text = document.createElement("span");
      text.textContent = o.label ?? o.value;
      item.appendChild(input); item.appendChild(text);
      if (o.hint) {
        const small = document.createElement("small");
        small.className = "muted";
        small.textContent = o.hint;
        item.appendChild(small);
      }
      group.appendChild(item);
      boxes.push(input);
    }
    row.appendChild(group);
    getters.set(f.key, f.type === "radio"
      ? () => (boxes.find((b) => b.checked) || {}).value ?? ""
      : () => boxes.filter((b) => b.checked).map((b) => b.value));
  } else {
    const input = document.createElement("input");
    input.type = "text";
    input.value = f.value || "";
    input.placeholder = f.placeholder || "";
    input.required = !!f.required;
    if (f.pattern) input.pattern = f.pattern;
    if (f.readOnly) input.readOnly = true;
    if (f.list) input.setAttribute("list", f.list);
    input.dataset.key = f.key;
    row.appendChild(input);
    getters.set(f.key, () => input.value.trim());
  }

  if (f.hint) {
    const small = document.createElement("small");
    small.className = "muted form-modal-field-hint";
    small.textContent = f.hint;
    row.appendChild(small);
  }
  return row;
}
