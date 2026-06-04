const API_URL = "http://127.0.0.1:17891/api/translate-selection";

const MENU_ITEMS = [
  { id: "explain", title: "AI 翻译解释" },
  { id: "natural", title: "自然翻译" },
  { id: "polish", title: "学术润色" },
];

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    for (const item of MENU_ITEMS) {
      chrome.contextMenus.create({
        id: item.id,
        title: item.title,
        contexts: ["selection"],
      });
    }
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const text = (info.selectionText || "").trim();
  if (!text || !tab?.id) {
    return;
  }

  const payload = {
    text,
    source: "chrome-extension",
    url: tab.url || "",
    mode: info.menuItemId || "explain",
  };

  await sendStatus(tab.id, { state: "loading", original: text });

  try {
    const { localApiToken } = await chrome.storage.local.get("localApiToken");
    const headers = { "Content-Type": "application/json" };
    if (localApiToken) {
      headers["X-LiveTranslate-Token"] = localApiToken;
    }

    const response = await fetch(API_URL, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(formatApiError(response.status, data.error));
    }
    await chrome.storage.local.set({ lastResult: data });
    await sendStatus(tab.id, { state: "done", result: data });
  } catch (error) {
    const message = error?.message || "本地客户端未启动或请求失败";
    await chrome.storage.local.set({
      lastResult: { original: text, error: message },
    });
    await sendStatus(tab.id, { state: "error", original: text, error: message });
  }
});

async function sendStatus(tabId, message) {
  try {
    await chrome.tabs.sendMessage(tabId, {
      type: "LIVE_TRANSLATE_SELECTION_RESULT",
      ...message,
    });
  } catch (_error) {
    // Some pages block content scripts. The popup still shows the last result.
  }
}

function formatApiError(status, error) {
  if (status === 401) {
    return "本地 API Token 不正确，请检查插件 popup 和客户端设置。";
  }
  if (error === "text is required") {
    return "没有读取到选中文本。";
  }
  if (error === "unsupported mode") {
    return "当前翻译模式不受支持。";
  }
  return error || `HTTP ${status}`;
}
