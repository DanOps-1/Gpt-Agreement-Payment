<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 08: 打码</div>
    <h2 class="step-h">$&nbsp;打码平台 (可选)<span class="term-cursor"></span></h2>
    <p class="step-sub">不填就跳过，浏览器 passive captcha 兜底。</p>

    <TermToggle v-model="enabled">启用打码平台</TermToggle>

    <div v-if="enabled" class="form-stack" style="margin-top:16px">
      <TermField v-model="form.api_url" label="API base URL · api_url" placeholder="https://api.example.com" />
      <TermField v-model="form.client_key" label="Client Key · client_key" type="password" />
      <div class="step-actions">
        <TermBtn :loading="loading" @click="run">测试 createTask</TermBtn>
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
const init = store.answers.captcha ?? {};
const enabled = ref(!!init.api_url);
const form = ref({
  api_url: init.api_url ?? "",
  client_key: init.client_key ?? "",
});
const loading = ref(false);
const result = ref<PreflightResult | null>(null);

async function run() {
  store.setAnswer("captcha", enabled.value ? form.value : {});
  await store.saveToServer();
  loading.value = true;
  try {
    result.value = await store.runPreflight("captcha", form.value);
  } finally { loading.value = false; }
}

watch([form, enabled], () => store.setAnswer("captcha", enabled.value ? form.value : {}), { deep: true });

function icon(s: string) {
  return s === "ok" ? "✓" : s === "fail" ? "✗" : s === "warn" ? "▲" : "○";
}
</script>
