<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 04: Cloudflare KV</div>
    <h2 class="step-h">$&nbsp;OTP Reception (CF Email Worker → KV)<span class="term-cursor"></span></h2>
    <p class="step-sub">
      Just fill in an API token, and the backend will handle the rest (creating KV, uploading Worker, and setting up catch-all routing for all zones listed in Step 03). The token defaults to <code>cf_token</code> from Step 03, but you can provide a separate one with fuller permissions (requires <code>Workers Scripts:Edit</code> +
      <code>Workers KV:Edit</code> + <code>Email Routing Rules:Edit</code>).
    </p>

    <div class="form-stack">
      <TermField
        v-model="form.api_token"
        label="API Token · api_token"
        type="password"
        :placeholder="defaultTokenPlaceholder"
      />
      <TermField
        v-model="form.fallback_to"
        label="Backup Forwarding · fallback_to (Optional)"
        placeholder="Forward OTP to this email as well (safety measure during migration)"
      />
    </div>

    <div class="step-actions">
      <TermBtn :loading="deploying" @click="deploy">Deploy + Test</TermBtn>
    </div>

    <div v-if="deployResult" class="result-block result--ok" style="margin-top:14px">
      <div class="result-head"><span class="result-icon">✓</span> Deployment Successful</div>
      <ul class="result-list">
        <li class="row-ok"><span class="row-name">account</span><span class="row-msg">{{ deployResult.account_name }} ({{ deployResult.account_id }})</span></li>
        <li class="row-ok"><span class="row-name">kv_namespace_id</span><span class="row-msg">{{ deployResult.kv_namespace_id }}</span></li>
        <li class="row-ok"><span class="row-name">worker</span><span class="row-msg">{{ deployResult.worker_name }}</span></li>
        <li
          v-for="z in deployResult.zones_configured"
          :key="z.zone"
          :class="z.ok ? 'row-ok' : 'row-fail'"
        >
          <span class="row-name">zone:{{ z.zone }}</span>
          <span class="row-msg">
            {{ z.ok ? `before=[${z.before}] → worker` : `Failed: ${z.error}` }}
          </span>
        </li>
        <li v-if="deployResult.secrets_path" class="row-ok">
          <span class="row-name">SQLite runtime_meta[secrets]</span>
          <span class="row-msg">Saved to {{ deployResult.secrets_path }}</span>
        </li>
      </ul>
    </div>

    <div v-if="error" class="result-block result--fail" style="margin-top:14px">
      <div class="result-head"><span class="result-icon">✗</span> {{ error }}</div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, computed, watch } from "vue";
import { useWizardStore } from "../../stores/wizard";
import type { PreflightResult } from "../../api/client";
import { api } from "../../api/client";
import TermField from "../term/TermField.vue";
import TermBtn from "../term/TermBtn.vue";

const store = useWizardStore();
const cfAns = (store.answers.cloudflare ?? {}) as any;
const init = (store.answers.cloudflare_kv ?? {}) as any;

const form = ref({
  api_token: init.api_token ?? "",
  fallback_to: init.fallback_to ?? "",
});

const defaultTokenPlaceholder = computed(() =>
  cfAns.cf_token ? "Leave empty = use cf_token from Step 03" : "Paste token"
);

const deploying = ref(false);
const deployResult = ref<any>(
  init.account_id
    ? {
        account_name: init.account_name ?? "",
        account_id: init.account_id,
        kv_namespace_id: init.kv_namespace_id,
        worker_name: init.worker_name ?? "otp-relay",
        zones_configured: init.zones_configured ?? [],
        secrets_path: init.secrets_path ?? "",
      }
    : null
);
const error = ref<string>("");

async function deploy() {
  error.value = "";
  deployResult.value = null;
  const token = (form.value.api_token || cfAns.cf_token || "").trim();
  if (!token) {
    error.value = "Missing API token (either fill here or in Step 03 as cf_token)";
    return;
  }
  const zones: string[] = (cfAns.zone_names ?? []) as string[];
  if (!zones.length) {
    error.value = "Step 03 zone_names missing, return to Step 03 first";
    return;
  }

  deploying.value = true;
  try {
    const r = await api.post("/cloudflare_kv/auto-setup", {
      api_token: token,
      zones,
      worker_name: "otp-relay",
      kv_name: "OTP_KV",
      fallback_to: form.value.fallback_to,
    });
    const res = r.data;
    deployResult.value = res;
    // Save returned fields to answers to display them next time wizard is opened
    store.setAnswer("cloudflare_kv", {
      api_token: token,
      fallback_to: form.value.fallback_to,
      account_id: res.account_id,
      account_name: res.account_name,
      kv_namespace_id: res.kv_namespace_id,
      worker_name: res.worker_name,
      zones_configured: res.zones_configured,
      secrets_path: res.secrets_path,
    });
    await store.saveToServer();

    // Mark preflight as ok upon successful deployment to unlock subsequent step gates
    const allOk = (res.zones_configured ?? []).every((z: any) => z.ok);
    const result: PreflightResult = allOk
      ? { status: "ok", message: `Deployment complete, ${res.zones_configured.length} zones switched to worker`, checks: [] }
      : { status: "warn", message: "Deployment partially successful, check zone list above", checks: [] };
    store.setPreflight("cloudflare_kv", result);
  } catch (e: any) {
    error.value = e?.response?.data?.detail || String(e);
  } finally {
    deploying.value = false;
  }
}

watch(form, () => {
  // form only syncs when user changes token/fallback, does not overwrite deployed fields
  const cur = (store.answers.cloudflare_kv ?? {}) as any;
  store.setAnswer("cloudflare_kv", {
    ...cur,
    api_token: form.value.api_token,
    fallback_to: form.value.fallback_to,
  });
}, { deep: true });
</script>
