<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 01: Mode + Payment Method</div>
    <h2 class="step-h">$&nbsp;Running Mode<span class="term-cursor"></span></h2>
    <p class="step-sub">Each mode requires different configurations. You can change this later.</p>

    <TermChoice v-model="mode" :options="modeOptions" :cols="2" @update:modelValue="onModeChange" />

    <div class="term-divider" data-tail="──────────" style="margin-top:32px">Payment Method</div>
    <h3 class="step-h2">$&nbsp;Payment Method</h3>
    <p class="step-sub">Determines which steps (Step 06 PayPal / Step 07 Card) will be shown. "Dual Backup" means both are used, toggled by <code>--paypal</code>.</p>

    <TermChoice v-model="payment" :options="paymentOptions" :cols="2" @update:modelValue="onPaymentChange" />
  </section>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { useWizardStore } from "../../stores/wizard";
import TermChoice from "../term/TermChoice.vue";

const store = useWizardStore();
const mode = ref(store.answers.mode?.mode ?? "single");
const payment = ref(store.answers.payment?.method ?? "both");

const modeOptions = [
  { value: "single", label: "single — 1×", desc: "Single run: Register one account + Pay once" },
  { value: "batch", label: "batch — N×", desc: "Batch: Loop through N pipelines" },
  { value: "self_dealer", label: "self_dealer — 1+N", desc: "Self-dealer: 1 owner (pays) + N members (joined)" },
  { value: "daemon", label: "daemon — ∞", desc: "Daemon: Maintain a pool of accounts" },
  { value: "free_register", label: "free_register — Free Account + rt + CPA", desc: "Register free ChatGPT accounts → Get refresh_token via OAuth → Push CPA(free), no payment" },
  { value: "free_backfill_rt", label: "free_backfill_rt — Backfill rt for existing", desc: "Read existing records from DB to backfill rt + push CPA, skip successful/deactivated" },
];

const paymentOptions = [
  { value: "paypal", label: "PayPal", desc: "PayPal balance · Email via catch-all/CF KV" },
  { value: "card", label: "Card Only", desc: "Direct Stripe card payment" },
  { value: "both", label: "Dual Backup", desc: "PayPal + Card, toggle via --paypal" },
  { value: "gopay", label: "GoPay", desc: "Indonesian e-wallet · Plus only · WhatsApp OTP + PIN" },
];

function onModeChange(v: string) {
  store.setAnswer("mode", { mode: v });
  store.saveToServer();
}
function onPaymentChange(v: string) {
  store.setAnswer("payment", { method: v });
  store.saveToServer();
}
</script>

<style scoped>
.step-h2 { font-size: 22px; font-weight: 700; letter-spacing: 0.04em; margin: 4px 0 4px; color: var(--fg-primary); }
code { background: var(--bg-panel); padding: 1px 5px; border: 1px solid var(--border); font-size: 12px; }
</style>
