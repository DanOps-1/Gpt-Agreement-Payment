<template>
  <div class="stepper-track">
    <div class="stepper-rail" ref="railEl">
      <span
        v-for="(s, i) in steps"
        :key="s.n"
        class="stepper-cell"
        :class="{
          done: i + 1 < store.currentStep && !store.isStepHidden(s.n),
          active: i + 1 === store.currentStep,
          skipped: store.isStepHidden(s.n),
        }"
        @click="go(s.n)"
      >
        <span class="cell-num">{{ String(s.n).padStart(2, '0') }}</span>
        <span class="cell-title">{{ s.title }}</span>
      </span>
    </div>
    <div class="stepper-meta">
      Phase: {{ currentPhase }} // Step {{ store.currentStep }}/14
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, nextTick } from "vue";
import { useWizardStore } from "../stores/wizard";

const store = useWizardStore();
const railEl = ref<HTMLElement | null>(null);

watch(() => store.currentStep, async () => {
  await nextTick();
  if (!railEl.value) return;
  const active = railEl.value.querySelector('.stepper-cell.active') as HTMLElement | null;
  if (active) active.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
});

// step 6 slot is Main Payment Method config, PayPal / GoPay shared -- label switches with pm,
// otherwise choosing GoPay would still show PAYPAL in stepper, misaligning with the actual form.
const steps = computed(() => {
  const pm = (store.answers.payment as any)?.method;
  const paySlotTitle = pm === "gopay" ? "GOPAY" : "PAYPAL";
  return [
    { n: 1, title: "Mode", phase: "Basic" },
    { n: 2, title: "System", phase: "Basic" },
    { n: 3, title: "CF", phase: "Basic" },
    { n: 4, title: "CF KV", phase: "Basic" },
    { n: 5, title: "Proxy", phase: "Basic" },
    { n: 6, title: paySlotTitle, phase: "Payment" },
    { n: 7, title: "Card", phase: "Payment" },
    { n: 8, title: "Captcha", phase: "CAPTCHA" },
    { n: 9, title: "VLM", phase: "CAPTCHA" },
    { n: 10, title: "TEAM", phase: "Downstream" },
    { n: 11, title: "Push", phase: "Downstream" },
    { n: 12, title: "DAEMON", phase: "Downstream" },
    { n: 13, title: "STRIPE", phase: "Downstream" },
    { n: 14, title: "Complete", phase: "Complete" },
  ];
});

const currentPhase = computed(() => steps.value[store.currentStep - 1]?.phase ?? "");

function go(n: number) {
  if (store.isStepHidden(n)) return;
  store.setStep(n);
  store.saveToServer();
}
</script>

<style scoped>
.stepper-track { padding: 0; }
.stepper-rail {
  display: flex;
  gap: 0;
  overflow-x: auto;
  border-bottom: 1px solid var(--border);
  scrollbar-width: thin;
  scrollbar-color: var(--border-strong) transparent;
}
.stepper-rail::-webkit-scrollbar { height: 3px; }
.stepper-rail::-webkit-scrollbar-track { background: transparent; }
.stepper-rail::-webkit-scrollbar-thumb { background: var(--border-strong); }
.stepper-cell {
  flex: 0 0 auto;
  min-width: 84px;
  padding: 10px 8px 8px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  border-right: 1px solid var(--border);
  user-select: none;
  transition: background 60ms linear;
}
.stepper-cell:last-child { border-right: 0; }
.stepper-cell:hover { background: var(--bg-panel); }
.cell-num { font-size: 10px; color: var(--fg-tertiary); letter-spacing: 0.1em; }
.cell-title { font-size: 11px; font-weight: 700; letter-spacing: 0.08em; color: var(--fg-secondary); }
.stepper-cell.done .cell-num,
.stepper-cell.done .cell-title { color: var(--ok); }
.stepper-cell.done .cell-num::before { content: '✓ '; }
.stepper-cell.active { background: var(--bg-panel); }
.stepper-cell.active .cell-num,
.stepper-cell.active .cell-title { color: var(--accent); }
.stepper-cell.active .cell-num::before { content: '› '; }
.stepper-cell.skipped { opacity: 0.32; cursor: not-allowed; }
.stepper-cell.skipped:hover { background: transparent; }
.stepper-cell.skipped .cell-num,
.stepper-cell.skipped .cell-title { color: var(--fg-tertiary); }
.stepper-cell.skipped .cell-num::before { content: '⊘ '; }
.stepper-meta { padding: 6px 16px; font-size: 11px; color: var(--fg-tertiary); letter-spacing: 0.05em; }
</style>
