<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 11: 下游推送</div>
    <h2 class="step-h">$&nbsp;下游推送 (全部可选)<span class="term-cursor"></span></h2>

    <div class="term-divider" style="margin-top:8px">gpt-team</div>
    <TermToggle v-model="ts.enabled">启用 gpt-team</TermToggle>
    <div v-if="ts.enabled" class="form-stack" style="margin-top:12px">
      <TermField v-model="ts.base_url" label="Base URL · base_url" />
      <TermField v-model="ts.username" label="用户名 · username" />
      <TermField v-model="ts.password" label="密码 · password" type="password" />
      <div class="step-actions">
        <TermBtn :loading="tsLoading" @click="testTs">登录测试</TermBtn>
      </div>
      <div v-if="tsResult" class="result-block" :class="`result--${tsResult.status}`">
        <div class="result-head">
          <span class="result-icon">{{ icon(tsResult.status) }}</span>
          <span>{{ tsResult.message }}</span>
        </div>
      </div>
    </div>

    <div class="term-divider" style="margin-top:20px">CPA</div>
    <TermToggle v-model="cpa.enabled">启用 CPA</TermToggle>
    <div v-if="cpa.enabled" class="form-stack" style="margin-top:12px">
      <TermField v-model="cpa.base_url" label="Base URL · base_url" />
      <TermField v-model="cpa.admin_key" label="Admin Key · admin_key" type="password" />
      <div class="step-actions">
        <TermBtn :loading="cpaLoading" @click="testCpa">健康检查</TermBtn>
      </div>
      <div v-if="cpaResult" class="result-block" :class="`result--${cpaResult.status}`">
        <div class="result-head">
          <span class="result-icon">{{ icon(cpaResult.status) }}</span>
          <span>{{ cpaResult.message }}</span>
        </div>
      </div>
    </div>

    <div class="term-divider" style="margin-top:20px">sub2api</div>
    <TermToggle v-model="sub2api.enabled">启用 sub2api</TermToggle>
    <div v-if="sub2api.enabled" class="form-stack" style="margin-top:12px">
      <TermField v-model="sub2api.base_url" label="Base URL · base_url" />
      <TermField v-model="sub2api.admin_token" label="Admin JWT · admin_token" type="password" />
      <TermField v-model="sub2api.admin_email" label="Admin Email · admin_email" />
      <TermField v-model="sub2api.admin_password" label="Admin Password · admin_password" type="password" />
      <TermField v-model="sub2api.oauth_client_id" label="OAuth Client ID · oauth_client_id" />
      <TermField v-model="sub2api.group_ids" label="Group IDs · group_ids" placeholder="1,2,3" />
      <TermField v-model="sub2api.concurrency" label="Concurrency · concurrency" type="number" />
      <TermField v-model="sub2api.priority" label="Priority · priority" type="number" />
      <TermField v-model="sub2api.load_factor" label="Load Factor · load_factor" type="number" />
      <TermField v-model="sub2api.rate_multiplier" label="Rate Multiplier · rate_multiplier" type="number" />
      <div class="step-actions">
        <TermBtn :loading="sub2apiLoading" @click="testSub2api">健康检查</TermBtn>
      </div>
      <div v-if="sub2apiResult" class="result-block" :class="`result--${sub2apiResult.status}`">
        <div class="result-head">
          <span class="result-icon">{{ icon(sub2apiResult.status) }}</span>
          <span>{{ sub2apiResult.message }}</span>
        </div>
      </div>
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
const sub2apiInit = store.answers.sub2api ?? {};

// 开关默认关闭（不读 init.enabled），但其余字段保留 source 同步的值
// 这样用户启用 toggle 时直接看到预填的 url/凭据
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
const sub2api = ref({
  enabled: false,
  base_url: sub2apiInit.base_url ?? "",
  admin_token: sub2apiInit.admin_token ?? sub2apiInit.admin_jwt ?? sub2apiInit.admin_key ?? "",
  admin_email: sub2apiInit.admin_email ?? sub2apiInit.username ?? "",
  admin_password: sub2apiInit.admin_password ?? sub2apiInit.password ?? "",
  oauth_client_id: sub2apiInit.oauth_client_id ?? "",
  group_ids: Array.isArray(sub2apiInit.group_ids) ? sub2apiInit.group_ids.join(",") : (sub2apiInit.group_ids ?? ""),
  concurrency: sub2apiInit.concurrency ?? 1,
  priority: sub2apiInit.priority ?? 0,
  load_factor: sub2apiInit.load_factor ?? 1,
  rate_multiplier: sub2apiInit.rate_multiplier ?? 1,
});

// 立即同步到 store 覆盖可能从 source 同步过来的 enabled=true，
// 否则 UI 显示关但 wizard state / 导出仍会写 enabled=true
onMounted(() => {
  store.setAnswer("team_system", {});
  store.setAnswer("cpa", {});
  store.setAnswer("sub2api", {});
  store.saveToServer();
});
const tsLoading = ref(false);
const cpaLoading = ref(false);
const sub2apiLoading = ref(false);
const tsResult = ref<PreflightResult | null>(null);
const cpaResult = ref<PreflightResult | null>(null);
const sub2apiResult = ref<PreflightResult | null>(null);

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
async function testSub2api() {
  sub2apiLoading.value = true;
  try {
    sub2apiResult.value = await store.runPreflight("sub2api", {
      base_url: sub2api.value.base_url,
      admin_token: sub2api.value.admin_token,
      admin_email: sub2api.value.admin_email,
      admin_password: sub2api.value.admin_password,
    });
  } finally { sub2apiLoading.value = false; }
}
watch([ts, cpa, sub2api], () => {
  store.setAnswer("team_system", ts.value.enabled ? ts.value : {});
  store.setAnswer("cpa", cpa.value.enabled ? cpa.value : {});
  store.setAnswer("sub2api", sub2api.value.enabled ? sub2api.value : {});
  store.saveToServer();
}, { deep: true });

function icon(s: string) {
  return s === "ok" ? "✓" : s === "fail" ? "✗" : s === "warn" ? "▲" : "○";
}
</script>
