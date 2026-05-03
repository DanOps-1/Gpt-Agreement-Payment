#!/usr/bin/env node
/* WhatsApp sidecar for GoPay OTP capture.
 *
 * Preferred engine is Baileys (raw WhatsApp multi-device socket, no Chromium).
 * whatsapp-web.js remains available as a fallback by setting WA_ENGINE=wwebjs.
 *
 * Environment:
 *   WA_ENGINE       baileys | wwebjs  (default: baileys)
 *   WA_LOGIN_MODE   qr | pairing
 *   WA_PAIRING_PHONE  digits with country code (pairing mode only)
 *   WA_STATE_URL    WebUI internal endpoint that persists state/OTP into SQLite
 *   WA_RELAY_TOKEN  shared token for WA_STATE_URL
 *   WA_STATE_FILE   deprecated JSON state output (only used without WA_STATE_URL)
 *   WA_OTP_FILE     deprecated plain OTP output (optional legacy file-provider mirror)
 *   WA_SESSION_DIR  persistent WhatsApp session directory
 *   WA_HEADLESS     "1" (default) or "0"; wwebjs only
 */
const fs = require("fs");
const path = require("path");
const QRCode = require("qrcode");

const engine = (process.env.WA_ENGINE || "baileys").toLowerCase();
const mode = (process.env.WA_LOGIN_MODE || "qr").toLowerCase();
const pairingPhone = (process.env.WA_PAIRING_PHONE || "").replace(/\D/g, "");
const stateUrl = process.env.WA_STATE_URL || "";
const relayToken = process.env.WA_RELAY_TOKEN || "";
const stateFile = process.env.WA_STATE_FILE || "";
const otpFile = process.env.WA_OTP_FILE || "";
const sessionDir = process.env.WA_SESSION_DIR || path.join(process.cwd(), ".wa-session");
const headless = (process.env.WA_HEADLESS || "1") !== "0";
const dbMode = !!stateUrl;
let memoryState = {};

if (!dbMode && (!stateFile || !otpFile)) {
  console.error("WA_STATE_URL or WA_STATE_FILE+WA_OTP_FILE is required");
  process.exit(2);
}

function ensureDir(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function atomicWrite(filePath, data) {
  ensureDir(filePath);
  const tmp = `${filePath}.tmp`;
  fs.writeFileSync(tmp, data);
  fs.renameSync(tmp, filePath);
}

function postState(state) {
  if (!dbMode) return;
  fetch(stateUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-WA-Relay-Token": relayToken,
    },
    body: JSON.stringify(state),
  }).catch((err) => {
    console.error(`[state_post_error] ${err && err.message ? err.message : err}`);
  });
}

function readState() {
  if (dbMode) return memoryState;
  try {
    return JSON.parse(fs.readFileSync(stateFile, "utf8"));
  } catch {
    return {};
  }
}

function writeState(patch) {
  const state = {
    ...readState(),
    updated_at: Date.now() / 1000,
    ...patch,
  };
  memoryState = state;
  if (dbMode) {
    postState(state);
  } else {
    atomicWrite(stateFile, JSON.stringify(state, null, 2));
  }
  return state;
}

function extractOtp(text) {
  if (!text) return "";
  const patterns = [
    /(?:otp|one[-\s]*time|verification|verify|code|kode|verifikasi|gopay|gojek|whatsapp|验证码|驗證碼)[^\d]{0,100}(\d{4,8})(?!\d)/gis,
    /(?<!\d)(\d{4,8})(?!\d)[^\n\r]{0,100}(?:otp|one[-\s]*time|verification|verify|code|kode|verifikasi|gopay|gojek|验证码|驗證碼)/gis,
    /(?<!\d)(\d{6})(?!\d)/g,
  ];
  for (const re of patterns) {
    const matches = [...String(text).matchAll(re)];
    for (let i = matches.length - 1; i >= 0; i -= 1) {
      const groups = matches[i].slice(1);
      for (let j = groups.length - 1; j >= 0; j -= 1) {
        const digits = String(groups[j] || "").replace(/\D/g, "");
        if (digits.length >= 4 && digits.length <= 8) return digits;
      }
    }
  }
  return "";
}

function looksLikeOtpPrivacyPlaceholder(text) {
  const s = String(text || "");
  return (
    /一次性密码/.test(s) && /主要设备|主设备|使用 WhatsApp 的主要设备/.test(s)
  ) || (
    /one[-\s]*time password/i.test(s) && /primary device|main device/i.test(s)
  ) || (
    /kode|otp|verification|verifikasi/i.test(s) && /primary device|main device/i.test(s)
  );
}

function makeSidecarMessage({ body, from, author, id, type }) {
  return {
    body: String(body || ""),
    from: from || "",
    author: author || "",
    id: { _serialized: id || "" },
    type: type || "",
  };
}

function recordPrivacyBlocked(msg) {
  const item = {
    ts: Date.now() / 1000,
    from: msg.from || "",
    author: msg.author || "",
    id: (msg.id && msg.id._serialized) || "",
    text: String(msg.body || "").slice(0, 500),
    engine,
    reason: "whatsapp_primary_device_only",
  };
  const state = readState();
  const privacyHistory = Array.isArray(state.privacy_history) ? state.privacy_history : [];
  privacyHistory.push(item);
  writeState({
    status: "connected",
    privacy_blocked: item,
    privacy_history: privacyHistory.slice(-20),
  });
  console.log(`[otp_privacy] WhatsApp hid OTP on linked device; from=${item.from}. Enter code from primary phone in WebUI fallback.`);
}

function recordOtp(code, msg) {
  if (!code) return;
  const item = {
    otp: code,
    ts: Date.now() / 1000,
    from: msg.from || "",
    author: msg.author || "",
    id: (msg.id && msg.id._serialized) || "",
    text: String(msg.body || "").slice(0, 500),
    engine,
  };
  if (otpFile) {
    atomicWrite(otpFile, `${code}\n`);
  }
  const state = readState();
  const history = Array.isArray(state.history) ? state.history : [];
  history.push(item);
  writeState({
    status: "connected",
    latest: item,
    history: history.slice(-50),
  });
  console.log(`[otp] ${code} from=${item.from}`);
}

function handleMessage(msg, source) {
  const body = String(msg.body || "");
  const code = extractOtp(body);
  if (code) {
    recordOtp(code, msg);
    return;
  }
  if (looksLikeOtpPrivacyPlaceholder(body)) {
    recordPrivacyBlocked(msg);
    return;
  }
  if (/\b(gopay|gojek|otp|verification|verifikasi|kode)\b/i.test(body) || /一次性密码|验证码|驗證碼/.test(body)) {
    console.log(`[msg_no_otp] source=${source} from=${msg.from || ""} type=${msg.type || ""} text=${body.slice(0, 220).replace(/\s+/g, " ")}`);
  }
}

function redactDigits(text) {
  return String(text || "").replace(/\b\d{4,8}\b/g, "[digits]").slice(0, 500);
}

function summarizeRawMessage(meta) {
  const item = {
    ts: Date.now() / 1000,
    engine,
    from: meta.from || "",
    author: meta.author || "",
    id: meta.id || "",
    type: meta.type || "",
    text: redactDigits(meta.text || ""),
  };
  const state = readState();
  const rawHistory = Array.isArray(state.raw_history) ? state.raw_history : [];
  rawHistory.push(item);
  writeState({ latest_raw: item, raw_history: rawHistory.slice(-30) });
}

function baileysContentType(message) {
  if (!message || typeof message !== "object") return "";
  return Object.keys(message).find((k) => k !== "messageContextInfo" && message[k] != null) || "";
}

function unwrapBaileysMessage(message) {
  let cur = message || {};
  for (let i = 0; i < 8; i += 1) {
    if (cur.ephemeralMessage?.message) cur = cur.ephemeralMessage.message;
    else if (cur.viewOnceMessage?.message) cur = cur.viewOnceMessage.message;
    else if (cur.viewOnceMessageV2?.message) cur = cur.viewOnceMessageV2.message;
    else if (cur.documentWithCaptionMessage?.message) cur = cur.documentWithCaptionMessage.message;
    else break;
  }
  return cur;
}

const TEXT_KEYS = new Set([
  "conversation", "text", "caption", "selectedDisplayText", "title", "description",
  "body", "footer", "contentText", "matchedText", "canonicalUrl", "name",
]);

function collectBaileysText(value, pieces = [], depth = 0, parentKey = "") {
  if (depth > 8 || value == null) return pieces;
  if (typeof value === "string") {
    if (TEXT_KEYS.has(parentKey)) pieces.push(value);
    return pieces;
  }
  if (typeof value !== "object") return pieces;
  if (Array.isArray(value)) {
    for (const item of value) collectBaileysText(item, pieces, depth + 1, parentKey);
    return pieces;
  }
  for (const [key, child] of Object.entries(value)) {
    if (key === "jpegThumbnail" || key === "mediaKey" || key === "fileSha256" || key === "fileEncSha256") continue;
    collectBaileysText(child, pieces, depth + 1, key);
  }
  return pieces;
}

function baileysMessageToSidecar(msg) {
  const content = unwrapBaileysMessage(msg.message || {});
  const type = baileysContentType(content);
  const pieces = collectBaileysText(content);
  const text = [...new Set(pieces.map((s) => String(s).trim()).filter(Boolean))].join("\n");
  return makeSidecarMessage({
    body: text,
    from: msg.key?.remoteJid || "",
    author: msg.key?.participant || "",
    id: msg.key?.id || "",
    type,
  });
}

writeState({
  status: "starting",
  engine,
  mode,
  session_dir: sessionDir,
  otp_file: otpFile || "",
  otp_store: dbMode ? "sqlite" : "file",
});

async function qrToDataUrl(qr) {
  return QRCode.toDataURL(qr, { margin: 1, scale: 6 });
}

async function startBaileys() {
  const P = require("pino");
  const {
    default: makeWASocket,
    Browsers,
    DisconnectReason,
    fetchLatestBaileysVersion,
    makeCacheableSignalKeyStore,
    useMultiFileAuthState,
  } = require("@whiskeysockets/baileys");

  const logger = P({ level: process.env.WA_LOG_LEVEL || "silent" });
  const authDir = path.join(sessionDir, "baileys-gopay");
  fs.mkdirSync(authDir, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(authDir);

  let version;
  try {
    const latest = await fetchLatestBaileysVersion();
    version = latest.version;
    console.log(`[wa] Baileys WA version: ${version.join(".")}`);
  } catch (e) {
    console.log(`[wa] Baileys version lookup failed, using package default: ${String(e && e.message || e)}`);
  }

  const sock = makeWASocket({
    ...(version ? { version } : {}),
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    browser: Browsers.macOS("Desktop"),
    logger,
    printQRInTerminal: false,
    markOnlineOnConnect: false,
    syncFullHistory: true,
    generateHighQualityLinkPreview: false,
  });

  let pairingRequested = false;
  if (mode === "pairing" && pairingPhone && !state.creds.registered) {
    pairingRequested = true;
    setTimeout(async () => {
      try {
        const code = await sock.requestPairingCode(pairingPhone);
        writeState({ status: "awaiting_pairing_code", code, qr: null, qr_data_url: null });
        console.log(`[pairing] code=${code}`);
      } catch (e) {
        writeState({ status: "error", error: `pairing code failed: ${String(e && e.message || e)}` });
      }
    }, 1500);
  }

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      try {
        const qrDataUrl = await qrToDataUrl(qr);
        writeState({
          status: pairingRequested ? "awaiting_pairing_code" : "awaiting_qr_scan",
          qr,
          qr_data_url: qrDataUrl,
          code: null,
        });
      } catch (e) {
        writeState({ status: "awaiting_qr_scan", qr, qr_error: String(e && e.message || e) });
      }
      if (!pairingRequested) console.log("[wa] QR ready, scan in WhatsApp → Linked Devices → Link a device");
    }
    if (connection === "connecting") {
      writeState({ status: "connecting" });
    } else if (connection === "open") {
      writeState({ status: "connected", qr: null, qr_data_url: null, code: null, percent: 100, message: "WhatsApp" });
      console.log("[ready] WhatsApp connected (baileys)");
    } else if (connection === "close") {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const reason = String(lastDisconnect?.error?.message || lastDisconnect?.error || "");
      writeState({ status: "disconnected", reason, status_code: statusCode });
      console.log(`[wa] disconnected: ${reason || statusCode || "unknown"}`);
      if (statusCode === DisconnectReason.loggedOut) {
        process.exit(0);
      }
      process.exit(0);
    }
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    for (const m of messages || []) {
      if (!m || m.key?.fromMe || !m.message) continue;
      const sidecarMsg = baileysMessageToSidecar(m);
      summarizeRawMessage({
        from: sidecarMsg.from,
        author: sidecarMsg.author,
        id: sidecarMsg.id?._serialized,
        type: sidecarMsg.type,
        text: sidecarMsg.body,
      });
      handleMessage(sidecarMsg, `baileys:${type || "upsert"}`);
    }
  });

  const shutdown = async (code) => {
    writeState({ status: "stopping" });
    try { sock.end(new Error("sidecar shutdown")); } catch {}
    process.exit(code);
  };
  process.on("SIGTERM", () => shutdown(0));
  process.on("SIGINT", () => shutdown(130));
}

function startWwebjs() {
  const { Client, LocalAuth } = require("whatsapp-web.js");
  const client = new Client({
    authStrategy: new LocalAuth({
      clientId: "gopay",
      dataPath: sessionDir,
    }),
    puppeteer: {
      headless,
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
      ],
    },
  });

  client.on("loading_screen", (percent, message) => {
    writeState({ status: "loading", percent, message });
  });

  client.on("qr", async (qr) => {
    try {
      const qrDataUrl = await qrToDataUrl(qr);
      writeState({
        status: mode === "pairing" ? "awaiting_pairing_code" : "awaiting_qr_scan",
        qr,
        qr_data_url: qrDataUrl,
      });
    } catch (e) {
      writeState({ status: "awaiting_qr_scan", qr, qr_error: String(e && e.message || e) });
    }

    if (mode === "pairing" && pairingPhone) {
      try {
        const code = await client.requestPairingCode(pairingPhone);
        writeState({ status: "awaiting_pairing_code", code });
        console.log(`[pairing] code=${code}`);
      } catch (e) {
        writeState({ status: "error", error: `pairing code failed: ${String(e && e.message || e)}` });
      }
    }
  });

  client.on("authenticated", () => {
    writeState({ status: "authenticated", qr: null, qr_data_url: null, code: null });
  });

  client.on("auth_failure", (message) => {
    writeState({ status: "auth_failure", error: String(message || "") });
  });

  client.on("ready", () => {
    writeState({ status: "connected", qr: null, qr_data_url: null, code: null, percent: 100, message: "WhatsApp" });
    console.log("[ready] WhatsApp connected (wwebjs)");
  });

  client.on("disconnected", (reason) => {
    writeState({ status: "disconnected", reason: String(reason || "") });
    process.exit(0);
  });

  client.on("message", (msg) => {
    handleMessage(msg, "wwebjs:message");
  });

  client.on("message_create", (msg) => {
    if (!msg.fromMe) return;
    handleMessage(msg, "wwebjs:message_create");
  });

  process.on("SIGTERM", async () => {
    writeState({ status: "stopping" });
    try { await client.destroy(); } catch {}
    process.exit(0);
  });

  process.on("SIGINT", async () => {
    writeState({ status: "stopping" });
    try { await client.destroy(); } catch {}
    process.exit(130);
  });

  client.initialize().catch((e) => {
    writeState({ status: "error", error: String(e && e.stack || e) });
    console.error(e && e.stack || e);
    process.exit(1);
  });
}

if (engine === "baileys") {
  startBaileys().catch((e) => {
    writeState({ status: "error", error: String(e && e.stack || e) });
    console.error(e && e.stack || e);
    process.exit(1);
  });
} else if (engine === "wwebjs" || engine === "whatsapp-web.js") {
  startWwebjs();
} else {
  writeState({ status: "error", error: `unsupported WA_ENGINE=${engine}` });
  console.error(`unsupported WA_ENGINE=${engine}`);
  process.exit(2);
}
