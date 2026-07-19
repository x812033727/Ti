// Sanitizing Markdown 渲染器(Kimi 化 PR2):模型輸出 → 安全 DOM。
//
// 安全模型(鐵律):
// - 絕不使用 innerHTML 承載任何原文——所有節點以 createElement 建立、文字一律 textContent。
//   原文中的 HTML(<script>、<img onerror> 等)因此永遠是「字面文字」,不可能成為節點。
// - 連結僅接受 http(s) 協定;其他協定(javascript:/data:…)整段退回純文字。
//   通過的 <a> 一律 rel="noopener noreferrer" + target="_blank"。
// - 支援子集:段落/換行、# 標題(收斂為 h3/h4)、- * 清單、1. 有序清單、> 引用、
//   ``` 圍欄碼塊、行內 `code`、**粗體**、*斜體*、[文字](http(s)://…)。
//   不支援 img/HTML passthrough/表格——支援面=攻擊面,刻意最小。
// - 純函式模組:import 期零 DOM 觸碰(renderMarkdownInto 執行時才建節點)。
//
// API:renderMarkdownInto(container, text) —— 解析 text 並將結果節點 append 進
// container(不清空既有內容,由呼叫端決定)。回傳 container。

const SAFE_HREF = /^https?:\/\//i;

// --- 行內解析:code span 最優先(其內不再解析),再連結/粗體/斜體 ---------------
const INLINE_RE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)\s]+\))/;

function appendInline(el, text) {
  let rest = text;
  while (rest) {
    const m = INLINE_RE.exec(rest);
    if (!m) {
      el.appendChild(document.createTextNode(rest));
      return;
    }
    if (m.index > 0) el.appendChild(document.createTextNode(rest.slice(0, m.index)));
    const tok = m[0];
    if (tok.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = tok.slice(1, -1);
      el.appendChild(code);
    } else if (tok.startsWith("**")) {
      const b = document.createElement("strong");
      appendInline(b, tok.slice(2, -2));
      el.appendChild(b);
    } else if (tok.startsWith("*")) {
      const i = document.createElement("em");
      appendInline(i, tok.slice(1, -1));
      el.appendChild(i);
    } else {
      // [text](url):協定白名單,不合格整段退回字面文字(不產生任何 <a>)
      const close = tok.indexOf("](");
      const label = tok.slice(1, close);
      const url = tok.slice(close + 2, -1);
      if (SAFE_HREF.test(url)) {
        const a = document.createElement("a");
        a.setAttribute("href", url);
        a.setAttribute("rel", "noopener noreferrer");
        a.setAttribute("target", "_blank");
        appendInline(a, label);
        el.appendChild(a);
      } else {
        el.appendChild(document.createTextNode(tok));
      }
    }
    rest = rest.slice(m.index + tok.length);
  }
}

// --- 區塊解析 ---------------------------------------------------------------
export function renderMarkdownInto(container, text) {
  const lines = String(text ?? "").split("\n");
  let i = 0;
  let list = null; // 進行中的 <ul>/<ol>
  let listKind = ""; // 自行追蹤種類(測試 stub 無 tagName)

  const closeList = () => { list = null; listKind = ""; };

  while (i < lines.length) {
    const line = lines[i];

    // 圍欄碼塊:到下一個 ``` 或文末,內容原樣 textContent
    if (/^\s*```/.test(line)) {
      closeList();
      const buf = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1; // 吃掉收尾 ```(或已到文末)
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = buf.join("\n");
      pre.appendChild(code);
      container.appendChild(pre);
      continue;
    }

    // 空行=段落分隔
    if (!line.trim()) { closeList(); i += 1; continue; }

    // 標題:#/## 收斂 h3,###+ 收斂 h4(對話氣泡內不需要更大的層級)
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      closeList();
      const el = document.createElement(h[1].length <= 2 ? "h3" : "h4");
      appendInline(el, h[2]);
      container.appendChild(el);
      i += 1;
      continue;
    }

    // 引用
    if (/^\s*>\s?/.test(line)) {
      closeList();
      const quote = document.createElement("blockquote");
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      const p = document.createElement("p");
      appendInline(p, buf.join("\n"));
      quote.appendChild(p);
      container.appendChild(quote);
      continue;
    }

    // 清單(無序 - * / 有序 1.)
    const ul = /^\s*[-*]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (ul || ol) {
      const kind = ul ? "ul" : "ol";
      if (!list || listKind !== kind) {
        list = document.createElement(kind);
        listKind = kind;
        container.appendChild(list);
      }
      const li = document.createElement("li");
      appendInline(li, (ul || ol)[1]);
      list.appendChild(li);
      i += 1;
      continue;
    }

    // 段落:連續非空行合併,行間以 <br> 呈現
    closeList();
    const p = document.createElement("p");
    let first = true;
    while (i < lines.length && lines[i].trim() && !/^\s*(```|#{1,6}\s|>|[-*]\s|\d+[.)]\s)/.test(lines[i])) {
      if (!first) p.appendChild(document.createElement("br"));
      appendInline(p, lines[i]);
      first = false;
      i += 1;
    }
    container.appendChild(p);
  }
  return container;
}
