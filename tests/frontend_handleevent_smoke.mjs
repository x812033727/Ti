// 前端相容性 smoke：先掛全域 DOM stub，再 import 真實 web/js/events-render.js，
// 驗證 handleEvent 對「未知事件」與「新事件」都不會崩潰（依賴 switch 無 default）。

// 一個「對任何屬性存取/呼叫都安全」的萬用 stub（支援鏈式：el.classList.toggle(...) 等）。
const makeStub = () =>
  new Proxy(function () {}, {
    get: (_t, prop) => {
      if (prop === Symbol.toPrimitive) return () => "";
      return makeStub();
    },
    set: () => true,
    apply: () => makeStub(),
    construct: () => makeStub(),
  });

const doc = {
  querySelector: () => makeStub(),
  querySelectorAll: () => [],
  createElement: () => makeStub(),
  body: makeStub(),
};

Object.assign(globalThis, {
  document: doc,
  window: { addEventListener: () => {}, matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) },
  location: { protocol: "http:", host: "localhost", href: "" },
  WebSocket: function () { return makeStub(); },
  fetch: () => Promise.resolve({ json: () => Promise.resolve({}), ok: true }),
  setTimeout: () => 0,
  clearTimeout: () => {},
});
const mod = await import("../web/js/events-render.js");

const handleEvent = mod.handleEvent;
if (typeof handleEvent !== "function") {
  console.error("handleEvent 未定義");
  process.exit(1);
}

// 1) 完全未知的事件型別 → 不應崩潰
// 2) payload 缺失 → 不應崩潰（依賴 ev.payload || {}）
// 3) 新事件 huddle / critic_review（含 limitation/passed 兩種分支）→ 不應崩潰
// 4) 額度感知派工 dispatch_decision（完整 payload／缺 model／無 payload）→ 不應崩潰
// 5) 3-AI 表決 vote_result（完整／平手／降級＋棄權／無 payload）→ 不應崩潰
// 6) 考核 appraisal（完整 payload／缺 model 以 role 指認／無 payload）→ 不應崩潰
const cases = [
  { type: "totally_unknown_event_xyz", session_id: "t", payload: { whatever: 1 } },
  { type: "another_future_event", session_id: "t" }, // 無 payload
  { type: "huddle", session_id: "t", payload: { title: "X", participants: ["pm", "engineer"], conclusion: "試試別的做法" } },
  { type: "huddle", session_id: "t", payload: { title: "X", limitation: true } },
  { type: "critic_review", session_id: "t", payload: { gate: "pm", passed: false, text: "缺錯誤處理" } },
  { type: "critic_review", session_id: "t", payload: { gate: "senior", passed: true } },
  { type: "dispatch_decision", session_id: "t", payload: { task_id: 2, title: "登入頁", role: "engineer", provider: "codex", model: "gpt-5.5", reason: "codex 用量最低" } },
  { type: "dispatch_decision", session_id: "t", payload: { task_id: 3, role: "engineer", provider: "claude", model: "" } },
  { type: "dispatch_decision", session_id: "t" }, // 無 payload
  { type: "vote_result", session_id: "t", payload: { topic: "儲存層用 SQLite 還是 JSON 檔", options: ["SQLite", "JSON 檔"], ballots: [{ voter: "pm", provider: "claude", choice: "SQLite" }, { voter: "voter_codex", provider: "codex", choice: "SQLite" }, { voter: "voter_minimax", provider: "minimax", choice: "JSON 檔" }], winner: "SQLite", tie: false, degraded: false } },
  { type: "vote_result", session_id: "t", payload: { topic: "平手案", options: ["A", "B"], ballots: [{ voter: "pm", provider: "claude", choice: "A" }, { voter: "voter_codex", provider: "codex", choice: "B" }], winner: "A", tie: true, degraded: false } },
  { type: "vote_result", session_id: "t", payload: { topic: "降級案", options: ["A", "B"], ballots: [{ voter: "pm", provider: "claude", choice: "" }], winner: "A", tie: false, degraded: true } },
  { type: "vote_result", session_id: "t" }, // 無 payload
  { type: "appraisal", session_id: "t", payload: { provider: "claude", model: "claude-opus-4-8", role: "engineer", score: 4, comment: "穩定高質量" } },
  { type: "appraisal", session_id: "t", payload: { provider: "", model: "", role: "qa", score: 5, comment: "" } },
  { type: "appraisal", session_id: "t" }, // 無 payload
  { type: "task_result", session_id: "t", payload: { task_id: 1, role: "engineer", provider: "claude", model: "claude-3-5-sonnet", duration_s: 15.2, qa_rounds: 1, input_tokens: 1200, output_tokens: 300, total_tokens: 1500, cost_usd: 0.0054, cost_source: "reported" } },
  { type: "task_result", session_id: "t" }, // 無 payload
  { type: "token_usage", session_id: "t", payload: { speaker: "engineer", provider: "claude", model: "claude-3-5-sonnet", prompt_tokens: 800, completion_tokens: 200, total_tokens: 1000, cost_usd: 0.0036, cache_read: 0, cache_write: 0, task_id: 1 } },
  { type: "token_usage", session_id: "t", payload: { speaker: "pm", provider: "codex", model: "gpt-5.5", prompt_tokens: 500, completion_tokens: 100, total_tokens: 600, cost_usd: null, cache_read: 0, cache_write: 0 } }, // 向後相容：無 task_id
  { type: "token_usage", session_id: "t" }, // 無 payload
];

try {
  for (const ev of cases) handleEvent(ev);
} catch (e) {
  console.error("handleEvent 對事件崩潰了：", e);
  process.exit(1);
}

console.log("OK: handleEvent 對未知事件與新事件皆不崩潰");
process.exit(0);
