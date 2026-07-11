// 共用趨勢圖原語：每日 done/fail 堆疊長條（div bar+fill 手法，零相依、stub 可測）。
// 先前洞察面板（.ins-trend-*）與儀表板（.dash-trend-*）各養一套近乎相同的實作——
// 統一到這裡，語意色走 --good/--bad token，深淺主題自動跟隨。
import { appendTextEl } from "../dom.js";

// container 內容會被清空重建。buckets: [{date, ok, fail, rate}]; totals: {ok, fail, rate}。
// opts.detail(b)：附加到欄位 tooltip 的額外行；opts.totalsLine=false 時不渲染合計列。
export function renderStackedTrend(container, buckets, totals = {}, opts = {}) {
  container.innerHTML = "";
  if (!buckets.length) {
    appendTextEl(container, "span", "muted", opts.emptyText || "尚無 audit 紀錄");
    return;
  }
  const pct = (r) => (r != null ? Math.round(r * 100) + "%" : "—");
  const maxN = Math.max(1, ...buckets.map((b) => (b.ok || 0) + (b.fail || 0)));
  const chart = document.createElement("div");
  chart.className = "trend-chart";
  for (const b of buckets) {
    const col = document.createElement("div");
    col.className = "trend-col";
    col.title =
      `${b.date}　完成 ${b.ok || 0}・失敗 ${b.fail || 0}・完成率 ${pct(b.rate)}` +
      (opts.detail ? "\n" + opts.detail(b) : "");
    const bar = document.createElement("div");
    bar.className = "trend-bar";
    const mk = (cls, n) => {
      const seg = document.createElement("div");
      seg.className = cls;
      seg.style.height = `${(n / maxN) * 100}%`;
      bar.appendChild(seg);
    };
    mk("seg-fail", b.fail || 0);
    mk("seg-ok", b.ok || 0);
    col.appendChild(bar);
    appendTextEl(col, "span", "trend-date", String(b.date || "").slice(5));
    chart.appendChild(col);
  }
  container.appendChild(chart);
  if (opts.totalsLine !== false) {
    appendTextEl(
      container,
      "div",
      "trend-total muted",
      `合計 完成 ${totals.ok ?? 0}・失敗 ${totals.fail ?? 0}・完成率 ${pct(totals.rate)}`,
    );
  }
}
