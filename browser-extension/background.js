// Asset Manager's browser extension (Chrome, Opera GX): right-click a picture > Asset Manager >
// Save to Library / Add to Artbook Page.
//
// The extension only finds the picture; the app's helper (core/browser_link.py) downloads and saves it. The
// browser starts that helper for each picture through "native messaging": Asset Manager > Settings > Browser >
// Set Up registers it, for this extension's ID only (the "key" in manifest.json fixes the ID).
//
// Permissions: contextMenus (the two entries), nativeMessaging (the helper), activeTab + scripting (look at the
// clicked picture for a bigger version, and show a note on the page; only on the tab you right-clicked, only
// then), downloads (when a site refuses the app, e.g. a picture behind a login: the browser downloads it with
// your login, and the app moves the file into place).

const HOST = "com.assetmanager.browser";
const MENU = {
  library: "Save to Library",
  artbook: "Add to Artbook Page",
};
const DOWNLOAD_FOLDER = "Asset Manager"; // in the browser's downloads folder, only while the app takes the file
const DOWNLOAD_SECONDS = 120;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    for (const [id, title] of Object.entries(MENU)) {
      chrome.contextMenus.create({ id, title, contexts: ["image"] });
    }
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (!(info.menuItemId in MENU)) return;
  send(info, tab).catch((error) => note(tab, false, "Asset Manager: " + (error && error.message ? error.message : error)));
});

async function send(info, tab) {
  const target = info.menuItemId;
  const page = info.pageUrl || (tab && tab.url) || "";
  const title = (tab && tab.title) || "";
  note(tab, null, target === "library" ? "Saving to Asset Manager..." : "Sending to the Artbook...");
  const sources = await lookAround(tab, info);
  let reply = await ask({ op: "save", target, sources, page, title });
  if (!reply.ok && reply.in_browser) {
    note(tab, null, "The site wants your login: downloading it in the browser...");
    const path = await downloadInBrowser(reply.in_browser);
    reply = await ask({ op: "take", target, path, url: reply.in_browser, page, title });
  }
  note(tab, !!reply.ok, reply.message || (reply.ok ? "Done." : "That didn't work."));
}

// The clicked picture's addresses, best first: an image its link points to, its srcset's biggest, then itself.
async function lookAround(tab, info) {
  const fallback = [info.srcUrl];
  if (!tab || tab.id === undefined) return fallback;
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id, frameIds: [info.frameId || 0] },
      func: pickSources,
      args: [info.srcUrl],
    });
    const found = result && Array.isArray(result.result) ? result.result : [];
    return found.length ? found : fallback;
  } catch (_error) {
    return fallback; // (a page the browser keeps extensions out of, or a frame from another site)
  }
}

// Runs in the page.
async function pickSources(srcUrl) {
  const out = [];
  const add = (url) => {
    if (!url) return;
    try {
      url = new URL(url, document.baseURI).href;
    } catch (_error) {
      return;
    }
    if (!out.includes(url)) out.push(url);
  };
  const picture = [...document.images].find((img) => img.currentSrc === srcUrl || img.src === srcUrl);
  if (picture) {
    const link = picture.closest("a[href]");
    if (link) {
      try {
        const query = new URL(link.href).searchParams;
        for (const key of ["imgurl", "mediaurl", "img_url", "image_url"]) add(query.get(key));
      } catch (_error) {}
      if (/\.(jpe?g|png|webp|gif|bmp|tiff?|avif)([?#]|$)/i.test(link.href)) add(link.href);
    }
    const sets = [picture.srcset];
    const frame = picture.closest("picture");
    if (frame) for (const source of frame.querySelectorAll("source[srcset]")) sets.push(source.srcset);
    const sized = [];
    for (const set of sets) {
      for (const part of (set || "").split(/,\s+/)) {
        const [url, size] = part.trim().split(/\s+/);
        if (!url) continue;
        let width = 1;
        if (size && size.endsWith("w")) width = parseFloat(size);
        else if (size && size.endsWith("x")) width = parseFloat(size) * 1000;
        sized.push([width || 1, url]);
      }
    }
    sized.sort((a, b) => b[0] - a[0]);
    for (const [, url] of sized) add(url);
  }
  if (srcUrl.startsWith("blob:")) {
    // (only this page can read it: hand over its data)
    try {
      const blob = await (await fetch(srcUrl)).blob();
      const data = await new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.readAsDataURL(blob);
      });
      if (typeof data === "string") out.push(data);
    } catch (_error) {}
  } else {
    add(srcUrl);
  }
  return out.filter((url) => /^(https?:|data:image\/)/i.test(url)).slice(0, 12);
}

function ask(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(HOST, message, (reply) => {
      const error = chrome.runtime.lastError;
      if (error || !reply) {
        resolve({
          ok: false,
          message:
            "Asset Manager isn't set up for this browser. In Asset Manager: Settings > Browser > Set Up. (" +
            (error ? error.message : "no answer") + ")",
        });
      } else {
        resolve(reply);
      }
    });
  });
}

// The browser's own download (with the site's login), for the app to take: the file's full path.
async function downloadInBrowser(url) {
  let finished;
  const done = new Promise((resolve) => (finished = resolve));
  let id = null;
  let timer = null;
  const changed = (delta) => {
    if (delta.id === id && delta.state && delta.state.current !== "in_progress") finished();
  };
  chrome.downloads.onChanged.addListener(changed);
  try {
    id = await chrome.downloads.download({
      url,
      filename: `${DOWNLOAD_FOLDER}/${Date.now()}`,
      conflictAction: "uniquify",
      saveAs: false,
    });
    const [now] = await chrome.downloads.search({ id });
    if (!now || now.state === "in_progress") {
      const late = new Promise((resolve) => (timer = setTimeout(resolve, DOWNLOAD_SECONDS * 1000)));
      await Promise.race([done, late]);
    }
    const [item] = await chrome.downloads.search({ id });
    if (!item || item.state !== "complete") {
      if (item && item.state === "in_progress") chrome.downloads.cancel(id);
      throw new Error("The browser couldn't download it either.");
    }
    return item.filename;
  } finally {
    clearTimeout(timer);
    chrome.downloads.onChanged.removeListener(changed);
    if (id !== null) chrome.downloads.erase({ id }); // (off the downloads list: the app moves the file)
  }
}

// A note in the page's corner: ok null = working on it (stays), true / false = how it went (fades).
function note(tab, ok, text) {
  if (!tab || tab.id === undefined) return;
  chrome.scripting
    .executeScript({ target: { tabId: tab.id }, func: showNote, args: [ok, text] })
    .catch(() => {}); // (a page the browser keeps extensions out of: nothing to show it on)
}

// Runs in the page.
function showNote(ok, text) {
  const id = "asset-manager-note";
  let host = document.getElementById(id);
  if (!host) {
    host = document.createElement("div");
    host.id = id;
    host.attachShadow({ mode: "open" }).appendChild(document.createElement("div"));
    document.documentElement.appendChild(host);
  }
  const box = host.shadowRoot.firstChild;
  box.textContent = text;
  box.style.cssText = [
    "position:fixed", "z-index:2147483647", "right:16px", "bottom:16px", "max-width:440px",
    "padding:10px 14px", "border-radius:8px", "font:13px/1.45 'Segoe UI',system-ui,sans-serif",
    "color:#f4f4f5", "box-shadow:0 6px 20px rgba(0,0,0,.35)", "pointer-events:none",
    "transition:opacity .35s", "opacity:1", "white-space:pre-line",
    "background:" + (ok === false ? "#7f1d1d" : ok ? "#14532d" : "#1f2937"),
  ].join(";");
  clearTimeout(host.timer);
  if (ok !== null) {
    host.timer = setTimeout(() => {
      box.style.opacity = "0";
      host.timer = setTimeout(() => host.remove(), 400);
    }, ok ? 3500 : 7000);
  }
}
