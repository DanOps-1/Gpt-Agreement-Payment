<template>
  <button class="gopay-inspector-btn" type="button" @click="open">
    GoPay解绑检测
  </button>

  <Teleport to="body">
    <div v-if="openState" class="gopay-inspector-overlay" @click.self="close">
      <div class="gopay-inspector-modal">
        <div class="inspector-head">
          <div>
            <div class="inspector-title">$ GoPay 解绑检测</div>
            <div class="inspector-sub">读取当前 config 中的 linkedapps 请求，检测已绑定 App，并支持手动解绑。</div>
          </div>
          <button class="icon-btn" type="button" @click="close" title="关闭">×</button>
        </div>

        <div class="inspector-actions">
          <button class="term-like-btn" type="button" :disabled="loading" @click="inspect">
            {{ loading ? "检测中..." : "检测当前绑定 App" }}
          </button>
          <span v-if="meta" class="inspector-meta">{{ meta }}</span>
        </div>

        <div v-if="error" class="status-box fail">
          {{ error }}
        </div>

        <div v-if="result" class="summary-row">
          <span>has_data={{ String(result.has_data) }}</span>
          <span>services={{ result.services_count ?? 0 }}</span>
          <span>accounts={{ result.accounts_count ?? 0 }}</span>
        </div>

        <div v-if="entries.length" class="linked-list">
          <div v-for="entry in entries" :key="entryKey(entry)" class="linked-item">
            <div class="linked-main">
              <div class="linked-title">{{ entry.service_name || entry.association_name || "Unknown App" }}</div>
              <div class="linked-grid">
                <span>service_id</span><code>{{ entry.service_id || "-" }}</code>
                <span>link_id</span><code>{{ entry.link_id || "-" }}</code>
                <span>unlink_url</span><code>{{ entry.unlink_url || "-" }}</code>
                <span>service_unlink</span><code>{{ entry.service_unlink_url || "-" }}</code>
                <span>allow</span><code>account={{ String(entry.allow_account_unlink) }} service={{ String(entry.allow_service_unlink) }}</code>
              </div>
            </div>
            <button
              class="term-like-btn danger"
              type="button"
              :disabled="unlinkingKey === entryKey(entry)"
              @click="manualUnlink(entry)"
            >
              {{ unlinkingKey === entryKey(entry) ? "解绑中..." : "手动解绑" }}
            </button>
          </div>
        </div>

        <div v-else-if="result && !loading" class="status-box">
          当前 linkedapps data 中没有解析到 linked_accounts 绑定项。
        </div>

        <div v-if="unlinkResult" class="result-panel" :class="{ ok: unlinkResult.ok, fail: !unlinkResult.ok }">
          <div class="result-title">手动解绑结果: {{ unlinkResult.ok ? "成功" : "失败" }}</div>
          <div class="result-grid">
            <span>reason</span><code>{{ unlinkResult.reason || "-" }}</code>
            <span>unlink_status</span><code>{{ unlinkResult.unlink_status_code || "-" }}</code>
            <span>verify_status</span><code>{{ unlinkResult.verify_status_code || "-" }}</code>
            <span>target</span><code>{{ unlinkResult.unlink_target_url || "-" }}</code>
          </div>
          <pre>{{ pretty(unlinkResult) }}</pre>
        </div>

        <details v-if="result" class="raw-details">
          <summary>查看 linkedapps 原始响应</summary>
          <pre>{{ pretty(result.body_json ?? result.body ?? "") }}</pre>
        </details>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { useMessage } from "naive-ui";
import { api } from "../api/client";
import { useWizardStore } from "../stores/wizard";

const message = useMessage();
const store = useWizardStore();

const openState = ref(false);
const loading = ref(false);
const error = ref("");
const result = ref<any>(null);
const unlinkResult = ref<any>(null);
const unlinkingKey = ref("");

const entries = computed<any[]>(() => Array.isArray(result.value?.entries) ? result.value.entries : []);
const meta = computed(() => {
  if (!result.value) return "";
  return `${result.value.status_code || ""} ${result.value.content_type || ""}`.trim();
});

function entryKey(entry: any) {
  return [
    entry.link_id || "",
    entry.unlink_url || "",
    entry.service_unlink_url || "",
    entry.service_id || "",
  ].join("|");
}

function pretty(value: any) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? "");
  }
}

async function open() {
  openState.value = true;
  await inspect();
}

function close() {
  openState.value = false;
}

async function inspect() {
  loading.value = true;
  error.value = "";
  unlinkResult.value = null;
  try {
    await persistWizardAutoUnbind();
    const r = await api.post("/config/gopay/auto-unbind/linkedapps", { timeout: 20 });
    result.value = r.data;
    if (entries.value.length) {
      message.success(`检测到 ${entries.value.length} 个 GoPay 绑定项`);
    } else {
      message.warning("linkedapps 返回 data，但未解析到绑定项");
    }
  } catch (e: any) {
    result.value = null;
    error.value = e.response?.data?.detail || e.message || "检测失败";
    message.error(error.value);
  } finally {
    loading.value = false;
  }
}

async function persistWizardAutoUnbind() {
  const gp = (store.answers.gopay || {}) as any;
  const raw = String(gp.auto_unbind_raw_request || gp.auto_unbind?.raw_request || "");
  const baseUrl = String(gp.auto_unbind_base_url || gp.auto_unbind?.base_url || "");
  if (!raw.trim()) return;
  await api.post("/config/gopay/auto-unbind", {
    raw_request: raw,
    base_url: baseUrl,
  });
}

async function manualUnlink(entry: any) {
  const key = entryKey(entry);
  unlinkingKey.value = key;
  unlinkResult.value = null;
  error.value = "";
  try {
    const r = await api.post("/config/gopay/auto-unbind/manual", {
      unlink_url: entry.unlink_url || "",
      service_unlink_url: entry.service_unlink_url || "",
      link_id: entry.link_id || "",
      timeout: 20,
    });
    unlinkResult.value = r.data;
    if (r.data?.ok) {
      message.success("GoPay 手动解绑成功");
      await inspect();
      unlinkResult.value = r.data;
    } else {
      message.error(r.data?.reason || "GoPay 手动解绑失败");
    }
  } catch (e: any) {
    error.value = e.response?.data?.detail || e.message || "手动解绑失败";
    message.error(error.value);
  } finally {
    unlinkingKey.value = "";
  }
}
</script>

<style scoped>
.gopay-inspector-btn {
  background: transparent;
  border: 1px solid var(--border-strong);
  color: var(--fg-secondary);
  padding: 6px 12px;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.gopay-inspector-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--bg-panel);
}
.gopay-inspector-overlay {
  position: fixed;
  inset: 0;
  z-index: 1200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(0, 0, 0, 0.42);
}
.gopay-inspector-modal {
  width: min(980px, 96vw);
  max-height: 92vh;
  overflow: auto;
  background: var(--bg-base);
  border: 1px solid var(--accent);
  box-shadow: 0 14px 48px rgba(0,0,0,.25);
  padding: 18px;
}
.inspector-head {
  display: flex;
  justify-content: space-between;
  gap: 18px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 12px;
}
.inspector-title {
  color: var(--accent);
  font-weight: 700;
  font-size: 15px;
}
.inspector-sub {
  margin-top: 6px;
  color: var(--fg-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.icon-btn {
  width: 28px;
  height: 28px;
  border: 1px solid var(--border-strong);
  background: transparent;
  color: var(--fg-secondary);
  font: inherit;
  cursor: pointer;
}
.icon-btn:hover {
  color: var(--accent);
  border-color: var(--accent);
}
.inspector-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 14px 0;
}
.term-like-btn {
  background: transparent;
  border: 1px solid var(--accent-strong);
  color: var(--accent-strong);
  padding: 8px 12px;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.term-like-btn:hover:not(:disabled) {
  background: var(--accent-strong);
  color: #fff;
}
.term-like-btn:disabled {
  opacity: .5;
  cursor: not-allowed;
}
.term-like-btn.danger {
  border-color: var(--err);
  color: var(--err);
  white-space: nowrap;
}
.term-like-btn.danger:hover:not(:disabled) {
  background: var(--err);
  color: #fff;
}
.inspector-meta,
.summary-row {
  color: var(--fg-tertiary);
  font-size: 12px;
}
.summary-row {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-bottom: 12px;
}
.status-box {
  border: 1px dashed var(--border);
  background: var(--bg-panel);
  padding: 12px;
  color: var(--fg-secondary);
  font-size: 12px;
}
.status-box.fail {
  border-color: var(--err);
  color: var(--err);
  background: #fbecec;
}
.linked-list {
  display: grid;
  gap: 10px;
}
.linked-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 14px;
  align-items: start;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  padding: 12px;
}
.linked-title {
  font-weight: 700;
  color: var(--fg-primary);
  margin-bottom: 8px;
}
.linked-grid,
.result-grid {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  gap: 6px 10px;
  font-size: 12px;
}
.linked-grid span,
.result-grid span {
  color: var(--fg-tertiary);
}
code {
  color: var(--fg-primary);
  word-break: break-all;
}
.result-panel,
.raw-details {
  margin-top: 14px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  padding: 12px;
}
.result-panel.ok {
  border-color: var(--ok);
  background: #ebf5ee;
}
.result-panel.fail {
  border-color: var(--err);
  background: #fbecec;
}
.result-title {
  font-weight: 700;
  margin-bottom: 8px;
}
pre {
  margin: 10px 0 0;
  max-height: 300px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font: inherit;
  font-size: 11px;
  line-height: 1.55;
  color: var(--fg-primary);
}
.raw-details summary {
  cursor: pointer;
  color: var(--accent);
  font-size: 12px;
}
@media (max-width: 720px) {
  .linked-item { grid-template-columns: 1fr; }
  .linked-grid,
  .result-grid { grid-template-columns: 1fr; }
}
</style>
