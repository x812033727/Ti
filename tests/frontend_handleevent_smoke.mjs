// 前端相容性 smoke：在 stub 過的 DOM 環境載入 web/app.js，
// 驗證 handleEvent 對「未知事件」與「新事件」都不會崩潰（依賴 switch 無 default）。
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const appPath = path.join(here, "..", "web", "app.js");
const source = fs.readFileSync(appPath, "utf-8");

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

const sandbox = {
  document: doc,
  window: {},
  location: { protocol: "http:", host: "localhost", href: "" },
  WebSocket: function () { return makeStub(); },
  fetch: () => Promise.resolve({ json: () => Promise.resolve({}), ok: true }),
  setTimeout: () => 0,
  clearTimeout: () => {},
  console,
  Promise,
  JSON,
  Date,
  Object,
  Array,
  encodeURIComponent,
};
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
vm.runInContext(source, ctx, { filename: "app.js" });

const handleEvent = ctx.handleEvent;
if (typeof handleEvent !== "function") {
  console.error("handleEvent 未定義");
  process.exit(1);
}

// 1) 完全未知的事件型別 → 不應崩潰
// 2) payload 缺失 → 不應崩潰（依賴 ev.payload || {}）
// 3) 新事件 huddle / critic_review（含 limitation/passed 兩種分支）→ 不應崩潰
const cases = [
  { type: "totally_unknown_event_xyz", session_id: "t", payload: { whatever: 1 } },
  { type: "another_future_event", session_id: "t" }, // 無 payload
  { type: "huddle", session_id: "t", payload: { title: "X", participants: ["pm", "engineer"], conclusion: "試試別的做法" } },
  { type: "huddle", session_id: "t", payload: { title: "X", limitation: true } },
  { type: "critic_review", session_id: "t", payload: { gate: "pm", passed: false, text: "缺錯誤處理" } },
  { type: "critic_review", session_id: "t", payload: { gate: "senior", passed: true } },
];

try {
  for (const ev of cases) handleEvent(ev);
} catch (e) {
  console.error("handleEvent 對事件崩潰了：", e);
  process.exit(1);
}

console.log("OK: handleEvent 對未知事件與新事件皆不崩潰");
process.exit(0);
