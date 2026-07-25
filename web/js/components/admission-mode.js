// Task-admission desired/effective generation 的純 view model。
// 不讀 DOM，讓 Autopilot 看板、設定面板與 Node 守門測試共用同一 fallback。

const MODES = ["off", "shadow", "enforce"];

export function admissionModeView(state, legacyMode = "shadow") {
  const fallback = MODES.includes(legacyMode) ? legacyMode : "shadow";
  if (!state || typeof state !== "object") {
    return {
      effective: fallback,
      desired: fallback,
      pending: false,
      healthy: true,
      text: `准入 ${fallback}（相容模式）`,
    };
  }

  const modesKnown = MODES.includes(state.effective) && MODES.includes(state.desired);
  const effective = modesKnown ? state.effective : "shadow";
  const desired = modesKnown ? state.desired : "shadow";
  const generation = Number.isInteger(state.generation) ? state.generation : 0;
  const effectiveGeneration = Number.isInteger(state.effective_generation)
    ? state.effective_generation
    : generation;
  const pending = modesKnown && (
    state.pending === true || generation !== effectiveGeneration
  );
  const healthy = state.healthy !== false;

  let text;
  if (!healthy) {
    const fault = typeof state.error === "string" && state.error ? `：${state.error}` : "";
    text =
      `准入狀態異常${fault}；目前 ${effective} g${effectiveGeneration}` +
      `${pending ? `，要求 ${desired} g${generation}` : ""}（已停止取件）`;
  } else if (pending) {
    text =
      `准入 ${effective} g${effectiveGeneration} → ${desired} g${generation}` +
      "（等待任務邊界）";
  } else {
    text = `准入 ${effective} g${effectiveGeneration}`;
  }
  return { effective, desired, pending, healthy, text };
}
