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

    <RouterLink class="wa-login-entry" to="/whatsapp">
      <span class="wa-login-prompt">$</span>
      查看外部 OTP / 旧 WhatsApp 扫码入口
    </RouterLink>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";
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
});

const status = ref<{ external_otp_token?: string }>({});
let timer: number | undefined;

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
  } catch {}
}

async function copy(value: string) {
  if (!value) return;
  await navigator.clipboard.writeText(value);
  message.success("已复制");
}

watch(form, () => {
  store.setAnswer("gopay", {
    ...form.value,
    otp: {
      source: "auto",
      timeout: form.value.otp_timeout,
      interval: 1,
    },
  });
  store.saveToServer();
}, { deep: true });

onMounted(() => {
  refreshStatus();
  timer = window.setInterval(refreshStatus, 5000);
});

onUnmounted(() => {
  if (timer) window.clearInterval(timer);
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
.wa-login-entry:hover { background: rgba(93, 255, 174, 0.12); }
.wa-login-prompt { color: var(--fg-primary); }
</style>
