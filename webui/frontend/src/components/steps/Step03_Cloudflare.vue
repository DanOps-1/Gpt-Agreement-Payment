<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 03: 邮箱接码</div>
    <h2 class="step-h">$&nbsp;邮箱接码源<span class="term-cursor"></span></h2>
    <p class="step-sub">选择注册和 OAuth 登录时接收 OpenAI 邮件验证码的后端。</p>

    <div class="form-stack">
      <TermChoice v-model="provider" :options="providerOptions" :cols="2" @update:modelValue="onProviderChange" />
    </div>

    <div v-if="provider === 'cloudflare_kv'" class="form-stack provider-form">
      <TermField
        v-model="form.cf_token"
        label="API Token · cf_token"
        type="password"
        placeholder="cf api token"
      />
      <label class="tf">
        <span class="tf-tag">Zone 列表 · zone_names</span>
        <textarea
          v-model="zoneText"
          class="tf-textarea"
          placeholder="一行一个，如 example.com"
          rows="3"
        ></textarea>
      </label>
    </div>

    <div v-else class="form-stack provider-form">
      <TermField
        v-model="luckmail.api_key"
        label="API Key · api_key"
        type="password"
        placeholder="LuckMail OpenAPI api key"
      />
      <TermField
        v-model="luckmail.project_code"
        label="项目代码 · project_code"
        placeholder="LuckMail 后台项目 code，如 openai"
      />
      <TermSelect
        v-model="luckmail.email_type"
        label="邮箱类型 · email_type"
        :options="emailTypeOptions"
      />
      <TermSelect
        v-model="luckmail.domain"
        label="邮箱域 · domain"
        :options="domainOptions"
      />
      <TermField
        v-model="luckmail.base_url"
        label="Base URL · base_url"
        placeholder="https://mails.luckyous.com"
      />
      <TermField
        v-model="luckmail.timeout_seconds"
        label="超时秒数 · timeout_seconds"
        type="number"
      />
    </div>

    <div v-if="provider === 'cloudflare_kv'" class="step-actions">
      <TermBtn :loading="loading" @click="run">测试 token + zones</TermBtn>
    </div>
    <div v-else class="step-actions">
      <TermBtn :loading="loading" @click="runLuckMail">测试 LuckMail</TermBtn>
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
import TermChoice from "../term/TermChoice.vue";
import TermSelect from "../term/TermSelect.vue";

const store = useWizardStore();
const provider = ref(store.answers.mail_provider?.provider ?? "cloudflare_kv");
const init = store.answers.cloudflare ?? {};
const form = ref({
  cf_token: init.cf_token ?? "",
  zone_names: (init.zone_names ?? []) as string[],
});
const lmInit = store.answers.luckmail ?? {};
const luckmail = ref({
  api_key: lmInit.api_key ?? "",
  base_url: lmInit.base_url ?? "https://mails.luckyous.com",
  project_code: lmInit.project_code ?? "",
  email_type: lmInit.email_type ?? "ms_graph",
  domain: lmInit.domain ?? "",
  poll_interval_s: lmInit.poll_interval_s ?? 3,
  timeout_seconds: lmInit.timeout_seconds ?? 180,
});
const providerOptions = [
  { value: "cloudflare_kv", label: "Cloudflare KV", desc: "catch-all 域名 + Email Worker → KV" },
  { value: "luckmail_ms_graph", label: "LuckMail Graph", desc: "LuckMail Mode A · Microsoft Graph 邮箱" },
];
const emailTypeOptions = [
  { value: "ms_graph", label: "ms_graph", desc: "Microsoft Graph API (outlook.com / hotmail.com)" },
];
const domainOptions = [
  { value: "", label: "自动分配", desc: "由 LuckMail 按库存选择 Outlook/Hotmail" },
  { value: "outlook.com", label: "outlook.com" },
  { value: "hotmail.com", label: "hotmail.com" },
];
const zoneText = computed({
  get: () => form.value.zone_names.join("\n"),
  set: (v: string) => (form.value.zone_names = v.split("\n").map((s) => s.trim()).filter(Boolean)),
});
const loading = ref(false);
const result = ref<PreflightResult | null>(
  provider.value === "luckmail_ms_graph" ? (store.preflight.luckmail ?? null) : (store.preflight.cloudflare ?? null)
);

function onProviderChange(v: string) {
  store.setAnswer("mail_provider", { provider: v });
  result.value = v === "luckmail_ms_graph" ? (store.preflight.luckmail ?? null) : (store.preflight.cloudflare ?? null);
  store.saveToServer();
}

async function run() {
  store.setAnswer("cloudflare", form.value);
  store.setAnswer("mail_provider", { provider: provider.value });
  await store.saveToServer();
  loading.value = true;
  try {
    result.value = await store.runPreflight("cloudflare", {
      cf_token: form.value.cf_token,
      zone_names: form.value.zone_names,
    });
  } finally { loading.value = false; }
}

async function runLuckMail() {
  store.setAnswer("mail_provider", { provider: provider.value });
  store.setAnswer("luckmail", luckmail.value);
  await store.saveToServer();
  loading.value = true;
  try {
    result.value = await store.runPreflight("luckmail", {
      api_key: luckmail.value.api_key,
      base_url: luckmail.value.base_url,
      project_code: luckmail.value.project_code,
      email_type: luckmail.value.email_type,
    });
  } finally { loading.value = false; }
}

watch(form, () => store.setAnswer("cloudflare", form.value), { deep: true });
watch(provider, () => store.setAnswer("mail_provider", { provider: provider.value }));
watch(luckmail, () => store.setAnswer("luckmail", luckmail.value), { deep: true });

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
.provider-form { margin-top: 12px; }
</style>
