<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 05: Proxy</div>
    <h2 class="step-h">$&nbsp;Proxy<span class="term-cursor"></span></h2>
    <p class="step-sub">PayPal is region-locked, Stripe is country-locked. Exit node must be in EU/US.</p>

    <TermChoice
      v-model="form.mode"
      :options="modeOptions"
      :cols="3"
    />

    <div v-if="form.mode === 'webshare'" style="margin-top:16px">
      <div class="info-block">
        <p>
          <strong>Webshare Mode = Proxy + 12-way self-healing</strong>. In daemon mode, the pipeline will automatically:
        </p>
        <ul class="info-list">
          <li>Rotate IP (Webshare API rotate) after N cumulative <code>no_invite_permission</code> errors</li>
          <li>Rotate CF zone after rotating through enough IPs in the same zone</li>
          <li>Rotate zone after consecutive registration failures</li>
          <li>Enter N-hour cooldown when quota is exhausted</li>
          <li>Auto-restart local gost relay via watchdog if it crashes</li>
          <li>Sync gpt-team global proxy after IP rotation</li>
        </ul>
        <p style="margin-top:8px">
          Default parameters are usually sufficient. Thresholds can be adjusted in "Advanced" below. See <code>docs/daemon-mode.md</code> for the self-healing loop details.
        </p>
      </div>

      <div class="form-stack">
        <TermField v-model="form.api_key" label="Webshare API Key · api_key" type="password" />
        <TermField v-model="form.lock_country" label="Lock Exit Country · lock_country" placeholder="US (Lock country)" />
      </div>

      <div class="step-actions">
        <TermBtn :loading="loading" @click="testWebshare">Test Webshare</TermBtn>
        <button class="advanced-toggle" @click="showAdvanced = !showAdvanced">
          {{ showAdvanced ? '▾' : '▸' }} Advanced (Rotation thresholds / gost / downstream sync)
        </button>
      </div>

      <div v-if="showAdvanced" class="form-stack" style="margin-top:12px">
        <TermField v-model.number="form.refresh_threshold" label="IP rotation threshold · refresh_threshold" type="number" placeholder="2 (rotate IP after N no_perm errors)" />
        <TermField v-model.number="form.zone_rotate_after_ip_rotations" label="IP rotations per zone · zone_rotate_after_ip_rotations" type="number" placeholder="2 (rotate zone after N IP rotations)" />
        <TermField v-model.number="form.zone_rotate_on_reg_fails" label="Reg failures per zone · zone_rotate_on_reg_fails" type="number" placeholder="3 (rotate zone after N consecutive reg failures)" />
        <TermField v-model.number="form.no_rotation_cooldown_s" label="Quota cooldown seconds · no_rotation_cooldown_s" type="number" placeholder="10800 (seconds to cooldown when quota exhausted)" />
        <TermField v-model.number="form.gost_listen_port" label="gost relay port · gost_listen_port" type="number" placeholder="18898 (local gost relay port)" />
        <label class="toggle-row">
          <input type="checkbox" v-model="form.sync_team_proxy" />
          <span>Sync gpt-team global proxy after IP rotation (sync_team_proxy)</span>
        </label>
      </div>
    </div>

    <div v-if="form.mode === 'manual'" class="form-stack" style="margin-top:16px">
      <TermField v-model="form.url" label="Proxy URL · url" placeholder="socks5://user:pw@host:port" />
      <TermField v-model="form.expected_country" label="Expected Country · expected_country" placeholder="US" />
      <div class="step-actions">
        <TermBtn :loading="loading" @click="testProxy">Test Exit IP</TermBtn>
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
import TermChoice from "../term/TermChoice.vue";

const store = useWizardStore();
const init = store.answers.proxy ?? {};
const form = ref({
  mode: init.mode ?? "manual",
  url: init.url ?? "",
  expected_country: init.expected_country ?? "US",
  api_key: init.api_key ?? "",
  lock_country: init.lock_country ?? "US",
  // webshare advanced (defaults match pipeline.py)
  refresh_threshold: init.refresh_threshold ?? 2,
  zone_rotate_after_ip_rotations: init.zone_rotate_after_ip_rotations ?? 2,
  zone_rotate_on_reg_fails: init.zone_rotate_on_reg_fails ?? 3,
  no_rotation_cooldown_s: init.no_rotation_cooldown_s ?? 10800,
  gost_listen_port: init.gost_listen_port ?? 18898,
  sync_team_proxy: init.sync_team_proxy ?? true,
});
const loading = ref(false);
const result = ref<PreflightResult | null>(null);
const showAdvanced = ref(false);

const modeOptions = [
  { value: "webshare", label: "webshare", desc: "Webshare API managed + 12-way self-healing" },
  { value: "manual", label: "manual", desc: "Manual socks5/http" },
  { value: "none", label: "none", desc: "No proxy (Direct)" },
];

async function testProxy() {
  store.setAnswer("proxy", form.value);
  await store.saveToServer();
  loading.value = true;
  try {
    result.value = await store.runPreflight("proxy", {
      mode: form.value.mode,
      url: form.value.url,
      expected_country: form.value.expected_country,
    });
  } finally { loading.value = false; }
}

async function testWebshare() {
  store.setAnswer("proxy", form.value);
  await store.saveToServer();
  loading.value = true;
  try {
    result.value = await store.runPreflight("webshare", { api_key: form.value.api_key });
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
</style>
