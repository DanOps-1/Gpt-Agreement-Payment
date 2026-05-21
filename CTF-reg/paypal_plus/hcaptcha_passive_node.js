#!/usr/bin/env node
/*
 * Protocol-only PayPal hCaptcha passive token helper.
 *
 * This intentionally does not drive paypal.com pages.  It loads only the
 * paypalobjects hcaptchapassive_eval.html bridge in happy-dom, lets the
 * official hCaptcha passive JS run, and returns the postMessage token emitted
 * by PayPal's bridge:
 *
 *   { token, renderData }
 *
 * Input is JSON on stdin:
 *   {
 *     "iframeUrl": "https://www.paypalobjects.com/.../hcaptchapassive_eval.html?...",
 *     "parentUrl": "https://www.paypal.com/checkoutweb/signup?...",
 *     "userAgent": "...",
 *     "timeoutMs": 60000
 *   }
 */

const fs = require('fs');
const http = require('http');
const https = require('https');

function loadHappyDOM() {
  const candidates = [
    'happy-dom',
    '/app/webui/frontend/node_modules/happy-dom',
    '/root/Gpt-Agreement-Payment/webui/frontend/node_modules/happy-dom'
  ];
  let lastErr = null;
  for (const name of candidates) {
    try {
      return require(name);
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr || new Error('happy-dom not found');
}

const { Window } = loadHappyDOM();

const DEFAULT_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ' +
  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36';

function readStdin() {
  return fs.readFileSync(0, 'utf8');
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function fetchText(url, userAgent, redirects = 0) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const mod = u.protocol === 'http:' ? http : https;
    const req = mod.get(
      u,
      {
        headers: {
          'user-agent': userAgent || DEFAULT_UA,
          'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
          'accept-language': 'en-US,en;q=0.9',
          'referer': 'https://www.paypal.com/',
        },
      },
      (res) => {
        const status = res.statusCode || 0;
        const loc = res.headers.location;
        if (status >= 300 && status < 400 && loc && redirects < 5) {
          res.resume();
          fetchText(new URL(loc, url).toString(), userAgent, redirects + 1).then(resolve, reject);
          return;
        }
        const chunks = [];
        res.on('data', (d) => chunks.push(Buffer.from(d)));
        res.on('end', () => {
          if (status < 200 || status >= 300) {
            reject(new Error(`GET ${url} status=${status}`));
            return;
          }
          resolve(Buffer.concat(chunks).toString('utf8'));
        });
      }
    );
    req.on('error', reject);
    req.setTimeout(30000, () => {
      req.destroy(new Error(`GET ${url} timeout`));
    });
  });
}

function define(obj, key, value) {
  try {
    Object.defineProperty(obj, key, {
      configurable: true,
      enumerable: true,
      get: typeof value === 'function' ? value : () => value,
    });
  } catch (_) {
    try {
      obj[key] = typeof value === 'function' ? value() : value;
    } catch (_) {}
  }
}

function patchCanvas(win) {
  try {
    if (!win.HTMLCanvasElement || !win.HTMLCanvasElement.prototype) return;
    win.HTMLCanvasElement.prototype.getContext = function getContext(type) {
      const ctx = {
        canvas: this,
        fillRect() {},
        clearRect() {},
        getImageData() { return { data: new win.Uint8ClampedArray(4) }; },
        putImageData() {},
        createImageData() { return { data: new win.Uint8ClampedArray(4) }; },
        setTransform() {},
        resetTransform() {},
        drawImage() {},
        save() {},
        restore() {},
        beginPath() {},
        closePath() {},
        moveTo() {},
        lineTo() {},
        bezierCurveTo() {},
        quadraticCurveTo() {},
        arc() {},
        rect() {},
        clip() {},
        stroke() {},
        fill() {},
        fillText() {},
        strokeText() {},
        translate() {},
        scale() {},
        rotate() {},
        measureText() { return { width: 42 }; },
        transform() {},
        createLinearGradient() { return { addColorStop() {} }; },
        createPattern() { return null; },
        getExtension() { return null; },
        getParameter() {
          if (type && String(type).toLowerCase().includes('webgl')) {
            return '';
          }
          return '';
        },
      };
      return ctx;
    };
    win.HTMLCanvasElement.prototype.toDataURL = function toDataURL() {
      return 'data:image/png;base64,iVBORw0KGgo=';
    };
  } catch (_) {}
}

function patchNavigator(win, userAgent) {
  const nav = win.navigator;
  try { nav.userAgent = userAgent; } catch (_) {}
  define(nav, 'userAgent', userAgent);
  define(nav, 'appCodeName', 'Mozilla');
  define(nav, 'appName', 'Netscape');
  define(nav, 'appVersion', userAgent.replace(/^Mozilla\//, ''));
  define(nav, 'platform', 'Win32');
  define(nav, 'vendor', 'Google Inc.');
  define(nav, 'language', 'en-US');
  define(nav, 'languages', ['en-US', 'en']);
  define(nav, 'webdriver', undefined);
  define(nav, 'deviceMemory', 8);
  define(nav, 'hardwareConcurrency', 8);
  define(nav, 'maxTouchPoints', 0);
  define(nav, 'cookieEnabled', true);
  define(nav, 'onLine', true);
  try {
    define(nav, 'plugins', [
      { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    ]);
    define(nav, 'mimeTypes', [
      { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
    ]);
  } catch (_) {}
}

function patchWindow(win, userAgent, parentUrl, parentPostMessage) {
  if (!win || win.__pps_hcap_patched) return;
  try {
    Object.defineProperty(win, '__pps_hcap_patched', { value: true, configurable: true });
  } catch (_) {
    win.__pps_hcap_patched = true;
  }
  patchNavigator(win, userAgent);
  patchCanvas(win);
  try {
    win.screen.width = 1440;
    win.screen.height = 900;
    win.screen.availWidth = 1440;
    win.screen.availHeight = 860;
    win.screen.colorDepth = 24;
    win.screen.pixelDepth = 24;
  } catch (_) {}
  try { win.outerWidth = 1440; win.outerHeight = 900; win.innerWidth = 1440; win.innerHeight = 860; } catch (_) {}
  try { define(win.document, 'hidden', false); define(win.document, 'visibilityState', 'visible'); } catch (_) {}
  try { Object.defineProperty(win.document, 'referrer', { value: parentUrl || 'https://www.paypal.com/', configurable: true }); } catch (_) {}
  try { Object.defineProperty(win.location, 'ancestorOrigins', { value: ['https://www.paypal.com'], configurable: true }); } catch (_) {}
  try {
    win.matchMedia = win.matchMedia || function matchMedia(query) {
      return { matches: /landscape/.test(String(query)), media: query, onchange: null, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; } };
    };
  } catch (_) {}
  try {
    if (parentPostMessage) {
      Object.defineProperty(win, 'parent', { configurable: true, value: { postMessage: parentPostMessage } });
    }
  } catch (_) {}
  try {
    win.console = {
      log: (...args) => console.error('[hcap]', ...args),
      info: (...args) => console.error('[hcap]', ...args),
      warn: (...args) => console.error('[hcap:warn]', ...args),
      error: (...args) => console.error('[hcap:error]', ...args),
      debug: () => {},
    };
  } catch (_) {}
}

function parseMessage(data) {
  try {
    return typeof data === 'string' ? JSON.parse(data) : data;
  } catch (_) {
    return { raw: String(data || '') };
  }
}

(async () => {
  const input = JSON.parse(readStdin() || '{}');
  const iframeUrl = String(input.iframeUrl || input.iframe_url || '').trim();
  if (!iframeUrl) throw new Error('iframeUrl is required');
  const parentUrl = String(input.parentUrl || input.parent_url || 'https://www.paypal.com/').trim();
  const userAgent = String(input.userAgent || input.user_agent || DEFAULT_UA);
  const timeoutMs = Math.max(10000, Number(input.timeoutMs || input.timeout_ms || 60000));
  const startedAt = Date.now();
  const messages = [];
  let resolvedToken = '';
  let resolvedRenderData = {};
  let terminalError = '';

  const parentPostMessage = (data, targetOrigin) => {
    const msg = parseMessage(data);
    messages.push({
      t: Date.now(),
      targetOrigin: String(targetOrigin || ''),
      msg,
    });
    if (msg && msg.log && msg.captchaState) {
      console.error('[hcap-state]', msg.captchaState);
    }
    const token = msg && typeof msg.token === 'string' ? msg.token : '';
    if (token) {
      if (token === 'NOT_REACHABLE' || token === 'RENDER_FAILURE' || token === 'EMPTY_TOKEN') {
        terminalError = token;
      } else {
        resolvedToken = token;
        resolvedRenderData = msg.renderData || {};
      }
    }
  };

  const win = new Window({
    url: iframeUrl,
    width: 1440,
    height: 900,
    settings: {
      disableJavaScriptEvaluation: false,
      disableJavaScriptFileLoading: false,
      disableCSSFileLoading: false,
      disableIframePageLoading: false,
      fetch: { disableSameOriginPolicy: true },
      navigation: { crossOriginPolicy: 'anyOrigin' },
      navigator: { userAgent, maxTouchPoints: 0 },
      timer: { maxTimeout: Math.max(timeoutMs + 5000, 65000), maxIntervalIterations: 100000 },
    },
  });

  patchWindow(win, userAgent, parentUrl, parentPostMessage);

  const html = String(input.html || '') || await fetchText(iframeUrl, userAgent);
  win.document.write(html);
  win.document.close();

  const patchTimer = setInterval(() => {
    try {
      patchWindow(win, userAgent, parentUrl, parentPostMessage);
      for (const iframe of Array.from(win.document.querySelectorAll('iframe'))) {
        try {
          if (iframe.contentWindow) {
            patchWindow(iframe.contentWindow, userAgent, iframeUrl, null);
          }
        } catch (_) {}
      }
    } catch (_) {}
  }, 100);

  try {
    await win.happyDOM.waitUntilComplete({ timeout: Math.min(timeoutMs, 60000) }).catch(() => {});
    while (Date.now() - startedAt < timeoutMs) {
      if (resolvedToken) break;
      if (terminalError === 'NOT_REACHABLE' || terminalError === 'RENDER_FAILURE') break;
      await sleep(250);
    }
  } finally {
    clearInterval(patchTimer);
  }

  const iframeSrcs = [];
  try {
    for (const iframe of Array.from(win.document.querySelectorAll('iframe'))) {
      iframeSrcs.push(String(iframe.src || '').slice(0, 300));
    }
  } catch (_) {}

  const states = messages
    .map((m) => m.msg && m.msg.captchaState)
    .filter(Boolean);
  const out = {
    ok: Boolean(resolvedToken),
    token: resolvedToken,
    renderData: resolvedRenderData,
    error: resolvedToken ? '' : (terminalError || 'timeout/no_token'),
    elapsedMs: Date.now() - startedAt,
    states,
    iframeCount: iframeSrcs.length,
    iframeSrcs,
  };
  process.stdout.write(JSON.stringify(out));
  process.exit(resolvedToken ? 0 : 2);
})().catch((err) => {
  process.stdout.write(JSON.stringify({
    ok: false,
    error: err && (err.stack || err.message) || String(err),
  }));
  process.exit(1);
});
