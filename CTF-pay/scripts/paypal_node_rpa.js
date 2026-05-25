#!/usr/bin/env node
/*
 * PayPal temporary-account checkout RPA.
 *
 * This intentionally keeps the PayPal part inside a real Chromium page so
 * PayPal's own checkout JS/FraudNet/DataDome/recaptcha/passive telemetry can
 * run in one continuous browser context.  The Python caller only supplies the
 * Stripe->PayPal redirect URL and the same values used by the userscript v32.
 *
 * stdin:  JSON payload
 * stdout: JSON result
 * stderr: human log lines
 */

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

// When multiple workers run concurrently, add worker_id to /tmp/paypal_node_rpa_* filename to prevent conflicts
// Python side exports NCPP_WORKER_ID on spawn; single worker defaults to empty, path remains backward compatible.
const _WORKER_ID = (process.env.NCPP_WORKER_ID || '').trim().replace(/[^A-Za-z0-9_\-]/g, '');
const T_BASE = `/tmp/paypal_node_rpa${_WORKER_ID ? '_' + _WORKER_ID : ''}`;
const T = (suffix) => `${T_BASE}_${suffix}`;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Phone OTP critical section coordination (concurrent workers share same phone). Via webui parallel_runner
// HTTP lock; when env is missing (single worker / CLI) it's a no-op to maintain backward compatibility.
const PHONE_LOCK_URL = (process.env.NCPP_PHONE_LOCK_URL || '').trim();

async function tryAcquirePhoneLock(phone, workerId) {
  if (!PHONE_LOCK_URL || !phone || !workerId) return { ok: true, noop: true };
  const url = `${PHONE_LOCK_URL}/acquire?phone=${encodeURIComponent(phone)}&worker=${encodeURIComponent(workerId)}`;
  try {
    const r = await fetch(url, { method: 'POST', signal: AbortSignal.timeout(5000) });
    if (r.status === 200) return { ok: true };
    let body = null;
    try { body = await r.json(); } catch (_) { body = null; }
    return { ok: false, status: r.status, body };
  } catch (e) {
    return { ok: false, error: e && e.message ? e.message : String(e) };
  }
}

async function waitAcquirePhoneLock(phone, workerId, timeoutMs = 600000) {
  if (!PHONE_LOCK_URL || !phone || !workerId) return true;
  const deadline = Date.now() + timeoutMs;
  let lastHolder = '';
  let attempts = 0;
  while (Date.now() < deadline) {
    attempts++;
    const r = await tryAcquirePhoneLock(phone, workerId);
    if (r.ok) {
      if (!r.noop && attempts > 1) {
        log('phone-lock acquired after wait', phone, `worker=${workerId}`, `attempts=${attempts}`);
      } else if (!r.noop) {
        log('phone-lock acquired', phone, `worker=${workerId}`);
      }
      return true;
    }
    const holder = (r.body && r.body.detail && r.body.detail.holder) || (r.body && r.body.holder) || '';
    if (holder && holder !== lastHolder) {
      lastHolder = holder;
      log('phone-lock busy, waiting', phone, `holder=${holder}`);
    }
    await sleep(750);
  }
  log('phone-lock acquire TIMEOUT', phone, `worker=${workerId}`);
  return false;
}

async function releasePhoneLock(phone, workerId) {
  if (!PHONE_LOCK_URL || !phone || !workerId) return;
  try {
    await fetch(
      `${PHONE_LOCK_URL}/release?phone=${encodeURIComponent(phone)}&worker=${encodeURIComponent(workerId)}`,
      { method: 'POST', signal: AbortSignal.timeout(3000) },
    );
    log('phone-lock released', phone, `worker=${workerId}`);
  } catch (e) {
    log('phone-lock release error', e && e.message ? e.message : e);
  }
}

function log(...args) {
  const line = `[node-rpa ${new Date().toISOString()}] ${args.join(' ')}`;
  try { fs.appendFileSync(T('live.log'), `${line}\n`); } catch (_) {}
  console.error(line);
}

function saveState(state) {
  try {
    fs.writeFileSync(T('state.json'), JSON.stringify({
      ts: new Date().toISOString(),
      ...state,
    }, null, 2));
  } catch (_) {}
}

function redactSensitiveText(value) {
  return String(value || '')
    .replace(/([?&]key=)[^&\s"']+/gi, '$1<redacted>')
    .replace(/\b(?:\d[ -]?){12,19}\b/g, '<card-redacted>')
    .replace(/\b\d{6}\b/g, '<otp-redacted>')
    .replace(/\bEC-[A-Z0-9-]+\b/g, 'EC-<redacted>')
    .replace(/\bBA-[A-Z0-9-]+\b/g, 'BA-<redacted>')
    .replace(/("?(?:access_token|refresh_token|token|auth|authorization|cookie|x-paypal-internal-euat)"?\s*[:=]\s*")([^"]+)/gi, '$1<redacted>');
}

function redactHeaders(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers || {})) {
    if (/cookie|authorization|x-paypal-internal-euat/i.test(k)) {
      out[k] = `<redacted:${String(v || '').length}>`;
    } else {
      out[k] = redactSensitiveText(v);
    }
  }
  return out;
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => { data += chunk; });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

function globDirs(root, prefix) {
  try {
    return fs.readdirSync(root)
      .filter((x) => x.startsWith(prefix))
      .map((x) => path.join(root, x))
      .sort()
      .reverse();
  } catch (_) {
    return [];
  }
}

function findChromiumExecutable() {
  const candidates = [];
  if (process.env.PPS_CHROMIUM_EXECUTABLE) candidates.push(process.env.PPS_CHROMIUM_EXECUTABLE);
  for (const d of globDirs('/root/.cache/ms-playwright', 'chromium-')) {
    candidates.push(path.join(d, 'chrome-linux64', 'chrome'));
    candidates.push(path.join(d, 'chrome-linux', 'chrome'));
  }
  candidates.push('/usr/bin/google-chrome');
  candidates.push('/usr/bin/chromium');
  candidates.push('/usr/bin/chromium-browser');
  for (const c of candidates) {
    try {
      if (c && fs.existsSync(c)) return c;
    } catch (_) {}
  }
  return '';
}

function proxyForPlaywright(raw) {
  if (!raw) return undefined;
  try {
    const u = new URL(raw);
    const scheme = u.protocol.replace(':', '') === 'socks5h' ? 'socks5' : u.protocol.replace(':', '');
    const out = { server: `${scheme}://${u.hostname}:${u.port}` };
    if (u.username) out.username = decodeURIComponent(u.username);
    if (u.password) out.password = decodeURIComponent(u.password);
    return out;
  } catch (_) {
    return undefined;
  }
}

function randEmail() {
  const c = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let e = '';
  for (let i = 0; i < 16; i++) e += c[Math.floor(Math.random() * c.length)];
  return `${e}@gmail.com`;
}

function randPass() {
  const L = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ';
  const D = '0123456789';
  const S = '!@#$%^';
  const A = L + D + S;
  let p = L[Math.floor(Math.random() * 26)]
    + L[26 + Math.floor(Math.random() * 26)]
    + D[Math.floor(Math.random() * 10)]
    + S[Math.floor(Math.random() * 6)];
  for (let i = 4; i < 14; i++) p += A[Math.floor(Math.random() * A.length)];
  return p.split('').sort(() => Math.random() - 0.5).join('');
}

function normalizeExpiry(expiry) {
  const parts = String(expiry || '').replace(/\//g, ' ').split(/\s+/).filter(Boolean);
  if (parts.length >= 2) {
    const mm = parts[0].padStart(2, '0').slice(0, 2);
    const yy = parts[1].length === 4 ? parts[1].slice(-2) : parts[1].padStart(2, '0').slice(-2);
    return `${mm} / ${yy}`;
  }
  return String(expiry || '03 / 30');
}

function phoneForUi(phone) {
  let p = String(phone || '').replace(/[^\d]/g, '');
  if (p.length === 11 && p.startsWith('1')) p = p.slice(1);
  return p;
}

async function getAddress(payload) {
  const a = payload.address || {};
  if (a.line1 || a.street) {
    return {
      street: a.line1 || a.street,
      city: a.city || 'New York',
      state: a.state || 'New York',
      zip: String(a.postalCode || a.postal_code || a.zip || '10001').slice(0, 5),
    };
  }
  try {
    const r = await fetch('https://www.meiguodizhi.com/api/v1/dz', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: '/', method: 'address' }),
    });
    const d = await r.json();
    const x = d.address || d || {};
    return {
      street: x.Address || x.street || '123 Main St',
      city: x.City || x.city || 'New York',
      state: x.State_Full || x.State || x.state || 'New York',
      zip: String(x.Zip_Code || x.zip || '10001').slice(0, 5),
    };
  } catch (e) {
    log('addr fallback', e.message || e);
    return { street: '123 Main St', city: 'New York', state: 'New York', zip: '10001' };
  }
}

async function addUserscriptStyle(page) {
  try {
    await page.addStyleTag({
      content: [
        '#captcha-standalone,.captcha-overlay,.captcha-container,',
        '.AddressAutocomplete-results,[class*="AddressAutocomplete-results"]',
        '{display:none!important;height:0!important;overflow:hidden!important}',
      ].join(''),
    });
  } catch (_) {}
}

async function evalAllFrames(page, fn, arg) {
  const out = [];
  for (const frame of page.frames()) {
    try {
      out.push(await frame.evaluate(fn, arg));
    } catch (_) {
      out.push(null);
    }
  }
  return out;
}

const fillByIdScript = ({ id, val }) => {
  const candidates = [
    document.getElementById(id),
    document.querySelector(`input[name="${CSS.escape(id)}"]`),
    document.querySelector(`textarea[name="${CSS.escape(id)}"]`),
    document.querySelector(`input[autocomplete="${CSS.escape(id)}"]`),
  ].filter(Boolean);
  const el = candidates[0];
  if (!el) return false;
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  try { el.focus(); } catch (_) {}
  if (desc && desc.set) desc.set.call(el, String(val)); else el.value = String(val);
  for (const ev of ['keydown', 'input', 'keyup', 'change', 'blur']) {
    try { el.dispatchEvent(new Event(ev, { bubbles: true })); } catch (_) {}
  }
  return true;
};

const fillSelScript = ({ sel, val }) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  try { el.focus(); } catch (_) {}
  if (desc && desc.set) desc.set.call(el, String(val)); else el.value = String(val);
  for (const ev of ['keydown', 'input', 'keyup', 'change', 'blur']) {
    try { el.dispatchEvent(new Event(ev, { bubbles: true })); } catch (_) {}
  }
  return true;
};

const selectByTextScript = ({ id, text }) => {
  const el = document.getElementById(id) || document.querySelector(`select[name="${CSS.escape(id)}"]`);
  if (!el || !el.options) return false;
  const rect = el.getBoundingClientRect();
  if (el.offsetParent === null || rect.width <= 0 || rect.height <= 0) return false;
  const needle = String(text || '').toLowerCase();
  const options = Array.from(el.options)
    .map((opt) => ({
      opt,
      value: String(opt.value || ''),
      label: String(opt.text || ''),
    }))
    .filter((x) => x.value || x.label.trim());
  let chosen = options.find((x) => x.value.toLowerCase() === needle);
  if (!chosen && needle.length > 2) {
    chosen = options.find((x) => {
      const label = x.label.toLowerCase();
      return label === needle || label.includes(needle) || needle.includes(label);
    });
  }
  if (!chosen && needle.length > 2) {
    chosen = options.find((x) => `${x.label} ${x.value}`.toLowerCase().includes(needle));
  }
  if (chosen) {
    const value = chosen.value;
    try { el.focus(); } catch (_) {}
    const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
    if (desc && desc.set) desc.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return value || true;
  }
  return false;
};

async function fillAny(page, id, val) {
  if (!val) return false;
  const res = await evalAllFrames(page, fillByIdScript, { id, val });
  const ok = res.some(Boolean);
  if (ok) log('fill', id, 'ok');
  return ok;
}

async function fillSelectorAny(page, sel, val) {
  if (!val) return false;
  const res = await evalAllFrames(page, fillSelScript, { sel, val });
  const ok = res.some(Boolean);
  if (ok) log('fill', sel, 'ok');
  return ok;
}

async function selectAny(page, id, text) {
  const res = await evalAllFrames(page, selectByTextScript, { id, text });
  const ok = res.some(Boolean);
  if (ok) log('select', id, text, JSON.stringify(res.filter(Boolean)[0]));
  return ok;
}

async function clickByText(page, regex, label, timeoutMs = 1500) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const frame of page.frames()) {
      let handles = [];
      try {
        handles = await frame.$$('button, a, [role="button"], input[type="submit"], input[type="button"], span');
      } catch (_) {
        continue;
      }
      for (const h of handles) {
        try {
          const visible = await h.isVisible();
          if (!visible) continue;
          const disabled = await h.evaluate((el) => !!el.disabled || el.getAttribute('aria-disabled') === 'true');
          if (disabled) continue;
          const text = (await h.evaluate((el) => (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim())) || '';
          if (!regex.test(text)) continue;
          log('click', label || regex, JSON.stringify(text.slice(0, 80)));
          await h.click({ timeout: 5000 });
          return true;
        } catch (_) {}
      }
    }
    await sleep(250);
  }
  return false;
}

async function paypalBillingDiag(page, label = 'paypal-billing') {
  try {
    const rows = await evalAllFrames(page, () => {
      const consent = document.getElementById('consentButton')
        || document.querySelector('[data-id="consentButton"]')
        || document.querySelector('button[data-atomic-wait-intent="Approve_Billing_Agreement"]')
        || Array.from(document.querySelectorAll('button')).find((b) => /agree\s+(and|&)\s+continue/i.test((b.innerText || b.textContent || '').replace(/\s+/g, ' ').trim()));
      const addCard = document.querySelector('a[data-atomic-wait-intent="Add_New_Card"], a[aria-label="Add card"]');
      const form = consent ? consent.closest('form') : document.querySelector('form[data-testid="consent-form"]');
      const visibleButtons = Array.from(document.querySelectorAll('button, a, [role="button"]'))
        .filter((el) => {
          const r = el.getBoundingClientRect();
          return el.offsetParent !== null && r.width > 0 && r.height > 0;
        })
        .map((el) => ({
          tag: el.tagName,
          id: el.id || '',
          dataId: el.getAttribute('data-id') || '',
          intent: el.getAttribute('data-atomic-wait-intent') || '',
          text: (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 80),
          disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
        }))
        .filter((x) => x.text || x.id || x.dataId || x.intent)
        .slice(0, 12);
      return {
        href: location.href.slice(0, 180),
        body: (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 500),
        consent: consent ? {
          text: (consent.innerText || consent.textContent || consent.value || '').replace(/\s+/g, ' ').trim(),
          disabled: !!consent.disabled || consent.getAttribute('aria-disabled') === 'true',
          id: consent.id || '',
          dataId: consent.getAttribute('data-id') || '',
          intent: consent.getAttribute('data-atomic-wait-intent') || '',
          task: consent.getAttribute('data-atomic-wait-task') || '',
        } : null,
        addCard: addCard ? {
          text: (addCard.innerText || addCard.textContent || addCard.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim(),
          href: addCard.getAttribute('href') || '',
          visible: (() => {
            const r = addCard.getBoundingClientRect();
            return addCard.offsetParent !== null && r.width > 0 && r.height > 0;
          })(),
        } : null,
        form: form ? {
          method: form.getAttribute('method') || '',
          action: form.getAttribute('action') || '',
          testid: form.getAttribute('data-testid') || '',
          hiddenNames: Array.from(form.querySelectorAll('input[type="hidden"]')).map((x) => x.name || x.id || '').filter(Boolean).slice(0, 12),
        } : null,
        visibleButtons,
      };
    }, null);
    const useful = rows.filter(Boolean).filter((x) => x.consent || x.addCard || x.body);
    log('paypal billing diag', label, JSON.stringify(useful).slice(0, 1800));
  } catch (_) {}
}

async function clickPaypalConsentButton(page, label = 'paypal-consent', timeoutMs = 8000, preDelayMs = 0) {
  if (preDelayMs > 0) {
    log('paypal consent wait before click', `${preDelayMs}ms`);
    await sleep(preDelayMs);
  }
  await paypalBillingDiag(page, `${label}-before-click`);
  const selectors = [
    '#consentButton',
    '[data-id="consentButton"]',
    'button[data-atomic-wait-intent="Approve_Billing_Agreement"]',
    'button[data-atomic-wait-task="select_agree_and_continue"]',
    'button[type="submit"]',
  ];
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const frame of page.frames()) {
      for (const sel of selectors) {
        try {
          const el = await frame.$(sel);
          if (!el || !(await el.isVisible())) continue;
          const info = await el.evaluate((x) => ({
            text: (x.innerText || x.textContent || x.value || x.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim(),
            disabled: !!x.disabled || x.getAttribute('aria-disabled') === 'true',
          })).catch(() => ({ text: '', disabled: false }));
          if (info.disabled) continue;
          if (!/agree\s*(and|&)\s*continue|continue|confirm|pay/i.test(info.text || '') && !/consentButton/i.test(sel)) {
            continue;
          }
          try {
            await el.evaluate((x) => x.scrollIntoView({ block: 'center', inline: 'center' }));
          } catch (_) {}
          await sleep(350);
          const box = await el.boundingBox().catch(() => null);
          log('click', label, `${sel} ${JSON.stringify((info.text || '').slice(0, 80))}${box ? ` box=${Math.round(box.x)},${Math.round(box.y)},${Math.round(box.width)}x${Math.round(box.height)}` : ''}`);
          if (box) {
            const x = box.x + box.width / 2;
            const y = box.y + box.height / 2;
            await page.mouse.move(x, y, { steps: 8 });
            await sleep(120);
            await page.mouse.down();
            await sleep(90);
            await page.mouse.up();
          } else {
            await el.click({ timeout: 5000 });
          }
          return true;
        } catch (_) {}
      }
    }
    await sleep(300);
  }
  return clickByText(page, /^(agree[\s&]+continue|agree and continue|continue|agree|confirm|next|pay)$/i, label, 2000);
}

async function clickSelectorAny(page, selectors, label) {
  for (const frame of page.frames()) {
    for (const sel of selectors) {
      try {
        const el = await frame.$(sel);
        if (el && await el.isVisible()) {
          const disabled = await el.evaluate((x) => !!x.disabled || x.getAttribute('aria-disabled') === 'true').catch(() => false);
          if (disabled) continue;
          log('click', label || sel, sel);
          await el.click({ timeout: 5000 });
          return true;
        }
      } catch (_) {}
    }
  }
  return false;
}

async function clickPaypalInitialPayWithCard(page, label = 'paypal-pay-with-card') {
  // Successful mitm trace:
  //   POST /pay?...paypal_client_cfci=...-Pay_With_Card
  //   multipart: formName=createAccountAction
  //
  // Do NOT include legacy unified-login selectors such as #startOnboardingFlow
  // here.  They jump into /signin/onboarding and bypass the /pay UL guest state
  // machine that later makes fallback billing authorize correctly.
  return clickSelectorAny(page, [
    'button[data-atomic-wait-intent="Pay_With_Card"]',
    'form:has(input[name="formName"][value="createAccountAction"]) button[type="submit"]',
    'form:has(input[name="1_formName"][value="createAccountAction"]) button[type="submit"]',
  ], label);
}

async function clickPaypalContinueToPayment(page, label = 'paypal-continue-to-payment') {
  // Successful mitm trace:
  //   POST /pay/?...paypal_client_cfci=...-Continue_To_Payment&ctxId=...
  //   multipart: login_email=<random>, formName=createAccount
  return clickSelectorAny(page, [
    'button[data-atomic-wait-intent="Continue_To_Payment"]',
    'form:has(input[name="formName"][value="createAccount"]) button[type="submit"]',
    'form:has(input[name="1_formName"][value="createAccount"]) button[type="submit"]',
    'form[name="beginOnboardingFlow"] button[type="submit"]',
    '#onboardingFlow form button[type="submit"]',
    'form button.actionContinue[type="submit"]',
    'button.actionContinue[type="submit"]',
  ], label)
    || clickByText(
      page,
      /^(continue|next|create an account|sign up|支払いを続ける|続行|次へ|アカウントを開設する|アカウントを作成する|创建账户|创建.*[账帐][户号]|注册)$/i,
      label,
      1500,
    );
}

async function clickPaypalLegacyStartOnboarding(page, label = 'paypal-legacy-start-onboarding') {
  return clickSelectorAny(page, ['#startOnboardingFlow'], label)
    || clickByText(page, /^(create an account|sign up|アカウントを開設する|アカウントを作成する|创建账户|创建.*[账帐][户号]|注册)$/i, label, 1000);
}

function extractPaypalBaToken(rawUrl) {
  try {
    const u = new URL(rawUrl);
    return u.searchParams.get('ba_token') || u.searchParams.get('token') || '';
  } catch (_) {
    const m = String(rawUrl || '').match(/\b(?:ba_token|token)=(BA-[A-Z0-9-]+)/i);
    return m ? m[1] : '';
  }
}

async function forcePaypalPayRouteFromBa(page, rawUrl) {
  const ba = extractPaypalBaToken(rawUrl);
  if (!/^BA-[A-Z0-9-]+$/i.test(ba || '')) return false;
  const ssrt = Date.now();
  const target = `https://www.paypal.com/pay?ssrt=${ssrt}&token=${encodeURIComponent(ba)}&ul=1`;
  log('paypal legacy /agreements page; force IWC /pay guest route', target.replace(/BA-[A-Z0-9-]+/i, 'BA-<redacted>'));
  try {
    await page.goto(target, { waitUntil: 'commit', timeout: 60000, referer: rawUrl });
  } catch (e) {
    log('paypal force /pay route goto warning', e && e.message ? e.message : e);
  }
  await sleep(2500);
  return true;
}

function pickFirst(text, patterns) {
  for (const pat of patterns) {
    const m = String(text || '').match(pat);
    if (m && m[1]) return m[1];
  }
  return '';
}

async function paypalAuthorizeFromBillingRuntime(page, label = 'paypal-runtime-authorize') {
  // On the fallback billing page the visible "Agree and Continue" button is
  // supposed to issue POST /graphql/ operationName=authorize.  On some PayPal
  // edges the form can submit before the JS handler wins, landing on
  // /pay/generic-error INVALID_REQUEST.  Recreate the exact same same-origin
  // fetch from the hydrated page data before falling back to a physical click.
  const html = (await page.content().catch(() => '')).replace(/&quot;/g, '"');
  const runtime = {
    href: page.url(),
    ecToken: pickFirst(html, [
      /\\"ecToken\\"\s*:\s*\\"(EC-[A-Z0-9-]+)\\"/i,
      /"ecToken"\s*:\s*"(EC-[A-Z0-9-]+)"/i,
      /[?&]token=(EC-[A-Z0-9-]+)/i,
    ]),
    clientMetadataId: pickFirst(html, [
      /\\"clientMetadataId\\"\s*:\s*\\"([0-9a-f-]{36})\\"/i,
      /"clientMetadataId"\s*:\s*"([0-9a-f-]{36})"/i,
    ]),
    euat: pickFirst(html, [
      /\\"x-paypal-internal-euat\\"\s*:\s*\\"([^"\\]+)\\"/i,
      /"x-paypal-internal-euat"\s*:\s*"([^"]+)"/i,
    ]),
    nsid: pickFirst(html, [
      /\\"PayPal-Nsid\\"\s*:\s*\\"([^"\\]+)\\"/i,
      /"PayPal-Nsid"\s*:\s*"([^"]+)"/i,
    ]),
  };

  const ecToken = runtime.ecToken || pickFirst(runtime.href || '', [/[?&]token=(EC-[A-Z0-9-]+)/i]);
  if (!ecToken || !runtime.euat || !runtime.clientMetadataId) {
    log(label, 'missing runtime fields',
      `ec=${!!ecToken}`,
      `euat=${runtime.euat ? runtime.euat.length : 0}`,
      `cmid=${!!runtime.clientMetadataId}`);
    return { ok: false, error: 'missing_runtime_fields' };
  }

  const query = `mutation authorize($billingAgreementId: String!, $addressId: String, $fundingPreference: billingFundingPreferenceInput, $legalAgreements: billingLegalAgreementsInput) {
  billing {
    authorize(
      billingAgreementId: $billingAgreementId
      addressId: $addressId
      fundingPreference: $fundingPreference
      legalAgreements: $legalAgreements
    ) {
      billingAgreementToken
      paymentAction
      returnURL {
        href
        __typename
      }
      buyer {
        userId
        __typename
      }
      __typename
    }
    __typename
  }
}
`;
  const req = {
    ecToken,
    clientMetadataId: runtime.clientMetadataId,
    euat: runtime.euat,
    nsid: runtime.nsid || '',
    query,
  };
  log(label, 'POST /graphql authorize',
    `ec=${ecToken.replace(/EC-[A-Z0-9-]+/i, 'EC-<redacted>')}`,
    `cmid=${runtime.clientMetadataId}`,
    `euat_len=${runtime.euat.length}`);

  const res = await page.evaluate(async (x) => {
    const headers = {
      accept: '*/*',
      'content-type': 'application/json',
      'x-app-name': 'checkoutuinodeweb',
      'x-requested-with': 'fetch',
      'paypal-client-metadata-id': x.clientMetadataId,
      'x-paypal-internal-euat': x.euat,
    };
    if (x.nsid) headers['PayPal-Nsid'] = x.nsid;
    const body = JSON.stringify([{
      operationName: 'authorize',
      variables: {
        billingAgreementId: x.ecToken,
        fundingPreference: { balancePreference: 'OPT_OUT' },
        legalAgreements: {},
      },
      query: x.query,
    }]);
    const r = await fetch('/graphql/', {
      method: 'POST',
      credentials: 'include',
      headers,
      body,
    });
    return { status: r.status, text: await r.text() };
  }, req).catch((e) => ({ status: 0, text: String(e && e.message || e) }));

  let parsed = null;
  try { parsed = JSON.parse(res.text); } catch (_) {}
  const returnUrl = Array.isArray(parsed)
    ? (parsed[0]?.data?.billing?.authorize?.returnURL?.href || '')
    : '';
  if (returnUrl) {
    log(label, 'authorize ok; goto return url', returnUrl.slice(0, 160));
    try {
      await page.goto(returnUrl, { waitUntil: 'commit', timeout: 60000 });
    } catch (e) {
      log(label, 'return goto warning', e && e.message ? e.message : e);
    }
    return { ok: true, returnUrl, status: res.status };
  }
  const errText = redactSensitiveText(res.text || '').slice(0, 1000);
  log(label, 'authorize failed', `status=${res.status}`, errText);
  return { ok: false, error: errText || `status=${res.status}`, status: res.status };
}

async function clickSubmitLike(page) {
  if (await clickSelectorAny(page, [
    'button[data-testid="submit-button"]',
    'button[data-testid="hosted-payment-submit-button"]',
    'button[data-atomic-wait-intent="Submit_Email"]',
    'button.SubmitButton--complete',
    '#submitButton',
    '#continue',
  ], 'submit-selector')) return true;
  return clickByText(page, /^(下一页|next|subscribe|pay|continue|agree|agree and continue|verify|confirm|create account|agree\s*&\s*continue)$/i, 'submit-text', 3000);
}

async function getOtp(smsApiUrl, timeoutMs, baselineText = '', opts = {}) {
  const deadline = Date.now() + timeoutMs;
  const shouldAbort = typeof opts.shouldAbort === 'function' ? opts.shouldAbort : () => false;
  let attempt = 0;
  while (Date.now() < deadline) {
    if (shouldAbort()) {
      log('sms abort: datadome captcha blocked PayPal from dispatching SMS');
      return '__ABORTED__';
    }
    attempt++;
    try {
      // Node global fetch has no default timeout, must explicitly use AbortSignal.timeout otherwise sms
      // gateway occasionally hangs connection causing entire RPA deadlock (5+ minutes with no new events after OTP modal appears).
      const r = await fetch(smsApiUrl, { method: 'GET', signal: AbortSignal.timeout(10000) });
      const t = (await r.text()).trim();
      log('sms attempt', attempt, t.slice(0, 80).replace(/[0-9]{6}/g, '******'));
      if (baselineText && t === baselineText) {
        await sleep(3000);
        continue;
      }
      if (t.startsWith('yes|')) {
        const m = t.match(/PayPal[^0-9]*(\d{6})/i) || t.match(/\b(\d{6})\b/);
        if (m) return m[1];
      }
    } catch (e) {
      log('sms error', e.message || e);
    }
    await sleep(3000);
  }
  return '';
}

const findOtpScript = () => {
  function find(root) {
    const single = root.querySelector('input[autocomplete="one-time-code"]')
      || root.querySelector('input[name="otp"]')
      || root.querySelector('input[name="code"]')
      || root.querySelector('input[type="tel"][maxlength="6"]');
    if (single) return { type: 'single', count: 1 };
    const ml1 = root.querySelectorAll('input[maxlength="1"]');
    if (ml1.length >= 6) return { type: 'grid', count: ml1.length };
    const ins = Array.from(root.querySelectorAll('input')).filter((x) => {
      const r = x.getBoundingClientRect();
      return x.offsetParent !== null && r.width >= 10 && r.width <= 100 && r.height >= 10 && r.height <= 100;
    });
    if (ins.length >= 6) return { type: 'small', count: ins.length };
    return null;
  }
  return find(document);
};

const fillOtpScript = (code) => {
  function inputs() {
    const single = document.querySelector('input[autocomplete="one-time-code"]')
      || document.querySelector('input[name="otp"]')
      || document.querySelector('input[name="code"]')
      || document.querySelector('input[type="tel"][maxlength="6"]');
    if (single) return { type: 'single', inputs: [single] };
    const ml1 = Array.from(document.querySelectorAll('input[maxlength="1"]'));
    if (ml1.length >= 6) return { type: 'grid', inputs: ml1.slice(0, 6) };
    const small = Array.from(document.querySelectorAll('input')).filter((x) => {
      const r = x.getBoundingClientRect();
      return x.offsetParent !== null && r.width >= 10 && r.width <= 100 && r.height >= 10 && r.height <= 100;
    });
    if (small.length >= 6) return { type: 'small', inputs: small.slice(0, 6) };
    return null;
  }
  const found = inputs();
  if (!found) return false;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  if (found.type === 'single') {
    const el = found.inputs[0];
    el.focus();
    setter.call(el, code);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }
  for (let i = 0; i < 6; i++) {
    const el = found.inputs[i];
    el.focus();
    setter.call(el, code[i]);
    el.dispatchEvent(new KeyboardEvent('keydown', { key: code[i], bubbles: true }));
    try { el.dispatchEvent(new InputEvent('input', { bubbles: true, data: code[i], inputType: 'insertText' })); }
    catch (_) { el.dispatchEvent(new Event('input', { bubbles: true })); }
    el.dispatchEvent(new KeyboardEvent('keyup', { key: code[i], bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  found.inputs[5].blur();
  return true;
};

async function hasOtp(page) {
  const res = await evalAllFrames(page, findOtpScript, null);
  return res.find(Boolean) || null;
}

async function fillOtp(page, code) {
  const res = await evalAllFrames(page, fillOtpScript, code);
  return res.some(Boolean);
}

async function pageSnapshot(page, outPrefix) {
  // Add timeout to screenshot / content to prevent chromium main thread being blocked by hcaptcha
  // iframe + PerimeterX EvalError, causing fullPage to never return → entire RPA deadlock.
  const withTimeout = (promise, ms, fallback) =>
    Promise.race([
      promise,
      new Promise((resolve) => setTimeout(() => resolve(fallback), ms)),
    ]);
  try {
    await withTimeout(
      page.screenshot({ path: `${outPrefix}.png`, fullPage: true, timeout: 8000 }),
      10000,
      null,
    );
  } catch (_) {}
  try {
    const html = await withTimeout(page.content(), 8000, "");
    if (html) fs.writeFileSync(`${outPrefix}.html`, html);
  } catch (_) {}
}

function isSuccessUrl(url) {
  return /pm-redirects\.stripe\.com\/return\//i.test(url)
    || /returned_from_redirect=true/i.test(url)
    || /redirect_status=succeeded/i.test(url)
    || /chatgpt\.com\/payments\/success/i.test(url)
    || /pay\.openai\.com\/c\/pay\/.*returned_from_redirect=true/i.test(url);
}

async function maybeDismiss(page) {
  await clickByText(page, /^(accept all|accept|got it|ok|同意|接受)$/i, 'dismiss', 500).catch(() => false);
}

async function stripeFieldVisible(page) {
  return evalAllFrames(page, () => {
    const selectors = [
      '#billingAddressLine1',
      '#billingLocality',
      '#billingPostalCode',
      '#billingAdministrativeArea',
      '#billingCountry',
      '[data-testid="paypal-accordion-item-button"]',
      '.paypal-accordion-item button',
      '#termsOfServiceConsentCheckbox',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (el.offsetParent !== null && r.width > 0 && r.height > 0) return true;
    }
    return false;
  }, null).then((xs) => xs.some(Boolean)).catch(() => false);
}

async function stripeClickManualAddress(page) {
  const clicked = await evalAllFrames(page, () => {
    const nodes = document.querySelectorAll('button, a, [role="button"], span');
    for (const n of nodes) {
      const t = (n.textContent || '').replace(/\s+/g, ' ').trim();
      if (/^enter address manually$/i.test(t) && n.offsetParent !== null) {
        n.click();
        return true;
      }
    }
    return false;
  }, null).then((xs) => xs.some(Boolean)).catch(() => false);
  if (clicked) {
    log('stripe clicked Enter address manually');
    return true;
  }
  await evalAllFrames(page, () => {
    const l1 = document.querySelector('#billingAddressLine1');
    if (l1) {
      try {
        l1.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Escape', code: 'Escape', keyCode: 27, which: 27, bubbles: true,
        }));
      } catch (_) {}
      try { l1.blur(); } catch (_) {}
    }
    return true;
  }, null).catch(() => []);
  log('stripe manual link not found; sent Escape');
  return false;
}

async function stripeCheckTerms(page) {
  for (const frame of page.frames()) {
    for (const sel of [
      '#termsOfServiceConsentCheckbox',
      'input[type="checkbox"][name*="terms" i]',
    ]) {
      try {
        const el = await frame.$(sel);
        if (!el || !(await el.isVisible())) continue;
        const checkedNow = await el.evaluate((x) => !!x.checked).catch(() => false);
        if (!checkedNow) {
          try {
            await el.click({ timeout: 5000 });
          } catch (_) {
            await el.click({ timeout: 5000, force: true });
          }
          log('stripe checkbox checked');
          return true;
        }
        return false;
      } catch (_) {}
    }
  }
  const checked = await evalAllFrames(page, () => {
    const cb = document.getElementById('termsOfServiceConsentCheckbox');
    if (cb && cb.offsetParent !== null && !cb.checked) {
      cb.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      cb.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      cb.click();
      cb.dispatchEvent(new Event('input', { bubbles: true }));
      cb.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    return false;
  }, null).then((xs) => xs.some(Boolean)).catch(() => false);
  if (checked) log('stripe checkbox checked fallback');
  return checked;
}

async function stripePaypalSelected(page) {
  return evalAllFrames(page, () => {
    const input = document.getElementById('payment-method-accordion-item-title-paypal')
      || document.querySelector('input[type="radio"][value="paypal" i]');
    return !!(input && input.checked);
  }, null).then((xs) => xs.some(Boolean)).catch(() => false);
}

async function stripeLogCheckoutState(page, label) {
  try {
    const states = await evalAllFrames(page, () => {
      const paypal = document.getElementById('payment-method-accordion-item-title-paypal');
      const card = document.getElementById('payment-method-accordion-item-title-card');
      const submit = document.querySelector('button[data-testid="hosted-payment-submit-button"]');
      const terms = document.getElementById('termsOfServiceConsentCheckbox');
      return {
        href: location.href.slice(0, 120),
        paypalChecked: !!(paypal && paypal.checked),
        cardChecked: !!(card && card.checked),
        termsChecked: !!(terms && terms.checked),
        submitText: submit ? (submit.innerText || submit.textContent || '').replace(/\s+/g, ' ').trim() : '',
        submitDisabled: !!(submit && submit.disabled),
        body: (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 500),
      };
    }, null);
    const useful = states.filter((x) => x && (x.submitText || x.body));
    log('stripe state', label, JSON.stringify(useful).slice(0, 1200));
  } catch (_) {}
}

async function stripeVisibleDue(page) {
  try {
    const states = await evalAllFrames(page, () => {
      const body = (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim();
      const m = body.match(/(?:total\s+due\s+today|due\s+today)\s+(.{0,80})/i);
      if (!m) return null;
      const raw = m[1].trim();
      const n = raw.match(/([0-9]+(?:[,.][0-9]{2})?)/);
      if (!n) return { text: raw.slice(0, 80), amountCents: null };
      const amount = Number(n[1].replace(/,/g, '.'));
      if (!Number.isFinite(amount)) return { text: raw.slice(0, 80), amountCents: null };
      return { text: raw.slice(0, 80), amountCents: Math.round(amount * 100) };
    }, null);
    return states.filter(Boolean).find((x) => x && x.amountCents !== null) || null;
  } catch (_) {
    return null;
  }
}

async function clickStripePaypal(page) {
  for (const frame of page.frames()) {
    for (const sel of [
      '#payment-method-accordion-item-title-paypal',
      'label[for="payment-method-accordion-item-title-paypal"]',
    ]) {
      try {
        const el = await frame.$(sel);
        if (!el || !(await el.isVisible())) continue;
        try {
          await el.click({ timeout: 5000 });
          await sleep(700);
          if (await stripePaypalSelected(page)) {
            log('click stripe-paypal-radio', sel);
            return true;
          }
        } catch (_) {}
        try {
          // Stripe's expanded accordion cover often intercepts pointer events
          // on the underlying radio.  A forced Playwright click still emits a
          // trusted event and matches a human clicking the PayPal row.
          await el.click({ timeout: 5000, force: true });
          await sleep(700);
          if (await stripePaypalSelected(page)) {
            log('click stripe-paypal-radio force', sel);
            return true;
          }
        } catch (_) {}
        const box = await el.boundingBox().catch(() => null);
        if (box) {
          await page.mouse.click(box.x + Math.min(Math.max(box.width / 2, 8), 40), box.y + box.height / 2);
          await sleep(700);
          if (await stripePaypalSelected(page)) {
            log('click stripe-paypal-radio mouse', sel);
            return true;
          }
        }
      } catch (_) {}
    }
  }

  const radioClicked = await evalAllFrames(page, () => {
    const input = document.getElementById('payment-method-accordion-item-title-paypal')
      || document.querySelector('input[type="radio"][value="paypal" i]')
      || Array.from(document.querySelectorAll('input[type="radio"]')).find((el) => {
        const label = document.querySelector(`label[for="${CSS.escape(el.id || '')}"]`);
        const txt = `${el.value || ''} ${label ? (label.innerText || label.textContent || '') : ''}`.toLowerCase();
        return txt.includes('paypal');
      });
    if (input) {
      const r = input.getBoundingClientRect();
      if (input.offsetParent !== null && r.width > 0 && r.height > 0) {
        try { input.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
        try { input.click(); } catch (_) {}
        try { input.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) {}
        try { input.dispatchEvent(new Event('change', { bubbles: true })); } catch (_) {}
        return true;
      }
    }
    const nodes = Array.from(document.querySelectorAll('button, label, div, span, [role="button"]'));
    for (const n of nodes) {
      const t = (n.innerText || n.textContent || n.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
      if (!/paypal/i.test(t)) continue;
      const r = n.getBoundingClientRect();
      if (n.offsetParent !== null && r.width > 0 && r.height > 0) {
        try { n.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
        try { n.click(); return true; } catch (_) {}
      }
    }
    return false;
  }, null).then((xs) => xs.some(Boolean)).catch(() => false);
  if (radioClicked && await stripePaypalSelected(page)) {
    log('click stripe-paypal-radio fallback');
    await sleep(500);
    return true;
  }

  const clicked = await clickSelectorAny(page, [
    '[data-testid="paypal-accordion-item-button"]',
    '.paypal-accordion-item button',
    'button[aria-label*="PayPal" i]',
    '[role="button"][aria-label*="PayPal" i]',
  ], 'stripe-paypal-accordion');
  if (clicked && await stripePaypalSelected(page)) {
    // userscript v32 clicks twice; keep the same behavior because Stripe's
    // accordion sometimes needs the second click after its internal state flip.
    await sleep(500);
    await clickSelectorAny(page, [
      '[data-testid="paypal-accordion-item-button"]',
      '.paypal-accordion-item button',
      'button[aria-label*="PayPal" i]',
      '[role="button"][aria-label*="PayPal" i]',
    ], 'stripe-paypal-accordion-again').catch(() => false);
  }
  return false;
}

async function fillStripeCheckoutLikeUserscript(page, addr, expectedDueCents = 0) {
  log('stripe checkout detected; filling userscript v32 billing fields');
  let paypalOk = await stripePaypalSelected(page);
  for (let i = 0; i < 4 && !paypalOk; i++) {
    await clickStripePaypal(page).catch(() => false);
    await sleep(700);
    paypalOk = await stripePaypalSelected(page);
  }
  if (!paypalOk) {
    await stripeLogCheckoutState(page, 'paypal-not-selected');
    return false;
  }
  await sleep(800);
  await selectAny(page, 'billingCountry', 'US');
  await selectAny(page, 'country', 'US');
  await sleep(1200);

  await fillSelectorAny(page, '#billingAddressLine1', addr.street);
  await sleep(900);
  await stripeClickManualAddress(page);
  await sleep(800);

  await fillSelectorAny(page, '#billingAddressLine1', addr.street);
  await fillSelectorAny(page, '#billingLocality', addr.city);
  await fillSelectorAny(page, '#billingPostalCode', addr.zip);
  await selectAny(page, 'billingAdministrativeArea', addr.state);
  await stripeCheckTerms(page);
  await stripeLogCheckoutState(page, 'before-submit');
  const due = await stripeVisibleDue(page);
  if (due) log('stripe visible due', JSON.stringify(due));
  if (Number(expectedDueCents || 0) === 0 && due && Number(due.amountCents || 0) > 0) {
    await pageSnapshot(page, T('due_mismatch'));
    throw new Error(`stripe_due_mismatch_before_submit amount_cents=${due.amountCents} text=${due.text}`);
  }
  await sleep(900);
  await clickSubmitLike(page);
  await sleep(3000);
  return true;
}

async function main() {
  const payload = JSON.parse(await readStdin());
  try { fs.writeFileSync(T('live.log'), ''); } catch (_) {}
  const timeoutMs = Number(payload.timeoutMs || 600000);
  const executablePath = findChromiumExecutable();
  if (!executablePath) throw new Error('Chromium executable not found; run playwright install chromium');
  const proxy = proxyForPlaywright(payload.proxy || '');
  const addr = await getAddress(payload);
  const email = payload.email || randEmail();
  const password = payload.password || randPass();
  const phone = phoneForUi(payload.phone || process.env.PPS_PAYPAL_PHONE || '');
  // When concurrent workers share same phone, use PHONE_LOCK_URL during OTP phase to queue and avoid SMS collision.
  // Single worker / no PHONE_LOCK_URL: both ensure/release are no-ops.
  const _LOCK_WID = (process.env.NCPP_WORKER_ID || '').trim();
  let phoneLockHeld = false;
  const ensurePhoneLock = async () => {
    if (phoneLockHeld || !PHONE_LOCK_URL || !_LOCK_WID) return;
    log('phone-lock acquiring (about to submit form, will trigger SMS)', phone);
    const got = await waitAcquirePhoneLock(phone, _LOCK_WID, 900000);
    phoneLockHeld = !!got;
  };
  const releaseIfHeld = async () => {
    if (!phoneLockHeld) return;
    phoneLockHeld = false;
    await releasePhoneLock(phone, _LOCK_WID);
  };
  const cardNumber = String(payload.cardNumber || '').replace(/\s+/g, '');
  const cardExpiry = normalizeExpiry(payload.cardExpiry || '03/30');
  const cardCvv = String(payload.cardCvv || '');
  const expectedDueCents = Number(payload.expectedDueCents ?? 0);
  const smsApiUrl = payload.smsApiUrl || process.env.PPS_SMS_API_URL || '';
  const fallbackConsentDelayMs = Math.max(0, Number(
    payload.fallbackConsentDelayMs
    ?? process.env.PPS_PAYPAL_FALLBACK_CONSENT_DELAY_MS
    ?? process.env.PPS_PAYPAL_FALLBACK_DELAY_MS
    ?? 12000,
  ) || 0);
  let smsBaselineText = '';
  if (smsApiUrl) {
    try {
      const r = await fetch(smsApiUrl, { method: 'GET' });
      smsBaselineText = (await r.text()).trim();
      if (smsBaselineText) log('sms baseline', smsBaselineText.slice(0, 80).replace(/[0-9]{6}/g, '******'));
    } catch (e) {
      log('sms baseline error', e.message || e);
    }
  }
  const headless = !!payload.headless;
  const profileDir = payload.profileDir || T(`${Date.now()}`);
  fs.mkdirSync(profileDir, { recursive: true });

  log('launch chromium', executablePath, `headless=${headless}`, proxy ? `proxy=${proxy.server}` : 'proxy=none');
  const browser = await chromium.launchPersistentContext(profileDir, {
    executablePath,
    headless,
    proxy,
    ignoreDefaultArgs: ['--enable-automation'],
    viewport: { width: 1440, height: 900 },
    locale: 'en-US',
    timezoneId: 'America/Chicago',
    userAgent: payload.userAgent || undefined,
    args: [
      '--disable-blink-features=AutomationControlled',
      '--disable-dev-shm-usage',
      '--no-sandbox',
      '--disable-infobars',
      '--window-size=1440,900',
    ],
    ignoreHTTPSErrors: true,
  });
  await browser.addInitScript(() => {
    try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (_) {}
    try { window.chrome = window.chrome || { runtime: {} }; } catch (_) {}
  });
  const page = browser.pages()[0] || await browser.newPage();
  let capturedReturnUrl = '';
  let decisiveError = '';
  let ccLinkedToFullAccount = false;
  let dataDomeBlocked = false;
  let cardAccountCandidateInvalid = false;
  const authorizeCapturePath = T('authorize_capture.json');
  const authorizeCaptures = [];
  const flushAuthorizeCaptures = () => {
    try {
      fs.writeFileSync(authorizeCapturePath, JSON.stringify({
        ts: new Date().toISOString(),
        captures: authorizeCaptures,
      }, null, 2));
    } catch (_) {}
  };
  page.on('request', (req) => {
    const u = req.url();
    if (/pm-redirects\.stripe\.com\/return\//i.test(u)) {
      capturedReturnUrl = u;
      log('captured return', u.slice(0, 180));
    }
    if (/paypal\.com\/graphql\/?/i.test(u)) {
      const postData = req.postData() || '';
      if (/authorize/i.test(postData) || /operationName["']?\s*:\s*["']authorize["']/i.test(postData)) {
        authorizeCaptures.push({
          phase: 'request',
          ts: new Date().toISOString(),
          url: u,
          method: req.method(),
          headers: redactHeaders(req.headers()),
          postData: redactSensitiveText(postData).slice(0, 10000),
        });
        flushAuthorizeCaptures();
        log('captured authorize request', `method=${req.method()}`, `url=${u.slice(0, 180)}`);
      }
    }
  });
  page.on('response', async (res) => {
    try {
      const req = res.request();
      const u = res.url();
      if (/paypal\.com\/graphql\/?/i.test(u)) {
        const postData = req.postData() || '';
        if (/authorize/i.test(postData) || /operationName["']?\s*:\s*["']authorize["']/i.test(postData)) {
          const body = await res.text().catch(() => '');
          authorizeCaptures.push({
            phase: 'response',
            ts: new Date().toISOString(),
            url: u,
            status: res.status(),
            statusText: res.statusText(),
            requestHeaders: redactHeaders(req.headers()),
            responseHeaders: redactHeaders(res.headers()),
            postData: redactSensitiveText(postData).slice(0, 10000),
            responseBody: redactSensitiveText(body).slice(0, 20000),
          });
          flushAuthorizeCaptures();
          log('captured authorize response', `status=${res.status()}`, `url=${u.slice(0, 180)}`);
        }
      }
    } catch (e) {
      log('authorize capture error', e && e.message ? e.message : e);
    }
  });
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) log('nav', page.url().slice(0, 180));
  });
  page.on('console', (msg) => {
    const t = msg.text();
    if (/INSTRUMENT_SHARING_LIMIT_EXCEEDED|CARD_GENERIC_ERROR|ISSUER_DECLINE|generic-error/i.test(t) && !decisiveError) {
      decisiveError = t.slice(0, 500);
    }
    if (/CC_LINKED_TO_FULL_ACCOUNT/i.test(t)) {
      ccLinkedToFullAccount = true;
      if (!decisiveError) decisiveError = 'CC_LINKED_TO_FULL_ACCOUNT';
    }
    // PayPal createMember validation rejection, usually persona/card combo hits risk control rules;
    // retry submit is useless (infinite same GraphQL fail), exit early to let upper layer re-roll persona.
    if (/CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR/i.test(t)) {
      cardAccountCandidateInvalid = true;
      if (!decisiveError) decisiveError = 'CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR';
    }
    // DataDome slider timeout / active rejection → PayPal won't actually send SMS, exit early to avoid wasting 4 minutes.
    // slider_timeout is PayPal's t.paypal.com/ts beacon URL, appears in fetch CORS error text.
    if (/event_name=slider_timeout|datadome.*?(?:blocked|denied|captcha_failed)/i.test(t)) {
      dataDomeBlocked = true;
      if (!decisiveError) decisiveError = 'paypal_datadome_blocked';
    }
    if (/\[PP\]|captcha|paypal|error|warn/i.test(t)) log('console', t.slice(0, 300));
  });

  const deadline = Date.now() + timeoutMs;
  let formFilled = false;
  let otpHandled = false;
  let stripeFilled = false;
  let stripePaypalClicks = 0;
  let lastStripeSubmitClick = 0;
  let lastUrl = '';
  let lastGenericClick = 0;
  let lastSubmitClick = 0;
  let createClicks = 0;
  let paypalFallbackContinueClicks = 0;
  let paypalFallbackSeenAt = 0;
  let paypalFallbackAgreed = false;
  let paypalCardErrorSeenAt = 0;
  let paypalBillingNoConsentSeenAt = 0;
  let paypalBillingNoConsentSnapSaved = false;
  let onboardEmailSeenAt = 0;
  let onboardEmailClicks = 0;
  let onboardEmailStuckSnapSaved = false;
  let paypalForcedPayRoute = false;
  let paypalLegacyStartClicks = 0;
  let paypalLegacyStartLastClick = 0;
  let paypalLegacyStartSeenAt = 0;
  let paypalSignupGuestNormalized = false;
  let captchaSnapSaved = false;

  let initialReferer = payload.referer || '';
  const startUrl = payload.checkoutUrl || payload.redirectUrl;
  try {
    const ru = new URL(startUrl);
    if (!initialReferer && /(^|\.)stripe\.com$/i.test(ru.hostname)) {
      initialReferer = 'https://checkout.stripe.com/';
    }
  } catch (_) {}
  try {
    await page.goto(startUrl, {
      // Do not wait for PayPal's full DOM lifecycle here.  Challenge /
      // telemetry pages may keep DOMContentLoaded/load pending; the control
      // loop below can act as soon as the main-frame navigation commits.
      waitUntil: 'commit',
      timeout: 90000,
      referer: initialReferer || undefined,
    });
  } catch (e) {
    log('initial goto wait failed; continue with current page', e && e.message ? e.message : e);
  }
  await addUserscriptStyle(page);

  while (Date.now() < deadline) {
    const url = page.url();
    saveState({
      url,
      host: (() => { try { return new URL(url).hostname; } catch (_) { return ''; } })(),
      pathname: (() => { try { return new URL(url).pathname; } catch (_) { return ''; } })(),
      formFilled,
      otpHandled,
      stripeFilled,
      capturedReturnUrl,
    });
    if (url !== lastUrl) {
      log('url', url.slice(0, 220));
      lastUrl = url;
    }
    if (ccLinkedToFullAccount) {
      log('decisive PayPal error CC_LINKED_TO_FULL_ACCOUNT; persona/card is poisoned, bailing for re-roll');
      await pageSnapshot(page, T('cc_linked')).catch(() => {});
      const finalUrl = page.url();
      await releaseIfHeld();
      await browser.close();
      return {
        success: false,
        error: 'paypal_cc_linked_to_full_account',
        finalUrl,
        returnUrl: capturedReturnUrl,
      };
    }
    if (dataDomeBlocked) {
      log('decisive PayPal error paypal_datadome_blocked; bailing (current proxy/fingerprint flagged)');
      await pageSnapshot(page, T('datadome')).catch(() => {});
      const finalUrl = page.url();
      await releaseIfHeld();
      await browser.close();
      return {
        success: false,
        error: 'paypal_datadome_blocked',
        finalUrl,
        returnUrl: capturedReturnUrl,
      };
    }
    if (cardAccountCandidateInvalid) {
      log('decisive PayPal error CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR; persona/card combo rejected, bailing for re-roll');
      await pageSnapshot(page, T('card_invalid')).catch(() => {});
      const finalUrl = page.url();
      await releaseIfHeld();
      await browser.close();
      return {
        success: false,
        error: 'paypal_create_card_account_validation_error',
        finalUrl,
        returnUrl: capturedReturnUrl,
      };
    }
    if (decisiveError || /\/pay\/generic-error|\/checkoutweb\/genericError/i.test(url)) {
      const errHost = (() => { try { return new URL(url).hostname; } catch (_) { return ''; } })();
      const errPath = (() => { try { return new URL(url).pathname; } catch (_) { return ''; } })();
      const cardFundingError = /INSTRUMENT_SHARING_LIMIT_EXCEEDED|CARD_GENERIC_ERROR|ISSUER_DECLINE/i.test(decisiveError || '');
      // Successful userscript HAR for #24 shows PayPal's recovery landing at
      // /webapps/hermes?...fallback=1&fromSignupLite=true&billingLite=1, not
      // only the newer /pay/billing shell.  That Hermes page contains the
      // final "Agree and Continue" button which issues billing.authorize.
      const hermesBillingFallback = (
        /\/webapps\/hermes/i.test(errPath)
        && /(?:fallback=1|fromSignupLite=true|billingLite=1|reason=Q0FSRF9HRU5FUklDX0VSUk9S)/i.test(url)
      );
      if (
        cardFundingError
        && /paypal\.com/i.test(errHost)
        && !/\/pay\/billing|\/pay\/generic-error|\/checkoutweb\/genericError/i.test(errPath)
        && !hermesBillingFallback
      ) {
        if (!paypalCardErrorSeenAt) paypalCardErrorSeenAt = Date.now();
        if (Date.now() - paypalCardErrorSeenAt < 45000) {
          log('paypal funding error observed; waiting for Hermes fallback', decisiveError.slice(0, 120));
          await sleep(2500);
          continue;
        }
      }
      const recoverablePaypalFallback = (
        /paypal\.com/i.test(errHost)
        && (/\/pay\/billing/i.test(errPath) || hermesBillingFallback)
        && cardFundingError
      );
      if (recoverablePaypalFallback && paypalFallbackContinueClicks < 3) {
        if (!paypalFallbackSeenAt) paypalFallbackSeenAt = Date.now();
        await pageSnapshot(page, T(`fallback_billing_${paypalFallbackContinueClicks}`));
        const waitedMs = Date.now() - paypalFallbackSeenAt;
        const manualAuthorize = await paypalAuthorizeFromBillingRuntime(page, 'paypal-fallback-runtime-authorize');
        if (manualAuthorize.ok) {
          paypalFallbackContinueClicks++;
          paypalFallbackAgreed = true;
          capturedReturnUrl = manualAuthorize.returnUrl || capturedReturnUrl;
          decisiveError = '';
          await sleep(3000);
          continue;
        }
        const clicked = await clickPaypalConsentButton(
          page,
          'paypal-fallback-agree-continue',
          4000,
          paypalFallbackContinueClicks === 0 ? Math.max(0, fallbackConsentDelayMs - waitedMs) : 3000,
        );
        if (clicked) {
          paypalFallbackContinueClicks++;
          paypalFallbackAgreed = true;
          // This page is a PayPal fallback recovery step.  Do not treat the
          // earlier sign_up_new_member mutation error as terminal yet; the
          // user-confirmed successful Tampermonkey path continues from here.
          log('paypal fallback billing accepted after error; waiting for redirect');
          decisiveError = '';
          await sleep(6000);
          continue;
        }
        log('paypal fallback billing missing Agree and Continue; retry with a fresh PayPal signup');
        await pageSnapshot(page, T('no_agree_continue'));
        const finalUrl = page.url();
        await browser.close();
        return {
          success: false,
          error: 'paypal_fallback_missing_agree_continue_retry_new_paypal_account',
          finalUrl,
          returnUrl: capturedReturnUrl,
        };
      }
      const err = (
        /\/pay\/generic-error|\/checkoutweb\/genericError/i.test(url) && paypalFallbackAgreed
          ? 'paypal_generic_error_after_agree_continue'
          : (decisiveError || 'paypal generic-error')
      );
      log('decisive PayPal error', err.slice(0, 300));
      await pageSnapshot(page, T('error_page'));
      const finalUrl = page.url();
      await browser.close();
      return {
        success: false,
        error: err,
        finalUrl,
        returnUrl: capturedReturnUrl,
        paypalFallbackAgreed,
      };
    }
    const host = (() => { try { return new URL(url).hostname; } catch (_) { return ''; } })();
    const pathname = (() => { try { return new URL(url).pathname; } catch (_) { return ''; } })();

    const openaiRedirectSucceeded = /pay\.openai\.com\/.*redirect_status=succeeded/i.test(url);
    const pmRedirectSuccess = /pm-redirects\.stripe\.com\/return\/.*status=success/i.test(url);
    const chatgptSuccess = /chatgpt\.com\/payments\/success/i.test(url);
    const chatgptLandingAfterReturn = /chatgpt\.com\/?/i.test(host) && !!capturedReturnUrl;
    if (chatgptSuccess || chatgptLandingAfterReturn || pmRedirectSuccess) {
      log('success url reached');
      await sleep(3000);
      const finalUrl = page.url();
      await browser.close();
      return { success: true, finalUrl, returnUrl: capturedReturnUrl };
    }
    if (openaiRedirectSucceeded) {
      log('openai redirect_status=succeeded seen; waiting for chatgpt success landing');
      await sleep(2000);
      continue;
    }

    await addUserscriptStyle(page);
    await maybeDismiss(page);

    // Full userscript path: start from pay.openai.com / checkout.stripe.com
    // and let Stripe's own checkout JS produce the PayPal redirect.  This is
    // intentionally different from the older "protocol Stripe + browser
    // PayPal" hybrid, which created a BA token without the exact page runtime
    // context that the Tampermonkey flow has.
    if (/pay\.openai\.com$/i.test(host) || /checkout\.stripe\.com$/i.test(host)) {
      const visible = await stripeFieldVisible(page);
      if (stripePaypalClicks < 2) {
        const clicked = await clickStripePaypal(page).catch(() => false);
        if (clicked) {
          stripePaypalClicks++;
          await sleep(1800);
          continue;
        }
      }
      if (visible && !stripeFilled) {
        const ok = await fillStripeCheckoutLikeUserscript(page, addr, expectedDueCents);
        if (ok) {
          stripeFilled = true;
          lastStripeSubmitClick = Date.now();
        } else {
          await sleep(2500);
        }
        continue;
      }
      if (stripeFilled && Date.now() - lastStripeSubmitClick > 5000) {
        await stripeCheckTerms(page);
        await clickSubmitLike(page);
        lastStripeSubmitClick = Date.now();
        await sleep(2500);
        continue;
      }
    }

    // PayPal login/pay entry: choose the same guest account creation path as
    // the successful mitm trace.  The important part is to pass through PayPal's
    // modular /pay UL state machine:
    //   Pay_With_Card -> email/createAccount -> ulOnboardRedirect=true
    // before checkoutweb/signup.  The legacy unified-login
    // #startOnboardingFlow path is only a last-resort fallback.
    if (/paypal\.com$/i.test(host) || /paypal\.com/i.test(host)) {
      const isAgreementsApprove = /\/agreements\/approve/i.test(pathname);
      const isUlOnboardRedirectApprove = isAgreementsApprove && /ulOnboardRedirect=true/i.test(url);
      const isPaypalPayRoute = /\/pay\/?$/i.test(pathname);
      const isSigninRoute = /\/signin/i.test(pathname);
      if (/\/checkoutweb\/signup/i.test(pathname) && !/modxo_redirect_reason=guest_user/i.test(url) && !paypalSignupGuestNormalized) {
        try {
          const u = new URL(url);
          u.searchParams.set('ul', '1');
          u.searchParams.set('modxo_redirect_reason', 'guest_user');
          paypalSignupGuestNormalized = true;
          log('paypal signup URL normalize to guest_user', u.toString().slice(0, 220));
          await page.goto(u.toString(), { waitUntil: 'commit', timeout: 60000, referer: url });
          await sleep(2500);
          continue;
        } catch (e) {
          log('paypal signup URL normalize failed', e && e.message ? e.message : e);
        }
      }
      const onboardEmailVisible = await evalAllFrames(page, () => {
        const card = document.getElementById('cardNumber') || document.querySelector('input[name="cardNumber"], input[name="cardnumber"]');
        const billing = document.getElementById('billingLine1') || document.getElementById('billingCity');
        for (const x of [card, billing].filter(Boolean)) {
          const rx = x.getBoundingClientRect();
          if (x.offsetParent !== null && rx.width > 0 && rx.height > 0) return false;
        }
        const el = document.getElementById('onboardingFlowEmail')
          || document.querySelector('input[name="login_email"]')
          || document.querySelector('input[type="email"]');
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return el.offsetParent !== null && r.width > 0 && r.height > 0;
      }, null).then((xs) => xs.some(Boolean)).catch(() => false);
      const onboardingFlowEmailVisible = await evalAllFrames(page, () => {
        const el = document.getElementById('onboardingFlowEmail');
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return el.offsetParent !== null && r.width > 0 && r.height > 0;
      }, null).then((xs) => xs.some(Boolean)).catch(() => false);
      const legacyStartVisible = await evalAllFrames(page, () => {
        const el = document.getElementById('startOnboardingFlow');
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return el.offsetParent !== null && r.width > 0 && r.height > 0;
      }, null).then((xs) => xs.some(Boolean)).catch(() => false);
      const legacyUnifiedLoginVisible = await evalAllFrames(page, () => {
        const start = document.getElementById('startOnboardingFlow');
        const emailEl = document.getElementById('onboardingFlowEmail') || document.querySelector('input[name="login_email"]');
        const signUpEndPoint = document.querySelector('input[name="signUpEndPoint"]');
        const hasAtomicPay = !!document.querySelector('button[data-atomic-wait-intent="Pay_With_Card"], button[data-atomic-wait-intent="Continue_To_Payment"]');
        const visible = (el) => {
          if (!el) return false;
          const r = el.getBoundingClientRect();
          return el.offsetParent !== null && r.width > 0 && r.height > 0;
        };
        return !hasAtomicPay && (
          visible(start)
          || visible(emailEl)
          || (signUpEndPoint && /\/webapps\/mpp\/account-selection/i.test(signUpEndPoint.value || ''))
        );
      }, null).then((xs) => xs.some(Boolean)).catch(() => false);

      if ((isAgreementsApprove && !isUlOnboardRedirectApprove) || isPaypalPayRoute) {
        const clickedPayWithCard = await clickPaypalInitialPayWithCard(
          page,
          isAgreementsApprove ? 'paypal-agreements-pay-with-card' : 'paypal-pay-with-card',
        );
        if (clickedPayWithCard) {
          createClicks++;
          await sleep(3500);
          continue;
        }
        if (
          isAgreementsApprove
          && !isUlOnboardRedirectApprove
          && legacyUnifiedLoginVisible
          && !paypalForcedPayRoute
          && process.env.PPS_PAYPAL_FORCE_PAY_ROUTE === '1'
        ) {
          paypalForcedPayRoute = true;
          await pageSnapshot(page, T('legacy_agreements_before_force_pay'));
          await forcePaypalPayRouteFromBa(page, url);
          continue;
        }
      }

      if (onboardEmailVisible) {
        if (!onboardEmailSeenAt) onboardEmailSeenAt = Date.now();
        // Initial modern /agreements pages should be clicked via Pay_With_Card
        // first.  Fill email only after /pay has entered the createAccount
        // screen, or when we explicitly had to fall back from legacy UL.
        const canFillOnboardEmail = !isAgreementsApprove
          || paypalForcedPayRoute
          || (legacyUnifiedLoginVisible && onboardingFlowEmailVisible);
        if (canFillOnboardEmail) {
          await fillAny(page, 'onboardingFlowEmail', email);
          await fillAny(page, 'login_email', email);
          await fillAny(page, 'email', email);
          await fillSelectorAny(page, 'input[type="email"]', email);
          await sleep(500);
        }
      } else if (!/\/agreements\/approve/i.test(pathname)) {
        onboardEmailSeenAt = 0;
        onboardEmailClicks = 0;
        onboardEmailStuckSnapSaved = false;
      }

      const canSubmitOnboardEmail = isPaypalPayRoute
        || isSigninRoute
        || paypalForcedPayRoute
        || (legacyUnifiedLoginVisible && onboardingFlowEmailVisible);
      if (onboardEmailVisible && canSubmitOnboardEmail) {
        if (
          onboardEmailSeenAt
          && (Date.now() - onboardEmailSeenAt > 75000 || onboardEmailClicks >= 12)
        ) {
          if (!onboardEmailStuckSnapSaved) {
            onboardEmailStuckSnapSaved = true;
            log('paypal onboarding email stuck; retry with a fresh PayPal signup');
            await pageSnapshot(page, T('onboarding_email_stuck'));
          }
          const finalUrl = page.url();
          await browser.close();
          return {
            success: false,
            error: 'paypal_onboarding_email_stuck_retry_new_paypal_account',
            finalUrl,
            returnUrl: capturedReturnUrl,
          };
        }
        const clickedContinueToPayment = await clickPaypalContinueToPayment(
          page,
          legacyUnifiedLoginVisible ? 'paypal-legacy-onboarding-email-submit' : 'paypal-continue-to-payment',
        );
        if (clickedContinueToPayment) {
          onboardEmailClicks++;
          await sleep(3500);
          continue;
        }
      }

      if (isAgreementsApprove && !isUlOnboardRedirectApprove && legacyUnifiedLoginVisible) {
        // Keep this legacy path isolated from the generic button clicker.  It is
        // not the preferred success path, but if direct /pay forcing is not
        // accepted by the current PayPal edge, this at least avoids idling on the
        // email page forever.
        if (!paypalLegacyStartSeenAt) paypalLegacyStartSeenAt = Date.now();
        if (
          legacyStartVisible
          && !onboardingFlowEmailVisible
          && paypalLegacyStartClicks < 5
          && Date.now() - paypalLegacyStartLastClick > 6500
        ) {
          const clickedLegacy = await clickPaypalLegacyStartOnboarding(page);
          if (clickedLegacy) {
            paypalLegacyStartClicks++;
            paypalLegacyStartLastClick = Date.now();
            await sleep(3000);
            continue;
          }
        }
        if (!onboardingFlowEmailVisible && Date.now() - paypalLegacyStartSeenAt > 90000) {
          log('paypal legacy start onboarding stuck; retry with fresh PayPal signup');
          await pageSnapshot(page, T('legacy_start_stuck'));
          const finalUrl = page.url();
          await browser.close();
          return {
            success: false,
            error: 'paypal_legacy_start_onboarding_stuck_retry_new_paypal_account',
            finalUrl,
            returnUrl: capturedReturnUrl,
          };
        }
        await sleep(1500);
        continue;
      }

      if (createClicks < 3 && !onboardEmailVisible && !isAgreementsApprove) {
        const clickedPayWithCard = await clickPaypalInitialPayWithCard(page, 'paypal-guest-entry-no-email');
        if (clickedPayWithCard) {
          createClicks++;
          await sleep(3000);
          continue;
        }
        const clickedCreate = await clickByText(
          page,
          /^(create an account|创建.*[账帐][户号]|注册|sign up)$/i,
          'create-account',
          1000,
        );
        if (clickedCreate) {
          createClicks++;
          await sleep(2500);
          continue;
        }
        const clickedGuest = await clickSelectorAny(page, ['#guestCheckout'], 'guest-checkout');
        if (clickedGuest) {
          createClicks++;
          await sleep(2500);
          continue;
        }
      }

      if (/\/pay\/billing/i.test(pathname) && paypalFallbackContinueClicks < 3) {
        const billingState = await evalAllFrames(page, () => {
          const btn = document.getElementById('consentButton')
            || document.querySelector('[data-id="consentButton"]')
            || document.querySelector('button[data-atomic-wait-intent="Approve_Billing_Agreement"]');
          const add = document.querySelector('a[data-atomic-wait-intent="Add_New_Card"], a[aria-label="Add card"]');
          const body = (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim();
          const visible = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            return el.offsetParent !== null && r.width > 0 && r.height > 0;
          };
          return {
            hasConsentButton: visible(btn),
            hasAddCard: visible(add),
            looksLikeBilling: /Set up once|Pay faster next time|Pay with|Add card/i.test(body),
            body: body.slice(0, 300),
          };
        }, null).then((xs) => xs.filter(Boolean).find((x) => x.hasConsentButton || x.hasAddCard || x.looksLikeBilling) || null).catch(() => null);
        const hasConsentButton = !!(billingState && billingState.hasConsentButton);
        if (hasConsentButton) {
          if (!paypalFallbackSeenAt) paypalFallbackSeenAt = Date.now();
          await pageSnapshot(page, T(`fallback_billing_${paypalFallbackContinueClicks}`));
          const waitedMs = Date.now() - paypalFallbackSeenAt;
          const manualAuthorize = await paypalAuthorizeFromBillingRuntime(page, 'paypal-billing-runtime-authorize');
          if (manualAuthorize.ok) {
            paypalFallbackContinueClicks++;
            paypalFallbackAgreed = true;
            capturedReturnUrl = manualAuthorize.returnUrl || capturedReturnUrl;
            decisiveError = '';
            await sleep(3000);
            continue;
          }
          const clicked = await clickPaypalConsentButton(
            page,
            'paypal-billing-agree-continue',
            4000,
            paypalFallbackContinueClicks === 0 ? Math.max(0, fallbackConsentDelayMs - waitedMs) : 3000,
          );
          if (clicked) {
            paypalFallbackContinueClicks++;
            paypalFallbackAgreed = true;
            decisiveError = '';
            await sleep(6000);
            continue;
          }
        } else if (billingState && (billingState.hasAddCard || billingState.looksLikeBilling)) {
          // PayPal sometimes lands on the fallback billing shell without the
          // consent button because the just-created guest account did not end
          // up with a usable wallet funding source.  The userscript path then
          // succeeds only after starting over with a new PayPal guest signup,
          // not by clicking Add card inside the stale account.
          if (!paypalBillingNoConsentSeenAt) paypalBillingNoConsentSeenAt = Date.now();
          if (!paypalBillingNoConsentSnapSaved) {
            paypalBillingNoConsentSnapSaved = true;
            log('paypal billing page has no Agree and Continue yet', JSON.stringify(billingState).slice(0, 500));
            await pageSnapshot(page, T('no_agree_continue'));
          }
          if (Date.now() - paypalBillingNoConsentSeenAt > 25000) {
            log('paypal billing still missing Agree and Continue; retry with a fresh PayPal signup');
            const finalUrl = page.url();
            await browser.close();
            return {
              success: false,
              error: 'paypal_billing_missing_agree_continue_retry_new_paypal_account',
              finalUrl,
              returnUrl: capturedReturnUrl,
            };
          }
        }
      }

      if (/\/pay\/?$/i.test(pathname) || /\/signin/i.test(pathname)) {
        // If a login email box is shown, type a random email only to unlock
        // PayPal's "Create account" branch, matching userscript /pay behavior.
        const didEmail = await fillAny(page, 'email', email)
          || await fillAny(page, 'login_email', email)
          || await fillSelectorAny(page, 'input[type="email"]', email);
        if (didEmail) {
          await sleep(800);
          const continueToPayment = await clickPaypalContinueToPayment(page, 'paypal-pay-continue-to-payment');
          if (continueToPayment) {
            createClicks++;
            await sleep(3000);
            continue;
          }
          const ca = await clickByText(page, /^(create an account|创建.*[账帐][户号]|注册|sign up)$/i, 'create-after-email', 1500);
          if (ca) {
            createClicks++;
            await sleep(2500);
            continue;
          }
        }
      }

      // Checkout signup form: fill exactly the userscript v32 field order.
      const hasForm = await evalAllFrames(page, () => {
        const card = document.getElementById('cardNumber') || document.querySelector('input[name="cardNumber"]');
        const billing = document.getElementById('billingLine1') || document.getElementById('billingCity');
        for (const el of [card, billing].filter(Boolean)) {
          const r = el.getBoundingClientRect();
          if (el.offsetParent !== null && r.width > 0 && r.height > 0) return true;
        }
        return false;
      }, null).then((xs) => xs.some(Boolean)).catch(() => false);

      if (hasForm && !formFilled) {
        log('checkout form detected; filling userscript v32 fields');
        const countryChanged = await selectAny(page, 'country', 'US');
        if (countryChanged) {
          log('country -> US, wait for PayPal form rerender');
          await sleep(3000);
        }
        await fillAny(page, 'email', email);
        await fillAny(page, 'phone', phone);
        await fillAny(page, 'cardNumber', cardNumber);
        await fillAny(page, 'cardExpiry', cardExpiry);
        await fillAny(page, 'cardCvv', cardCvv);
        await fillAny(page, 'password', password);
        await fillAny(page, 'firstName', payload.firstName || 'James');
        await fillAny(page, 'lastName', payload.lastName || 'Smith');
        await fillAny(page, 'billingLine1', addr.street);
        await fillAny(page, 'billingCity', addr.city);
        await fillAny(page, 'billingPostalCode', addr.zip);
        await selectAny(page, 'billingState', addr.state);
        await selectAny(page, 'billingAdministrativeArea', addr.state);
        formFilled = true;
        await sleep(800);
        // When concurrent workers contend for same phone, block here to queue for lock;
        // after acquiring lock, click submit to let PayPal send SMS, avoid two workers triggering SMS collision simultaneously.
        await ensurePhoneLock();
        await clickSubmitLike(page);
        lastSubmitClick = Date.now();
        await sleep(3500);
        continue;
      }

      const otpInfo = await hasOtp(page);
      if (otpInfo && !otpHandled) {
        log('OTP modal detected', JSON.stringify(otpInfo));
        if (!smsApiUrl) throw new Error('smsApiUrl missing for PayPal OTP');
        const code = await getOtp(
          smsApiUrl,
          Number(payload.otpTimeoutMs || 180000),
          smsBaselineText,
          { shouldAbort: () => dataDomeBlocked },
        );
        if (code === '__ABORTED__') {
          log('decisive PayPal error paypal_datadome_blocked; SMS never dispatched, bailing');
          await pageSnapshot(page, T('datadome')).catch(() => {});
          const finalUrl = page.url();
          await releaseIfHeld();
          await browser.close();
          return {
            success: false,
            error: 'paypal_datadome_blocked',
            finalUrl,
            returnUrl: capturedReturnUrl,
          };
        }
        if (!code) {
          // OTP timeout: release lock to let other worker take over; current worker throws error for upper layer to retry.
          await releaseIfHeld();
          throw new Error('PayPal OTP timeout');
        }
        const ok = await fillOtp(page, code);
        log('OTP fill', ok ? 'ok' : 'miss');
        otpHandled = true;
        // OTP filled → release phone lock to let next worker enter critical section.
        // post-OTP phase (Hermes / Stripe return) no longer needs SMS, can run in parallel with other workers.
        await releaseIfHeld();
        await sleep(1200);
        await clickSubmitLike(page);
        await sleep(4000);
        continue;
      }

      if (hasForm && formFilled && Date.now() - lastSubmitClick > 8000) {
        const diag = await evalAllFrames(page, () => {
          const visible = Array.from(document.querySelectorAll('input, select, textarea')).filter((el) => {
            const r = el.getBoundingClientRect();
            return el.offsetParent !== null && r.width > 0 && r.height > 0;
          }).map((el) => ({
            id: el.id || '',
            name: el.name || '',
            value: String(el.value || '').slice(0, 20),
            ariaInvalid: el.getAttribute('aria-invalid') || '',
          })).slice(0, 40);
          const errors = Array.from(document.querySelectorAll('[role="alert"], .error, [class*="error"], [id*="Error"], [class*="Error"]'))
            .filter((el) => el.offsetParent !== null)
            .map((el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
            .filter(Boolean)
            .slice(0, 10);
          return { visible, errors };
        }, null).catch(() => []);
        log('form still present; retry submit diag=', JSON.stringify(diag).slice(0, 1000));
        // Re-apply the address tail in case PayPal rerendered after country switch.
        await fillAny(page, 'billingLine1', addr.street);
        await fillAny(page, 'billingCity', addr.city);
        await fillAny(page, 'billingPostalCode', addr.zip);
        await selectAny(page, 'billingState', addr.state);
        await selectAny(page, 'billingAdministrativeArea', addr.state);
        await clickSubmitLike(page);
        lastSubmitClick = Date.now();
        await sleep(3500);
        continue;
      }

      // Generic PayPal post-signup pages: "Set up once", "Agree and Continue",
      // billing agreement confirm, etc.
      if (Date.now() - lastGenericClick > 3000) {
        const clicked = await clickByText(
          page,
          /^(agree[\s&]+continue|agree and continue|continue|agree|confirm|next|pay|verify)$/i,
          'generic-continue',
          1000,
        );
        if (clicked) {
          lastGenericClick = Date.now();
          await sleep(3000);
          continue;
        }
      }
    }

    // Save breadcrumbs if a real visible captcha page appears and does not
    // auto-resolve. We keep waiting a bit because PayPal sometimes swaps a
    // passive challenge out after its own scripts finish.
    const captchaVisible = await evalAllFrames(page, () => {
      const t = (document.body && document.body.innerText || '').slice(0, 1200);
      const textLooksLikeChallenge = /Security Challenge|security check|unusual activity|reCAPTCHA|hCaptcha|请验证|請驗證|人机验证|人機驗證/i.test(t);
      const visibleChallenge = Array.from(document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], #captcha-standalone, .captcha-overlay, .captcha-container')).some((el) => {
        const r = el.getBoundingClientRect();
        return el.offsetParent !== null && r.width > 10 && r.height > 10;
      });
      return textLooksLikeChallenge || visibleChallenge;
    }, null).then((xs) => xs.some(Boolean)).catch(() => false);
    if (captchaVisible && !captchaSnapSaved) {
      captchaSnapSaved = true;
      log('captcha visible; snapshot saved, keep waiting for PayPal JS to resolve');
      await pageSnapshot(page, T('captcha_seen'));
    }
    if (captchaVisible && Date.now() + 30000 > deadline) {
      await pageSnapshot(page, T('captcha'));
    }

    await sleep(1000);
  }

  await pageSnapshot(page, T('timeout'));
  saveState({
    url: page.url(),
    host: (() => { try { return new URL(page.url()).hostname; } catch (_) { return ''; } })(),
    pathname: (() => { try { return new URL(page.url()).pathname; } catch (_) { return ''; } })(),
    formFilled,
    otpHandled,
    capturedReturnUrl,
    timeout: true,
  });
  const finalUrl = page.url();
  await browser.close();
  return { success: false, error: 'timeout', finalUrl, returnUrl: capturedReturnUrl };
}

main()
  .then((result) => {
    try { fs.writeFileSync(T('result.json'), JSON.stringify(result, null, 2)); } catch (_) {}
    process.stdout.write(JSON.stringify(result, null, 2));
  })
  .catch((err) => {
    const result = {
      success: false,
      error: String(err && err.stack || err),
    };
    try { fs.writeFileSync(T('result.json'), JSON.stringify(result, null, 2)); } catch (_) {}
    try { fs.writeFileSync(T('error.json'), JSON.stringify(result, null, 2)); } catch (_) {}
    process.stdout.write(JSON.stringify(result, null, 2));
    process.exitCode = 1;
  });
