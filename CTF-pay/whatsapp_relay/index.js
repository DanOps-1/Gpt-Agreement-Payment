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
// GoPay / GoJek / Midtrans 商户名（理想匹配条件，发件人或消息体都行）
const SENDER_PATTERNS = [/gojek/i, /gopay/i, /midtrans/i];
// 通用 OTP 模板关键词（GoPay 偶尔走「X is your verification code...」通用格式，
// 这种没商户名）。命中任一即认为是 OTP 消息。
const GENERIC_OTP_PATTERNS = [
  /\bverification code\b/i,
  /\bverifikasi\b/i,        // Indonesian
  /\bone[- ]?time\b/i,
  /\bOTP\b/,
  /\bkode\b/i,              // Indonesian "kode"
];
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
let reconnectAttempts = 0;

async function boot() {
  fs.mkdirSync(SESSION_DIR, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();
  log(`Baileys WA version: ${version.join('.')}`);

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    // 关键：自称 Chrome 浏览器（跟 web.whatsapp.com 类似）。Desktop client
    // 在 WhatsApp 服务端有更严格限流，业务 OTP 不下发到 Desktop 链接的
    // 设备。Browser 类型则放行（web.whatsapp.com 能收到 OTP 就是这个原因）。
    // 用 Baileys 自带 helper 拼出来格式最兼容。
    browser: Browsers.macOS('Chrome'),
    logger: pino({ level: 'silent' }),
    // 标记设备 online，否则 WhatsApp 默认认为离线 → 不推消息（只在主设备
    // 显示），网页版默认也是 online。
    markOnlineOnConnect: true,
    // 完整历史同步：保证连上来时漏掉的窗口期消息也能拿到（业务 OTP 在
    // 这窗口里就发了的话否则永远抓不到）。
    syncFullHistory: true,
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
      writeState({ status: 'disconnected', login_mode: LOGIN_MODE, reason, code });
      log(`disconnected: ${reason} (statusCode=${code})`);

      // 只在明确是 401 logged out 时才退出 —— DisconnectReason 在新版本
      // 可能 reshape，硬编码数字最稳。
      if (code === 401) {
        log('logged out (401), exiting');
        process.exit(0);
      }
      // 其它都按 transient 处理，自动重连（指数退避避免风暴）
      const delay = Math.min(2000 * Math.pow(2, reconnectAttempts), 30000);
      reconnectAttempts++;
      log(`reconnecting in ${delay}ms... (attempt ${reconnectAttempts})`);
      setTimeout(() => boot().catch((e) => {
        writeState({ status: 'error', error: 'reconnect 失败: ' + e.message });
        log('reconnect failed: ' + e.message);
        process.exit(1);
      }), delay);
    }
    if (connection === 'open') {
      reconnectAttempts = 0;
    }
  });

  // ───────── OTP capture ─────────
  // 不再过滤 type — 之前 GoPay OTP 没被抓到，可能消息走 'prepend' 或其它 type
  sock.ev.on('messages.upsert', ({ messages, type }) => {
    log(`messages.upsert type=${type} count=${messages.length}`);
    for (const m of messages) {
      try {
        if (m.key?.fromMe) continue; // 自己发的不抓
        const { pushName, remoteJid } = extractSender(m);
        const body = extractText(m.message);
        const otpMatch = body.match(OTP_REGEX);
        // 全量日志（debug 用）：每条收到的消息都打一行
        log(`msg from="${(pushName || '').slice(0, 60)}" jid=${remoteJid.slice(0, 40)} body="${body.slice(0, 80).replace(/\n/g, ' ')}" has_6digit=${!!otpMatch}`);

        // 当 body 为空但消息存在时（template/interactive 等）输出消息 type 帮助排查
        if (!body && m.message) {
          const types = Object.keys(m.message).filter((k) => k !== 'messageContextInfo');
          log(`  └─ body 空，消息 type=[${types.join(',')}] 完整 message: ${JSON.stringify(m.message).slice(0, 400)}`);
        }

        if (!otpMatch) continue;

        const senderMatch = SENDER_PATTERNS.some((re) =>
          re.test(pushName) || re.test(body) || re.test(remoteJid)
        );
        const genericMatch = GENERIC_OTP_PATTERNS.some((re) => re.test(body));
        if (!senderMatch && !genericMatch) {
          log(`  └─ 6 位数字命中但既无 gopay/gojek/midtrans 商户名也无通用 OTP 关键词，跳过`);
          continue;
        }

        const otp = otpMatch[1];
        fs.writeFileSync(OTP_FILE, otp);
        log(`OTP captured: ${otp} (from "${(pushName || '').slice(0, 40)}" jid=${remoteJid.slice(0, 30)} ${senderMatch ? 'sender_match' : 'generic_match'})`);
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
