<template>
  <section class="step-fade-in">
    <template v-if="store.isStepHidden(7)">
      <div class="term-divider" data-tail="──────────">步骤 07: 支付卡 — 已跳过</div>
      <h2 class="step-h">$&nbsp;此步已跳过<span class="term-cursor"></span></h2>
      <p class="step-sub">你在 step 1 选了"PayPal"支付，卡片配置不需要。</p>
      <div class="step-actions">
        <button class="term-btn term-btn--ghost" @click="goStep1">返回 step 1 修改</button>
      </div>
    </template>
    <template v-else>
      <div class="term-divider" data-tail="──────────">步骤 07: 支付卡</div>
      <h2 class="step-h">$&nbsp;支付卡 + 账单地址<span class="term-cursor"></span></h2>

      <div class="form-stack">
        <TermField v-model="form.number" label="卡号 · number" />
        <TermField v-model="form.cvc" label="CVC · cvc" />
        <div class="card-exp-row">
          <TermField v-model="form.exp_month" label="月 · exp_month" placeholder="01" />
          <TermField v-model="form.exp_year" label="年 · exp_year" placeholder="2027" />
        </div>
        <TermField v-model="form.name" label="持卡人 · name" />
        <TermField v-model="form.country" label="国家 · country" placeholder="US (ISO 2)" />
        <TermField v-model="form.currency" label="货币 · currency" placeholder="USD" />
        <TermField v-model="form.address_line1" label="地址 · address_line1" />
        <TermField v-model="form.address_city" label="城市 · address_city" />
        <TermField v-model="form.address_state" label="州/省 · address_state" placeholder="CA" />
        <TermField v-model="form.postal_code" label="邮编 · postal_code" />
      </div>

      <div class="step-actions">
        <TermBtn :loading="loading" @click="run">校验</TermBtn>
      </div>

      <div v-if="result" class="result-block" :class="`result--${result.status}`">
        <div class="result-head">
          <span class="result-icon">{{ icon(result.status) }}</span>
          <span>{{ result.message }}</span>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from "vue";
import { useWizardStore } from "../../stores/wizard";
import type { PreflightResult } from "../../api/client";
import TermField from "../term/TermField.vue";
import TermBtn from "../term/TermBtn.vue";

const store = useWizardStore();
const init = store.answers.card ?? {};
const form = ref({
  number: init.number ?? "",
  cvc: init.cvc ?? "",
  exp_month: init.exp_month ?? "",
  exp_year: init.exp_year ?? "",
  name: init.name ?? "JOHN DOE",
  country: init.country ?? "US",
  currency: init.currency ?? "USD",
  address_line1: init.address_line1 ?? "1 Example Street",
  address_city: init.address_city ?? "San Francisco",
  address_state: init.address_state ?? "CA",
  postal_code: init.postal_code ?? "94105",
});
const loading = ref(false);
const result = ref<PreflightResult | null>(null);

async function run() {
  store.setAnswer("card", form.value);
  await store.saveToServer();
  loading.value = true;
  try {
    result.value = await store.runPreflight("card", form.value);
  } finally { loading.value = false; }
}
watch(form, () => store.setAnswer("card", form.value), { deep: true });

function icon(s: string) {
  return s === "ok" ? "✓" : s === "fail" ? "✗" : s === "warn" ? "▲" : "○";
}

function goStep1() {
  store.setStep(1);
  store.saveToServer();
}
</script>

<style scoped>
.card-exp-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0;
}
</style>
