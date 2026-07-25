// 任務准入裁決的獨立呈現層。
// admission 只描述「是否適合進入哪條工作路徑」，不得取代 task.status 的生命週期語意。

const OUTCOMES = Object.freeze({
  ready: { label: "可執行", cls: "ok" },
  investigation: { label: "進入調查", cls: "run" },
  needs_clarification: { label: "需要補充", cls: "wait" },
  no_change: { label: "無需變更", cls: "ok" },
  blocked: { label: "已阻擋", cls: "bad" },
});

const SUMMARY_LIMIT = 180;

function safeText(value) {
  if (typeof value !== "string" && typeof value !== "number") return "";
  return String(value).trim().slice(0, SUMMARY_LIMIT);
}

function admissionSummary(admission) {
  const explicit = safeText(admission?.summary);
  if (explicit) return explicit;

  const reasons = Array.isArray(admission?.reasons) ? admission.reasons : [];
  for (const reason of reasons) {
    const text = safeText(reason);
    if (text) return text;
  }

  const missing = Array.isArray(admission?.missing_fields)
    ? admission.missing_fields.map(safeText).filter(Boolean)
    : [];
  return missing.length ? `缺少欄位：${missing.join("、")}`.slice(0, SUMMARY_LIMIT) : "";
}

// 純函式：將後端裁決縮成白名單呈現模型。未知值不顯示，避免臆測新語意。
export function admissionModel(admission) {
  if (admission?.released_by_mode) return null;
  const definition = OUTCOMES[admission?.outcome];
  if (!definition) return null;
  return {
    label: definition.label,
    cls: definition.cls,
    summary: admissionSummary(admission),
  };
}

export function createAdmissionPresentation(admission = null) {
  const container = document.createElement("div");
  const chip = document.createElement("span");
  chip.className = "tc-admission-chip";
  container.appendChild(chip);
  const summary = document.createElement("span");
  summary.className = "tc-admission-summary muted";
  container.appendChild(summary);
  updateAdmissionPresentation(container, admission);
  return container;
}

export function updateAdmissionPresentation(container, admission) {
  const model = admissionModel(admission);
  const chip = container.querySelector(".tc-admission-chip");
  const summary = container.querySelector(".tc-admission-summary");

  if (!model) {
    container.className = "tc-admission hidden";
    chip.textContent = "";
    chip.className = "tc-admission-chip";
    summary.textContent = "";
    summary.className = "tc-admission-summary muted hidden";
    return null;
  }

  container.className = "tc-admission";
  chip.textContent = `准入：${model.label}`;
  chip.className = `tc-admission-chip ${model.cls}`;
  summary.textContent = model.summary;
  summary.className = model.summary
    ? "tc-admission-summary muted"
    : "tc-admission-summary muted hidden";
  return model;
}
