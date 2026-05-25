<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 01: Mode + Payment Method</div>
    <h2 class="step-h">$&nbsp;Run Mode<span class="term-cursor"></span></h2>
    <p class="step-sub">Different fields are asked for each mode. You can choose randomly first; you can change it later.</p>

    <TermChoice v-model="mode" :options="modeOptions" :cols="2" @update:modelValue="onModeChange" />

    <div class="term-divider" data-tail="──────────" style="margin-top:32px">Payment Method</div>
    <h3 class="step-h2">$&nbsp;Payment Method</h3>
    <p class="step-sub">Determines which sections in step 6 (PayPal) and step 7 (Card) are displayed. "Dual Backup" is equivalent to filling both; pipeline switches via <code>--paypal</code>.</p>

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
  { value: "single", label: "single — 1×", desc: "Single: Register one account + pay once" },
  { value: "batch", label: "batch — N×", desc: "Batch: Loop run N pipelines" },
  { value: "self_dealer", label: "self_dealer — 1+N", desc: "Self-dealing: 1 owner pays + N members onboard" },
  { value: "daemon", label: "daemon — ∞", desc: "Daemon: Maintain account pool capacity" },
  { value: "free_register", label: "free_register — Free account + rt + CPA", desc: "Loop register free ChatGPT accounts → OAuth get refresh_token → push CPA(free), no payment" },
  { value: "free_backfill_rt", label: "free_backfill_rt — Old account backfill rt", desc: "Read old account records from database to backfill rt + push CPA, skip already successful/canceled" },
];

const paymentOptions = [
  { value: "paypal", label: "PayPal", desc: "PayPal balance payment · Email via catch-all/CF KV" },
  { value: "card", label: "Card Only", desc: "Stripe direct card payment" },
  { value: "both", label: "Dual Backup", desc: "PayPal + Card, switch via --paypal" },
  { value: "gopay", label: "GoPay", desc: "Indonesia e-wallet · Plus exclusive · WhatsApp OTP + PIN" },
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