<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 06: GoPay 账号</div>
    <h2 class="step-h">$&nbsp;GoPay (印尼 e-wallet)<span class="term-cursor"></span></h2>
    <p class="step-sub">每个 ChatGPT Plus 订阅消耗 1 次 WhatsApp OTP + 2 次 PIN 输入。Lite 账号 (无印尼 KYC) 月限额约 IDR 2M ≈ 5-6 单。</p>

    <div class="form-stack">
      <TermField v-model="form.country_code" label="国家码 · country_code" placeholder="86 (中国大陆) / 62 (印尼)" />
      <TermField v-model="form.phone_number" label="手机号 · phone_number" placeholder="不带国家码，11 位数字" />
      <TermField v-model="form.pin" label="6 位 PIN · pin" type="password" placeholder="登录 GoJek/GoPay 时设的 PIN" />
      <TermField v-model.number="form.otp_timeout" label="OTP 等待超时秒数" type="number" />
      <TermSelect
        v-model="form.whatsapp_engine"
        label="WhatsApp 引擎"
        :options="engineOptions"
      />
    </div>

    <RouterLink class="wa-login-entry" to="/whatsapp">
      <span class="wa-login-prompt">$</span>
      WhatsApp 登录 / 扫码接收 GoPay OTP
    </RouterLink>

    <div class="hint-box">
      <p>前端只保留上面的 WhatsApp 登录入口。扫码连接后，后台会自动监听 WhatsApp 消息并把 GoPay OTP 写给支付流程读取。</p>
      <p>PIN 配置后自动用，绑定 + 扣款各用一次。</p>
      <p>同号重复绑定时第一次会返 406「account already linked」，gopay.py 会自动重试一次。</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from "vue";
import { RouterLink } from "vue-router";
import { useWizardStore } from "../../stores/wizard";
import TermField from "../term/TermField.vue";
import TermSelect from "../term/TermSelect.vue";

const store = useWizardStore();
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
  { value: "baileys", label: "Baileys (推荐)", desc: "直连 WhatsApp multi-device socket，启动更轻" },
  { value: "wwebjs", label: "whatsapp-web.js", desc: "Chromium 路径，兼容旧环境 / 调试用" },
];

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
</style>
