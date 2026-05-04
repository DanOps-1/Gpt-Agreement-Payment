<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 14: Finalize</div>
    <h2 class="step-h">$&nbsp;Review + Export<span class="term-cursor"></span></h2>
    <p class="step-sub">Below are the two configurations about to be written (snapshots based on your answers). After checking, click "Write to repo path", which will back up existing files before overwriting.</p>

    <div class="review-label">All Answers</div>
    <pre class="review-pre">{{ prettyAll }}</pre>

    <div class="step-actions">
      <TermBtn :loading="loading" @click="exportConfigs">Write to repo path</TermBtn>
      <TermBtn variant="ghost" @click="copy">Copy JSON</TermBtn>
      <TermBtn variant="ghost" @click="downloadJson">Download JSON</TermBtn>
    </div>

    <div v-if="result" class="result-block ok">
      <div class="result-head">
        <span class="result-icon">✓</span>
        <span>Written Successfully</span>
      </div>
      <div class="export-paths">
        <div class="export-path">{{ result.pay_path }}</div>
        <div class="export-path">{{ result.reg_path }}</div>
        <div v-if="result.backups?.length" class="export-path" style="color: var(--fg-tertiary)">
          Backups: {{ result.backups.join(", ") }}
        </div>
      </div>
      <pre class="export-cmd">xvfb-run -a python pipeline.py --config {{ relPath(result.pay_path) }} --paypal</pre>
      <TermBtn @click="goRun" style="margin-top:12px">Run in Web now →</TermBtn>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, computed } from "vue";
import { useRouter } from "vue-router";
import { useMessage } from "naive-ui";
import { useWizardStore } from "../../stores/wizard";
import { api } from "../../api/client";
import TermBtn from "../term/TermBtn.vue";

const router = useRouter();
const store = useWizardStore();
const message = useMessage();
const loading = ref(false);

interface ExportResult {
  pay_path: string;
  reg_path: string;
  backups: string[];
}
const result = ref<ExportResult | null>(null);

const prettyAll = computed(() => JSON.stringify(store.answers, null, 2));

async function exportConfigs() {
  loading.value = true;
  try {
    const r = await api.post<ExportResult>("/config/export", { answers: store.answers });
    result.value = r.data;
    message.success("Configuration written");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "Write failed");
  } finally { loading.value = false; }
}

function copy() {
  navigator.clipboard.writeText(prettyAll.value);
  message.success("Copied to clipboard");
}

function downloadJson() {
  const blob = new Blob([prettyAll.value], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "wizard-answers.json";
  a.click();
}

function relPath(p: string) {
  const idx = p.indexOf("CTF-pay");
  return idx >= 0 ? p.slice(idx) : p;
}

function goRun() {
  const mode = store.answers.mode?.mode || "single";
  router.push({ path: "/run", query: { mode } });
}
</script>

<style scoped>
.review-label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.1em;
  color: var(--fg-tertiary);
  text-transform: uppercase;
  margin-bottom: 4px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 6px;
}
.review-pre {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  padding: 14px 16px;
  font: inherit;
  font-size: 12px;
  line-height: 1.6;
  color: var(--fg-primary);
  overflow-x: auto;
  overflow-y: auto;
  max-height: 360px;
  margin: 0;
  white-space: pre;
}
.export-paths {
  margin-top: 8px;
  font-size: 12px;
  color: var(--ok);
}
.export-path { padding: 2px 0; }
.export-cmd {
  margin-top: 8px;
  background: var(--bg-base);
  border: 1px solid var(--border);
  padding: 8px 12px;
  font: inherit;
  font-size: 11px;
  color: var(--accent);
  overflow-x: auto;
  white-space: pre;
}
</style>
