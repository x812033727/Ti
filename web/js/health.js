// 健康狀態（離線示範徽章）與登入門禁檢查。
import { $, toast } from "./dom.js";

export async function loadHealth() {
  try {
    const h = await (await fetch("/api/health")).json();
    if (h.offline) {
      $("#offlineBadge").classList.remove("hidden");
      $("#requirement").placeholder = "離線示範模式：輸入任意需求即可看完整流程（不需 API 金鑰）";
    } else if (!h.has_api_key) {
      toast("未設定 ANTHROPIC_API_KEY；可用 TI_OFFLINE=1 啟動離線示範", "err");
    }
  } catch (e) { /* 忽略 */ }
}

// --- 登入 / 門禁 -------------------------------------------------------
export async function checkAuth() {
  try {
    const s = await (await fetch("/api/auth/status")).json();
    if (s.auth_enabled && !s.authed) {
      location.href = "/login";
      return false;
    }
    if (s.auth_enabled) {
      const btn = $("#logoutBtn");
      btn.classList.remove("hidden");
      btn.onclick = async () => {
        try { await fetch("/api/logout", { method: "POST" }); } catch (e) { /* 忽略 */ }
        location.href = "/login";
      };
    }
  } catch (e) { /* 忽略：門禁狀態無法取得時不阻擋 */ }
  return true;
}
