// QA task #1: aborted expert messages must render partial_text only in details.

class ClassList {
  constructor(el) {
    this.el = el;
    this.items = new Set();
  }

  setFromString(value) {
    this.items = new Set(String(value).split(/\s+/).filter(Boolean));
  }

  add(...values) {
    for (const value of values) this.items.add(value);
    this.el._className = [...this.items].join(" ");
  }

  remove(...values) {
    for (const value of values) this.items.delete(value);
    this.el._className = [...this.items].join(" ");
  }

  toggle(value, force) {
    if (force === true) this.items.add(value);
    else if (force === false) this.items.delete(value);
    else if (this.items.has(value)) this.items.delete(value);
    else this.items.add(value);
    this.el._className = [...this.items].join(" ");
  }

  contains(value) {
    return this.items.has(value);
  }
}

class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this.parentNode = null;
    this._text = String(text);
  }

  get textContent() {
    return this._text;
  }

  set textContent(value) {
    this._text = String(value);
  }

  querySelector() {
    return null;
  }

  querySelectorAll() {
    return [];
  }
}

class Element {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this._className = "";
    this._text = "";
    this._innerHTML = "";
    this.classList = new ClassList(this);
  }

  get className() {
    return this._className;
  }

  set className(value) {
    this._className = String(value);
    this.classList.setFromString(value);
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  get innerHTML() {
    return this._innerHTML;
  }

  set innerHTML(value) {
    const html = String(value);
    this._innerHTML = html;
    this._text = "";
    this.children = [];

    if (html.includes('class="av"') && html.includes('class="body"')) {
      const avatar = /<div class="av">([\s\S]*?)<\/div>/.exec(html)?.[1] ?? "";
      const name = /<div class="who">([\s\S]*?)<\/div>/.exec(html)?.[1] ?? "";
      const av = new Element("div");
      av.className = "av";
      av.textContent = avatar;
      const body = new Element("div");
      body.className = "body";
      const who = new Element("div");
      who.className = "who";
      who.textContent = name;
      const txt = new Element("div");
      txt.className = "txt";
      body.appendChild(who);
      body.appendChild(txt);
      this.appendChild(av);
      this.appendChild(body);
      return;
    }

    if (html === "<summary>查看中止前片段</summary>") {
      const summary = new Element("summary");
      summary.textContent = "查看中止前片段";
      this.appendChild(summary);
    }
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
    this.parentNode = null;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") this.className = value;
  }

  matches(selector) {
    if (selector.startsWith(".")) return this.classList.contains(selector.slice(1));
    if (selector.startsWith("#")) return this.attributes.id === selector.slice(1);
    return this.tagName.toLowerCase() === selector.toLowerCase();
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }

  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      if (!(node instanceof Element)) return;
      if (node.matches(selector)) found.push(node);
      for (const child of node.children) walk(child);
    };
    for (const child of this.children) walk(child);
    return found;
  }
}

const stream = new Element("div");
stream.setAttribute("id", "stream");

const document = {
  querySelector(selector) {
    if (selector === "#stream") return stream;
    return new Element("div");
  },
  querySelectorAll() {
    return [];
  },
  createElement(tagName) {
    return new Element(tagName);
  },
  createElementNS(_ns, tagName) {
    return new Element(tagName);
  },
  createTextNode(text) {
    return new TextNode(text);
  },
  getElementById() {
    return new Element("div");
  },
  body: new Element("body"),
};

Object.assign(globalThis, {
  document,
  location: { protocol: "http:", host: "localhost", href: "" },
  WebSocket: function WebSocket() {},
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  window: {
    addEventListener() {},
    matchMedia() {
      return { matches: false, addEventListener() {}, removeEventListener() {} };
    },
  },
  setTimeout: () => 0,
  clearTimeout() {},
});
globalThis.WebSocket.OPEN = 1;

const { addMessage, clearStream } = await import("../web/js/events-render.js");

const partial = "任務: 半截輸出 <script>alert(1)</script> 核可";
clearStream();
addMessage({
  speaker: "qa",
  name: "QA",
  avatar: "Q",
  text: "【系統】發言逾時中止（閒置上限）。",
  aborted: true,
  abort_reason: "idle",
  partial_text: partial,
});

const assert = (condition, message) => {
  if (!condition) {
    console.error(`FAIL: ${message}`);
    process.exit(1);
  }
};

const msg = stream.children[0];
assert(msg, "message appended");
assert(msg.classList.contains("aborted"), "message has aborted class");

const txt = msg.querySelector(".txt");
assert(txt.textContent.includes("發言逾時中止"), "main text contains timeout note");
assert(!txt.textContent.includes("任務:"), "partial marker leaked into main text");
assert(!txt.textContent.includes("<script>"), "partial HTML leaked into main text");
assert(!txt.querySelector("script"), "main text created script element");

const abortNote = msg.querySelector(".abort-note");
assert(abortNote.textContent === "因無進展被中止", "idle reason rendered");

const details = msg.querySelector(".abort-partial");
const pre = msg.querySelector("pre");
assert(details, "partial details rendered");
assert(pre.textContent === partial, "partial kept as literal text in details");
assert(!details.querySelector("script"), "partial details created script element");

console.log("OK: abort render contract");
