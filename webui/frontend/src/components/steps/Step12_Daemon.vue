<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 12: Daemon</div>
    <h2 class="step-h">$&nbsp;Daemon Parameters<span class="term-cursor"></span></h2>
    <p class="step-sub" v-if="!isDaemon" style="color: var(--warn)">Current mode is not daemon, you can skip this step.</p>

    <div v-if="isDaemon" class="form-stack">
      <TermField v-model="form.target_ok_accounts" label="Target OK Accounts · target_ok_accounts" type="number" />
      <TermField v-model="form.poll_interval_s" label="Poll Interval Seconds · poll_interval_s" type="number" />
      <TermField v-model="form.rate_limit_per_hour" label="Rate Limit Per Hour · rate_limit_per_hour" type="number" />
      <TermField v-model="form.max_consecutive_failures" label="Max Consecutive Failures · max_consecutive_failures" type="number" />
      <TermField v-model="form.seat_limit" label="Seat Limit · seat_limit" type="number" />
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
