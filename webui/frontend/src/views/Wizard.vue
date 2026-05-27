<template>
  <div class="wizard-root">
    <header class="wizard-header">
      <div class="brand">
        <span class="brand-prompt">$</span>
        <span class="brand-name">gpt-pay</span>
        <span class="brand-sub">// 配置向导</span>
        <span class="brand-clock">{{ clock }}</span>
      </div>
      <div class="run-nav">
        <RouterLink to="/wizard" class="nav-link active">配置向导</RouterLink>
        <RouterLink to="/run" class="nav-link">运行</RouterLink>
        <button class="header-btn" @click="logout" title="esc / Ctrl+L">退出</button>
      </div>
    </header>

    <TopStepper />

    <div class="wizard-body">
      <main class="wizard-main">
        <Transition name="step" mode="out-in">
          <component v-if="loaded" :is="currentStepComponent" :key="store.currentStep" />
          <div v-else class="wizard-loading">加载配置中<span class="term-cursor"></span></div>
        </Transition>
      </main>

      <PreflightPanel class="wizard-side" />
    </div>

    <nav class="wizard-foot">
      <button class="foot-btn" :disabled="store.currentStep <= 1" @click="prev" title="h or ←">
        <span class="fb-key">[H]</span> 上一步
      </button>
      <span class="foot-progress">
        <span class="fp-bar"><span class="fp-fill" :style="{ width: progressPct + '%' }"></span></span>
        <span class="fp-text">{{ String(store.currentStep).padStart(2,'0') }} / 14</span>
      </span>
      <button class="foot-btn foot-btn--primary" :disabled="store.currentStep >= 14" @click="next" title="l or →">
        下一步 <span class="fb-key">[L]</span>
      </button>
    </nav>

    <div class="hotkeys">
      <span><kbd>H</kbd>/<kbd>←</kbd> 上一步</span>
      <span><kbd>L</kbd>/<kbd>→</kbd> 下一步</span>
      <span><kbd>1-9</kbd> 跳到对应步</span>
      <span><kbd>Esc</kbd> 退出登录</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from "vue";
import { useRouter, RouterLink } from "vue-router";
import { useWizardStore } from "../stores/wizard";
import TopStepper from "../components/TopStepper.vue";
import PreflightPanel from "../components/PreflightPanel.vue";
import { api } from "../api/client";

import Step01 from "../components/steps/Step01_Welcome.vue";
import Step02 from "../components/steps/Step02_System.vue";
import Step03 from "../components/steps/Step03_Cloudflare.vue";
import Step04 from "../components/steps/Step04_CloudflareKV.vue";
import Step05 from "../components/steps/Step05_Proxy.vue";
import Step06 from "../components/steps/Step06_PayPal.vue";
import Step06GoPay from "../components/steps/Step06_GoPay.vue";
import Step07 from "../components/steps/Step07_Card.vue";
import Step08 from "../components/steps/Step08_Captcha.vue";
import Step09 from "../components/steps/Step09_VLM.vue";
import Step10 from "../components/steps/Step10_TeamPlan.vue";
import Step11 from "../components/steps/Step11_Downstream.vue";
import Step12 from "../components/steps/Step12_Daemon.vue";
import Step13 from "../components/steps/Step13_StripeRuntime.vue";
import Step14 from "../components/steps/Step14_Review.vue";

const STEPS = [Step01, Step02, Step03, Step04, Step05, Step06, Step07, Step08, Step09, Step10, Step11, Step12, Step13, Step14];

const store = useWizardStore();
const router = useRouter();
const currentStepComponent = computed(() => {
  // GoPay uses step 7 slot when selected.
  if (store.currentStep === 7) {
    const pm = (store.answers.payment as any)?.method;
    if (pm === "gopay") return Step06GoPay;
  }
  return STEPS[store.currentStep - 1];
});
const progressPct = computed(() => (store.currentStep / 14) * 100);

const clock = ref("");
const loaded = ref(false);
let clockInterval: ReturnType<typeof setInterval> | undefined;
function tickClock() {
  const d = new Date();
  clock.value = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

function prev() {
  let n = store.currentStep - 1;
  while (n >= 1 && store.isStepHidden(n)) n--;
  if (n >= 1) { store.setStep(n); store.saveToServer(); }
}
function next() {
  let n = store.currentStep + 1;
  while (n <= 14 && store.isStepHidden(n)) n++;
  if (n <= 14) { store.setStep(n); store.saveToServer(); }
}
async function logout() { await api.post("/logout"); router.push("/login"); }

function onKey(e: KeyboardEvent) {
  if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement)?.tagName)) return;
  if (e.key === "h" || e.key === "ArrowLeft") { prev(); }
  else if (e.key === "l" || e.key === "ArrowRight") { next(); }
  else if (e.key === "Escape") { logout(); }
  else if (/^[1-9]$/.test(e.key)) { store.setStep(parseInt(e.key, 10)); store.saveToServer(); }
}

onMounted(async () => {
  try { await store.loadFromServer(); } catch {}
  loaded.value = true;
  tickClock();
  clockInterval = setInterval(tickClock, 1000);
  window.addEventListener("keydown", onKey);
});
onBeforeUnmount(() => {
  if (clockInterval) clearInterval(clockInterval);
  window.removeEventListener("keydown", onKey);
});
</script>

<style scoped>
.wizard-root { min-height: 100vh; display: flex; flex-direction: column; }

.wizard-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px;
  border-bottom: 1px solid var(--border);
  animation: slide-down 320ms ease-out;
}
.brand { display: flex; align-items: baseline; gap: 10px; }
.brand-prompt { color: var(--accent); }
.brand-name { font-weight: 700; font-size: 18px; letter-spacing: 0.04em; }
.brand-sub { color: var(--fg-tertiary); font-size: 12px; }
.brand-clock { color: var(--fg-tertiary); font-size: 11px; margin-left: 16px; font-variant-numeric: tabular-nums; }
.header-btn { background: transparent; border: 1px solid var(--border-strong); color: var(--fg-secondary); padding: 4px 12px; font: inherit; font-size: 11px; letter-spacing: 0.08em; cursor: pointer; transition: all 60ms; margin-left: 12px; }
.header-btn:hover { background: var(--bg-raised); color: var(--fg-primary); border-color: var(--accent); }

.run-nav { display: flex; align-items: center; gap: 4px; }
.nav-link { padding: 6px 14px; color: var(--fg-secondary); text-decoration: none; font-size: 12px; letter-spacing: 0.06em; border: 1px solid transparent; transition: all 80ms; }
.nav-link:hover { color: var(--fg-primary); background: var(--bg-panel); }
.nav-link.active { color: var(--accent); border-color: var(--accent); background: var(--bg-panel); }

.wizard-body {
  flex: 1;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  gap: 0;
  min-height: 0;
  overflow: hidden;
}
.wizard-main {
  padding: 32px 40px;
  min-width: 0;
  overflow-y: auto;
}
.wizard-side {
  padding: 16px;
  overflow-y: hidden;
  border-left: 1px solid var(--border);
  display: flex;
}
.wizard-side > * { flex: 1; min-height: 0; }

.wizard-foot {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: 24px;
  padding: 12px 24px;
  border-top: 1px solid var(--border);
  background: var(--bg-panel);
}
.foot-btn { background: transparent; border: 1px solid var(--border-strong); color: var(--fg-secondary); padding: 8px 16px; font: inherit; font-size: 12px; letter-spacing: 0.06em; cursor: pointer; transition: all 80ms; display: inline-flex; align-items: center; gap: 8px; }
.foot-btn:not(:first-child) { justify-self: end; }
.foot-btn:disabled { opacity: 0.35; cursor: not-allowed; }
.foot-btn:not(:disabled):hover { background: var(--bg-raised); color: var(--fg-primary); border-color: var(--accent); }
.foot-btn--primary { color: var(--accent); border-color: var(--accent); }
.foot-btn--primary:not(:disabled):hover { background: var(--accent); color: var(--bg-base); }
.fb-key { font-size: 10px; color: var(--fg-tertiary); border: 1px solid var(--border-strong); padding: 1px 5px; }
.foot-btn--primary .fb-key { color: var(--accent); border-color: var(--accent-dim); }
.foot-btn--primary:hover .fb-key { color: var(--bg-base); border-color: var(--bg-base); }

.foot-progress { display: flex; flex-direction: column; gap: 4px; align-items: center; min-width: 240px; }
.fp-bar { width: 100%; height: 4px; background: var(--bg-base); border: 1px solid var(--border); position: relative; overflow: hidden; }
.fp-fill { display: block; height: 100%; background: var(--accent); transition: width 200ms ease-out; box-shadow: 0 0 8px var(--accent); }
.fp-text { color: var(--fg-tertiary); font-size: 11px; letter-spacing: 0.1em; }

.hotkeys { display: flex; gap: 16px; padding: 8px 24px; background: var(--bg-base); color: var(--fg-tertiary); font-size: 10px; border-top: 1px solid var(--border); user-select: none; }
.hotkeys kbd { background: var(--bg-panel); border: 1px solid var(--border-strong); color: var(--fg-secondary); padding: 1px 5px; font-size: 10px; font-family: inherit; }

.step-enter-active, .step-leave-active { transition: all 220ms ease-out; }
.step-enter-from { opacity: 0; transform: translateX(16px); filter: blur(2px); }
.step-leave-to { opacity: 0; transform: translateX(-12px); filter: blur(2px); }

.wizard-loading { color: var(--fg-tertiary); padding: 60px 0; text-align: center; font-size: 14px; }

@keyframes slide-down {
  from { transform: translateY(-8px); opacity: 0; }
  to { transform: translateY(0); opacity: 1; }
}

@media (max-width: 1100px) {
  .wizard-body { grid-template-columns: minmax(0, 1fr) 300px; }
}
@media (max-width: 900px) {
  .wizard-body { grid-template-columns: 1fr; overflow: visible; }
  .wizard-side { border-left: 0; border-top: 1px solid var(--border); padding: 12px 16px 16px; overflow-y: auto; }
  .hotkeys { display: none; }
  .wizard-foot { grid-template-columns: auto 1fr auto; gap: 12px; }
  .foot-progress { min-width: 0; }
}
</style>
