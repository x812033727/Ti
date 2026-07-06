// Autopilot activity 面板：用 node 載入真實 web/js/panels/autopilot.js，
// 驗證 ttft_s 有值時會顯示 chip、缺值時不會炸，且 heartbeat 會置頂顯示目前 turn。

class RecEl {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.className = "";
    this.textContent = "";
    this._inner = "";
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  appendChild(c) {
    this.children.push(c);
    return c;
  }
  set innerHTML(v) {
    this._inner = v;
    if (v === "") this.children = [];
  }
  get innerHTML() {
    return this._inner || "";
  }
  get childNodes() {
    return this.children;
  }
  querySelector() {
    return new RecEl("div");
  }
  querySelectorAll() {
    return [];
  }
  addEventListener() {}
  setAttribute() {}
  remove() {}
  focus() {}
}

const els = new Map();
function $(sel) {
  if (!els.has(sel)) els.set(sel, new RecEl("stub"));
  return els.get(sel);
}

const activity = new RecEl("ul");
els.set("#apActivity", activity);
els.set("#toast", new RecEl("div"));

const noop = () => {};
Object.assign(globalThis, {
  document: {
    querySelector: (s) => $(s),
    querySelectorAll: () => [],
    createElement: (t) => new RecEl(t),
    createTextNode: (t) => {
      const el = new RecEl("text");
      el.textContent = t;
      return el;
    },
    body: new RecEl("body"),
    activeElement: null,
    addEventListener: noop,
  },
  window: {
    addEventListener: noop,
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    location: { protocol: "http:", host: "localhost", href: "" },
  },
  location: { protocol: "http:", host: "localhost", href: "" },
  WebSocket: function () {
    return new RecEl("ws");
  },
  fetch: (url) =>
    Promise.resolve({
      ok: true,
      json: () =>
        Promise.resolve(
          String(url).includes("/api/autopilot/activity")
            ? {
                total: 2,
                tasks: [
                  {
                    id: 1,
                    title: "有 ttft_s",
                    status: "done",
                    updated_at: 1,
                    token_usage: {
                      by_provider: { claude: { total: 12 } },
                      by_model: { "claude-opus-4-8": { total: 12 } },
                      ttft_s: 0.123,
                    },
                  },
                  {
                    id: 2,
                    title: "舊 JSONL",
                    status: "done",
                    updated_at: 2,
                    token_usage: {
                      by_provider: { claude: { total: 5 } },
                      by_model: { "claude-sonnet": { total: 5 } },
                    },
                  },
                ],
              }
            : {},
        ),
    }),
  setTimeout: noop,
  clearTimeout: noop,
  setInterval: noop,
  clearInterval: noop,
});

const mod = await import("../web/js/panels/autopilot.js");
Date.now = () => 3_785_000;
await mod.refreshApActivity({ current_expert: "engineer", turn_started_at: 3_600 });

function collectByClass(root, cls, out = []) {
  if ((root.className || "").split(/\s+/).includes(cls)) out.push(root);
  for (const child of root.children || []) collectByClass(child, cls, out);
  return out;
}

const ttftChips = collectByClass(activity, "ttft");
if (ttftChips.length !== 1) {
  console.error(`FAIL: 只應渲染一個 TTFT chip，實際 ${ttftChips.length} 個`);
  process.exit(1);
}
if (ttftChips[0].textContent !== "TTFT 0.123s") {
  console.error(`FAIL: TTFT chip 文字不對：${ttftChips[0].textContent}`);
  process.exit(1);
}

const rows = activity.children || [];
if (rows.length !== 3) {
  console.error(`FAIL: 應渲染目前 turn + 2 筆 activity，實際 ${rows.length}`);
  process.exit(1);
}

const currentTurns = collectByClass(activity, "ap-current-turn");
if (currentTurns.length !== 1) {
  console.error(`FAIL: 應渲染一筆目前 turn，實際 ${currentTurns.length} 筆`);
  process.exit(1);
}

const titles = collectByClass(currentTurns[0], "ap-item-title").map((el) => el.textContent);
if (!titles.includes("目前輪到 engineer")) {
  console.error(`FAIL: 目前 turn 標題不對：${titles.join(" / ")}`);
  process.exit(1);
}

const times = collectByClass(currentTurns[0], "ap-item-time").map((el) => el.textContent);
if (!times.includes("已跑 3分05秒")) {
  console.error(`FAIL: 目前 turn 已跑時間不對：${times.join(" / ")}`);
  process.exit(1);
}

await mod.refreshApActivity({ current_expert: "senior", turn_started_at: null });
if (collectByClass(activity, "ap-current-turn").length !== 0) {
  console.error("FAIL: 缺 turn_started_at 的舊 heartbeat 不應渲染目前 turn");
  process.exit(1);
}

console.log("OK: autopilot activity 面板可安全讀取 ttft_s 並顯示目前 turn");
process.exit(0);
