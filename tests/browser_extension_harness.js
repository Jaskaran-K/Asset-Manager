// browser-extension/background.js with stand-in browser APIs (run by tests/test_batch7.py where Node is installed): menu set-up, the click flow (biggest srcset first,
// the helper's reply, a refusal handed to the browser's download, the file handed back), and the page note.
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const same = (a, b) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b));

const source = fs.readFileSync(process.argv[2], "utf8");
const log = [];
const listeners = {};
const on = (name) => ({ addListener: (fn) => (listeners[name] = listeners[name] || []).push(fn), removeListener: (fn) => (listeners[name] = (listeners[name] || []).filter((f) => f !== fn)) });
let nativeReplies = [];
let downloads = {};
let nextId = 1;
let pageScripts = [];
let pageFetches = [];
// The page's own fetch (an icon on its site, read with the user's login) and FileReader: stand-ins
const pageFetch = async (url, options) => {
  pageFetches.push([url, options && options.credentials]);
  return { ok: true, blob: async () => ({ size: 120, type: "image/png", url }) };
};
class PageFileReader {
  readAsDataURL(blob) {
    this.result = "data:image/png;base64,ICON" + blob.url.length;
    setTimeout(() => this.onload(), 1);
  }
}

// A page: one <img> with a srcset, inside a link to a page (not a picture)
function makeDocument() {
  const img = {
    currentSrc: "https://cdn.site.com/art/small.jpg",
    src: "https://cdn.site.com/art/small.jpg",
    srcset: "https://cdn.site.com/art/small.jpg 400w, https://cdn.site.com/art/huge.jpg 2400w, https://cdn.site.com/art/mid.jpg 1200w",
    closest: (sel) => (sel === "a[href]" ? { href: "https://site.com/gallery/42" } : null),
  };
  const shadowChild = { style: {}, textContent: "" };
  const host = { id: "", attachShadow: () => ({ appendChild: () => {}, firstChild: shadowChild }), shadowRoot: { firstChild: shadowChild }, remove: () => {} };
  const metas = {
    "og:title": "Gallery 42: the hooded mage",
    "og:description": "Costume breakdown, with fold studies.",
    "og:site_name": "Site",
    "og:image": "/covers/42.jpg",
  };
  return {
    baseURI: "https://site.com/gallery/42",
    title: "Gallery 42 | Site",
    images: [img],
    querySelector: (sel) => {
      const name = (sel.match(/property="([^"]+)"/) || [])[1];
      return metas[name] ? { content: metas[name] } : null;
    },
    querySelectorAll: (sel) =>
      sel.startsWith("link")
        ? [ // (its icons: the biggest is the one sent)
            { getAttribute: (name) => ({ rel: "icon", href: "/favicon-16.png", sizes: "16x16" })[name] || null },
            { getAttribute: (name) => ({ rel: "shortcut icon", href: "/favicon-96.png", sizes: "96x96" })[name] || null },
            { getAttribute: (name) => ({ rel: "stylesheet", href: "/site.css" })[name] || null },
          ]
        : [{ href: "https://site.com/artist/7", innerText: "  The   Artist  " }],
    getElementById: () => null,
    createElement: () => host,
    documentElement: { appendChild: () => {} },
    note: shadowChild,
  };
}
const document = makeDocument();

const chrome = {
  runtime: {
    onInstalled: on("installed"),
    lastError: undefined,
    sendNativeMessage: (host, message, callback) => {
      log.push(["native", host, message]);
      const reply = nativeReplies.shift();
      setTimeout(() => callback(reply), 1);
    },
  },
  contextMenus: {
    removeAll: (cb) => cb(),
    create: (item) => log.push(["menu", item]),
    onClicked: on("clicked"),
  },
  scripting: {
    executeScript: async ({ target, func, args }) => {
      pageScripts.push(func.name);
      const location = { hostname: "www.site.com", origin: "https://site.com" };
      const result = await vm.runInNewContext(`(${func.toString()})`, { document, location, URL, fetch: pageFetch, FileReader: PageFileReader, setTimeout, clearTimeout })(...args);
      return [{ result }];
    },
  },
  downloads: {
    onChanged: on("changed"),
    download: async (options) => {
      const id = nextId++;
      downloads[id] = { id, state: "in_progress", filename: "C:\\Users\\me\\Downloads\\" + options.filename.replace("/", "\\") };
      log.push(["download", options]);
      setTimeout(() => {
        downloads[id].state = "complete";
        for (const fn of listeners.changed || []) fn({ id, state: { current: "complete" } });
      }, 20);
      return id;
    },
    search: async ({ id }) => (downloads[id] ? [{ ...downloads[id] }] : []),
    cancel: () => {},
    erase: ({ id }) => log.push(["erase", id]),
  },
};

vm.runInNewContext(source, { chrome, URL, setTimeout, clearTimeout, console, Promise, Date });

(async () => {
  for (const fn of listeners.installed) fn();
  const menus = log.filter((e) => e[0] === "menu").map((e) => e[1]);
  same(menus.map((m) => m.id), ["library", "artbook", "unsorted", "page_artbook", "page_unsorted", "link_artbook", "link_unsorted"]);
  same(menus.map((m) => m.contexts[0]), ["image", "image", "image", "page", "page", "link", "link"]);
  same(menus.map((m) => m.title).slice(1, 4), ["Add to Artbook: Current Page", "Add to Artbook: Unsorted", "Add This Page as a Link: Current Page"]);

  // 1. Saved straight away: the biggest srcset entry goes first
  log.length = 0;
  nativeReplies = [{ ok: true, message: "Saved to your library: Web Images\\site.com\\huge.jpg" }];
  const tab = { id: 7, url: "https://site.com/gallery/42", title: "Gallery 42" };
  const info = { menuItemId: "library", srcUrl: "https://cdn.site.com/art/small.jpg", pageUrl: tab.url, frameId: 0 };
  for (const fn of listeners.clicked) fn(info, tab);
  await new Promise((r) => setTimeout(r, 100));
  const sent = log.filter((e) => e[0] === "native").map((e) => e[2]);
  assert.strictEqual(sent.length, 1);
  same(sent[0].sources, ["https://cdn.site.com/art/huge.jpg", "https://cdn.site.com/art/mid.jpg", "https://cdn.site.com/art/small.jpg"]);
  assert.strictEqual(sent[0].page, tab.url);
  assert.strictEqual(sent[0].title, "Gallery 42");
  assert.strictEqual(document.note.textContent, "Saved to your library: Web Images\\site.com\\huge.jpg");
  assert.ok(document.note.style.cssText.includes("#14532d"));
  console.log("saved: sources in order, note shown");

  // 2. The site refuses the app: the browser downloads it, the helper takes the file
  log.length = 0;
  nativeReplies = [
    { ok: false, in_browser: "https://cdn.site.com/art/huge.jpg", message: "The site refused it (403)." },
    { ok: true, message: 'Added to the Artbook page "Refs".' },
  ];
  for (const fn of listeners.clicked) fn({ ...info, menuItemId: "artbook" }, tab);
  await new Promise((r) => setTimeout(r, 200));
  const asked = log.filter((e) => e[0] === "native").map((e) => e[2]);
  assert.strictEqual(asked.length, 2);
  assert.strictEqual(asked[1].op, "take");
  assert.strictEqual(asked[1].target, "artbook");
  assert.match(asked[1].path, /\\Downloads\\Asset Manager\\\d+$/);
  const download = log.find((e) => e[0] === "download")[1];
  assert.strictEqual(download.url, "https://cdn.site.com/art/huge.jpg");
  assert.strictEqual(download.saveAs, false);
  assert.ok(log.some((e) => e[0] === "erase"));
  assert.strictEqual(document.note.textContent, 'Added to the Artbook page "Refs".');
  console.log("refused: downloaded by the browser, handed over, erased from its list");

  // 3. Not set up: the helper doesn't answer
  log.length = 0;
  nativeReplies = [undefined];
  chrome.runtime.lastError = { message: "Specified native messaging host not found." };
  for (const fn of listeners.clicked) fn(info, tab);
  await new Promise((r) => setTimeout(r, 100));
  chrome.runtime.lastError = undefined;
  assert.ok(document.note.textContent.startsWith("Asset Manager isn't set up for this browser"));
  assert.ok(document.note.style.cssText.includes("#7f1d1d"));
  console.log("not set up: says how to set it up");

  // 4. A picture to Unsorted; the page as a link card for Unsorted (read in the page)
  log.length = 0;
  nativeReplies = [{ ok: true, message: "Added to the Artbook's Unsorted tray." }];
  for (const fn of listeners.clicked) fn({ ...info, menuItemId: "unsorted" }, tab);
  await new Promise((r) => setTimeout(r, 100));
  assert.strictEqual(log.filter((e) => e[0] === "native")[0][2].target, "unsorted");
  log.length = 0;
  nativeReplies = [{ ok: true, message: "Added the link to the Artbook's Unsorted tray." }];
  for (const fn of listeners.clicked) fn({ menuItemId: "page_unsorted", pageUrl: tab.url, frameId: 0 }, tab);
  await new Promise((r) => setTimeout(r, 100));
  const link = log.filter((e) => e[0] === "native")[0][2];
  same([link.op, link.target, link.url], ["link", "unsorted", tab.url]);
  same([link.title, link.about, link.site, link.picture],
       ["Gallery 42: the hooded mage", "Costume breakdown, with fold studies.", "Site", "https://site.com/covers/42.jpg"]);
  same(pageFetches, [["https://site.com/favicon-96.png", "include"]]); // (its biggest icon, read in the page)
  same(link.icon, "data:image/png;base64,ICON" + "https://site.com/favicon-96.png".length);
  console.log("page link: its title, about, site, preview and icon read in the browser");

  // 5. A link on the page, for the open page; its preview refused: downloaded by the browser, handed over with the link
  log.length = 0;
  nativeReplies = [
    { ok: false, in_browser: "https://site.com/covers/7.jpg", message: "The site refused it (403)." },
    { ok: true, message: 'Added the link to the Artbook page "Refs".' },
  ];
  for (const fn of listeners.clicked) fn({ menuItemId: "link_artbook", linkUrl: "https://site.com/artist/7", pageUrl: tab.url, frameId: 0 }, tab);
  await new Promise((r) => setTimeout(r, 200));
  const asks = log.filter((e) => e[0] === "native").map((e) => e[2]);
  same([asks[0].op, asks[0].target, asks[0].url, asks[0].title], ["link", "artbook", "https://site.com/artist/7", "The Artist"]);
  same([asks[1].op, asks[1].link.url, asks[1].link.title], ["take", "https://site.com/artist/7", "The Artist"]);
  assert.ok(asks[0].icon.startsWith("data:image/png") && asks[1].link.icon === asks[0].icon); // (its site's icon, kept for the hand-over)
  assert.strictEqual(document.note.textContent, 'Added the link to the Artbook page "Refs".');
  console.log("link: its text, the picture handed over after the browser's download");

  // 6. A link to another site: its icon goes as an address (the app gets it); the page doesn't fetch it
  log.length = 0;
  pageFetches = [];
  nativeReplies = [{ ok: true, message: "Added the link to the Artbook's Unsorted tray." }];
  for (const fn of listeners.clicked) fn({ menuItemId: "link_unsorted", linkUrl: "https://other.org/a/b", pageUrl: tab.url, frameId: 0 }, tab);
  await new Promise((r) => setTimeout(r, 100));
  same(log.filter((e) => e[0] === "native")[0][2].icon, "https://other.org/favicon.ico");
  same(pageFetches, []);
  console.log("link elsewhere: its site's icon by address");
  console.log("ALL OK");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
