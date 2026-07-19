// Sanitizing markdown 渲染器(Kimi 化 PR2):XSS 向量必須全數化為字面文字,
// 合法子集(標題/清單/引用/碼塊/行內)渲染正確。全樹遍歷斷言,不抽樣。
import { install, expect, RecEl } from './_frontend_env.mjs';

install(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }));
const { renderMarkdownInto } = await import('../web/js/markdown.js');

function walk(el, fn) {
  fn(el);
  for (const c of el.children || []) walk(c, fn);
}
function tags(root) {
  const out = [];
  walk(root, (e) => out.push(e.tag));
  return out;
}
function allText(root) {
  let s = '';
  walk(root, (e) => { if (e.tag === 'text') s += e.textContent; else if (!e.children?.length && e.textContent) s += e.textContent; });
  return s;
}
function render(text) {
  const c = new RecEl('div');
  renderMarkdownInto(c, text);
  return c;
}

// --- XSS 向量:任何 HTML 都必須是字面文字,絕不成節點 -------------------------
const vectors = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '<svg onload=alert(1)>',
  'text with <b onmouseover=alert(1)>bold</b> inline',
  '**bold with <script>x</script> inside**',
  '`code with <img src=x>`',
  '> quote <iframe src=x>',
  '- item <script>y</script>',
];
for (const v of vectors) {
  const root = render(v);
  const bad = tags(root).filter((t) => ['script', 'img', 'svg', 'iframe', 'b'].includes(t));
  expect(bad.length === 0, `不得產生 HTML 節點:${v} → ${bad}`);
  expect(allText(root).includes('<'), `原文 HTML 應保留為字面文字:${v}`);
}

// 危險協定連結:整段退回字面文字,零 <a>
for (const v of ['[x](javascript:alert(1))', '[x](data:text/html,hi)', '[x](vbscript:x)']) {
  const root = render(v);
  expect(!tags(root).includes('a'), `危險協定不得產生連結:${v}`);
  expect(allText(root).includes('[x](', ), '應保留字面文字');
}

// 合法 http(s) 連結:href 正確+rel/target 強制
{
  const root = render('see [docs](https://example.com/a) here');
  let a = null;
  walk(root, (e) => { if (e.tag === 'a') a = e; });
  expect(a && a.getAttribute('href') === 'https://example.com/a', '合法連結應保留 href');
  expect(a.getAttribute('rel') === 'noopener noreferrer', '應強制 rel');
  expect(a.getAttribute('target') === '_blank', '應強制 target');
}

// --- 合法子集渲染 -----------------------------------------------------------
{
  const root = render('# 標題\n\n段落一行\n第二行\n\n- 甲\n- 乙\n1. 一\n2. 二\n\n> 引言\n\n```\nconst x = 1;\n```\n尾段 `code` 與 **粗** 和 *斜*');
  const t = tags(root);
  for (const need of ['h3', 'p', 'br', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'strong', 'em']) {
    expect(t.includes(need), `應含 <${need}>`);
  }
  // 碼塊內容原樣
  let pre = null;
  walk(root, (e) => { if (e.tag === 'pre') pre = e; });
  expect(pre.children[0].textContent === 'const x = 1;', '碼塊內容原樣保留');
  // ul 兩項、ol 兩項
  let ul = null, ol = null;
  walk(root, (e) => { if (e.tag === 'ul') ul = e; if (e.tag === 'ol') ol = e; });
  expect(ul.children.length === 2 && ol.children.length === 2, '清單項數正確');
}

// ### 深標題收斂 h4;空輸入/null 容錯
expect(tags(render('### 深標題')).includes('h4'), '###+ 收斂 h4');
expect(render('').children.length === 0, '空字串零輸出');
expect(render(null).children.length === 0, 'null 容錯');

console.log('OK');
