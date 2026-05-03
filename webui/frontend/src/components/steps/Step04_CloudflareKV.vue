<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 04: Cloudflare KV</div>
    <h2 class="step-h">$&nbsp;OTP 接收（CF Email Worker → KV）<span class="term-cursor"></span></h2>
    <p class="step-sub">
      只填一个 API token，剩下的（建 KV、上传 Worker、给 Step 03 配的所有 zone
      切 catch-all 路由）后端一键搞定。token 默认借 Step 03 的 <code>cf_token</code>，
      也可以单独填一个权限更全的（需 <code>Workers Scripts:Edit</code> +
      <code>Workers KV:Edit</code> + <code>Email Routing Rules:Edit</code>）。
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
        label="备份转发 · fallback_to (可选)"
        placeholder="抓到 OTP 后同时转发一份到这个邮箱（迁移期保险）"
      />
    </div>

    <div class="step-actions">
      <TermBtn :loading="deploying" @click="deploy">一键部署 + 测试</TermBtn>
    </div>

    <div v-if="deployResult" class="result-block result--ok" style="margin-top:14px">
      <div class="result-head"><span class="result-icon">✓</span> 部署成功</div>
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
            {{ z.ok ? `before=[${z.before}] → worker` : `失败: ${z.error}` }}
          </span>
        </li>
        <li v-if="deployResult.secrets_path" class="row-ok">
          <span class="row-name">SQLite runtime_meta[secrets]</span>
          <span class="row-msg">已落 {{ deployResult.secrets_path }}</span>
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
  cfAns.cf_token ? "留空 = 用 Step 03 的 cf_token" : "粘贴 token"
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
    error.value = "缺 API token（要么填这里，要么在 Step 03 填 cf_token）";
    return;
  }
  const zones: string[] = (cfAns.zone_names ?? []) as string[];
  if (!zones.length) {
    error.value = "Step 03 还没填 zone_names，先回 Step 03 配 zones";
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
    // 答案里把回来的字段也存上，下次进 wizard 直接显示
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

    // 一键部署成功也给 preflight 写一个 ok，方便 step gate 解锁
    const allOk = (res.zones_configured ?? []).every((z: any) => z.ok);
    const result: PreflightResult = allOk
      ? { status: "ok", message: `部署完成，${res.zones_configured.length} 个 zone 已切到 worker`, checks: [] }
      : { status: "warn", message: "部署部分成功，看上面 zone 列表", checks: [] };
    store.setPreflight("cloudflare_kv", result);
  } catch (e: any) {
    error.value = e?.response?.data?.detail || String(e);
  } finally {
    deploying.value = false;
  }
}

watch(form, () => {
  // form 只在用户改 token / fallback 时同步，不覆盖 deploy 后的字段
  const cur = (store.answers.cloudflare_kv ?? {}) as any;
  store.setAnswer("cloudflare_kv", {
    ...cur,
    api_token: form.value.api_token,
    fallback_to: form.value.fallback_to,
  });
}, { deep: true });
</script>
