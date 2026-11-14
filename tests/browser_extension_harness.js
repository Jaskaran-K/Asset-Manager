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
  return {
    baseURI: "https://site.com/gallery/42",
    images: [img],
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
      const result = await vm.runInNewContext(`(${func.toString()})`, { document, URL, fetch, FileReader: undefined, setTimeout, clearTimeout })(...args);
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
  same(log.filter((e) => e[0] === "menu").map((e) => e[1].id), ["library", "artbook"]);
  same(log.find((e) => e[0] === "menu")[1].contexts, ["image"]);

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
  console.log("ALL OK");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
