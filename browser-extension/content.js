const BOX_ID = "live-translate-selection-box";

chrome.runtime.onMessage.addListener((message) => {
  if (message.type !== "LIVE_TRANSLATE_SELECTION_RESULT") {
    return;
  }
  showResult(message);
});

function showResult(message) {
  let box = document.getElementById(BOX_ID);
  if (!box) {
    box = document.createElement("div");
    box.id = BOX_ID;
    box.innerHTML = `
      <div class="lt-header">
        <strong>LiveTranslate</strong>
        <button type="button" aria-label="Close">×</button>
      </div>
      <div class="lt-body"></div>
    `;
    document.documentElement.appendChild(box);
    box.querySelector("button").addEventListener("click", () => box.remove());
  }

  applyStyle(box);
  const body = box.querySelector(".lt-body");
  if (message.state === "loading") {
    body.textContent = "翻译中...";
  } else if (message.state === "error") {
    body.textContent = message.error || "请求失败";
  } else {
    body.innerHTML = renderResult(message.result || {});
  }
}

function renderResult(result) {
  const parts = [
    ["原文", result.original],
    ["译文", result.translation],
    ["解释", result.explanation],
    ["润色", result.polished],
  ].filter(([, value]) => value);

  return parts
    .map(([label, value]) => `
      <section>
        <div class="lt-label">${escapeHtml(label)}</div>
        <div class="lt-text">${escapeHtml(value)}</div>
      </section>
    `)
    .join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function applyStyle(box) {
  if (box.dataset.ready) {
    return;
  }
  box.dataset.ready = "1";
  const style = document.createElement("style");
  style.textContent = `
    #${BOX_ID} {
      position: fixed;
      right: 24px;
      bottom: 24px;
      z-index: 2147483647;
      width: min(420px, calc(100vw - 48px));
      max-height: min(520px, calc(100vh - 48px));
      overflow: auto;
      color: #f8fafc;
      background: rgba(15, 23, 42, 0.96);
      border: 1px solid rgba(148, 163, 184, 0.35);
      border-radius: 8px;
      box-shadow: 0 16px 48px rgba(15, 23, 42, 0.35);
      font: 14px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    #${BOX_ID} .lt-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.25);
    }
    #${BOX_ID} button {
      color: #f8fafc;
      background: transparent;
      border: 0;
      font-size: 20px;
      line-height: 1;
      cursor: pointer;
    }
    #${BOX_ID} .lt-body {
      padding: 12px;
    }
    #${BOX_ID} section + section {
      margin-top: 12px;
    }
    #${BOX_ID} .lt-label {
      margin-bottom: 4px;
      color: #93c5fd;
      font-size: 12px;
    }
    #${BOX_ID} .lt-text {
      white-space: pre-wrap;
      word-break: break-word;
    }
  `;
  document.documentElement.appendChild(style);
}
