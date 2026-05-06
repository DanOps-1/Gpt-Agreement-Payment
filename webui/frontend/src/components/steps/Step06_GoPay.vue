<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="----------">STEP 06: GOPAY OTP</div>
    <h2 class="step-h">$&nbsp;GoPay Accounts + External OTP<span class="term-cursor"></span></h2>
    <p class="step-sub">
      Configure one or more GoPay wallets. Each payment run randomly selects one account, and OTP polling is scoped to that account phone.
    </p>

    <div class="accounts-panel">
      <div class="accounts-head">
        <span class="label">GoPay Accounts</span>
        <TermBtn variant="ghost" @click="addAccount">Add account</TermBtn>
      </div>

      <div v-for="(account, idx) in form.accounts" :key="account.id" class="account-box">
        <div class="account-title">
          <span>GoPay #{{ idx + 1 }}</span>
          <TermBtn
            variant="danger"
            :disabled="form.accounts.length <= 1"
            @click="removeAccount(idx)"
          >
            Delete
          </TermBtn>
        </div>
        <div class="form-stack">
          <TermField v-model="account.label" label="Label" placeholder="wallet label" />
          <TermField v-model="account.country_code" label="country_code" placeholder="62" />
          <TermField v-model="account.phone_number" label="phone_number" placeholder="81234567890" />
          <TermField v-model="account.pin" label="PIN" type="password" placeholder="GoPay PIN" />
        </div>
      </div>
    </div>

    <div class="form-stack otp-settings">
      <TermField v-model.number="form.otp_timeout" label="OTP timeout seconds" type="number" />
    </div>

    <div class="external-card">
      <div class="external-head">
        <div>
          <span class="label">Webhook URL</span>
          <code>{{ webhookUrl }}</code>
        </div>
        <TermBtn variant="ghost" @click="copy(webhookUrl)">Copy URL</TermBtn>
      </div>

      <div class="external-head">
        <div>
          <span class="label">Authorization Token</span>
          <code>{{ status.external_otp_token || "loading..." }}</code>
        </div>
        <TermBtn
          variant="ghost"
          :disabled="!status.external_otp_token"
          @click="copy(status.external_otp_token || '')"
        >
          Copy token
        </TermBtn>
      </div>

      <div class="hint-box">
        <p>Header: <code>Authorization: Bearer {{ status.external_otp_token || "xxx" }}</code></p>
        <p>Body: <code>{{ bodyTemplate }}</code></p>
      </div>

      <div class="curl-box">
        <div class="label">curl test</div>
        <pre>{{ curlExample }}</pre>
        <TermBtn variant="ghost" @click="copy(curlExample)">Copy curl</TermBtn>
      </div>
    </div>

    <div class="gopay-actions">
      <button class="wa-login-entry" type="button" @click="openOtpTest">
        <span class="wa-login-prompt">$</span>
        Test OTP
      </button>
      <button class="wa-login-entry" type="button" @click="openUnbindDialog">
        <span class="wa-login-prompt">$</span>
        Auto unbind
      </button>
    </div>

    <Teleport to="body">
      <div v-if="otpDialog.open" class="otp-overlay" @click.self="closeOtpTest">
        <div class="otp-modal">
          <div class="otp-head">
            <span class="otp-prompt">$</span> GoPay WhatsApp OTP
          </div>
          <p class="otp-desc">
            Send an OTP payload to the webhook. The phone field should match one configured GoPay account.
          </p>
          <input
            class="otp-input"
            v-model="otpDialog.value"
            maxlength="8"
            autofocus
            disabled
            placeholder="000000"
          />
          <div v-if="otpDialog.success" class="otp-success">
            Webhook received the OTP.
          </div>
          <div v-else-if="otpDialog.preparing" class="otp-waiting">
            Preparing test session...
          </div>
          <div v-else class="otp-waiting">
            Waiting for webhook OTP...
          </div>
          <div class="otp-actions">
            <TermBtn variant="ghost" @click="closeOtpTest">Close</TermBtn>
          </div>
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <div v-if="unbindDialog.open" class="otp-overlay" @click.self="closeUnbindDialog">
        <div class="unbind-modal">
          <div class="otp-head">
            <span class="otp-prompt">$</span> GoPay Auto Unbind
          </div>
          <p class="otp-desc">
            Save raw linked-apps requests for the existing unbind helper.
          </p>
          <TermField
            v-model="unbindDialog.base_url"
            label="Base URL"
            placeholder="https://customer.gopayapi.com"
          />
          <textarea
            class="unbind-input"
            v-model="unbindDialog.value"
            autofocus
            spellcheck="false"
            placeholder="Paste GET /v1/linkedapps raw request..."
          />
          <div class="label unlink-label">PATCH raw unlink request</div>
          <textarea
            class="unbind-input unlink-patch-input"
            v-model="unbindDialog.unlink_raw_request"
            spellcheck="false"
            placeholder="Paste PATCH /v1/links/{link_id} raw request..."
          />
          <div v-if="unbindDialog.body || unbindDialog.fetchMeta" class="unbind-response">
            <div class="label">Response Body</div>
            <div v-if="unbindDialog.fetchMeta" class="unbind-meta">{{ unbindDialog.fetchMeta }}</div>
            <div v-if="unbindDialog.hasData" class="unlink-url-box ok">
              <span class="label">LinkedApps Data</span>
              <code>data found</code>
            </div>
            <div v-if="unbindDialog.preview_unlink_url" class="unlink-url-box">
              <span class="label">Preview Unlink URL</span>
              <code>{{ unbindDialog.preview_unlink_url }}</code>
            </div>
            <pre>{{ unbindDialog.body }}</pre>
          </div>
          <div class="otp-actions">
            <TermBtn variant="ghost" @click="closeUnbindDialog">Cancel</TermBtn>
            <TermBtn variant="ghost" :loading="unbindDialog.fetching" @click="fetchUnbindBody">Fetch body</TermBtn>
            <TermBtn @click="saveUnbindRequest">Save</TermBtn>
          </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useMessage } from "naive-ui";
import { api } from "../../api/client";
import { useWizardStore } from "../../stores/wizard";
import TermBtn from "../term/TermBtn.vue";
import TermField from "../term/TermField.vue";

type GoPayAccountForm = {
  id: string;
  label: string;
  country_code: string;
  phone_number: string;
  pin: string;
  midtrans_client_id?: string;
};

const store = useWizardStore();
const message = useMessage();
const init = store.answers.gopay ?? {};
const initOtp = init.otp ?? {};

function newAccount(seed?: Partial<GoPayAccountForm>, index = 0): GoPayAccountForm {
  return {
    id: seed?.id || `${Date.now()}-${Math.random().toString(16).slice(2)}-${index}`,
    label: seed?.label || `account-${index + 1}`,
    country_code: seed?.country_code ?? "62",
    phone_number: seed?.phone_number ?? "",
    pin: seed?.pin ?? "",
    midtrans_client_id: seed?.midtrans_client_id ?? "",
  };
}

function initialAccounts(): GoPayAccountForm[] {
  const raw = Array.isArray(init.accounts) ? init.accounts : [];
  const accounts = raw
    .filter((item: any) => item && typeof item === "object")
    .map((item: any, idx: number) => newAccount({
      label: item.label ?? item.name ?? `account-${idx + 1}`,
      country_code: item.country_code ?? init.country_code ?? "62",
      phone_number: item.phone_number ?? "",
      pin: item.pin ?? "",
      midtrans_client_id: item.midtrans_client_id ?? init.midtrans_client_id ?? "",
    }, idx));
  if (accounts.length) return accounts;
  return [newAccount({
    label: init.label ?? "account-1",
    country_code: init.country_code ?? "62",
    phone_number: init.phone_number ?? "",
    pin: init.pin ?? "",
    midtrans_client_id: init.midtrans_client_id ?? "",
  }, 0)];
}

const form = ref({
  accounts: initialAccounts(),
  otp_timeout: init.otp_timeout ?? initOtp.timeout ?? 300,
  auto_unbind_raw_request: init.auto_unbind_raw_request ?? init.auto_unbind?.raw_request ?? "",
  auto_unbind_base_url: init.auto_unbind_base_url ?? init.auto_unbind?.base_url ?? "",
  auto_unbind_unlink_raw_request: init.auto_unbind_unlink_raw_request ?? init.auto_unbind?.unlink_raw_request ?? "",
});

const status = ref<{
  external_otp_token?: string;
  updated_at?: number;
  latest?: {
    otp?: string;
    phone?: string;
    country_code?: string;
    ts?: number;
    received_at?: number;
    source?: string;
  };
}>({});
const otpDialog = ref({
  open: false,
  value: "",
  success: false,
  since: 0,
  preparing: false,
});
const unbindDialog = ref({
  open: false,
  value: "",
  base_url: "",
  unlink_raw_request: "",
  fetching: false,
  body: "",
  fetchMeta: "",
  hasData: false,
  preview_unlink_url: "",
});
let timer: number | undefined;
let otpTestTimer: number | undefined;

const webhookUrl = computed(() => {
  const base = import.meta.env.BASE_URL || "/";
  return new URL(`${base}api/whatsapp/external-otp`, window.location.origin).toString();
});

const sampleAccount = computed(() => form.value.accounts[0] || newAccount({}, 0));
const samplePhone = computed(() => sampleAccount.value.phone_number || "81234567890");
const sampleCountryCode = computed(() => sampleAccount.value.country_code || "62");

const curlExample = computed(() => `curl -X POST '${webhookUrl.value}' \\
  -H 'Authorization: Bearer ${status.value.external_otp_token || "xxx"}' \\
  -H 'Content-Type: application/json' \\
  -d '{"otp":"123456","phone":"${samplePhone.value}","country_code":"${sampleCountryCode.value}","source":"android-notification-forwarder","ts":1234567890}'`);

const bodyTemplate = computed(
  () => `{"otp":"123456","phone":"${samplePhone.value}","country_code":"${sampleCountryCode.value}","source":"android-notification-forwarder","ts":{{timestamp}}}`,
);

function addAccount() {
  form.value.accounts.push(newAccount({}, form.value.accounts.length));
}

function removeAccount(index: number) {
  if (form.value.accounts.length <= 1) return;
  form.value.accounts.splice(index, 1);
}

async function refreshStatus() {
  try {
    const r = await api.get("/whatsapp/status");
    status.value = r.data;
    maybeResolveOtpTest();
  } catch {}
}

async function copy(value: string) {
  if (!value) return;
  await navigator.clipboard.writeText(value);
  message.success("Copied");
}

async function openOtpTest() {
  otpDialog.value = {
    open: true,
    value: "",
    success: false,
    since: Date.now() / 1000,
    preparing: true,
  };
  if (otpTestTimer) window.clearInterval(otpTestTimer);
  try {
    const r = await api.post("/whatsapp/test-otp/start");
    otpDialog.value.since = Number(r.data?.since || otpDialog.value.since);
    if (r.data?.status) status.value = r.data.status;
  } catch {
    message.warning("Test session failed to start; using browser time.");
  } finally {
    otpDialog.value.preparing = false;
  }
  await refreshStatus();
  otpTestTimer = window.setInterval(refreshStatus, 1000);
}

function closeOtpTest() {
  otpDialog.value.open = false;
  if (otpTestTimer) {
    window.clearInterval(otpTestTimer);
    otpTestTimer = undefined;
  }
}

function maybeResolveOtpTest() {
  if (!otpDialog.value.open || otpDialog.value.success || otpDialog.value.preparing) return;
  const latest = status.value.latest;
  const otp = latest?.otp || "";
  const arrivedAt = Number(status.value.updated_at || latest?.received_at || 0);
  if (!otp || arrivedAt < otpDialog.value.since) return;
  otpDialog.value.value = otp;
  otpDialog.value.success = true;
  message.success("OTP webhook test passed");
  if (otpTestTimer) {
    window.clearInterval(otpTestTimer);
    otpTestTimer = undefined;
  }
}

function openUnbindDialog() {
  unbindDialog.value.open = true;
  unbindDialog.value.value = form.value.auto_unbind_raw_request || "";
  unbindDialog.value.base_url = form.value.auto_unbind_base_url || "";
  unbindDialog.value.unlink_raw_request = form.value.auto_unbind_unlink_raw_request || "";
  unbindDialog.value.body = "";
  unbindDialog.value.fetchMeta = "";
  unbindDialog.value.hasData = false;
  unbindDialog.value.preview_unlink_url = "";
}

function closeUnbindDialog() {
  unbindDialog.value.open = false;
}

async function saveUnbindRequest() {
  form.value.auto_unbind_raw_request = unbindDialog.value.value;
  form.value.auto_unbind_base_url = unbindDialog.value.base_url.trim();
  form.value.auto_unbind_unlink_raw_request = unbindDialog.value.unlink_raw_request;
  store.setAnswer("gopay", buildGopayAnswer());
  try {
    await store.saveToServer();
    await api.post("/config/gopay/auto-unbind", {
      raw_request: form.value.auto_unbind_raw_request,
      base_url: form.value.auto_unbind_base_url,
      unlink_raw_request: form.value.auto_unbind_unlink_raw_request,
    });
    closeUnbindDialog();
    message.success("Auto unbind request saved");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "Failed to save auto unbind request");
  }
}

async function fetchUnbindBody() {
  if (!unbindDialog.value.value.trim()) {
    message.warning("Paste a raw request first");
    return;
  }
  unbindDialog.value.fetching = true;
  unbindDialog.value.body = "";
  unbindDialog.value.fetchMeta = "";
  unbindDialog.value.hasData = false;
  unbindDialog.value.preview_unlink_url = "";
  try {
    const r = await api.post("/config/gopay/auto-unbind/fetch-body", {
      base_url: unbindDialog.value.base_url,
      raw_request: unbindDialog.value.value,
    });
    const body = typeof r.data?.body === "string"
      ? r.data.body
      : JSON.stringify(r.data?.body_json ?? "", null, 2);
    unbindDialog.value.body = body;
    unbindDialog.value.fetchMeta = `${r.data?.status_code || ""} ${r.data?.content_type || ""}`.trim();
    unbindDialog.value.hasData = Boolean(r.data?.has_data);
    unbindDialog.value.preview_unlink_url = r.data?.unlink_url || "";
    message.success(unbindDialog.value.hasData ? "LinkedApps data found" : "Response fetched");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "Failed to fetch body");
  } finally {
    unbindDialog.value.fetching = false;
  }
}

function cleanAccount(account: GoPayAccountForm, index: number) {
  return {
    label: account.label || `account-${index + 1}`,
    country_code: String(account.country_code || "").replace(/^\+/, ""),
    phone_number: String(account.phone_number || ""),
    pin: String(account.pin || ""),
    midtrans_client_id: String(account.midtrans_client_id || ""),
  };
}

function buildGopayAnswer() {
  const accounts = form.value.accounts.map(cleanAccount);
  const first = accounts[0] || cleanAccount(newAccount({}, 0), 0);
  return {
    country_code: first.country_code,
    phone_number: first.phone_number,
    pin: first.pin,
    midtrans_client_id: first.midtrans_client_id,
    accounts,
    otp_timeout: form.value.otp_timeout,
    auto_unbind_raw_request: form.value.auto_unbind_raw_request,
    auto_unbind_base_url: form.value.auto_unbind_base_url,
    auto_unbind_unlink_raw_request: form.value.auto_unbind_unlink_raw_request,
    otp: {
      source: "auto",
      timeout: form.value.otp_timeout,
      interval: 1,
    },
  };
}

watch(form, () => {
  store.setAnswer("gopay", buildGopayAnswer());
  store.saveToServer();
}, { deep: true });

onMounted(() => {
  refreshStatus();
  timer = window.setInterval(refreshStatus, 5000);
});

onUnmounted(() => {
  if (timer) window.clearInterval(timer);
  if (otpTestTimer) window.clearInterval(otpTestTimer);
});
</script>

<style scoped>
.accounts-panel {
  display: grid;
  gap: 14px;
}
.accounts-head,
.account-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.account-box {
  display: grid;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
}
.account-title {
  color: var(--accent);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.otp-settings {
  margin-top: 14px;
}
.external-card {
  margin-top: 22px;
  display: grid;
  gap: 14px;
  padding: 16px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
}
.external-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  padding-bottom: 12px;
  border-bottom: 1px dashed var(--border);
}
.label {
  display: block;
  margin-bottom: 6px;
  color: var(--fg-tertiary);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
code {
  word-break: break-all;
}
.hint-box {
  padding: 12px 14px;
  border: 1px dashed var(--border);
  background: rgba(255,255,255,0.03);
  font-size: 12px;
  color: var(--fg-secondary);
}
.hint-box p {
  margin: 6px 0;
}
.curl-box {
  display: grid;
  gap: 10px;
}
.curl-box pre {
  overflow: auto;
  margin: 0;
  padding: 12px;
  border: 1px solid var(--border);
  background: var(--bg-base);
  color: var(--fg-primary);
  font-size: 12px;
  line-height: 1.6;
}
.gopay-actions {
  margin-top: 18px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.wa-login-entry {
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
  font-family: inherit;
  cursor: pointer;
}
.wa-login-entry:hover {
  background: rgba(93, 255, 174, 0.12);
}
.wa-login-prompt {
  color: var(--fg-primary);
}
.otp-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.otp-modal,
.unbind-modal {
  background: var(--bg-base);
  border: 1px solid var(--accent);
  padding: 24px 28px;
  font-family: inherit;
  box-shadow: 0 10px 40px rgba(0,0,0,0.25);
}
.otp-modal {
  width: min(420px, 90vw);
}
.unbind-modal {
  width: min(720px, 92vw);
}
.otp-head {
  font-weight: 700;
  font-size: 14px;
  letter-spacing: 0.06em;
  color: var(--accent);
  margin-bottom: 4px;
}
.otp-prompt {
  color: var(--fg-tertiary);
  margin-right: 6px;
}
.otp-desc {
  color: var(--fg-secondary);
  font-size: 12px;
  line-height: 1.6;
  margin: 8px 0 16px;
}
.otp-input {
  width: 100%;
  box-sizing: border-box;
  padding: 12px 14px;
  border: 1px solid var(--border-strong);
  background: var(--bg-panel);
  font: inherit;
  font-size: 22px;
  letter-spacing: 0.4em;
  text-align: center;
  color: var(--fg-primary);
  outline: none;
  font-variant-numeric: tabular-nums;
}
.otp-input:disabled {
  opacity: 1;
  cursor: default;
}
.otp-input:focus {
  border-color: var(--accent);
}
.unbind-input {
  width: 100%;
  min-height: 280px;
  box-sizing: border-box;
  resize: vertical;
  padding: 12px 14px;
  border: 1px solid var(--border-strong);
  background: var(--bg-panel);
  color: var(--fg-primary);
  font: inherit;
  font-size: 12px;
  line-height: 1.6;
  outline: none;
  white-space: pre;
}
.unlink-label {
  margin-top: 12px;
}
.unlink-patch-input {
  min-height: 180px;
}
.unbind-input:focus {
  border-color: var(--accent);
}
.unbind-response {
  margin-top: 12px;
  border: 1px dashed var(--border);
  background: var(--bg-panel);
  padding: 12px;
}
.unbind-response pre {
  margin: 0;
  max-height: 260px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--fg-primary);
  font-size: 12px;
  line-height: 1.6;
}
.unbind-meta {
  margin-bottom: 8px;
  color: var(--fg-tertiary);
  font-size: 11px;
}
.unlink-url-box {
  margin-bottom: 10px;
  padding: 10px;
  border: 1px solid var(--border);
  background: var(--bg-base);
}
.unlink-url-box.ok {
  border-color: var(--ok);
}
.unlink-url-box code {
  display: block;
  color: var(--accent);
  word-break: break-all;
}
.otp-success,
.otp-waiting {
  margin-top: 12px;
  font-size: 12px;
  line-height: 1.6;
}
.otp-success {
  color: var(--ok);
}
.otp-waiting {
  color: var(--fg-tertiary);
}
.otp-actions {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
