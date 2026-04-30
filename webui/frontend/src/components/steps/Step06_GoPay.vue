<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 07: GoPay 账号</div>
    <h2 class="step-h">$&nbsp;GoPay (印尼 e-wallet)<span class="term-cursor"></span></h2>
    <p class="step-sub">每个 ChatGPT Plus 订阅消耗 1 次 WhatsApp OTP + 2 次 PIN 输入。Lite 账号 (无印尼 KYC) 月限额约 IDR 2M ≈ 5-6 单。</p>

    <div class="form-stack">
      <TermField v-model="form.country_code" label="国家码 · country_code" placeholder="86 (中国大陆) / 62 (印尼)" />
      <TermField v-model="form.phone_number" label="手机号 · phone_number" placeholder="不带国家码，11 位数字" />
      <TermField v-model="form.pin" label="6 位 PIN · pin" type="password" placeholder="登录 GoJek/GoPay 时设的 PIN" />
    </div>

    <div class="hint-box">
      <p>WhatsApp OTP 在运行页弹模态框输入；PIN 配置后自动用，绑定 + 扣款各用一次。</p>
      <p>同号重复绑定时第一次会返 406「account already linked」，gopay.py 会自动重试一次。</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from "vue";
import { useWizardStore } from "../../stores/wizard";
import TermField from "../term/TermField.vue";

const store = useWizardStore();
const init = store.answers.gopay ?? {};
const form = ref({
  country_code: init.country_code ?? "86",
  phone_number: init.phone_number ?? "",
  pin: init.pin ?? "",
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
</style>
