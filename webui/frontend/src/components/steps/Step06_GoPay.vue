<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="----------">第 06 步：GOPAY 验证码</div>
    <h2 class="step-h">$&nbsp;GoPay 账号与外部验证码<span class="term-cursor"></span></h2>
    <p class="step-sub">
      配置一个或多个 GoPay 钱包。每次支付会随机选择一个账号，OTP 轮询会按该账号手机号过滤。
    </p>

    <div class="accounts-panel">
      <div class="accounts-head">
        <span class="label">GoPay 账号</span>
        <TermBtn variant="ghost" @click="addAccount">添加账号</TermBtn>
      </div>

      <div v-for="(account, idx) in form.accounts" :key="account.id" class="account-box">
        <div class="account-title">
          <span>GoPay #{{ idx + 1 }}</span>
          <TermBtn
            variant="danger"
            :disabled="form.accounts.length <= 1"
            @click="removeAccount(idx)"
          >
            删除
          </TermBtn>
        </div>
        <div class="form-stack">
          <TermField v-model="account.label" label="标签" placeholder="钱包标签" />
          <TermField v-model="account.country_code" label="国家区号" placeholder="62" />
          <TermField v-model="account.phone_number" label="手机号" placeholder="81234567890" />
          <TermField v-model="account.pin" label="支付 PIN" type="password" placeholder="GoPay PIN" />
          <label class="qr-toggle">
            <input v-model="account.disabled" type="checkbox" />
            <span>
              <strong>禁用此号码</strong>
              <small>勾选后运行时不会选择这个 GoPay 账号。</small>
            </span>
          </label>
          <label class="qr-toggle">
            <input v-model="account.use_sms_otp" type="checkbox" />
            <span>
              <strong>SMS 验证码</strong>
              <small>勾选后绑定 OTP 使用短信 resend-otp，并从轮询地址提取 6 位验证码。</small>
            </span>
          </label>
          <TermField
            v-if="account.use_sms_otp"
            v-model="account.sms_otp_poll_url"
            label="SMS 轮询地址"
            placeholder="https://example.com/latest-sms"
          />
        </div>
      </div>
    </div>

    <div class="auto-signup-card">
      <label class="qr-toggle">
        <input v-model="form.auto_signup.enabled" type="checkbox" />
        <span>
          <strong>启用一键注册 GoPay</strong>
          <small>运行时自动向 SMSBower 取号，完成注册、换 token、设置支付码，再进入 link 短信验证码流程。</small>
        </span>
      </label>

      <div v-if="form.auto_signup.enabled" class="form-stack auto-signup-fields">
        <TermField
          v-model="form.auto_signup.smsbower_api_key"
          label="SMSBower API Key"
          type="password"
          placeholder="handler_api.php 的 api_key"
        />
        <TermField
          v-model="form.auto_signup.service"
          label="SMSBower 服务"
          placeholder="ni"
        />
        <TermField
          v-model="form.auto_signup.country"
          label="SMSBower 国家"
          placeholder="6"
        />
        <TermField
          v-model="form.auto_signup.country_code"
          label="GoPay 区号"
          placeholder="62"
        />
        <TermField
          v-model="form.auto_signup.pin"
          label="支付码"
          type="password"
          placeholder="6 位支付码"
        />
        <TermField
          v-model="form.auto_signup.name"
          label="昵称"
          placeholder="SJC"
        />
        <TermField
          v-model="form.auto_signup.email"
          label="邮箱"
          placeholder="可留空"
        />
        <TermField
          v-model="form.auto_signup.max_price"
          label="最高单价"
          placeholder="可留空"
        />
        <TermField
          v-model.number="form.auto_signup.otp_timeout"
          label="验证码超时"
          type="number"
          placeholder="180"
        />
        <TermField
          v-model.number="form.auto_signup.otp_interval"
          label="轮询间隔"
          type="number"
          placeholder="5"
        />
        <TermField
          v-model.number="form.auto_signup.phone_ttl_seconds"
          label="手机号存活秒数"
          type="number"
          placeholder="960"
        />
        <TermField
          v-model="form.auto_signup.smsbower_url"
          label="SMSBower 地址"
          placeholder="https://smsbower.page/stubs/handler_api.php"
        />
        <TermField
          v-model="form.auto_signup.client_id"
          label="client_id"
          placeholder="gopay:consumer:app"
        />
        <TermField
          v-model="form.auto_signup.client_secret"
          label="client_secret"
          type="password"
          placeholder="GoPay client_secret"
        />
        <TermField
          v-model="form.auto_signup.signup_authorization"
          label="Signup Basic"
          placeholder="Basic ..."
        />
      </div>
    </div>

    <div class="form-stack otp-settings">
      <label class="qr-toggle">
        <input v-model="form.qr_payment" type="checkbox" />
        <span>
          <strong>二维码支付</strong>
          <small>勾选后支付阶段尝试生成 QR/QRIS，扫码后自动继续轮询结果。</small>
        </span>
      </label>
      <TermField v-if="form.qr_payment" v-model.number="form.qr_wait_timeout" label="扫码等待秒数" type="number" />
      <TermField v-model.number="form.otp_timeout" label="OTP 超时秒数" type="number" />
    </div>

    <div class="external-card">
      <div class="external-head">
        <div>
          <span class="label">回调地址</span>
          <code>{{ webhookUrl }}</code>
        </div>
        <TermBtn variant="ghost" @click="copy(webhookUrl)">复制地址</TermBtn>
      </div>

      <div class="external-head">
        <div>
          <span class="label">授权令牌</span>
          <code>{{ status.external_otp_token || "加载中..." }}</code>
        </div>
        <TermBtn
          variant="ghost"
          :disabled="!status.external_otp_token"
          @click="copy(status.external_otp_token || '')"
        >
          复制令牌
        </TermBtn>
      </div>

      <div class="hint-box">
        <p>请求头：<code>Authorization: Bearer {{ status.external_otp_token || "xxx" }}</code></p>
        <p>请求体：<code>{{ bodyTemplate }}</code></p>
      </div>

      <div class="curl-box">
        <div class="label">curl 测试</div>
        <pre>{{ curlExample }}</pre>
        <TermBtn variant="ghost" @click="copy(curlExample)">复制 curl</TermBtn>
      </div>
    </div>

    <div class="gopay-actions">
      <button
        class="wa-login-entry"
        type="button"
        :disabled="!otpAccountOptions.length"
        @click="openOtpTest"
      >
        <span class="wa-login-prompt">$</span>
        测试验证码
      </button>
    </div>

    <Teleport to="body">
      <div v-if="otpDialog.open" class="otp-overlay" @click.self="closeOtpTest">
        <div class="otp-modal">
          <div class="otp-head">
            <span class="otp-prompt">$</span> GoPay WhatsApp 验证码
          </div>
          <p class="otp-desc">
            向回调地址发送验证码测试载荷。请选择要验证的 GoPay 手机号，收到的验证码会按该手机号过滤。
          </p>
          <TermSelect
            v-model="otpDialog.accountKey"
            class="otp-phone-select"
            label="测试手机号"
            :options="otpAccountOptions"
          />
          <input
            class="otp-input"
            v-model="otpDialog.value"
            maxlength="8"
            autofocus
            disabled
            placeholder="000000"
          />
          <div v-if="otpDialog.success" class="otp-success">
            回调地址已收到该手机号的验证码。
          </div>
          <div v-else-if="otpDialog.preparing" class="otp-waiting">
            正在准备测试会话...
          </div>
          <div v-else class="otp-waiting">
            正在等待回调验证码...
          </div>
          <div class="otp-actions">
            <TermBtn variant="ghost" @click="closeOtpTest">关闭</TermBtn>
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
import TermSelect from "../term/TermSelect.vue";

type GoPayAccountForm = {
  id: string;
  label: string;
  country_code: string;
  phone_number: string;
  pin: string;
  midtrans_client_id?: string;
  disabled: boolean;
  use_sms_otp: boolean;
  sms_otp?: boolean;
  sms_otp_poll_url: string;
  sms_otp_url?: string;
  auto_login_phone: string;
  login_phone?: string;
  auto_login_otp_poll_url?: string;
  auto_login_token_dir?: string;
};

type GoPayAutoSignupForm = {
  enabled: boolean;
  smsbower_api_key: string;
  service: string;
  country: string;
  country_code: string;
  pin: string;
  name: string;
  email: string;
  max_price: string;
  otp_timeout: number;
  otp_interval: number;
  phone_ttl_seconds: number;
  parallel_prepare: boolean;
  reuse_ready_phone: boolean;
  hold_phone_until_link: boolean;
  release_on_chatgpt_fail: boolean;
  smsbower_url: string;
  client_id: string;
  client_secret: string;
  signup_authorization: string;
  token_grant_type: string;
};

const store = useWizardStore();
const message = useMessage();
const init = store.answers.gopay ?? {};
const initOtp = init.otp ?? {};
const initAutoSignup = init.auto_signup ?? {};

function newAccount(seed?: Partial<GoPayAccountForm>, index = 0): GoPayAccountForm {
  return {
    id: seed?.id || `${Date.now()}-${Math.random().toString(16).slice(2)}-${index}`,
    label: seed?.label || `account-${index + 1}`,
    country_code: seed?.country_code ?? "62",
    phone_number: seed?.phone_number ?? "",
    pin: seed?.pin ?? "",
    midtrans_client_id: seed?.midtrans_client_id ?? "",
    disabled: Boolean(seed?.disabled),
    use_sms_otp: Boolean(seed?.use_sms_otp || seed?.sms_otp),
    sms_otp_poll_url: seed?.sms_otp_poll_url ?? seed?.sms_otp_url ?? "",
    auto_login_phone: seed?.auto_login_phone ?? seed?.login_phone ?? "",
    auto_login_otp_poll_url: seed?.auto_login_otp_poll_url ?? "",
    auto_login_token_dir: seed?.auto_login_token_dir ?? "",
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
      disabled: Boolean(item.disabled),
      use_sms_otp: Boolean(item.use_sms_otp || item.sms_otp),
      sms_otp_poll_url: item.sms_otp_poll_url ?? item.sms_otp_url ?? "",
      auto_login_phone: item.auto_login_phone ?? item.login_phone ?? "",
      auto_login_otp_poll_url: item.auto_login_otp_poll_url ?? "",
      auto_login_token_dir: item.auto_login_token_dir ?? "",
    }, idx));
  if (accounts.length) return accounts;
  return [newAccount({
    label: init.label ?? "account-1",
    country_code: init.country_code ?? "62",
    phone_number: init.phone_number ?? "",
    pin: init.pin ?? "",
    midtrans_client_id: init.midtrans_client_id ?? "",
    disabled: Boolean(init.disabled),
    use_sms_otp: Boolean(init.use_sms_otp || init.sms_otp),
    sms_otp_poll_url: init.sms_otp_poll_url ?? init.sms_otp_url ?? "",
    auto_login_phone: init.auto_login_phone ?? init.login_phone ?? "",
    auto_login_otp_poll_url: init.auto_login_otp_poll_url ?? "",
    auto_login_token_dir: init.auto_login_token_dir ?? "",
  }, 0)];
}

function initialAutoSignup(): GoPayAutoSignupForm {
  return {
    enabled: Boolean(initAutoSignup.enabled),
    smsbower_api_key: initAutoSignup.smsbower_api_key ?? initAutoSignup.api_key ?? "",
    service: initAutoSignup.service ?? "ni",
    country: initAutoSignup.country ?? "6",
    country_code: String(initAutoSignup.country_code ?? init.country_code ?? "62").replace(/^\+/, ""),
    pin: initAutoSignup.pin ?? init.pin ?? "",
    name: initAutoSignup.name ?? "SJC",
    email: initAutoSignup.email ?? "",
    max_price: initAutoSignup.max_price ?? "",
    otp_timeout: Number(initAutoSignup.otp_timeout ?? 180),
    otp_interval: Number(initAutoSignup.otp_interval ?? 5),
    phone_ttl_seconds: Number(initAutoSignup.phone_ttl_seconds ?? 960),
    parallel_prepare: initAutoSignup.parallel_prepare ?? true,
    reuse_ready_phone: initAutoSignup.reuse_ready_phone ?? true,
    hold_phone_until_link: initAutoSignup.hold_phone_until_link ?? true,
    release_on_chatgpt_fail: initAutoSignup.release_on_chatgpt_fail ?? false,
    smsbower_url: initAutoSignup.smsbower_url ?? "https://smsbower.page/stubs/handler_api.php",
    client_id: initAutoSignup.client_id ?? "gopay:consumer:app",
    client_secret: initAutoSignup.client_secret ?? "raOUumeMRBNifqvZRFjvsgTnjAlaA9",
    signup_authorization: initAutoSignup.signup_authorization ?? "",
    token_grant_type: initAutoSignup.token_grant_type ?? "refresh_token",
  };
}

const form = ref({
  accounts: initialAccounts(),
  auto_signup: initialAutoSignup(),
  qr_payment: Boolean(init.qr_payment || init.qr_enabled || ["qr", "qris", "qr_payment"].includes(String(init.payment_mode || init.mode || "").toLowerCase())),
  qr_wait_timeout: init.qr_wait_timeout ?? 300,
  otp_timeout: init.otp_timeout ?? initOtp.timeout ?? 300,
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
  accountKey: "",
  success: false,
  since: 0,
  preparing: false,
});
let timer: number | undefined;
let otpTestTimer: number | undefined;

const webhookUrl = computed(() => {
  const base = import.meta.env.BASE_URL || "/";
  return new URL(`${base}api/whatsapp/external-otp`, window.location.origin).toString();
});

const otpAccountOptions = computed(() => form.value.accounts
  .map((account, idx) => {
    const phone = String(account.phone_number || "").trim();
    const countryCode = String(account.country_code || "").replace(/^\+/, "") || "62";
    if (!phone) return null;
    const label = account.label
      ? `${account.label} (+${countryCode} ${phone})`
      : `GoPay #${idx + 1} (+${countryCode} ${phone})`;
    return {
      value: `${countryCode}:${phone}`,
      label,
      desc: `仅接收 +${countryCode} ${phone} 的 OTP`,
    };
  })
  .filter(Boolean) as { value: string; label: string; desc: string }[]);

const selectedOtpAccount = computed(() => {
  const fallback = otpAccountOptions.value[0]?.value || "";
  const key = otpDialog.value.accountKey || fallback;
  const [countryCode = "62", phone = ""] = String(key).split(":");
  return {
    country_code: countryCode || "62",
    phone_number: phone,
  };
});

const sampleAccount = computed(() => selectedOtpAccount.value.phone_number ? selectedOtpAccount.value : (form.value.accounts[0] || newAccount({}, 0)));
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
    const params = otpDialog.value.open && !otpDialog.value.success && !otpDialog.value.preparing
      ? {
        since: otpDialog.value.since,
        phone: selectedOtpAccount.value.phone_number,
        country_code: selectedOtpAccount.value.country_code,
      }
      : {};
    const r = await api.get("/whatsapp/status", { params });
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
  if (!otpAccountOptions.value.length) {
    message.warning("请先配置 GoPay 手机号");
    return;
  }
  if (!otpDialog.value.accountKey || !otpAccountOptions.value.some((opt) => opt.value === otpDialog.value.accountKey)) {
    otpDialog.value.accountKey = otpAccountOptions.value[0].value;
  }
  otpDialog.value = {
    open: true,
    value: "",
    accountKey: otpDialog.value.accountKey,
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
    message.warning("测试会话启动失败，改用浏览器时间。");
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
  message.success("验证码回调测试通过");
  if (otpTestTimer) {
    window.clearInterval(otpTestTimer);
    otpTestTimer = undefined;
  }
}

function cleanAccount(account: GoPayAccountForm, index: number) {
  const cleaned: any = {
    label: account.label || `account-${index + 1}`,
    country_code: String(account.country_code || "").replace(/^\+/, ""),
    phone_number: String(account.phone_number || ""),
    pin: String(account.pin || ""),
    midtrans_client_id: String(account.midtrans_client_id || ""),
  };
  if (account.disabled) {
    cleaned.disabled = true;
  }
  if (account.use_sms_otp) {
    cleaned.use_sms_otp = true;
    cleaned.sms_otp_poll_url = String(account.sms_otp_poll_url || "").trim();
  }
  if (String(account.auto_login_phone || "").trim()) {
    cleaned.auto_login_phone = String(account.auto_login_phone || "").trim();
  }
  if (String(account.auto_login_otp_poll_url || "").trim()) {
    cleaned.auto_login_otp_poll_url = String(account.auto_login_otp_poll_url || "").trim();
  }
  if (String(account.auto_login_token_dir || "").trim()) {
    cleaned.auto_login_token_dir = String(account.auto_login_token_dir || "").trim();
  }
  return cleaned;
}

function cleanAutoSignup(value: GoPayAutoSignupForm) {
  const cleaned: any = {
    enabled: Boolean(value.enabled),
    smsbower_api_key: String(value.smsbower_api_key || "").trim(),
    service: String(value.service || "ni").trim(),
    country: String(value.country || "6").trim(),
    country_code: String(value.country_code || "62").replace(/^\+/, "").trim(),
    pin: String(value.pin || "").trim(),
    name: String(value.name || "SJC").trim(),
    email: String(value.email || "").trim(),
    otp_timeout: Number(value.otp_timeout || 180),
    otp_interval: Number(value.otp_interval || 5),
    phone_ttl_seconds: Number(value.phone_ttl_seconds || 960),
    parallel_prepare: Boolean(value.parallel_prepare),
    reuse_ready_phone: Boolean(value.reuse_ready_phone),
    hold_phone_until_link: Boolean(value.hold_phone_until_link),
    release_on_chatgpt_fail: Boolean(value.release_on_chatgpt_fail),
  };
  const maxPrice = String(value.max_price || "").trim();
  if (maxPrice) cleaned.max_price = maxPrice;
  const smsbowerUrl = String(value.smsbower_url || "").trim();
  if (smsbowerUrl) cleaned.smsbower_url = smsbowerUrl;
  const clientId = String(value.client_id || "").trim();
  if (clientId) cleaned.client_id = clientId;
  const clientSecret = String(value.client_secret || "").trim();
  if (clientSecret) cleaned.client_secret = clientSecret;
  const signupAuthorization = String(value.signup_authorization || "").trim();
  if (signupAuthorization) cleaned.signup_authorization = signupAuthorization;
  const tokenGrantType = String(value.token_grant_type || "").trim();
  if (tokenGrantType) cleaned.token_grant_type = tokenGrantType;
  return cleaned;
}

function buildGopayAnswer() {
  const accounts = form.value.accounts.map(cleanAccount);
  const first = accounts[0] || cleanAccount(newAccount({}, 0), 0);
  const autoSignup = cleanAutoSignup(form.value.auto_signup);
  return {
    country_code: first.country_code || autoSignup.country_code || "62",
    phone_number: first.phone_number,
    pin: first.pin || autoSignup.pin,
    midtrans_client_id: first.midtrans_client_id,
    accounts,
    auto_signup: autoSignup,
    qr_payment: Boolean(form.value.qr_payment),
    payment_mode: form.value.qr_payment ? "qr" : "tokenization",
    qr_wait_timeout: form.value.qr_wait_timeout,
    otp_timeout: form.value.otp_timeout,
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

watch(otpAccountOptions, (options) => {
  if (!options.length) {
    otpDialog.value.accountKey = "";
    return;
  }
  if (!options.some((opt) => opt.value === otpDialog.value.accountKey)) {
    otpDialog.value.accountKey = options[0].value;
  }
}, { immediate: true });

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
.accounts-tools {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 10px;
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
.auto-signup-card {
  margin-top: 18px;
  display: grid;
  gap: 14px;
}
.auto-signup-fields {
  padding: 14px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
}
.qr-toggle {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border: 1px dashed var(--border);
  background: rgba(255,255,255,0.03);
  color: var(--fg-primary);
  cursor: pointer;
}
.qr-toggle input {
  width: 16px;
  height: 16px;
  margin: 2px 0 0;
  accent-color: var(--accent);
}
.qr-toggle strong,
.qr-toggle small {
  display: block;
}
.qr-toggle small {
  margin-top: 4px;
  color: var(--fg-secondary);
  font-size: 12px;
  line-height: 1.5;
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
.wa-login-entry:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.wa-login-entry:disabled:hover {
  background: rgba(93, 255, 174, 0.06);
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
