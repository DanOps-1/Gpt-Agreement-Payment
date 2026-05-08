<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 05: 代理</div>
    <h2 class="step-h">$&nbsp;代理<span class="term-cursor"></span></h2>
    <p class="step-sub">PayPal 锁地区，Stripe 锁国家。出口必须在 EU/US。</p>

    <TermChoice
      v-model="form.mode"
      :options="modeOptions"
      :cols="3"
    />

    <div v-if="form.mode === 'webshare'" style="margin-top:16px">
      <div class="info-block">
        <p>
          <strong>Webshare 模式 = 代理 + 12 路自愈</strong>。pipeline 在 daemon 模式下会自动:
        </p>
        <ul class="info-list">
          <li>累计 N 次 <code>no_invite_permission</code> 触发换 IP（webshare API rotate）</li>
          <li>同 zone 换够 IP 后自动切 CF zone</li>
          <li>注册连续失败也切 zone</li>
          <li>配额耗尽进 N 小时冷却</li>
          <li>本地 gost 中继挂掉看门狗自动拉起</li>
          <li>换 IP 后同步 gpt-team 全局代理</li>
        </ul>
        <p style="margin-top:8px">
          多数情况用默认参数即可，下方"高级"里能调阈值。详见 <code>docs/daemon-mode.md</code> 12 路自愈环。
        </p>
      </div>

      <div class="form-stack">
        <TermField v-model="form.api_key" label="Webshare API Key · api_key" type="password" />
        <TermField v-model="form.register_country" label="注册出口国 · register_country" placeholder="US" />
        <TermField v-model="form.payment_country" label="支付出口国 · payment_country" placeholder="JP" />
      </div>

      <div class="step-actions">
        <TermBtn :loading="loading" @click="testWebshare">测试 Webshare</TermBtn>
        <button class="advanced-toggle" @click="showAdvanced = !showAdvanced">
          {{ showAdvanced ? '▾' : '▸' }} 高级（轮换阈值 / gost / 下游同步）
        </button>
      </div>

      <div v-if="showAdvanced" class="form-stack" style="margin-top:12px">
        <TermField v-model.number="form.refresh_threshold" label="换 IP 阈值 · refresh_threshold" type="number" placeholder="2 (累计 N 次 no_perm 后换 IP)" />
        <TermField v-model.number="form.zone_rotate_after_ip_rotations" label="切 zone IP 轮换数 · zone_rotate_after_ip_rotations" type="number" placeholder="2 (同 zone 换 N 次 IP 后切 zone)" />
        <TermField v-model.number="form.zone_rotate_on_reg_fails" label="切 zone 注册失败数 · zone_rotate_on_reg_fails" type="number" placeholder="3 (连续 N 次注册失败切 zone)" />
        <TermField v-model.number="form.no_rotation_cooldown_s" label="配额冷却秒数 · no_rotation_cooldown_s" type="number" placeholder="10800 (配额耗尽冷却秒数)" />
        <TermField v-model.number="form.gost_listen_port" label="gost 中继端口 · gost_listen_port" type="number" placeholder="18898 (本地 gost 中继端口)" />
        <label class="toggle-row">
          <input type="checkbox" v-model="form.refresh_before_register" />
          <span>注册开始前获取一次代理 (refresh_before_register)</span>
        </label>
        <label class="toggle-row">
          <input type="checkbox" v-model="form.refresh_between_stages" />
          <span>支付开始前重新获取代理 (refresh_between_stages)</span>
        </label>
        <label class="toggle-row">
          <input type="checkbox" v-model="form.sync_team_proxy" />
          <span>换 IP 后同步 gpt-team 全局代理 (sync_team_proxy)</span>
        </label>
      </div>
    </div>

    <div v-if="form.mode === 'manual'" class="form-stack" style="margin-top:16px">
      <label class="tf">
        <span class="tf-tag">代理 URL · url</span>
        <textarea
          v-model="proxyText"
          class="tf-textarea"
          placeholder="第一行注册代理&#10;第二行支付代理"
          rows="3"
        ></textarea>
      </label>
      <TermField v-model="form.expected_country" label="期望国家 · expected_country" placeholder="US" />
      <div class="form-stack">
        <TermField v-model="form.register_expected_country" label="注册期望国家 · register_expected_country" placeholder="US" />
        <TermField v-model="form.payment_expected_country" label="支付期望国家 · payment_expected_country" placeholder="JP" />
      </div>
      <div class="step-actions">
        <TermBtn :loading="loading" @click="testProxy">测试出口 IP</TermBtn>
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
import { computed, ref, watch } from "vue";
import { useWizardStore } from "../../stores/wizard";
import type { PreflightResult } from "../../api/client";
import TermField from "../term/TermField.vue";
import TermBtn from "../term/TermBtn.vue";
import TermChoice from "../term/TermChoice.vue";

const store = useWizardStore();
const init = store.answers.proxy ?? {};
const form = ref({
  mode: init.mode ?? "manual",
  url: init.url ?? "",
  urls: (init.urls ?? (init.url ? String(init.url).split("\n") : [])) as string[],
  expected_country: init.expected_country ?? "US",
  register_expected_country: init.register_expected_country ?? init.expected_country ?? "US",
  payment_expected_country: init.payment_expected_country ?? "JP",
  api_key: init.api_key ?? "",
  lock_country: init.lock_country ?? init.register_country ?? "US",
  register_country: init.register_country ?? init.lock_country ?? "US",
  payment_country: init.payment_country ?? "JP",
  // webshare 高级（默认值跟 pipeline.py 一致）
  refresh_threshold: init.refresh_threshold ?? 2,
  zone_rotate_after_ip_rotations: init.zone_rotate_after_ip_rotations ?? 2,
  zone_rotate_on_reg_fails: init.zone_rotate_on_reg_fails ?? 3,
  no_rotation_cooldown_s: init.no_rotation_cooldown_s ?? 10800,
  gost_listen_port: init.gost_listen_port ?? 18898,
  refresh_before_register: init.refresh_before_register ?? true,
  refresh_between_stages: init.refresh_between_stages ?? true,
  sync_team_proxy: init.sync_team_proxy ?? true,
});
const proxyText = computed({
  get: () => (form.value.urls?.length ? form.value.urls : (form.value.url ? [form.value.url] : [])).join("\n"),
  set: (v: string) => {
    const lines = v.split("\n").map((s) => s.trim()).filter(Boolean);
    form.value.urls = lines;
    form.value.url = lines[0] || "";
  },
});
const loading = ref(false);
const result = ref<PreflightResult | null>(null);
const showAdvanced = ref(false);

const modeOptions = [
  { value: "webshare", label: "webshare", desc: "Webshare API 托管 + 12 路自愈" },
  { value: "manual", label: "manual", desc: "手动 socks5/http" },
  { value: "none", label: "none", desc: "不用代理（直连）" },
];

async function testProxy() {
  store.setAnswer("proxy", form.value);
  await store.saveToServer();
  loading.value = true;
  try {
    result.value = await store.runPreflight("proxy", {
      mode: form.value.mode,
      url: form.value.url,
      urls: form.value.urls,
      expected_country: form.value.expected_country,
      register_expected_country: form.value.register_expected_country,
      payment_expected_country: form.value.payment_expected_country,
    });
  } finally { loading.value = false; }
}

async function testWebshare() {
  store.setAnswer("proxy", form.value);
  await store.saveToServer();
  loading.value = true;
  try {
    result.value = await store.runPreflight("webshare", {
      api_key: form.value.api_key,
      register_country: form.value.register_country,
      payment_country: form.value.payment_country,
    });
  } finally { loading.value = false; }
}

watch(form, () => store.setAnswer("proxy", form.value), { deep: true });

function icon(s: string) {
  return s === "ok" ? "✓" : s === "fail" ? "✗" : s === "warn" ? "▲" : "○";
}
</script>

<style scoped>
.info-block {
  background: var(--bg-panel);
  border-left: 3px solid var(--accent);
  padding: 10px 14px;
  margin: 0 0 16px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--fg-secondary);
}
.info-block p { margin: 0; }
.info-block strong { color: var(--fg-primary); font-weight: 700; }
.info-list { margin: 6px 0 0 18px; padding: 0; }
.info-list li { margin: 3px 0; }
code {
  background: var(--bg-base);
  padding: 1px 5px;
  border: 1px solid var(--border);
  font-size: 11px;
}
.advanced-toggle {
  background: transparent;
  border: 0;
  color: var(--fg-secondary);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  padding: 8px 0;
}
.advanced-toggle:hover { color: var(--accent); }
.toggle-row { display: flex; align-items: center; gap: 8px; cursor: pointer; padding: 8px 0; font-size: 13px; }
.toggle-row input { accent-color: var(--accent); }
.tf { display: grid; grid-template-columns: minmax(140px, max-content) minmax(0, 1fr); border: 1px solid var(--border); background: var(--bg-base); transition: border-color 80ms; }
.tf:focus-within { border-color: var(--accent); }
.tf-tag { background: var(--bg-panel); color: var(--fg-tertiary); padding: 10px 12px; font-size: 11px; font-weight: 700; letter-spacing: 0.04em; border-right: 1px solid var(--border); display: flex; align-items: flex-start; white-space: nowrap; }
.tf-textarea { background: transparent; border: 0; padding: 10px 12px; color: var(--fg-primary); font: inherit; font-size: 13px; outline: none; resize: vertical; min-height: 72px; width: 100%; }
.tf-textarea::placeholder { color: var(--fg-tertiary); opacity: 0.6; }
</style>
