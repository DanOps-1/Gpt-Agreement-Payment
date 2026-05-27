<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 02: 系统</div>
    <h2 class="step-h">$&nbsp;系统依赖体检<span class="term-cursor"></span></h2>
    <p class="step-sub">看你机器上 Camoufox / xvfb-run / Playwright 装没装。</p>

    <div class="step-actions">
      <TermBtn :loading="loading" @click="run">重新检查</TermBtn>
    </div>

    <div v-if="result" class="result-block" :class="`result--${result.status}`" style="margin-top:16px">
      <div class="result-head">
        <span class="result-icon">{{ icon(result.status) }}</span>
        <span>{{ result.message }}</span>
      </div>
      <ul v-if="result.checks?.length" class="result-list">
        <li v-for="c in result.checks" :key="c.name" :class="`row-${c.status}`">
          <span class="row-name">{{ sym(c.status) }} {{ c.name }}</span>
          <span class="row-msg">{{ c.message }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useWizardStore } from "../../stores/wizard";
import type { PreflightResult } from "../../api/client";
import TermBtn from "../term/TermBtn.vue";

const store = useWizardStore();
const loading = ref(false);
const result = ref<PreflightResult | null>(store.preflight.system ?? null);

async function run() {
  loading.value = true;
  try {
    result.value = await store.runPreflight("system", {});
  } finally {
    loading.value = false;
  }
}

onMounted(() => { if (!result.value) run(); });

function icon(s: string) {
  return s === "ok" ? "✓" : s === "fail" ? "✗" : s === "warn" ? "▲" : "○";
}
function sym(s: string) {
  return s === "ok" ? "✓" : s === "fail" ? "✗" : s === "warn" ? "!" : "○";
}
</script>
