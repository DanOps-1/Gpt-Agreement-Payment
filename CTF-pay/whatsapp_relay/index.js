#!/usr/bin/env node
/**
 * WhatsApp OTP Relay sidecar (Baileys, pure WebSocket).
 *
 * Replaces the old whatsapp-web.js + Puppeteer Chromium implementation.
 * Memory footprint dropped from ~400MB (Chromium) to ~50MB (pure Node WS).
 * Same external interface (state file, OTP file, env knobs), so wa_relay.py
 * doesn't need to change.
 *
 * Two login modes:
 *   - WA_LOGIN_MODE=qr (default): emit QR string for the wizard UI to render
 *   - WA_LOGIN_MODE=pairing + WA_PAIRING_PHONE=8617788949030: requests an
 *     8-char pairing code that user types in WhatsApp → Linked Devices →
 *     "Link with phone number"
 */
'use strict';

const fs = require('fs');
const path = require('path');
const QRCode = require('qrcode');
const pino = require('pino');
const {
  default: makeWASocket,
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  Browsers,
} = require('@whiskeysockets/baileys');

// ───────── env ─────────
const STATE_FILE = process.env.WA_STATE_FILE || '/tmp/wa_state.json';
const OTP_FILE = process.env.WA_OTP_FILE || '/tmp/wa_otp.txt';
const SESSION_DIR = process.env.WA_SESSION_DIR || path.resolve(__dirname, '.baileys-session');
const LOGIN_MODE = (process.env.WA_LOGIN_MODE || 'qr').toLowerCase();
const PAIRING_PHONE = (process.env.WA_PAIRING_PHONE || '').replace(/[^\d]/g, '');

// ───────── filters ─────────
// 抓 GoPay/GoJek/Midtrans 任一关键词；可通过 env 加自定义白名单
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
    fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
    fs.writeFileSync(STATE_FILE, JSON.stringify(payload, null, 2));
  } catch (e) {
    console.error('[wa] state write failed:', e.message);
  }
}

function log(msg) {
  console.log(`[wa] ${msg}`);
}

function extractText(message) {
  // Baileys message types: conversation, extendedTextMessage, imageMessage.caption, etc.
  if (!message) return '';
  if (message.conversation) return message.conversation;
  if (message.extendedTextMessage) return message.extendedTextMessage.text || '';
  if (message.imageMessage) return message.imageMessage.caption || '';
  if (message.videoMessage) return message.videoMessage.caption || '';
  if (message.documentWithCaptionMessage) {
    return message.documentWithCaptionMessage.message?.documentMessage?.caption || '';
  }
  if (message.buttonsResponseMessage) return message.buttonsResponseMessage.selectedDisplayText || '';
  if (message.templateButtonReplyMessage) return message.templateButtonReplyMessage.selectedDisplayText || '';
  return '';
}

function extractSender(msgInfo) {
  // pushName comes from the sender; key.remoteJid is the chat (e.g., 628XXX@s.whatsapp.net)
  const pushName = msgInfo.pushName || '';
  const remoteJid = (msgInfo.key && msgInfo.key.remoteJid) || '';
  return { pushName, remoteJid };
}

// Pre-flight cleanup of old state
try { fs.unlinkSync(STATE_FILE); } catch (_) {}
try { fs.unlinkSync(OTP_FILE); } catch (_) {}

writeState({ status: 'starting', login_mode: LOGIN_MODE });

// ───────── boot ─────────
async function boot() {
  fs.mkdirSync(SESSION_DIR, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();
  log(`Baileys WA version: ${version.join('.')}`);

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,           // 我们自己处理 QR
    browser: Browsers.macOS('Desktop'), // 让 web 显示「Desktop」而不是 Baileys
    logger: pino({ level: 'silent' }),  // 不要它的 noisy 日志
    syncFullHistory: false,             // 不下载历史记录，省内存 + 启动快
    markOnlineOnConnect: false,         // 别抢手机的 online 状态
  });

  sock.ev.on('creds.update', saveCreds);

  // ───────── pairing code 流程 ─────────
  // Baileys 要求在 connection.update 之前请求 pairing code
  if (LOGIN_MODE === 'pairing' && !sock.authState.creds.registered) {
    if (!PAIRING_PHONE) {
      writeState({ status: 'error', error: 'WA_PAIRING_PHONE 未设置（带国家码无加号，例如 8617788949030）' });
      log('pairing mode requires WA_PAIRING_PHONE env');
      process.exit(2);
    }
    // 等 1 秒让 socket 连上，然后请求 pairing code
    setTimeout(async () => {
      try {
        const code = await sock.requestPairingCode(PAIRING_PHONE);
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
    }, 1500);
  }

  // ───────── connection events ─────────
  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr && LOGIN_MODE === 'qr') {
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
        qr,
        qr_ascii: qrAscii,
        qr_data_url: qrPng,
      });
      log('QR ready, scan in WhatsApp → Linked Devices → Link a device');
    }

    if (connection === 'connecting') {
      writeState({ status: 'connecting', login_mode: LOGIN_MODE });
    }

    if (connection === 'open') {
      const me = sock.user || {};
      writeState({
        status: 'connected',
        login_mode: LOGIN_MODE,
        wid: me.id || '',
        pushname: me.name || me.verifiedName || '',
      });
      log(`connected as ${me.name || me.id || 'unknown'}`);
    }

    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode;
      const reason = lastDisconnect?.error?.message || `code=${code}`;
      writeState({ status: 'disconnected', login_mode: LOGIN_MODE, reason });
      log(`disconnected: ${reason}`);

      // logged out → don't auto-reconnect (user needs to re-pair)
      const loggedOut = code === DisconnectReason.loggedOut;
      if (loggedOut) {
        log('logged out, exiting');
        process.exit(0);
      }
      // transient disconnect → reconnect
      log('reconnecting in 2s...');
      setTimeout(() => boot().catch((e) => {
        writeState({ status: 'error', error: 'reconnect 失败: ' + e.message });
        log('reconnect failed: ' + e.message);
        process.exit(1);
      }), 2000);
    }
  });

  // ───────── OTP capture ─────────
  sock.ev.on('messages.upsert', ({ messages, type }) => {
    if (type !== 'notify' && type !== 'append') return; // skip 'prepend' (history sync)
    for (const m of messages) {
      try {
        if (m.key?.fromMe) continue; // 自己发的不抓
        const { pushName, remoteJid } = extractSender(m);
        const body = extractText(m.message);
        const otpMatch = body.match(OTP_REGEX);
        // 全量日志（debug 用）：每条收到的消息都打一行
        log(`msg from="${(pushName || '').slice(0, 60)}" jid=${remoteJid.slice(0, 40)} body="${body.slice(0, 80).replace(/\n/g, ' ')}" has_6digit=${!!otpMatch}`);

        if (!otpMatch) continue;

        const senderMatch = SENDER_PATTERNS.some((re) =>
          re.test(pushName) || re.test(body) || re.test(remoteJid)
        );
        if (!senderMatch) {
          log(`  └─ 6 位数字命中但发件人/正文/jid 无 gopay/gojek/midtrans 关键词，跳过（设 WA_OTP_SENDER_REGEX 可放宽）`);
          continue;
        }

        const otp = otpMatch[1];
        fs.writeFileSync(OTP_FILE, otp);
        log(`OTP captured: ${otp} (from "${(pushName || '').slice(0, 40)}" jid=${remoteJid.slice(0, 30)})`);
      } catch (e) {
        console.error('[wa] message handler error:', e.message);
      }
    }
  });
}

// Graceful shutdown
function cleanup() {
  log('shutting down ...');
  writeState({ status: 'shutdown' });
  process.exit(0);
}
process.on('SIGINT', cleanup);
process.on('SIGTERM', cleanup);

boot().catch((e) => {
  writeState({ status: 'error', error: 'boot 失败: ' + e.message });
  log('boot failed: ' + e.message);
  process.exit(1);
});
