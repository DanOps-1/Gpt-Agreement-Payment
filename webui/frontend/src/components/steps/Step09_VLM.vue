<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 09: VLM</div>
    <h2 class="step-h">$&nbsp;VLM endpoint (Optional)<span class="term-cursor"></span></h2>
    <p class="step-sub">Residential or pseudo-residential exit nodes usually don't trigger hCaptcha; if empty, it will auto-fallback to CLIP.</p>

    <TermToggle v-model="enabled">Enable VLM</TermToggle>

    <div v-if="enabled" class="form-stack" style="margin-top:16px">
      <TermField v-model="form.base_url" label="Base URL · base_url" placeholder="https://api.openai.com/v1" />
      <TermField v-model="form.api_key" label="API Key · api_key" type="password" />
      <TermField v-model="form.model" label="Model · model" placeholder="gpt-4o-mini" />
      <div class="step-actions">
        <TermBtn :loading="loading" @click="run">Test 1× completion</TermBtn>
      </div>
    </div>

    <div v-if="result" class="result-block" :class="`result--${result.status}`">
      <div class="result-head">
        <span class="result-icon">{{ icon(result.status) }}</span>
        <span>{{ result.message }}</span>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from "vue";
import { useWizardStore } from "../../stores/wizard";
import type { PreflightResult } from "../../api/client";
import TermField from "../term/TermField.vue";
import TermBtn from "../term/TermBtn.vue";
import TermToggle from "../term/TermToggle.vue";

const store = useWizardStore();
const init = store.answers.vlm ?? {};
const enabled = ref(!!init.api_key);
const form = ref({
  base_url: init.base_url ?? "https://api.openai.com/v1",
  api_key: init.api_key ?? "",
  model: init.model ?? "gpt-4o-mini",
});
const loading = ref(false);
const result = ref<PreflightResult | null>(null);

async function run() {
  store.setAnswer("vlm", enabled.value ? form.value : {});
  await store.saveToServer();
  loading.value = true;
  try {
    result.value = await store.runPreflight("vlm", form.value);
  } finally { loading.value = false; }
}

watch([form, enabled], () => store.setAnswer("vlm", enabled.value ? form.value : {}), { deep: true });

function icon(s: string) {
  return s === "ok" ? "✓" : s === "fail" ? "✗" : s === "warn" ? "▲" : "○";
}
</script>
