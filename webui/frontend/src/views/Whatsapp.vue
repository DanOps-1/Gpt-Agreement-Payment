<template>
  <div class="wa-root">
    <header class="wizard-header">
      <div class="brand">
        <span class="brand-prompt">$</span>
        <span class="brand-name">gpt-pay</span>
        <span class="brand-sub">// WhatsApp Login</span>
        <span class="brand-clock">{{ clock }}</span>
      </div>
      <div class="run-nav">
        <RouterLink to="/wizard" class="nav-link">Wizard</RouterLink>
        <RouterLink to="/run" class="nav-link">Run</RouterLink>
        <button class="header-btn" @click="logout">Logout</button>
      </div>
    </header>

    <main class="wa-main">
      <section class="wa-panel">
        <div class="term-divider" data-tail="──────────">WhatsApp Login Entrance</div>
        <h2 class="wa-title">Scan QR Code for WhatsApp Web<span class="term-cursor"></span></h2>
        <p class="wa-sub">
          This is the only WhatsApp Login entrance in the frontend. You can freely switch between Baileys / whatsapp-web.js before starting.
          Once connected, the backend sidecar automatically monitors WhatsApp messages, extracts GoPay OTPs, and writes them to the SQLite runtime database for the payment process.
        </p>

        <div class="engine-row">
          <TermSelect
            :model-value="selectedEngine"
            label="Engine · engine"
            :options="engineOptions"
            @update:modelValue="onEngineChange"
          />
        </div>

        <div class="status-card" :class="connectClass">
          <span class="status-dot">●</span>
          <span>{{ statusLabel }}</span>
          <span v-if="status.pid" class="status-meta">PID {{ status.pid }}</span>
        </div>

        <div class="wa-actions">
          <TermBtn :loading="starting" @click="startQr">
            {{ startButtonLabel }}
          </TermBtn>
          <TermBtn v-if="status.running" variant="danger" @click="stop">Stop sidecar</TermBtn>
          <TermBtn variant="danger" :loading="loggingOut" @click="logoutWa">Logout WhatsApp Login</TermBtn>
        </div>

        <div v-if="status.qr_data_url" class="qr-box">
          <img :src="status.qr_data_url" alt="WhatsApp login QR" />
          <p>Open WhatsApp -> Linked Devices -> Link a Device, scan the QR code.</p>
        </div>
        <div v-else-if="status.status === 'connected'" class="connected-box">
          <div class="ok-mark">✓</div>
          <div>
            <strong>WhatsApp Connected</strong>
            <p>GoPay OTPs will be automatically written to SQLite: <code>{{ status.database || "output/webui.db" }}</code></p>
          </div>
        </div>
        <div v-else class="empty-box">
          QR code will be displayed here after clicking "Start WhatsApp Login".
        </div>

        <div v-if="status.engine || status.preferred_engine" class="engine-box">
          <span class="engine-label">Current Engine</span>
          <code>{{ status.engine || status.preferred_engine }}</code>
          <span class="engine-meta">Preference: {{ status.preferred_engine || "baileys" }}</span>
          <span v-if="savingEngine" class="engine-saving">Saving...</span>
        </div>

        <div v-if="status.latest?.otp" class="latest-box">
          <span class="latest-label">Latest OTP</span>
          <code>{{ status.latest.otp }}</code>
          <span class="latest-text">{{ status.latest.text }}</span>
        </div>

        <details class="debug-box">
          <summary>Debug Status</summary>
          <pre>{{ statusJson }}</pre>
        </details>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink, useRouter } from "vue-router";
import { useMessage } from "naive-ui";
import { api } from "../api/client";
import TermBtn from "../components/term/TermBtn.vue";
import TermSelect from "../components/term/TermSelect.vue";

interface WaStatus {
  running: boolean;
  pid: number | null;
  mode: string;
  engine?: string;
  preferred_engine?: string;
  started_at: number | null;
  status: string;
  qr_data_url?: string | null;
  code?: string | null;
  percent?: number;
  message?: string;
  reason?: string;
  error?: string;
  database?: string;
  otp_source?: string;
  latest?: {
    otp?: string;
    text?: string;
    ts?: number;
    from?: string;
  };
}

const router = useRouter();
const message = useMessage();
const status = ref<WaStatus>({
  running: false,
  pid: null,
  mode: "",
  started_at: null,
  status: "stopped",
});
const starting = ref(false);
const loggingOut = ref(false);
const savingEngine = ref(false);
const selectedEngine = ref("baileys");
const engineOptions = [
  { value: "baileys", label: "Baileys (recommended)", desc: "Directly connect to WhatsApp multi-device socket, lighter startup" },
  { value: "wwebjs", label: "whatsapp-web.js", desc: "Chromium path, compatible with old environments / for debugging" },
];
const clock = ref("");
let clockTimer: ReturnType<typeof setInterval> | undefined;
let pollTimer: ReturnType<typeof setInterval> | undefined;
let preferredEngineHydrated = false;

const statusLabel = computed(() => {
  switch (status.value.status) {
    case "stopped": return "Not Started";
    case "starting": return "Starting...";
    case "loading": return `Loading (${status.value.percent ?? 0}%)`;
    case "awaiting_qr_scan": return "Awaiting QR Scan";
    case "authenticated": return "Authenticated, connecting";
    case "connected": return "Connected ✓";
    case "disconnected": return `Disconnected (${status.value.reason || "Unknown"})`;
    case "auth_failure": return `Auth Failure：${status.value.error || ""}`;
    case "error": return `Error: ${status.value.error || ""}`;
    default: return status.value.status || "Unknown";
  }
});

const connectClass = computed(() => {
  switch (status.value.status) {
    case "connected": return "ok";
    case "awaiting_qr_scan":
    case "authenticated":
    case "loading":
    case "starting": return "warn";
    case "disconnected":
    case "auth_failure":
    case "error": return "err";
    default: return "idle";
  }
});

const startButtonLabel = computed(() => {
  const engine = selectedEngine.value || "baileys";
  if (status.value.running && status.value.engine === engine) return "Refresh login status";
  if (status.value.running) return `Switch to ${engine} and restart`;
  return `Start ${engine}`;
});

const statusJson = computed(() => JSON.stringify(status.value, null, 2));

function tick() {
  const d = new Date();
  clock.value = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

async function refresh(syncPreferredEngine = false) {
  try {
    const r = await api.get("/whatsapp/status");
    status.value = r.data;
    if (syncPreferredEngine && !preferredEngineHydrated && r.data?.preferred_engine) {
      selectedEngine.value = r.data.preferred_engine;
      preferredEngineHydrated = true;
    }
  } catch {
    // polling only; ignore transient errors
  }
}

async function onEngineChange(engine: string) {
  const next = engine || "baileys";
  savingEngine.value = true;
  try {
    const r = await api.post("/whatsapp/settings", { engine: next });
    status.value = r.data;
    selectedEngine.value = r.data?.preferred_engine || next;
  } catch (e: any) {
    message.error(e.response?.data?.detail || "Failed to save engine preference");
  } finally {
    savingEngine.value = false;
  }
}

async function startQr() {
  starting.value = true;
  try {
    await api.post("/whatsapp/start", { mode: "qr", engine: selectedEngine.value || "baileys" });
    await refresh();
  } catch (e: any) {
    message.error(e.response?.data?.detail || "Start failed");
  } finally {
    starting.value = false;
  }
}

async function stop() {
  try {
    await api.post("/whatsapp/stop");
    await refresh();
  } catch (e: any) {
    message.error(e.response?.data?.detail || "Stop failed");
  }
}

async function logoutWa() {
  loggingOut.value = true;
  try {
    await api.post("/whatsapp/logout");
    await refresh();
    message.success("Logged out from WhatsApp Login");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "Logout failed");
  } finally {
    loggingOut.value = false;
  }
}

async function logout() {
  await api.post("/logout");
  router.push("/login");
}

onMounted(async () => {
  tick();
  clockTimer = setInterval(tick, 1000);
  await refresh(true);
  pollTimer = setInterval(() => {
    refresh(false);
  }, 1500);
  preferredEngineHydrated = true;
});

onBeforeUnmount(() => {
  if (clockTimer) clearInterval(clockTimer);
  if (pollTimer) clearInterval(pollTimer);
});
</script>

<style scoped>
.wa-root { min-height: 100vh; background: var(--bg-base); color: var(--fg-primary); }
.wizard-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px;
  border-bottom: 1px solid var(--border);
}
.brand { display: flex; align-items: baseline; gap: 10px; font-family: var(--font-mono); }
.brand-prompt { color: var(--accent); }
.brand-name { font-weight: 800; letter-spacing: 0.04em; }
.brand-sub, .brand-clock { color: var(--fg-tertiary); font-size: 12px; }
.run-nav { display: flex; align-items: center; gap: 10px; }
.nav-link, .header-btn {
  border: 1px solid var(--border);
  background: var(--bg-panel);
  color: var(--fg-secondary);
  text-decoration: none;
  padding: 7px 10px;
  font-size: 12px;
}
.nav-link:hover, .header-btn:hover { border-color: var(--accent); color: var(--accent); }
.header-btn { cursor: pointer; font: inherit; }
.wa-main { max-width: 780px; margin: 48px auto; padding: 0 24px; }
.wa-panel {
  border: 1px solid var(--border);
  background: var(--bg-panel);
  padding: 24px;
}
.wa-title { margin: 16px 0 8px; font-size: 22px; }
.wa-sub { color: var(--fg-tertiary); line-height: 1.8; font-size: 13px; }
.status-card {
  margin: 20px 0;
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--border);
  background: var(--bg-base);
  padding: 12px;
}
.status-card.ok { border-color: var(--ok); color: var(--ok); }
.status-card.warn { border-color: var(--warn); color: var(--warn); }
.status-card.err { border-color: var(--err); color: var(--err); }
.status-card.idle { color: var(--fg-tertiary); }
.status-meta { margin-left: auto; color: var(--fg-tertiary); font-size: 12px; }
.engine-row { margin-top: 18px; }
.wa-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 22px; }
.qr-box, .empty-box, .connected-box {
  border: 1px dashed var(--border);
  background: var(--bg-base);
  padding: 22px;
  text-align: center;
}
.qr-box img {
  width: min(320px, 90vw);
  background: white;
  padding: 12px;
}
.qr-box p, .empty-box, .connected-box p { color: var(--fg-tertiary); font-size: 13px; }
.engine-box {
  margin-top: 18px;
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--border);
  background: var(--bg-base);
  padding: 10px 12px;
  font-size: 12px;
}
.engine-label { color: var(--fg-tertiary); }
.engine-meta { margin-left: auto; color: var(--fg-tertiary); }
.engine-saving { color: var(--fg-tertiary); }
.connected-box {
  display: flex;
  align-items: center;
  gap: 16px;
  text-align: left;
}
.ok-mark {
  width: 42px;
  height: 42px;
  border: 1px solid var(--ok);
  color: var(--ok);
  display: grid;
  place-items: center;
  border-radius: 50%;
  font-size: 24px;
}
.latest-box {
  margin-top: 16px;
  display: grid;
  grid-template-columns: max-content max-content minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  border: 1px solid var(--border);
  background: var(--bg-base);
  padding: 10px 12px;
  font-size: 12px;
}
.latest-label { color: var(--fg-tertiary); }
.latest-text {
  min-width: 0;
  color: var(--fg-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.debug-box { margin-top: 16px; color: var(--fg-tertiary); font-size: 12px; }
.debug-box pre {
  overflow: auto;
  max-height: 280px;
  background: var(--bg-base);
  border: 1px solid var(--border);
  padding: 12px;
}
code { color: var(--accent); }
</style>
