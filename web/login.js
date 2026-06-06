// 登入頁：送出密碼，成功則導回工作室首頁，失敗顯示錯誤。

const form = document.querySelector("#loginForm");
const passwordInput = document.querySelector("#password");
const errorEl = document.querySelector("#loginError");
const loginBtn = document.querySelector("#loginBtn");

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.classList.remove("hidden");
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorEl.classList.add("hidden");
  loginBtn.disabled = true;
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: passwordInput.value }),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      location.href = "/";
      return;
    }
    showError(data.detail || "密碼錯誤");
  } catch (err) {
    showError("登入請求失敗，請稍後再試");
  }
  loginBtn.disabled = false;
  passwordInput.focus();
  passwordInput.select();
});
