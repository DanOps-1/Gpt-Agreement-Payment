<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 06: 外部 OTP 接入</div>
    <h2 class="step-h">$&nbsp;GoPay + 外部 OTP 接入<span class="term-cursor"></span></h2>
    <p class="step-sub">
      使用 Android 通知转发器或其他 webhook 工具把 WhatsApp/GoPay 的 6 位 OTP 推到 WebUI。
      Run 页的手动 OTP 弹窗仍保留为 fallback；外部 OTP 到达后会自动写入 SQLite 并关闭 fallback。
    </p>

    <div class="form-stack">
      <TermField v-model="form.country_code" label="国家码 · country_code" placeholder="62 (印尼) / 86 (中国大陆)" />
      <TermField v-model="form.phone_number" label="手机号 · phone_number" placeholder="不带国家码" />
      <TermField v-model="form.pin" label="6 位 PIN · pin" type="password" placeholder="GoPay PIN" />
      <TermField v-model.number="form.otp_timeout" label="OTP 等待超时秒数" type="number" />
    </div>

    <div class="external-card">
      <div class="external-head">
        <div>
          <span class="label">Webhook URL</span>
          <code>{{ webhookUrl }}</code>
        </div>
        <TermBtn variant="ghost" @click="copy(webhookUrl)">复制 URL</TermBtn>
      </div>

      <div class="external-head">
        <div>
          <span class="label">Authorization Token</span>
          <code>{{ status.external_otp_token || "加载中..." }}</code>
        </div>
        <TermBtn variant="ghost" :disabled="!status.external_otp_token" @click="copy(status.external_otp_token || '')">复制 token</TermBtn>
      </div>

      <div class="hint-box">
        <p><strong>Android 通知转发器配置</strong></p>
        <p>URL: <code>{{ webhookUrl }}</code></p>
        <p>Header: <code>Authorization: Bearer {{ status.external_otp_token || "xxx" }}</code></p>
        <p>Body 模板: <code>{{ bodyTemplate }}</code></p>
        <p>建议只匹配 GoPay/WhatsApp 通知中的 6 位数字，避免把其他验证码误投进支付流程。</p>
      </div>

      <div class="curl-box">
        <div class="label">curl 测试</div>
        <pre>{{ curlExample }}</pre>
        <TermBtn variant="ghost" @click="copy(curlExample)">复制 curl</TermBtn>
      </div>
    </div>

    <div class="gopay-actions">
      <button class="wa-login-entry" type="button" @click="openOtpTest">
        <span class="wa-login-prompt">$</span>
        测试 OTP
      </button>
      <button class="wa-login-entry" type="button" @click="openUnbindDialog">
        <span class="wa-login-prompt">$</span>
        自动解绑认证
      </button>
    </div>

    <Teleport to="body">
      <div v-if="otpDialog.open" class="otp-overlay" @click.self="closeOtpTest">
        <div class="otp-modal">
          <div class="otp-head">
            <span class="otp-prompt">$</span> GoPay WhatsApp OTP
          </div>
          <p class="otp-desc">
            保持这个窗口打开，然后用上面的 webhook 推送 OTP。收到后会自动填入验证码，并标记测试成功。
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
            测试成功：webhook 已接收 OTP
          </div>
          <div v-else-if="otpDialog.preparing" class="otp-waiting">
            正在创建测试会话...
          </div>
          <div v-else class="otp-waiting">
            等待 webhook 接收验证码...
          </div>
          <div class="otp-actions">
            <TermBtn variant="ghost" @click="closeOtpTest">关闭</TermBtn>
          </div>
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <div v-if="unbindDialog.open" class="otp-overlay" @click.self="closeUnbindDialog">
        <div class="unbind-modal">
          <div class="otp-head">
            <span class="otp-prompt">$</span> GoPay 自动解绑认证
          </div>
          <p class="otp-desc">
            粘贴原始请求内容，保存后会写入配置，后续自动解绑流程会读取这段请求。
          </p>
          <TermField
            v-model="unbindDialog.base_url"
            label="Base URL · base_url"
            placeholder="https://customer.gopayapi.com"
          />
          <textarea
            class="unbind-input"
            v-model="unbindDialog.value"
            autofocus
            spellcheck="false"
            placeholder="粘贴原始请求..."
          />
          <div v-if="unbindDialog.body || unbindDialog.fetchMeta" class="unbind-response">
            <div class="label">Response Body</div>
            <div v-if="unbindDialog.fetchMeta" class="unbind-meta">{{ unbindDialog.fetchMeta }}</div>
            <div v-if="unbindDialog.hasData" class="unlink-url-box ok">
              <span class="label">LinkedApps Data</span>
              <code>data 已获取，Run 支付成功后会重新获取 unlink_url 并自动解绑。</code>
            </div>
            <div v-if="unbindDialog.preview_unlink_url" class="unlink-url-box">
              <span class="label">Preview Unlink URL</span>
              <code>{{ unbindDialog.preview_unlink_url }}</code>
            </div>
            <pre>{{ unbindDialog.body }}</pre>
          </div>
          <div class="otp-actions">
            <TermBtn variant="ghost" @click="closeUnbindDialog">取消</TermBtn>
            <TermBtn variant="ghost" :loading="unbindDialog.fetching" @click="fetchUnbindBody">解析并获取 body</TermBtn>
            <TermBtn @click="saveUnbindRequest">保存</TermBtn>
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

const store = useWizardStore();
const message = useMessage();
const init = store.answers.gopay ?? {};
const initOtp = init.otp ?? {};

const form = ref({
  country_code: init.country_code ?? "62",
  phone_number: init.phone_number ?? "",
  pin: init.pin ?? "",
  otp_timeout: init.otp_timeout ?? initOtp.timeout ?? 300,
  auto_unbind_raw_request: init.auto_unbind_raw_request ?? init.auto_unbind?.raw_request ?? "",
  auto_unbind_base_url: init.auto_unbind_base_url ?? init.auto_unbind?.base_url ?? "",
});

const status = ref<{
  external_otp_token?: string;
  updated_at?: number;
  latest?: {
    otp?: string;
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

const curlExample = computed(() => `curl -X POST '${webhookUrl.value}' \\
  -H 'Authorization: Bearer ${status.value.external_otp_token || "xxx"}' \\
  -H 'Content-Type: application/json' \\
  -d '{"otp":"123456","source":"android-notification-forwarder","ts":1234567890}'`);

const bodyTemplate = '{"otp":"{{regex 6 digits}}","source":"android-notification-forwarder","ts":{{timestamp}}}';

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
  message.success("已复制");
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
    message.warning("测试会话创建失败，已使用浏览器时间作为备用");
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
  message.success("OTP webhook 测试成功");
  if (otpTestTimer) {
    window.clearInterval(otpTestTimer);
    otpTestTimer = undefined;
  }
}

function openUnbindDialog() {
  unbindDialog.value.open = true;
  unbindDialog.value.value = form.value.auto_unbind_raw_request || "";
  unbindDialog.value.base_url = form.value.auto_unbind_base_url || "";
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
  store.setAnswer("gopay", buildGopayAnswer());
  try {
    await store.saveToServer();
    await api.post("/config/gopay/auto-unbind", {
      raw_request: form.value.auto_unbind_raw_request,
      base_url: form.value.auto_unbind_base_url,
    });
    closeUnbindDialog();
    message.success("自动解绑原始请求已保存到 config");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "保存自动解绑请求失败");
  }
}

async function fetchUnbindBody() {
  if (!unbindDialog.value.value.trim()) {
    message.warning("请先粘贴原始请求");
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
    message.success(unbindDialog.value.hasData ? "已获取 linkedapps data" : "已获取响应 body，但未找到 data");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "获取 body 失败");
  } finally {
    unbindDialog.value.fetching = false;
  }
}

function buildGopayAnswer() {
  return {
    ...form.value,
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
.hint-box p { margin: 6px 0; }
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
.wa-login-entry:hover { background: rgba(93, 255, 174, 0.12); }
.wa-login-prompt { color: var(--fg-primary); }

/* GoPay OTP modal */
.otp-overlay {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
}
.otp-modal {
  background: var(--bg-base);
  border: 1px solid var(--accent);
  padding: 24px 28px;
  width: min(420px, 90vw);
  font-family: inherit;
  box-shadow: 0 10px 40px rgba(0,0,0,0.25);
}
.unbind-modal {
  background: var(--bg-base);
  border: 1px solid var(--accent);
  padding: 24px 28px;
  width: min(720px, 92vw);
  font-family: inherit;
  box-shadow: 0 10px 40px rgba(0,0,0,0.25);
}
.otp-head {
  font-weight: 700;
  font-size: 14px;
  letter-spacing: 0.06em;
  color: var(--accent);
  margin-bottom: 4px;
}
.otp-prompt { color: var(--fg-tertiary); margin-right: 6px; }
.otp-desc { color: var(--fg-secondary); font-size: 12px; line-height: 1.6; margin: 8px 0 16px; }
.otp-input {
  width: 100%; box-sizing: border-box;
  padding: 12px 14px;
  border: 1px solid var(--border-strong);
  background: var(--bg-panel);
  font: inherit; font-size: 22px;
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
.otp-input:focus { border-color: var(--accent); }
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
.unbind-input:focus { border-color: var(--accent); }
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
.otp-success { color: var(--ok); }
.otp-waiting { color: var(--fg-tertiary); }
.otp-actions { margin-top: 16px; display: flex; justify-content: flex-end; gap: 10px; }
</style>
