<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 11: Downstream Push</div>
    <h2 class="step-h">$&nbsp;Downstream Push (All Optional)<span class="term-cursor"></span></h2>

    <div class="term-divider" style="margin-top:8px">gpt-team</div>
    <TermToggle v-model="ts.enabled">Enable gpt-team</TermToggle>
    <div v-if="ts.enabled" class="form-stack" style="margin-top:12px">
      <TermField v-model="ts.base_url" label="Base URL · base_url" />
      <TermField v-model="ts.username" label="Username · username" />
      <TermField v-model="ts.password" label="Password · password" type="password" />
      <div class="step-actions">
        <TermBtn :loading="tsLoading" @click="testTs">Login Test</TermBtn>
      </div>
      <div v-if="tsResult" class="result-block" :class="`result--${tsResult.status}`">
        <div class="result-head">
          <span class="result-icon">{{ icon(tsResult.status) }}</span>
          <span>{{ tsResult.message }}</span>
        </div>
      </div>
    </div>

    <div class="term-divider" style="margin-top:20px">CPA</div>
    <TermToggle v-model="cpa.enabled">Enable CPA</TermToggle>
    <div v-if="cpa.enabled" class="form-stack" style="margin-top:12px">
      <TermField v-model="cpa.base_url" label="Base URL · base_url" />
      <TermField v-model="cpa.admin_key" label="Admin Key · admin_key" type="password" />
      <div class="step-actions">
        <TermBtn :loading="cpaLoading" @click="testCpa">Health Check</TermBtn>
      </div>
      <div v-if="cpaResult" class="result-block" :class="`result--${cpaResult.status}`">
        <div class="result-head">
          <span class="result-icon">{{ icon(cpaResult.status) }}</span>
          <span>{{ cpaResult.message }}</span>
        </div>
      </div>
    </div>

    <div class="term-divider" style="margin-top:20px">Retail Panel (cpa_autofill)</div>
    <TermToggle v-model="af.enabled">Enable Retail Panel Push</TermToggle>
    <div v-if="af.enabled" class="form-stack" style="margin-top:12px">
      <TermField v-model="af.base_url" label="Base URL · base_url (e.g: https://autofill.lukyface.com)" />
      <TermField v-model="af.api_token" label="API Token · Bearer token rotated from /supplier panel" type="password" />
      <p style="font-size:12px; color:#7a7363; margin:4px 0 0">Listing price (¥/account) is entered via popup during each push, not preset here.</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from "vue";
import { useWizardStore } from "../../stores/wizard";
import type { PreflightResult } from "../../api/client";
import TermField from "../term/TermField.vue";
import TermBtn from "../term/TermBtn.vue";
import TermToggle from "../term/TermToggle.vue";

const store = useWizardStore();
const tsInit = store.answers.team_system ?? {};
const cpaInit = store.answers.cpa ?? {};
const afInit = store.answers.cpa_autofill ?? {};

// Toggles default to off (do not read init.enabled), but other fields retain values synced from source
// This way when users enable the toggle, they immediately see pre-filled url/credentials
const ts = ref({
  enabled: false,
  base_url: tsInit.base_url ?? "http://127.0.0.1:3000",
  username: tsInit.username ?? "admin",
  password: tsInit.password ?? "",
});
const cpa = ref({
  enabled: false,
  base_url: cpaInit.base_url ?? "",
  admin_key: cpaInit.admin_key ?? "",
});
const af = ref({
  enabled: false,
  base_url: afInit.base_url ?? "https://autofill.lukyface.com",
  api_token: afInit.api_token ?? "",
});

// Immediately sync to store to override any enabled=true synced from source,
// otherwise UI shows off but wizard state / export still writes enabled=true
onMounted(() => {
  store.setAnswer("team_system", {});
  store.setAnswer("cpa", {});
  store.setAnswer("cpa_autofill", {});
  store.saveToServer();
});
const tsLoading = ref(false);
const cpaLoading = ref(false);
const tsResult = ref<PreflightResult | null>(null);
const cpaResult = ref<PreflightResult | null>(null);

async function testTs() {
  tsLoading.value = true;
  try {
    tsResult.value = await store.runPreflight("team_system", {
      base_url: ts.value.base_url,
      username: ts.value.username,
      password: ts.value.password,
    });
  } finally { tsLoading.value = false; }
}
async function testCpa() {
  cpaLoading.value = true;
  try {
    cpaResult.value = await store.runPreflight("cpa", {
      base_url: cpa.value.base_url,
      admin_key: cpa.value.admin_key,
    });
  } finally { cpaLoading.value = false; }
}
watch([ts, cpa, af], () => {
  store.setAnswer("team_system", ts.value.enabled ? ts.value : {});
  store.setAnswer("cpa", cpa.value.enabled ? cpa.value : {});
  store.setAnswer("cpa_autofill", af.value.enabled ? af.value : {});
  store.saveToServer();
}, { deep: true });

function icon(s: string) {
  return s === "ok" ? "✓" : s === "fail" ? "✗" : s === "warn" ? "▲" : "○";
}
</script>