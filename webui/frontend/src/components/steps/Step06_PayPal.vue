<template>
  <section class="step-fade-in">
    <template v-if="store.isStepHidden(6)">
      <div class="term-divider" data-tail="──────────">Step 06: PayPal — Skipped</div>
      <h2 class="step-h">$&nbsp;Step Skipped<span class="term-cursor"></span></h2>
      <p class="step-sub">You selected "Card-only" payment in Step 1, PayPal configuration is not required.</p>
      <div class="step-actions">
        <button class="term-btn term-btn--ghost" @click="goStep1">Go back to Step 1 to modify</button>
      </div>
    </template>
    <template v-else>
      <div class="term-divider" data-tail="──────────">Step 06: PayPal</div>
      <h2 class="step-h">$&nbsp;PayPal Credentials<span class="term-cursor"></span></h2>
      <p class="step-sub">The PayPal email must be an address under the catch-all domain configured in Step 03 (e.g., you@your-zone.com). 2FA OTP is automatically retrieved via CF Worker → KV, IMAP is no longer used.</p>

      <div class="form-stack">
        <TermField v-model="form.email" label="PayPal Email · email" placeholder="Must be an address within a catch-all zone" />
        <TermField v-model="form.password" label="PayPal Password · password" type="password" />
      </div>

      <div v-if="warning" class="result-block result--warn" style="margin-top:16px">
        <div class="result-head">
          <span class="result-icon">▲</span>
          <span>{{ warning }}</span>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref, computed, watch } from "vue";
import { useWizardStore } from "../../stores/wizard";
import TermField from "../term/TermField.vue";

const store = useWizardStore();
const init = store.answers.paypal ?? {};
const form = ref({
  email: init.email ?? "",
  password: init.password ?? "",
});

const warning = computed(() => {
  if (form.value.email && !form.value.email.includes("@")) return "Invalid email format";
  if (form.value.password && form.value.password.length < 6) return "Password looks too short";
  return null;
});

watch(form, () => {
  store.setAnswer("paypal", form.value);
  store.saveToServer();
}, { deep: true });

function goStep1() {
  store.setStep(1);
  store.saveToServer();
}
</script>
