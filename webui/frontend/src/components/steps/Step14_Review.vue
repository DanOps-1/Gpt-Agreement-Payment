<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 14: 完成</div>
    <h2 class="step-h">$&nbsp;Review + 导出<span class="term-cursor"></span></h2>
    <p class="step-sub">下面是即将写入的两份配置（基于你填的答案的快照）。检查无误后点"写到 repo 路径"，会先备份再覆盖。</p>

    <div class="review-label">所有答案</div>
    <pre class="review-pre">{{ prettyAll }}</pre>

    <div class="step-actions">
      <TermBtn :loading="loading" @click="exportConfigs">写到 repo 路径</TermBtn>
      <TermBtn variant="ghost" @click="copy">复制 JSON</TermBtn>
      <TermBtn variant="ghost" @click="downloadJson">下载 JSON</TermBtn>
    </div>

    <div v-if="result" class="result-block ok">
      <div class="result-head">
        <span class="result-icon">✓</span>
        <span>已写入</span>
      </div>
      <div class="export-paths">
        <div class="export-path">{{ result.pay_path }}</div>
        <div class="export-path">{{ result.reg_path }}</div>
        <div v-if="result.backups?.length" class="export-path" style="color: var(--fg-tertiary)">
          备份：{{ result.backups.join(", ") }}
        </div>
      </div>
      <pre class="export-cmd">python pipeline.py --config {{ relPath(result.pay_path) }} --paypal</pre>
      <div class="export-note">Linux 无桌面环境时，可改成 <code>xvfb-run -a python ...</code>。</div>
      <TermBtn @click="goRun" style="margin-top:12px">立即在 Web 里运行 →</TermBtn>
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
    message.success("配置已写入");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "写入失败");
  } finally { loading.value = false; }
}

function copy() {
  navigator.clipboard.writeText(prettyAll.value);
  message.success("已复制到剪贴板");
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
.export-note {
  margin-top: 8px;
  font-size: 12px;
  color: var(--fg-tertiary);
}
.export-note code {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  padding: 1px 4px;
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
