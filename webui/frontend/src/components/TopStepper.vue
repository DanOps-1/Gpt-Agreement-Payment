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
      阶段: {{ currentPhase }} // 第 {{ store.currentStep }}/14 步
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

// step 6 槽位是「主支付方式」配置，PayPal / GoPay 共用 —— 标签随 pm 切换，
// 否则选了 GoPay 时 stepper 还显示「PAYPAL」会跟实际表单错位。
const steps = computed(() => {
  const pm = (store.answers.payment as any)?.method;
  const paySlotTitle = pm === "gopay" ? "GOPAY" : "PAYPAL";
  return [
    { n: 1, title: "模式", phase: "基础" },
    { n: 2, title: "系统", phase: "基础" },
    { n: 3, title: "CF", phase: "基础" },
    { n: 4, title: "CF KV", phase: "基础" },
    { n: 5, title: "代理", phase: "基础" },
    { n: 6, title: paySlotTitle, phase: "支付" },
    { n: 7, title: "卡片", phase: "支付" },
    { n: 8, title: "打码", phase: "可选" },
    { n: 9, title: "VLM", phase: "可选" },
    { n: 10, title: "TEAM", phase: "下游" },
    { n: 11, title: "推送", phase: "下游" },
    { n: 12, title: "DAEMON", phase: "下游" },
    { n: 13, title: "STRIPE", phase: "下游" },
    { n: 14, title: "完成", phase: "出口" },
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
