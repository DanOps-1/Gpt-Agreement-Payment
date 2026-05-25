<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 07: GoPay Account</div>
    <h2 class="step-h">$&nbsp;GoPay (Indonesia e-wallet)<span class="term-cursor"></span></h2>
    <p class="step-sub">Each ChatGPT Plus subscription consumes 1 WhatsApp OTP + 2 PIN entries. Lite account (no Indonesia KYC) monthly limit ~IDR 2M ≈ 5-6 transactions.</p>

    <div class="form-stack">
      <TermField v-model="form.country_code" label="Country Code · country_code" placeholder="86 (Mainland China) / 62 (Indonesia)" />
      <TermField v-model="form.phone_number" label="Phone Number · phone_number" placeholder="Without country code, 11 digits" />
      <TermField v-model="form.pin" label="6-digit PIN · pin" type="password" placeholder="PIN set when logging into GoJek/GoPay" />
      <TermField v-model.number="form.otp_timeout" label="OTP Wait Timeout (seconds)" type="number" />
      <TermSelect
        v-model="form.whatsapp_engine"
        label="WhatsApp Engine"
        :options="engineOptions"
      />
    </div>

    <RouterLink class="wa-login-entry" to="/whatsapp">
      <span class="wa-login-prompt">$</span>
      WhatsApp Login / Scan to Receive GoPay OTP
    </RouterLink>

    <div class="hint-box">
      <p>Frontend only retains the WhatsApp login entry above. After scanning to connect, the backend automatically monitors WhatsApp messages and writes GoPay OTP for the payment process to read.</p>
      <p>PIN is automatically used after configuration, once for binding and once for deduction.</p>
      <p>When rebinding with the same number, the first attempt returns 406 "account already linked", and gopay.py will automatically retry once.</p>
    </div>

    <details class="ingest-box">
      <summary>External Service Push OTP (HTTP Interface)</summary>
      <p class="ingest-desc">
        Let any third-party service (SMS gateway, email parser, carrier receipt, etc.) POST verification code to the interface below.
        <strong>Time Window Mechanism</strong>: The interface is closed by default and only opens when the runtime page displays OTP input box (i.e., pipeline is waiting for OTP);
        POST returns 409 at other times. After successful POST, the runtime page automatically fills and submits, pipeline continues immediately.
      </p>
      <div class="ingest-status" :class="ingestInfo?.active ? 'on' : 'off'">
        <span class="status-dot">●</span>
        <span>{{ ingestInfo?.active ? "Interface window is open (pipeline is waiting for OTP)" : "Interface window is closed (will open after starting a GoPay process)" }}</span>
        <TermBtn variant="ghost" class="refresh-btn" @click="loadIngestInfo(true)">Refresh</TermBtn>
      </div>
      <div class="ingest-meta">
        <div><span class="ingest-label">Method</span><code>POST</code></div>
        <div><span class="ingest-label">URL</span><code>{{ ingestUrl || "Loading…" }}</code></div>
        <div><span class="ingest-label">Auth</span><code>X-WA-Relay-Token</code> header or <code>?token=</code> query parameter</div>
        <div><span class="ingest-label">Request Body</span><code>{"otp":"123456"}</code>(non-numeric characters are automatically stripped)</div>
        <div v-if="ingestInfo?.token"><span class="ingest-label">Token</span><code class="ingest-token">{{ ingestInfo.token }}</code></div>
      </div>
      <pre class="ingest-pre">{{ ingestCurl || "Loading…" }}</pre>
      <div v-if="ingestError" class="ingest-error">
        Load failed: {{ ingestError }}
        <TermBtn variant="ghost" @click="loadIngestInfo(true)">Retry</TermBtn>
      </div>
      <div class="ingest-actions">
        <TermBtn variant="ghost" :disabled="!ingestCurl" @click="copyIngestCurl">Copy curl</TermBtn>
        <TermBtn variant="ghost" :disabled="!ingestInfo?.token" @click="copyIngestToken">Copy Token Only</TermBtn>
      </div>
      <p class="ingest-note">
        Token is shared with WhatsApp sidecar / <code>/latest-otp</code>; clicking "Logout WhatsApp" on the WebUI "WhatsApp Login" page will not reset it.
      </p>
    </details>

    <details class="ingest-box">
      <summary>GoPay Link Status (HTTP Interface)</summary>
      <p class="ingest-desc">
        After each successful charge, this system marks the current phone number as <strong>linked</strong>.
        The next time GoPay process starts, it checks this status first: if still linked, the runtime page directly returns 409 to refuse startup, avoiding
        GoPay's 406 "account already linked". After manual unbinding on GoPay side, external services must POST <code>/unlink</code> to flip the status back so pipeline can continue.
      </p>
      <div class="ingest-status" :class="linkStatus?.linked ? 'on' : 'off'">
        <span class="status-dot">●</span>
        <span v-if="!form.phone_number">Phone number not configured</span>
        <span v-else-if="linkStatus?.linked">Linked ({{ formatTs(linkStatus.linked_at) }}) — Starting GoPay process will be rejected</span>
        <span v-else>Not linked — pipeline can start</span>
        <TermBtn variant="ghost" class="refresh-btn" @click="loadLinkStatus(true)">Refresh</TermBtn>
      </div>
      <div class="ingest-meta">
        <div><span class="ingest-label">Phone</span><code>{{ currentPhoneKey || "Not configured" }}</code></div>
        <div v-if="linkStatus?.payment_ref"><span class="ingest-label">Payment ref</span><code>{{ linkStatus.payment_ref }}</code></div>
        <div v-if="linkStatus?.linked_at"><span class="ingest-label">linked at</span><code>{{ formatTs(linkStatus.linked_at) }}</code></div>
        <div v-if="linkStatus?.unlinked_at"><span class="ingest-label">unlinked at</span><code>{{ formatTs(linkStatus.unlinked_at) }}</code></div>
        <div v-if="linkStatus?.last_changed_by"><span class="ingest-label">Changed by</span><code>{{ linkStatus.last_changed_by }}</code></div>
      </div>
      <div class="ingest-meta">
        <div><span class="ingest-label">Query</span><code>GET {{ ingestOrigin }}/api/gopay/link-state</code> or <code>/{phone}</code>(accept session or token)</div>
        <div><span class="ingest-label">Unlink</span><code>POST {{ ingestOrigin }}/api/gopay/link-state/unlink</code>(token only)</div>
        <div><span class="ingest-label">Request Body</span><code>{"phone":"86138...","source":"my-worker"}</code></div>
      </div>
      <pre class="ingest-pre">{{ unlinkCurl || "Loading…" }}</pre>
      <div v-if="linkError" class="ingest-error">
        Load failed: {{ linkError }}
        <TermBtn variant="ghost" @click="loadLinkStatus(true)">Retry</TermBtn>
      </div>
      <div class="ingest-actions">
        <TermBtn variant="ghost" :disabled="!unlinkCurl" @click="copyUnlinkCurl">Copy unlink curl</TermBtn>
        <TermBtn
          variant="danger"
          :loading="unlinking"
          :disabled="!linkStatus?.linked"
          @click="unlinkNow"
        >Unlink Now (This Machine Only)</TermBtn>
      </div>
      <p class="ingest-note">
        Manual unlink only clears this system's marking; the actual link on GoPay server side needs to be handled by you or external services on GoPay/Midtrans side.
      </p>
    </details>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import { useMessage } from "naive-ui";
import { useWizardStore } from "../../stores/wizard";
import { api } from "../../api/client";
import TermBtn from "../term/TermBtn.vue";
import TermField from "../term/TermField.vue";
import TermSelect from "../term/TermSelect.vue";

const store = useWizardStore();
const message = useMessage();
const init = store.answers.gopay ?? {};
const initOtp = init.otp ?? {};
const form = ref({
  country_code: init.country_code ?? "86",
  phone_number: init.phone_number ?? "",
  pin: init.pin ?? "",
  otp_timeout: init.otp_timeout ?? initOtp.timeout ?? 300,
  whatsapp_engine: init.whatsapp_engine ?? "baileys",
});

const engineOptions = [
  { value: "baileys", label: "Baileys (Recommended)", desc: "Direct connection to WhatsApp multi-device socket, lighter startup" },
  { value: "wwebjs", label: "whatsapp-web.js", desc: "Chromium path, compatible with legacy environments / debugging" },
];

interface IngestInfo {
  path: string;
  method: string;
  token: string;
  header_name: string;
  query_name: string;
  active: boolean;
}
const ingestInfo = ref<IngestInfo | null>(null);
const ingestError = ref("");
let ingestLoading = false;

const apiBase = computed(() => {
  // import.meta.env.BASE_URL is "/webui/" or "/" depending on how the build
  // was served. The path returned by the backend already starts with "/api"
  // and assumes that base, so we strip the trailing slash from BASE_URL.
  const base = (import.meta.env.BASE_URL || "/").replace(/\/$/, "");
  return `${window.location.origin}${base}`;
});

const ingestUrl = computed(() => {
  if (!ingestInfo.value) return "";
  return `${apiBase.value}${ingestInfo.value.path}`;
});

const ingestCurl = computed(() => {
  if (!ingestInfo.value) return "";
  return `curl -X ${ingestInfo.value.method} '${ingestUrl.value}' \\
  -H 'Content-Type: application/json' \\
  -H '${ingestInfo.value.header_name}: ${ingestInfo.value.token}' \\
  -d '{"otp":"123456"}'`;
});

async function loadIngestInfo(force = false) {
  if (ingestLoading) return;
  if (!force && ingestInfo.value) return;
  ingestLoading = true;
  ingestError.value = "";
  try {
    const r = await api.get<IngestInfo>("/whatsapp/ingest-info");
    ingestInfo.value = r.data;
  } catch (e: any) {
    ingestError.value = e?.response?.data?.detail || e?.message || "Unknown error";
  } finally {
    ingestLoading = false;
  }
}

onMounted(() => {
  loadIngestInfo();
  loadLinkStatus(true);
  // Both ingest-info.active (OTP API window) and link-state are live values:
  // ingest-info flips when the pipeline starts/finishes asking for an OTP, and
  // link-state flips after a successful charge. Poll both so the page reflects
  // current state without requiring a manual refresh click.
  linkPollTimer = setInterval(() => {
    loadLinkStatus(false);
    loadIngestInfo(true);
  }, 3000);
});

onBeforeUnmount(() => {
  if (linkPollTimer) clearInterval(linkPollTimer);
});

async function copyIngestCurl() {
  if (!ingestCurl.value) return;
  await navigator.clipboard.writeText(ingestCurl.value);
  message.success("curl example copied");
}

async function copyIngestToken() {
  if (!ingestInfo.value?.token) return;
  await navigator.clipboard.writeText(ingestInfo.value.token);
  message.success("Token copied");
}

interface LinkStatus {
  phone: string;
  linked: boolean;
  linked_at: number | null;
  unlinked_at: number | null;
  payment_ref: string;
  last_changed_by: string;
}
const linkStatus = ref<LinkStatus | null>(null);
const linkError = ref("");
const unlinking = ref(false);
let linkLoading = false;
let linkPollTimer: ReturnType<typeof setInterval> | undefined;

const ingestOrigin = computed(() => apiBase.value);

const currentPhoneKey = computed(() => {
  const cc = String(form.value.country_code || "").replace(/\D/g, "");
  const pn = String(form.value.phone_number || "").replace(/\D/g, "");
  if (!cc || !pn) return "";
  return cc + pn;
});

const unlinkCurl = computed(() => {
  if (!ingestInfo.value?.token) return "";
  const phone = currentPhoneKey.value || "<phone_digits>";
  return `curl -X POST '${apiBase.value}/api/gopay/link-state/unlink' \\
  -H 'Content-Type: application/json' \\
  -H '${ingestInfo.value.header_name}: ${ingestInfo.value.token}' \\
  -d '{"phone":"${phone}","source":"my-worker"}'`;
});

function formatTs(ts: number | null | undefined): string {
  if (!ts) return "—";
  try {
    return new Date(Number(ts) * 1000).toLocaleString();
  } catch {
    return String(ts);
  }
}

async function loadLinkStatus(force = false) {
  const phone = currentPhoneKey.value;
  if (!phone) {
    linkStatus.value = null;
    return;
  }
  if (linkLoading && !force) return;
  linkLoading = true;
  linkError.value = "";
  try {
    const r = await api.get<LinkStatus>(`/gopay/link-state/${encodeURIComponent(phone)}`);
    linkStatus.value = r.data;
  } catch (e: any) {
    linkError.value = e?.response?.data?.detail || e?.message || "Unknown error";
  } finally {
    linkLoading = false;
  }
}

async function copyUnlinkCurl() {
  if (!unlinkCurl.value) return;
  await navigator.clipboard.writeText(unlinkCurl.value);
  message.success("unlink curl copied");
}

async function unlinkNow() {
  if (!ingestInfo.value?.token) {
    message.error("Token not loaded");
    return;
  }
  const phone = currentPhoneKey.value;
  if (!phone) {
    message.warning("Please enter phone number first");
    return;
  }
  unlinking.value = true;
  try {
    await api.post(
      "/gopay/link-state/unlink",
      { phone, source: "webui_manual" },
      { headers: { [ingestInfo.value.header_name]: ingestInfo.value.token } },
    );
    await loadLinkStatus(true);
    message.success("Unlinked");
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "Unlink failed");
  } finally {
    unlinking.value = false;
  }
}

watch(() => currentPhoneKey.value, () => {
  loadLinkStatus(true);
});

watch(form, () => {
  store.setAnswer("gopay", form.value);
  store.saveToServer();
}, { deep: true });
</script>

<style scoped>
.hint-box {
  margin-top: 24px;
  padding: 12px 14px;
  border: 1px dashed var(--border);
  background: var(--bg-panel);
  font-size: 12px;
  color: var(--fg-tertiary);
}
.hint-box p { margin: 4px 0; }
.wa-login-entry {
  margin-top: 18px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--accent);
  color: var(--accent);
  background: rgba(93, 255, 174, 0.06);
  text-decoration: none;
  padding: 10px 14px;
  font-size: 13px;
  font-weight: 700;
}
.wa-login-entry:hover {
  background: rgba(93, 255, 174, 0.12);
}
.wa-login-prompt {
  color: var(--fg-primary);
}
.ingest-box {
  margin-top: 18px;
  border: 1px dashed var(--border);
  background: var(--bg-panel);
  padding: 12px 14px;
  font-size: 12px;
  color: var(--fg-secondary);
}
.ingest-box > summary {
  cursor: pointer;
  font-weight: 700;
  color: var(--accent);
  list-style: none;
}
.ingest-box > summary::-webkit-details-marker { display: none; }
.ingest-box > summary::before {
  content: "▸ ";
}
.ingest-box[open] > summary::before {
  content: "▾ ";
}
.ingest-desc {
  margin: 10px 0;
  color: var(--fg-tertiary);
  line-height: 1.7;
}
.ingest-meta {
  display: grid;
  grid-template-columns: 1fr;
  gap: 4px;
  margin: 8px 0;
}
.ingest-meta > div { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.ingest-label {
  display: inline-block;
  width: 56px;
  color: var(--fg-tertiary);
  flex-shrink: 0;
}
.ingest-token {
  word-break: break-all;
  user-select: all;
}
.ingest-pre {
  margin: 10px 0;
  background: var(--bg-base);
  border: 1px solid var(--border);
  padding: 10px 12px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--fg-primary);
}
.ingest-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.ingest-error {
  margin: 8px 0;
  padding: 8px 10px;
  border: 1px solid var(--err);
  color: var(--err);
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.ingest-status {
  margin: 10px 0;
  padding: 8px 10px;
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.ingest-status.on { border-color: var(--ok); color: var(--ok); }
.ingest-status.off { border-color: var(--fg-tertiary); color: var(--fg-tertiary); }
.ingest-status .status-dot { font-size: 14px; }
.ingest-status .refresh-btn { margin-left: auto; }
.ingest-note {
  margin-top: 10px;
  color: var(--fg-tertiary);
  line-height: 1.6;
}
.ingest-box code { color: var(--accent); }
</style>