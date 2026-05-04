<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 03: Cloudflare</div>
    <h2 class="step-h">$&nbsp;Cloudflare<span class="term-cursor"></span></h2>
    <p class="step-sub">API token must have DNS:Edit + Email Routing:Edit permissions, covering all zones you list.</p>

    <div class="form-stack">
      <TermField
        v-model="form.cf_token"
        label="API Token · cf_token"
        type="password"
        placeholder="cf api token"
      />
      <label class="tf">
        <span class="tf-tag">Zone List · zone_names</span>
        <textarea
          v-model="zoneText"
          class="tf-textarea"
          placeholder="One per line, e.g. example.com"
          rows="3"
        ></textarea>
      </label>
    </div>

    <div class="step-actions">
      <TermBtn :loading="loading" @click="run">Test token + zones</TermBtn>
    </div>

    <div v-if="result" class="result-block" :class="`result--${result.status}`">
      <div class="result-head">
        <span class="result-icon">{{ icon(result.status) }}</span>
        <span>{{ result.message }}</span>
      </div>
      <ul v-if="result.checks?.length" class="result-list">
        <li v-for="c in result.checks" :key="c.name" :class="`row-${c.status}`">
          <span class="row-name">{{ c.name }}</span>
          <span class="row-msg">{{ c.message }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, computed, watch } from "vue";
import { useWizardStore } from "../../stores/wizard";
import type { PreflightResult } from "../../api/client";
import TermField from "../term/TermField.vue";
import TermBtn from "../term/TermBtn.vue";

const store = useWizardStore();
const init = store.answers.cloudflare ?? {};
const form = ref({
  cf_token: init.cf_token ?? "",
  zone_names: (init.zone_names ?? []) as string[],
});
const zoneText = computed({
  get: () => form.value.zone_names.join("\n"),
  set: (v: string) => (form.value.zone_names = v.split("\n").map((s) => s.trim()).filter(Boolean)),
});
const loading = ref(false);
const result = ref<PreflightResult | null>(store.preflight.cloudflare ?? null);

async function run() {
  store.setAnswer("cloudflare", form.value);
  await store.saveToServer();
  loading.value = true;
  try {
    result.value = await store.runPreflight("cloudflare", {
      cf_token: form.value.cf_token,
      zone_names: form.value.zone_names,
    });
  } finally { loading.value = false; }
}

watch(form, () => store.setAnswer("cloudflare", form.value), { deep: true });

function icon(s: string) {
  return s === "ok" ? "✓" : s === "fail" ? "✗" : s === "warn" ? "▲" : "○";
}
</script>

<style scoped>
/* Local overrides only – shared styles come from theme.css */
.tf { display: grid; grid-template-columns: minmax(140px, max-content) minmax(0, 1fr); border: 1px solid var(--border); background: var(--bg-base); transition: border-color 80ms; }
.tf:focus-within { border-color: var(--accent); }
.tf-tag { background: var(--bg-panel); color: var(--fg-tertiary); padding: 10px 12px; font-size: 11px; font-weight: 700; letter-spacing: 0.04em; border-right: 1px solid var(--border); display: flex; align-items: flex-start; white-space: nowrap; }
.tf-textarea { background: transparent; border: 0; padding: 10px 12px; color: var(--fg-primary); font: inherit; font-size: 13px; outline: none; resize: vertical; min-height: 60px; width: 100%; }
.tf-textarea::placeholder { color: var(--fg-tertiary); opacity: 0.6; }
</style>
