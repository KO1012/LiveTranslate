const tokenInput = document.getElementById("token");
const tokenStatus = document.getElementById("token-status");
const saveToken = document.getElementById("save-token");
const resultRoot = document.getElementById("result");

chrome.storage.local.get(["lastResult", "localApiToken"]).then(({ lastResult, localApiToken }) => {
  tokenInput.value = localApiToken || "";
  renderResult(lastResult);
  refreshHealth();
});

saveToken.addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  if (token) {
    await chrome.storage.local.set({ localApiToken: token });
  } else {
    await chrome.storage.local.remove("localApiToken");
  }
  tokenStatus.textContent = "已保存";
  setTimeout(() => {
    tokenStatus.textContent = "只保存本地共享 token，不保存 API Key。";
  }, 1500);
});

function renderResult(lastResult) {
  if (!lastResult) {
    return;
  }
  if (lastResult.error) {
    resultRoot.innerHTML = `<div class="error">${escapeHtml(lastResult.error)}</div>`;
    return;
  }

  const parts = [
    ["原文", lastResult.original],
    ["译文", lastResult.translation],
    ["解释", lastResult.explanation],
    ["润色", lastResult.polished],
  ].filter(([, value]) => value);

  resultRoot.innerHTML = parts
    .map(([label, value]) => `
      <section>
        <div class="label">${escapeHtml(label)}</div>
        <div class="text">${escapeHtml(value)}</div>
      </section>
    `)
    .join("");
}

async function refreshHealth() {
  try {
    const response = await fetch("http://127.0.0.1:17891/api/health");
    const data = await response.json();
    if (data.token_required) {
      tokenStatus.textContent = "客户端已启用本地 API Token，请填写同一个值。";
    }
  } catch (_error) {
    tokenStatus.textContent = "本地客户端未启动。";
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
