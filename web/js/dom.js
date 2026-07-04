// DOM 小工具：選擇器、文字節點、toast。
// 鐵則：本模組（與所有非入口模組）頂層不得查 DOM、不得有副作用——
// Node 測試以「先掛全域 stub → 再 import 模組」載入，頂層副作用會讓測試無法建環境。

export const $ = (sel) => document.querySelector(sel);

export function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

export function appendTextEl(parent, tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = text;
  parent.appendChild(el);
  return el;
}
