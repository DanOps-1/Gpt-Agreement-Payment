#!/usr/bin/env node
/**
 * WhatsApp OTP Relay sidecar (Node).
 *
 * Watches a linked WhatsApp account for messages from GoPay/GoJek matching a
 * 6-digit OTP pattern, writes the latest OTP to a file the Python pipeline
 * polls (`gopay.file_watch_otp_provider`).
 *
 * Two login modes:
 *   - WA_LOGIN_MODE=qr (default): emits QR string for the wizard UI to render
 *   - WA_LOGIN_MODE=pairing + WA_PAIRING_PHONE=+12025550100: requests an
 *     8-char pairing code that user types in WhatsApp → Linked Devices →
 *     "Link with phone number"
 *
 * State + OTP via files (no IPC, kept dead simple):
 *   - WA_STATE_FILE  → JSON {status, qr|code|wid, ts}
 *   - WA_OTP_FILE    → just the 6-digit OTP, deleted by Python after read
 *   - WA_SESSION_DIR → wweb LocalAuth persistent dir (re-used across restarts)
 */
'use strict';

const fs = require('fs');
const path = require('path');
const QRCode = require('qrcode');
const { Client, LocalAuth } = require('whatsapp-web.js');

// ───────── env ─────────
const STATE_FILE = process.env.WA_STATE_FILE || '/tmp/wa_state.json';
const OTP_FILE = process.env.WA_OTP_FILE || '/tmp/wa_otp.txt';
const SESSION_DIR = process.env.WA_SESSION_DIR || path.resolve(__dirname, '.wwebjs-session');
const LOGIN_MODE = (process.env.WA_LOGIN_MODE || 'qr').toLowerCase();
const PAIRING_PHONE = (process.env.WA_PAIRING_PHONE || '').replace(/[^\d]/g, '');
const HEADLESS = process.env.WA_HEADLESS !== '0';

// ───────── filters ─────────
// 主动放宽：抓 GoPay/GoJek/Midtrans 任一关键词；可通过 env 加自定义白名单
const SENDER_PATTERNS = [/gojek/i, /gopay/i, /midtrans/i];
if (process.env.WA_OTP_SENDER_REGEX) {
  try {
    SENDER_PATTERNS.push(new RegExp(process.env.WA_OTP_SENDER_REGEX, 'i'));
  } catch (_) { /* ignore bad regex */ }
}
const OTP_REGEX = /\b(\d{6})\b/;

// ───────── helpers ─────────
function writeState(obj) {
  const payload = { ...obj, ts: Date.now() };
  try {
    fs.writeFileSync(STATE_FILE, JSON.stringify(payload, null, 2));
  } catch (e) {
    console.error('[wa] state write failed:', e.message);
  }
}

function log(msg) {
  console.log(`[wa] ${msg}`);
}

// Pre-flight cleanup of old state
try { fs.unlinkSync(STATE_FILE); } catch (_) {}
try { fs.unlinkSync(OTP_FILE); } catch (_) {}

writeState({ status: 'starting', login_mode: LOGIN_MODE });

// ───────── client ─────────
const client = new Client({
  authStrategy: new LocalAuth({ dataPath: SESSION_DIR }),
  puppeteer: {
    headless: HEADLESS,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-blink-features=AutomationControlled',
    ],
  },
});

// QR mode emits this; pairing mode also fires it the first time, but we
// override the state to show the pairing code instead.
let qrShownOnce = false;
client.on('qr', async (qr) => {
  if (LOGIN_MODE === 'pairing') return; // pairing UX handled below
  qrShownOnce = true;
  let qrAscii = '';
  try {
    qrAscii = await QRCode.toString(qr, { type: 'terminal', small: true });
  } catch (_) { /* ignore */ }
  let qrPng = '';
  try {
    qrPng = await QRCode.toDataURL(qr);
  } catch (_) { /* ignore */ }
  writeState({
    status: 'awaiting_qr_scan',
    login_mode: 'qr',
    qr,             // raw text payload
    qr_ascii: qrAscii,
    qr_data_url: qrPng,
  });
  log('QR ready, scan in WhatsApp → Linked Devices → Link a device');
});

client.on('loading_screen', (percent, msg) => {
  writeState({ status: 'loading', login_mode: LOGIN_MODE, percent: Number(percent), msg });
});

client.on('authenticated', () => {
  writeState({ status: 'authenticated', login_mode: LOGIN_MODE });
});

client.on('auth_failure', (msg) => {
  writeState({ status: 'auth_failure', login_mode: LOGIN_MODE, msg });
  log('auth failure: ' + msg);
});

client.on('ready', () => {
  const wid = client.info && client.info.wid && client.info.wid._serialized;
  const pushname = client.info && client.info.pushname;
  writeState({
    status: 'connected',
    login_mode: LOGIN_MODE,
    wid: wid || '',
    pushname: pushname || '',
  });
  log(`connected as ${pushname || wid || 'unknown'}`);
});

client.on('disconnected', (reason) => {
  writeState({ status: 'disconnected', login_mode: LOGIN_MODE, reason });
  log(`disconnected: ${reason}`);
});

// ───────── OTP capture ─────────
client.on('message', async (msg) => {
  try {
    const sender = (msg._data && (msg._data.notifyName || msg._data.author)) || msg.from || '';
    const body = msg.body || '';
    const senderMatch = SENDER_PATTERNS.some((re) => re.test(sender) || re.test(body));
    if (!senderMatch) return;
    const m = body.match(OTP_REGEX);
    if (!m) return;
    const otp = m[1];
    fs.writeFileSync(OTP_FILE, otp);
    log(`OTP captured: ${otp} (from "${sender.slice(0, 40)}")`);
  } catch (e) {
    console.error('[wa] message handler error:', e.message);
  }
});

// ───────── pairing code mode ─────────
async function handlePairingMode() {
  if (!PAIRING_PHONE) {
    writeState({ status: 'error', error: 'WA_PAIRING_PHONE 未设置（带国家码无加号，例如 8617788949030）' });
    log('pairing mode requires WA_PAIRING_PHONE env (digits only, e.g. 8617788949030)');
    return;
  }
  // Need to wait until puppeteer page is ready but before QR; whatsapp-web.js
  // exposes requestPairingCode on the client. Spin until it's available.
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (client.pupPage) break;
    await new Promise((r) => setTimeout(r, 500));
  }
  if (!client.pupPage) {
    writeState({ status: 'error', error: 'puppeteer 60s 内未就绪' });
    return;
  }
  try {
    const code = await client.requestPairingCode(PAIRING_PHONE, true);
    writeState({
      status: 'awaiting_pairing_code',
      login_mode: 'pairing',
      code,
      phone: PAIRING_PHONE,
    });
    log(`pairing code: ${code}  (在手机 WhatsApp → Linked Devices → Link with phone number 输入)`);
  } catch (e) {
    writeState({ status: 'error', error: 'requestPairingCode 失败: ' + e.message });
    log('pairing code request failed: ' + e.message);
  }
}

// ───────── boot ─────────
client.initialize().catch((e) => {
  writeState({ status: 'error', error: 'initialize 失败: ' + e.message });
  log('initialize failed: ' + e.message);
  process.exit(1);
});

if (LOGIN_MODE === 'pairing') {
  handlePairingMode();
}

// Graceful shutdown
function cleanup() {
  log('shutting down ...');
  writeState({ status: 'shutdown' });
  try { client.destroy(); } catch (_) {}
  process.exit(0);
}
process.on('SIGINT', cleanup);
process.on('SIGTERM', cleanup);
