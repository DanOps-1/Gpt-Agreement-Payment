<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 12: Daemon</div>
    <h2 class="step-h">$&nbsp;Daemon 参数<span class="term-cursor"></span></h2>
    <p class="step-sub" v-if="!isDaemon" style="color: var(--warn)">当前模式不是 daemon，跳过即可。</p>

    <div v-if="isDaemon" class="form-stack">
      <TermField v-model="form.target_ok_accounts" label="目标可用号数 · target_ok_accounts" type="number" />
      <TermField v-model="form.poll_interval_s" label="轮询间隔秒 · poll_interval_s" type="number" />
      <TermField v-model="form.rate_limit_per_hour" label="每小时上限 · rate_limit_per_hour" type="number" />
      <TermField v-model="form.max_consecutive_failures" label="连续失败上限 · max_consecutive_failures" type="number" />
      <TermField v-model="form.seat_limit" label="席位上限 · seat_limit" type="number" />
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, computed, watch } from "vue";
import { useWizardStore } from "../../stores/wizard";
import TermField from "../term/TermField.vue";

const store = useWizardStore();
const isDaemon = computed(() => store.answers.mode?.mode === "daemon");
const init = store.answers.daemon ?? {};
const form = ref({
  target_ok_accounts: init.target_ok_accounts ?? 20,
  poll_interval_s: init.poll_interval_s ?? 600,
  rate_limit_per_hour: init.rate_limit_per_hour ?? 0,
  max_consecutive_failures: init.max_consecutive_failures ?? 5,
  seat_limit: init.seat_limit ?? 5,
});
watch(form, () => {
  if (!isDaemon.value) { store.setAnswer("daemon", {}); return; }
  store.setAnswer("daemon", form.value);
  store.saveToServer();
}, { deep: true });
</script>
