<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 06: GoPay Account</div>
    <h2 class="step-h">$&nbsp;GoPay (Indonesian e-wallet)<span class="term-cursor"></span></h2>
    <p class="step-sub">Each ChatGPT Plus subscription consumes 1 WhatsApp OTP + 2 PIN entries. Lite accounts (no Indonesian KYC) have a monthly limit of ~IDR 2M ≈ 5-6 orders.</p>

    <div class="form-stack">
      <TermField v-model="form.country_code" label="Country Code · country_code" placeholder="86 (China) / 62 (Indonesia)" />
      <TermField v-model="form.phone_number" label="Phone Number · phone_number" placeholder="Without country code, 11-digit number" />
      <TermField v-model="form.pin" label="6-digit PIN · pin" type="password" placeholder="PIN set when logging into GoJek/GoPay" />
      <TermField v-model.number="form.otp_timeout" label="OTP Wait Timeout (s)" type="number" />
      <TermSelect
        v-model="form.whatsapp_engine"
        label="WhatsApp Engine"
        :options="engineOptions"
      />
    </div>

    <RouterLink class="wa-login-entry" to="/whatsapp">
      <span class="wa-login-prompt">$</span>
      WhatsApp Login / Scan for GoPay OTP
    </RouterLink>

    <div class="hint-box">
      <p>The frontend only keeps the WhatsApp login portal above. Once scanned and connected, the backend automatically monitors WhatsApp messages and writes GoPay OTPs for the payment process to read.</p>
      <p>PIN is used automatically after configuration, once for binding and once for payment.</p>
      <p>When re-linking the same number, the first attempt may return 406 "account already linked", gopay.py will automatically retry once.</p>
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
  { value: "baileys", label: "Baileys (Recommended)", desc: "Direct WhatsApp multi-device socket connection, more lightweight" },
  { value: "wwebjs", label: "whatsapp-web.js", desc: "Chromium-based, for compatibility/debugging" },
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
