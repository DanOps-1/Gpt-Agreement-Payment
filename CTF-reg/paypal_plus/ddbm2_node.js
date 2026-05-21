#!/usr/bin/env node
/*
 * Minimal browser shim for PayPal/DataDome ddbm2 tags.js.
 *
 * Input (stdin JSON):
 *   { tagsJs, pageUrl, referrer, cookie, userAgent, ddjsKey }
 * Output (stdout JSON):
 *   { ok, body, error }
 *
 * The script does not drive a browser or render PayPal.  It executes the
 * DataDome collection tag inside a small Node VM-shaped DOM, dispatches a few
 * user events, captures the generated x-www-form-urlencoded body that the tag
 * would POST to https://ddbm2.paypal.com/js/, and returns that body to Python.
 */

const fs = require("fs");

function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { data += chunk; });
    process.stdin.on("end", () => resolve(data));
  });
}

class Elem {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.style = {};
    this.attributes = {};
    this.parentNode = null;
    this.width = 300;
    this.height = 150;
  }
  setAttribute(k, v) { this.attributes[k] = String(v); this[k] = String(v); }
  getAttribute(k) { return this.attributes[k] || this[k] || null; }
  appendChild(e) { e.parentNode = this; this.children.push(e); return e; }
  removeChild(e) { this.children = this.children.filter((x) => x !== e); }
  addEventListener() {}
  removeEventListener() {}
  getBoundingClientRect() {
    const w = this.width || 100, h = this.height || 20;
    return { x: 0, y: 0, width: w, height: h, top: 0, left: 0, right: w, bottom: h };
  }
  getContext() {
    return {
      fillRect() {}, clearRect() {}, putImageData() {}, createImageData() { return []; },
      setTransform() {}, drawImage() {}, save() {}, fillText() {}, restore() {},
      beginPath() {}, moveTo() {}, lineTo() {}, closePath() {}, stroke() {},
      translate() {}, scale() {}, rotate() {}, arc() {}, fill() {}, transform() {},
      rect() {}, clip() {}, measureText() { return { width: 42 }; },
      getImageData() { return { data: new Uint8ClampedArray(4) }; },
      canvas: this,
    };
  }
  toDataURL() { return "data:image/png;base64,iVBORw0KGgo="; }
}

function storage() {
  return {
    getItem(k) { return Object.prototype.hasOwnProperty.call(this, k) ? this[k] : null; },
    setItem(k, v) { this[k] = String(v); },
    removeItem(k) { delete this[k]; },
  };
}

async function main() {
  const cfg = JSON.parse(await readStdin());
  const tagsJs = String(cfg.tagsJs || "");
  if (!tagsJs) throw new Error("missing tagsJs");

  const posted = [];
  const listeners = {};
  const addL = (_target, type, cb) => {
    if (typeof cb === "function") (listeners[type] = listeners[type] || []).push(cb);
  };
  const dispatch = (type, ev = {}) => {
    for (const cb of (listeners[type] || [])) {
      try {
        cb(Object.assign({
          type,
          isTrusted: true,
          screenX: 100,
          screenY: 100,
          clientX: 90,
          clientY: 90,
          pointerType: "mouse",
          buttons: 1,
          pressure: 0.5,
          key: "a",
        }, ev));
      } catch (_) {}
    }
  };

  const loc = new URL(String(cfg.pageUrl || "https://www.paypal.com/checkoutweb/signup"));
  const userAgent = String(cfg.userAgent || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36");

  const document = {
    cookie: String(cfg.cookie || "datadome=.keep"),
    currentScript: { src: "https://ddbm2.paypal.com/tags.js" },
    location: loc,
    URL: loc.href,
    referrer: String(cfg.referrer || "https://www.paypal.com/"),
    readyState: "complete",
    visibilityState: "visible",
    hidden: false,
    documentElement: new Elem("html"),
    body: new Elem("body"),
    createElement(tag) { return new Elem(tag); },
    createEvent() { return { initEvent() {} }; },
    getElementsByTagName(tag) { return String(tag).toLowerCase() === "script" ? [this.currentScript] : []; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener(type, cb) { addL(this, type, cb); },
    removeEventListener() {},
    hasFocus() { return true; },
  };
  document.documentElement.appendChild(document.body);

  const navigator = {
    userAgent,
    appName: "Netscape",
    appVersion: "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    platform: "Win32",
    product: "Gecko",
    productSub: "20030107",
    vendor: "Google Inc.",
    vendorSub: "",
    language: "en-US",
    languages: ["en-US", "en"],
    cookieEnabled: true,
    onLine: true,
    hardwareConcurrency: 16,
    deviceMemory: 8,
    maxTouchPoints: 0,
    plugins: [
      { name: "Chrome PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1, 0: { type: "application/pdf", suffixes: "pdf" } },
      { name: "Chromium PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1, 0: { type: "application/pdf", suffixes: "pdf" } },
      { name: "Microsoft Edge PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1, 0: { type: "application/pdf", suffixes: "pdf" } },
      { name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1, 0: { type: "application/pdf", suffixes: "pdf" } },
      { name: "WebKit built-in PDF", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1, 0: { type: "application/pdf", suffixes: "pdf" } },
    ],
    mimeTypes: [{ type: "application/pdf", suffixes: "pdf" }, { type: "text/pdf", suffixes: "pdf" }],
    connection: { effectiveType: "4g", rtt: 50, downlink: 10 },
    permissions: { query: () => Promise.resolve({ state: "prompt" }) },
    webdriver: undefined,
  };

  class XHR {
    constructor() {
      this.headers = {};
      this.readyState = 0;
      this.status = 0;
      this.responseText = "";
      this.withCredentials = false;
    }
    open(method, url, async = true) { this.method = method; this.url = url; this.async = async; }
    setRequestHeader(k, v) { this.headers[k] = v; }
    send(data) {
      const body = String(data || "");
      if (String(this.url || "").includes("ddbm2.paypal.com/js")) {
        posted.push({ url: this.url, body });
      }
      this.readyState = 4;
      this.status = 200;
      this.responseText = "{\"status\":200,\"cookie\":\"datadome=NODECOOKIE; Max-Age=2592000; Domain=.paypal.com; Path=/; Secure; SameSite=None\"}";
      if (this.onreadystatechange) this.onreadystatechange();
      if (this.onload) this.onload();
    }
    abort() {}
    getResponseHeader() { return null; }
  }

  const window = global;
  Object.defineProperty(global, "navigator", { value: navigator, configurable: true });
  Object.assign(global, {
    window,
    self: window,
    document,
    location: loc,
    screen: { width: 1440, height: 900, availWidth: 1440, availHeight: 820, colorDepth: 24, pixelDepth: 24, orientation: { type: "landscape-primary", angle: 0 } },
    history: { length: 2 },
    XMLHttpRequest: XHR,
    localStorage: Object.create(storage()),
    sessionStorage: Object.create(storage()),
    performance: { now: () => 1234.56, timing: { navigationStart: Date.now() - 5000 }, getEntriesByType: () => [] },
    chrome: { runtime: {} },
    CSS: { supports: () => true },
    URLSearchParams,
    Blob,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    btoa: (s) => Buffer.from(s, "binary").toString("base64"),
  });
  window.addEventListener = (type, cb) => addL(window, type, cb);
  window.removeEventListener = () => {};
  window.dispatchEvent = () => true;
  window.getComputedStyle = () => ({ getPropertyValue: () => "", color: "rgb(0, 0, 0)", width: "100px", height: "20px" });
  window.matchMedia = () => ({ matches: false, media: "", addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
  window.innerWidth = 1440;
  window.innerHeight = 734;
  window.outerWidth = 1440;
  window.outerHeight = 821;
  window.devicePixelRatio = 2;
  window.scrollTo = () => {};
  window.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
  window.cancelAnimationFrame = (id) => clearTimeout(id);
  window.requestIdleCallback = (cb) => setTimeout(() => cb({ timeRemaining: () => 50, didTimeout: false }), 0);

  global.ddoptions = { endpoint: "https://ddbm2.paypal.com/js/", testingMode: true };
  global.ddjskey = String(cfg.ddjsKey || "2D56F91C2AD1A8EB7C6A5CA65F5567");

  eval(tagsJs);

  setTimeout(() => {
    dispatch("mousemove");
    dispatch("pointermove");
    dispatch("click");
    dispatch("scroll");
    dispatch("keydown");
    dispatch("keyup");
  }, 100);
  setTimeout(() => {
    dispatch("mousemove");
    dispatch("click");
    dispatch("scroll");
  }, 1200);
  setTimeout(() => {
    dispatch("beforeunload");
    dispatch("pagehide");
  }, 2500);
  setTimeout(() => {
    const body = posted.find((x) => x.body && x.body.includes("jspl="))?.body || "";
    console.log(JSON.stringify({ ok: !!body, body, body_len: body.length }));
    process.exit(0);
  }, 4200);
}

main().catch((e) => {
  console.log(JSON.stringify({ ok: false, error: String(e && e.stack || e) }));
  process.exit(0);
});
