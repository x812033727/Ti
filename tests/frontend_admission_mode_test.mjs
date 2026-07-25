// Task-admission mode handshake：測試 Autopilot UI 的純 view model。
// 真實模組仍會載入 DOM 相依，因此沿用共用前端環境後再 dynamic import。
import { install, expect } from "./_frontend_env.mjs";

install(() => Promise.resolve({
  ok: true,
  json: () => Promise.resolve({}),
}));

const { admissionModeView } = await import("../web/js/panels/autopilot.js");
expect(typeof admissionModeView === "function", "autopilot 應 export admissionModeView()");

function expectView(view, expected, label) {
  expect(view.effective === expected.effective,
    `${label}: effective 應為 ${expected.effective}，實為 ${view.effective}`);
  expect(view.desired === expected.desired,
    `${label}: desired 應為 ${expected.desired}，實為 ${view.desired}`);
  expect(view.pending === expected.pending,
    `${label}: pending 應為 ${expected.pending}，實為 ${view.pending}`);
  expect(view.healthy === expected.healthy,
    `${label}: healthy 應為 ${expected.healthy}，實為 ${view.healthy}`);
  expect(typeof view.text === "string" && view.text.length > 0,
    `${label}: text 應為非空字串`);
}

// desired/effective 已在同一 generation：穩態只呈現目前生效模式。
let view = admissionModeView({
  desired: "enforce",
  effective: "enforce",
  generation: 12,
  effective_generation: 12,
  pending: false,
  healthy: true,
  error: "",
});
expectView(view, {
  effective: "enforce",
  desired: "enforce",
  pending: false,
  healthy: true,
}, "steady");
expect(view.text.includes("enforce") && view.text.includes("12"),
  `steady: text 應含生效模式與 generation，實為「${view.text}」`);

// request 已寫入、worker 尚未在任務邊界 ack：不得把 desired 當 effective。
view = admissionModeView({
  desired: "enforce",
  effective: "shadow",
  generation: 8,
  effective_generation: 7,
  pending: true,
  healthy: true,
  error: "",
});
expectView(view, {
  effective: "shadow",
  desired: "enforce",
  pending: true,
  healthy: true,
}, "pending");
expect(
  view.text.includes("shadow")
    && view.text.includes("enforce")
    && view.text.includes("7")
    && view.text.includes("8"),
  `pending: text 應同時呈現 effective/desired 與兩個 generation，實為「${view.text}」`,
);

// 後端明確回報不健康時，view 必須保留模式狀態並把 fault 顯示給 operator。
view = admissionModeView({
  desired: "off",
  effective: "enforce",
  generation: 10,
  effective_generation: 9,
  pending: true,
  healthy: false,
  error: "mode state corrupt",
});
expectView(view, {
  effective: "enforce",
  desired: "off",
  pending: true,
  healthy: false,
}, "fault");
expect(
  view.text.includes("mode state corrupt") || /異常|故障|fault|unhealthy/i.test(view.text),
  `fault: text 應明確呈現不健康狀態，實為「${view.text}」`,
);

// 舊後端沒有 handshake state 時採 compatibility view；安全預設必須是 shadow。
view = admissionModeView(undefined);
expectView(view, {
  effective: "shadow",
  desired: "shadow",
  pending: false,
  healthy: true,
}, "legacy default");
expect(view.text.includes("shadow") && !view.text.includes("enforce"),
  `legacy default: text 應呈現 shadow 而非 enforce，實為「${view.text}」`);

// 未知 mode 不能沿用舊的 enforce 預設；降級成可觀測、不攔截的 shadow。
view = admissionModeView({
  desired: "future-mode",
  effective: "future-mode",
  generation: 99,
  effective_generation: 99,
  pending: false,
  healthy: true,
});
expectView(view, {
  effective: "shadow",
  desired: "shadow",
  pending: false,
  healthy: true,
}, "unknown mode");
expect(view.text.includes("shadow") && !view.text.includes("enforce"),
  `unknown mode: text 應呈現 shadow 而非 enforce，實為「${view.text}」`);

console.log("OK: admissionModeView 覆蓋 steady/pending/fault/legacy fallback");
