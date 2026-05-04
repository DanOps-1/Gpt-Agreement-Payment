<template>
  <section class="step-fade-in">
    <template v-if="store.isStepHidden(13)">
      <div class="term-divider" data-tail="──────────">Step 13: Stripe Runtime — Skipped</div>
      <h2 class="step-h">$&nbsp;This step is skipped<span class="term-cursor"></span></h2>
      <p class="step-sub">You chose "PayPal" payment in step 1. The redirect path does not require Stripe runtime hashes (version has a fallback, js_checksum/rv_timestamp are only needed for the inline confirm path).</p>
      <div class="step-actions">
        <button class="term-btn term-btn--ghost" @click="goStep1">Go back to step 1 to modify</button>
      </div>
    </template>
    <template v-else>
      <div class="term-divider" data-tail="──────────">Step 13: Stripe Runtime</div>
      <h2 class="step-h">$&nbsp;Stripe runtime hashes<span class="term-cursor"></span></h2>
      <p class="step-sub">This step is most likely to get stuck. Click "Auto Sniff" to launch a headless Camoufox and go through the chatgpt.com pricing flow to get the current hashes. If it fails, you can fill it manually.</p>

      <div class="step-actions">
        <TermBtn :loading="sniffing" @click="sniff">Auto Sniff</TermBtn>
      </div>

      <div v-if="logLines.length" class="sniff-log">
        <div v-for="(line, i) in logLines" :key="i" class="sniff-line">{{ line }}</div>
      </div>

      <div class="term-divider" style="margin-top:20px">Manual Entry</div>
      <div class="form-stack">
        <TermField v-model="form.version" label="version" />
        <TermField v-model="form.js_checksum" label="js_checksum" />
        <TermField v-model="form.rv_timestamp" label="rv_timestamp" />
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from "vue";
import { useWizardStore } from "../../stores/wizard";
import { useMessage } from "naive-ui";
import TermField from "../term/TermField.vue";
import TermBtn from "../term/TermBtn.vue";

const store = useWizardStore();
const message = useMessage();
const init = store.answers.stripe_runtime ?? {};
const form = ref({
  version: init.version ?? "fed52f3bc6",
  js_checksum: init.js_checksum ?? "",
  rv_timestamp: init.rv_timestamp ?? "",
});
const sniffing = ref(false);
const logLines = ref<string[]>([]);

function sniff() {
  sniffing.value = true;
  logLines.value = [];
  const es = new EventSource(import.meta.env.BASE_URL + "api/sniff/stripe");
  es.addEventListener("status", (e) => {
    const data = JSON.parse((e as MessageEvent).data);
    logLines.value.push(`[status] ${data.phase}`);
  });
  es.addEventListener("result", (e) => {
    const data = JSON.parse((e as MessageEvent).data);
    form.value = { ...form.value, ...data };
    logLines.value.push(`[result] version=${data.version} js=${data.js_checksum} rv=${data.rv_timestamp}`);
    message.success("hashes filled");
  });
  es.addEventListener("error", (e) => {
    const m = e instanceof MessageEvent && e.data ? JSON.parse(e.data) : { reason: "stream error" };
    logLines.value.push(`[error] ${m.reason}`);
  });
  es.addEventListener("done", () => {
    sniffing.value = false;
    es.close();
  });
}

watch(form, () => { store.setAnswer("stripe_runtime", form.value); store.saveToServer(); }, { deep: true });

function goStep1() {
  store.setStep(1);
  store.saveToServer();
}
</script>

<style scoped>
.sniff-log {
  margin-top: 12px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  padding: 8px 12px;
  font-size: 11px;
  max-height: 120px;
  overflow-y: auto;
}
.sniff-line {
  padding: 2px 0;
  color: var(--fg-secondary);
  font-variant-numeric: tabular-nums;
}
.sniff-line:last-child { color: var(--accent); }
</style>
