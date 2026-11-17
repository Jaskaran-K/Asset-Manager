// Asset Manager's browser extension (Chrome, Opera GX). Right-click a picture > Asset Manager > Save to Library /
// Add to Artbook: Current Page / Add to Artbook: Unsorted. Right-click a page (or a link on it) > Asset Manager >
// Add This Page (Link) as a Link: Current Page / Unsorted: a link card, with the title, the line about it, the
// preview picture and the site's icon this browser sees (sites that refuse the app still show them to you).
//
// The extension only finds things; the app's helper (core/browser_link.py) downloads and saves them. The browser
// starts that helper through "native messaging": Asset Manager > Settings > Browser > Set Up registers it, for this
// extension's ID only (the "key" in manifest.json fixes the ID).
//
// Permissions: contextMenus (the entries), nativeMessaging (the helper), activeTab + scripting (look at the
// clicked picture for a bigger version, read the page's title, preview and icon, and show a note on the page; only on
// the tab you right-clicked, only then), downloads (when a site refuses the app, e.g. a picture behind a login:
// the browser downloads it with your login, and the app moves the file into place).

const HOST = "com.assetmanager.browser";
const PICTURE_MENU = { // (right-click a picture) -> where it goes
  library: ["Save to Library", "library"],
  artbook: ["Add to Artbook: Current Page", "artbook"],
  unsorted: ["Add to Artbook: Unsorted", "unsorted"],
};
const LINK_MENU = { // (right-click the page, or a link) -> [title, where it goes, which contexts]
  page_artbook: ["Add This Page as a Link: Current Page", "artbook", "page"],
  page_unsorted: ["Add This Page as a Link: Unsorted", "unsorted", "page"],
  link_artbook: ["Add This Link: Current Page", "artbook", "link"],
  link_unsorted: ["Add This Link: Unsorted", "unsorted", "link"],
};
const DOWNLOAD_FOLDER = "Asset Manager"; // in the browser's downloads folder, only while the app takes the file
const DOWNLOAD_SECONDS = 120;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    for (const [id, [title]] of Object.entries(PICTURE_MENU)) {
      chrome.contextMenus.create({ id, title, contexts: ["image"] });
    }
    for (const [id, [title, , context]] of Object.entries(LINK_MENU)) {
      chrome.contextMenus.create({ id, title, contexts: [context] });
    }
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const failed = (error) => note(tab, false, "Asset Manager: " + (error && error.message ? error.message : error));
  if (info.menuItemId in PICTURE_MENU) send(info, tab).catch(failed);
  else if (info.menuItemId in LINK_MENU) sendLink(info, tab).catch(failed);
});

async function send(info, tab) {
  const target = PICTURE_MENU[info.menuItemId][1];
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

// A page (or a link on it) as a link card: its title, the line about it, its site, its preview picture and its
// site's icon, read here in the browser; the app's helper gets the picture (or, if the site refuses it, the browser
// downloads it).
async function sendLink(info, tab) {
  const [, target, context] = LINK_MENU[info.menuItemId];
  const url = context === "link" ? info.linkUrl : info.pageUrl || (tab && tab.url) || "";
  if (!/^https?:/i.test(url || "")) throw new Error("Only web pages (http or https) can be added as links.");
  note(tab, null, "Sending the link to the Artbook...");
  let details = { title: "", about: "", site: "", picture: "", icon: "" };
  if (tab && tab.id !== undefined) {
    try {
      const [result] = await chrome.scripting.executeScript({
        target: { tabId: tab.id, frameIds: [info.frameId || 0] },
        func: readPage,
        args: [context === "link" ? url : ""],
      });
      if (result && result.result) details = result.result;
    } catch (_error) {} // (a page the browser keeps extensions out of: the app reads what it can)
  }
  const tabIcon = tab && /^(https?:|data:image\/)/i.test(tab.favIconUrl || "") ? tab.favIconUrl : "";
  const icon = details.icon || (context === "page" ? tabIcon : ""); // (else the icon the browser shows for the tab)
  const link = { url, title: details.title, about: details.about, site: details.site, icon };
  let reply = await ask({ op: "link", target, ...link, picture: details.picture });
  if (!reply.ok && reply.in_browser) {
    note(tab, null, "The site wants your login: downloading its picture in the browser...");
    const path = await downloadInBrowser(reply.in_browser);
    reply = await ask({ op: "take", target, path, url: reply.in_browser, page: url, title: details.title, link });
  }
  note(tab, !!reply.ok, reply.message || (reply.ok ? "Done." : "That didn't work."));
}

// Runs in the page: what a link card shows. For the page itself, its <meta> tags (Open Graph, Twitter,
// description), title and icon; for a link on it (linkUrl), the link's own text and its site's icon (the app reads
// the rest). An icon on this site is read here (as data: the site may refuse the app); one elsewhere goes as its address.
async function readPage(linkUrl) {
  const meta = (...names) => {
    for (const name of names) {
      const tag = document.querySelector(`meta[property="${name}"], meta[name="${name}"]`);
      if (tag && tag.content && tag.content.trim()) return tag.content.trim();
    }
    return "";
  };
  const absolute = (url) => {
    try {
      return url ? new URL(url, document.baseURI).href : "";
    } catch (_error) {
      return "";
    }
  };
  // The page's icon: the biggest its <link> tags list (an SVG counts as big), else the site's /favicon.ico
  const pageIcon = () => {
    let best = "";
    let bestScore = -1;
    for (const tag of document.querySelectorAll("link[rel][href]")) {
      const words = (tag.getAttribute("rel") || "").toLowerCase().split(/\s+/);
      const touch = words.includes("apple-touch-icon") || words.includes("apple-touch-icon-precomposed");
      if (!words.includes("icon") && !touch) continue;
      const sizes = (tag.getAttribute("sizes") || "").toLowerCase();
      const href = absolute(tag.getAttribute("href"));
      if (!href) continue;
      let score;
      if (/svg/i.test(tag.getAttribute("type") || "") || sizes.includes("any") || /\.svg([?#]|$)/i.test(href)) score = 256;
      else {
        const found = [...sizes.matchAll(/(\d+)x\d+/g)].map((m) => parseInt(m[1], 10));
        score = found.length ? Math.max(...found) : touch ? 180 : 16;
      }
      if (score > bestScore) [best, bestScore] = [href, score];
    }
    return best || absolute("/favicon.ico");
  };
  const readIcon = async (url) => {
    if (!url || url.startsWith("data:")) return /^data:image\//i.test(url) && url.length < 700000 ? url : "";
    try {
      if (new URL(url).origin !== location.origin) return url; // (another site's: the app gets it)
      const reply = await fetch(url, { credentials: "include" });
      const blob = reply.ok ? await reply.blob() : null;
      if (!blob || blob.size > 512 * 1024 || !/^image\//i.test(blob.type)) return url;
      return await new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(typeof reader.result === "string" ? reader.result : url);
        reader.onerror = () => resolve(url);
        reader.readAsDataURL(blob);
      });
    } catch (_error) {
      return url;
    }
  };
  if (linkUrl) {
    const anchor = [...document.querySelectorAll("a[href]")].find((a) => a.href === linkUrl);
    const text = anchor ? (anchor.innerText || anchor.title || anchor.getAttribute("aria-label") || "").trim() : "";
    let icon = "";
    try {
      const target = new URL(linkUrl);
      icon = target.origin === location.origin ? await readIcon(pageIcon()) : target.origin + "/favicon.ico";
    } catch (_error) {}
    return { title: text.replace(/\s+/g, " ").slice(0, 300), about: "", site: "", picture: "", icon };
  }
  return {
    title: (meta("og:title", "twitter:title") || document.title || "").slice(0, 300),
    about: meta("og:description", "description", "twitter:description").slice(0, 600),
    site: meta("og:site_name") || location.hostname.replace(/^www\./, ""),
    picture: absolute(meta("og:image", "og:image:url", "twitter:image")),
    icon: await readIcon(pageIcon()),
  };
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
