<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 01: 模式 + 支付方式</div>
    <h2 class="step-h">$&nbsp;运行模式<span class="term-cursor"></span></h2>
    <p class="step-sub">每个模式后面问的字段不同。可以先随便选，后面也能改。</p>

    <TermChoice v-model="mode" :options="modeOptions" :cols="2" @update:modelValue="onModeChange" />

    <div class="term-divider" data-tail="──────────" style="margin-top:32px">支付方式</div>
    <h3 class="step-h2">$&nbsp;支付方式</h3>
    <p class="step-sub">决定后面 step 6 (PayPal) 和 step 7 (卡) 哪些显示。"双备份"等同于两边都填，pipeline 按 <code>--paypal</code> 切换。</p>

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
  { value: "single", label: "single — 1×", desc: "单次：注册一个号 + 支付一次" },
  { value: "batch", label: "batch — N×", desc: "批量：循环跑 N 个 pipeline" },
  { value: "self_dealer", label: "self_dealer — 1+N", desc: "自产自销：1 owner 付费 + N 个 member 上车" },
  { value: "daemon", label: "daemon — ∞", desc: "常驻：维护补号池容量" },
  { value: "free_register", label: "free_register — 免费号 + rt + CPA", desc: "循环注册免费 ChatGPT 号 → OAuth 拿 refresh_token → 推 CPA(free)，不走支付" },
  { value: "free_backfill_rt", label: "free_backfill_rt — 老号补 rt", desc: "读数据库里的老号记录给账号补 rt + 推 CPA，跳过已成功/已注销" },
];

const paymentOptions = [
  { value: "paypal", label: "PayPal", desc: "PayPal 余额支付 · 邮箱走 catch-all/CF KV" },
  { value: "card", label: "纯卡", desc: "Stripe 直接刷卡" },
  { value: "both", label: "双备份", desc: "PayPal + 卡，按 --paypal 切" },
  { value: "gopay", label: "GoPay", desc: "印尼 e-wallet · Plus 专用 · WhatsApp OTP + PIN" },
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
