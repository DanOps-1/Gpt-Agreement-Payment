<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 04: 注册邮件</div>
    <h2 class="step-h">$&nbsp;OTP 接收<span class="term-cursor"></span></h2>
    <p class="step-sub">
      手动选择注册验证码来源。CF KV 走 catch-all Worker；Outlook 走数据库邮箱池，不会因为导入文件自动启用。
    </p>

    <div class="source-tabs">
      <button :class="{ active: mailSource.provider === 'cf' }" @click="setProvider('cf')">CF KV</button>
      <button :class="{ active: mailSource.provider === 'outlook' }" @click="setProvider('outlook')">Outlook 池</button>
    </div>

    <div class="form-stack">
      <template v-if="mailSource.provider === 'cf'">
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
      </template>

      <template v-else>
        <div class="outlook-card">
          <div class="pool-head">
            <div>
              <strong>Outlook 邮箱池</strong>
              <span>
                total={{ outlookPool.total || 0 }}
                available={{ outlookPool.available || 0 }}
                reserved={{ outlookPool.reserved || 0 }}
                used={{ outlookPool.used || 0 }}
                failed={{ outlookPool.failed || 0 }}
              </span>
            </div>
            <TermBtn variant="ghost" :loading="outlookPool.loading" @click="loadOutlookPoolStats">刷新</TermBtn>
          </div>
          <textarea
            v-model="outlookPool.text"
            class="tf-textarea outlook-textarea"
            rows="5"
            placeholder="每行一个：邮箱----密码----client_id----refresh_token"
          />
          <div class="pool-actions">
            <label class="file-btn">
              读取文件
              <input type="file" accept=".txt,.csv,text/plain" @change="loadOutlookPoolFile" />
            </label>
            <TermBtn :loading="outlookPool.importing" :disabled="!outlookPool.text.trim()" @click="importOutlookPool">
              导入到数据库
            </TermBtn>
          </div>
        </div>
      </template>
    </div>

    <div v-if="mailSource.provider === 'cf'" class="step-actions">
      <TermBtn :loading="deploying" @click="deploy">一键部署 + 测试</TermBtn>
    </div>

    <div v-else class="step-actions">
      <TermBtn :loading="outlookPool.loading" @click="confirmOutlookSource">启用 Outlook 邮箱池</TermBtn>
    </div>

    <div v-if="mailSource.provider === 'cf' && deployResult" class="result-block result--ok" style="margin-top:14px">
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
const sourceInit = (store.answers.mail_source ?? {}) as any;

const mailSource = ref({
  provider: sourceInit.provider === "outlook" ? "outlook" : "cf",
  outlook_poll_interval_s: sourceInit.outlook_poll_interval_s ?? 3,
  outlook_folder: sourceInit.outlook_folder ?? "Inbox",
});

const form = ref({
  api_token: init.api_token ?? "",
  fallback_to: init.fallback_to ?? "",
});
const outlookPool = ref({
  text: "",
  loading: false,
  importing: false,
  total: 0,
  available: 0,
  reserved: 0,
  used: 0,
  failed: 0,
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

if (mailSource.value.provider === "outlook") {
  loadOutlookPoolStats();
}

function setProvider(provider: "cf" | "outlook") {
  mailSource.value.provider = provider;
  store.setAnswer("mail_source", { ...mailSource.value });
  if (provider === "outlook") {
    loadOutlookPoolStats();
  }
}

async function loadOutlookPoolStats() {
  outlookPool.value.loading = true;
  try {
    const r = await api.get("/config/outlook-mail-pool");
    Object.assign(outlookPool.value, {
      total: r.data?.total || 0,
      available: r.data?.available || 0,
      reserved: r.data?.reserved || 0,
      used: r.data?.used || 0,
      failed: r.data?.failed || 0,
    });
  } catch (e: any) {
    error.value = `加载 Outlook 邮箱池失败：${e?.response?.data?.detail || e?.message || e}`;
  } finally {
    outlookPool.value.loading = false;
  }
}

async function loadOutlookPoolFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  try {
    outlookPool.value.text = await file.text();
  } catch (e: any) {
    error.value = `读取 Outlook 邮箱文件失败：${e?.message || e}`;
  } finally {
    input.value = "";
  }
}

async function importOutlookPool() {
  if (!outlookPool.value.text.trim()) {
    error.value = "请粘贴或读取 Outlook 账号内容";
    return;
  }
  error.value = "";
  outlookPool.value.importing = true;
  try {
    const r = await api.post("/config/outlook-mail-pool/import", {
      text: outlookPool.value.text,
    });
    const s = r.data?.result || {};
    outlookPool.value.text = "";
    await loadOutlookPoolStats();
    store.setPreflight("cloudflare_kv", {
      status: Number(s.available || outlookPool.value.available || 0) > 0 ? "ok" : "warn",
      message: `Outlook 邮箱池已导入：parsed=${s.parsed || 0} available=${s.available || outlookPool.value.available || 0}`,
      checks: [],
    });
  } catch (e: any) {
    error.value = `导入 Outlook 邮箱池失败：${e?.response?.data?.detail || e?.message || e}`;
  } finally {
    outlookPool.value.importing = false;
  }
}

async function confirmOutlookSource() {
  error.value = "";
  await loadOutlookPoolStats();
  store.setAnswer("mail_source", { ...mailSource.value });
  await store.saveToServer();
  const available = Number(outlookPool.value.available || 0);
  store.setPreflight("cloudflare_kv", {
    status: available > 0 ? "ok" : "fail",
    message: available > 0 ? `已启用 Outlook 邮箱池，available=${available}` : "Outlook 邮箱池没有可用账号",
    checks: [],
  });
}

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

watch(mailSource, async () => {
  store.setAnswer("mail_source", { ...mailSource.value });
  await store.saveToServer();
}, { deep: true });
</script>

<style scoped>
.source-tabs {
  display: inline-flex;
  border: 1px solid var(--border);
  background: var(--bg-base);
  margin-bottom: 14px;
}

.source-tabs button {
  border: 0;
  border-right: 1px solid var(--border);
  background: transparent;
  color: var(--fg-secondary);
  padding: 8px 14px;
  font: inherit;
  cursor: pointer;
}

.source-tabs button:last-child {
  border-right: 0;
}

.source-tabs button.active {
  color: var(--accent);
  background: var(--bg-panel);
}

.outlook-card {
  display: grid;
  gap: 10px;
  border: 1px solid var(--border);
  background: var(--bg-base);
  padding: 12px;
}

.pool-head,
.pool-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.pool-head strong {
  margin-right: 12px;
}

.pool-head span {
  color: var(--fg-tertiary);
  font-size: 12px;
  word-spacing: 8px;
}

.outlook-textarea {
  border: 1px solid var(--border-strong);
  background: var(--bg-panel);
  min-height: 120px;
}

.file-btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  min-height: 34px;
  padding: 0 12px;
  border: 1px solid var(--border-strong);
  background: var(--bg-panel);
  color: var(--fg-secondary);
  font-size: 13px;
  cursor: pointer;
}

.file-btn input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}
</style>
