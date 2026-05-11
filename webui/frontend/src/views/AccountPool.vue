<template>
  <div class="pool-page">
    <header class="pool-header">
      <div>
        <p class="eyebrow">Account Planning Pool</p>
        <h1>账号规划池</h1>
      </div>
      <div class="header-actions">
        <button class="btn ghost" type="button" @click="router.push('/run')">运行页</button>
        <button class="btn" type="button" :disabled="loading" @click="refresh">刷新</button>
      </div>
    </header>

    <div class="pool-layout">
      <aside class="side-tabs">
        <button :class="{ active: activeTab === 'accounts' }" type="button" @click="activeTab = 'accounts'">
          <span>账号管理</span>
          <small>池子流转</small>
        </button>
        <button :class="{ active: activeTab === 'checks' }" type="button" @click="activeTab = 'checks'">
          <span>状态检测</span>
          <small>Plus / Free / RT</small>
        </button>
      </aside>
      <main class="pool-main">
    <section class="stats-grid">
      <button
        v-for="status in statuses"
        :key="status.key"
        :class="['stat-card', { active: filter.status === status.key }]"
        type="button"
        @click="setStatus(status.key)"
      >
        <span>{{ status.label }}</span>
        <strong>{{ counts[status.key] || 0 }}</strong>
      </button>
    </section>

    <section v-if="activeTab === 'accounts'" class="toolbar">
      <div class="tabs">
        <button :class="{ active: filter.status === 'all' }" type="button" @click="setStatus('all')">全部</button>
        <button v-for="status in statuses" :key="status.key" :class="{ active: filter.status === status.key }" type="button" @click="setStatus(status.key)">
          {{ status.label }}
        </button>
      </div>
      <div class="rotation-row">
        <label class="switch-line">
          <input v-model="rotation.enabled" type="checkbox" />
          <span>开启任务轮转</span>
        </label>
        <label class="interval-field">
          <span>每</span>
          <input v-model.number="rotation.interval" type="number" min="1" />
          <span>次任务处理待激活池</span>
        </label>
        <button class="btn ghost" type="button" :disabled="savingRotation" @click="saveRotation">保存轮转</button>
      </div>
      <div class="search-row">
        <input v-model="filter.q" placeholder="搜索邮箱 / account_id" @keydown.enter="refresh" />
        <button class="btn ghost" type="button" @click="refresh">搜索</button>
        <button class="btn" type="button" @click="showImport = !showImport">导入邮箱</button>
      </div>
    </section>

    <template v-if="activeTab === 'accounts'">
    <section v-if="showImport" class="import-panel">
      <div class="panel-head">
        <div>
          <strong>导入未激活邮箱</strong>
          <span>支持一行一个邮箱，或 邮箱----密码----client_id----refresh_token</span>
        </div>
        <button class="btn ghost" type="button" :disabled="importing" @click="importEmails">导入到未激活池</button>
      </div>
      <textarea v-model="importText" rows="6" placeholder="name@example.com----password----client_id----mail_refresh_token" />
    </section>

    <section class="bulk-bar">
      <label class="select-all">
        <input type="checkbox" :checked="pageAllSelected" @change="togglePageSelect" />
        <span>本页 {{ selectedPageCount }} / {{ items.length }}</span>
      </label>
      <select v-model="moveTarget">
        <option value="">移动到...</option>
        <option v-for="status in moveStatuses" :key="status.key" :value="status.key">{{ status.label }}</option>
      </select>
      <input v-model="moveReason" placeholder="移动原因" />
      <button class="btn ghost" type="button" :disabled="!selectedIds.length || !moveTarget || moving" @click="moveSelected">批量移动</button>
      <button v-if="filter.status === 'in_progress'" class="btn" type="button" :disabled="!selectedIds.length || retryingAccounts" @click="retrySelected('registration')">重新注册执行</button>
      <button v-if="filter.status === 'registered_pending_plus'" class="btn" type="button" :disabled="!selectedIds.length || retryingAccounts" @click="retrySelected('payment')">重新支付执行</button>
      <button class="btn ghost" type="button" :disabled="claiming" @click="claimPreview">领取未激活邮箱</button>
      <select v-model="downloadTarget">
        <option value="cpa">下载 CPA</option>
        <option value="sub2api">下载 sub2api</option>
      </select>
      <button class="btn ghost" type="button" :disabled="!selectedIds.length || downloading" @click="downloadSelected">批量下载</button>
      <span class="bulk-info">已选 {{ selectedIds.length }} 个</span>
    </section>

    <section class="upload-panel">
      <div class="upload-targets">
        <label><input v-model="uploadTargets" type="checkbox" value="cpa" /> CPA</label>
        <label><input v-model="uploadTargets" type="checkbox" value="sub2api" /> sub2api</label>
        <label><input v-model="uploadTargets" type="checkbox" value="server" /> 服务器</label>
        <span class="muted">队列上传，每组 10 个</span>
      </div>
      <input v-model="serverImport.url" placeholder="服务器导入 URL" />
      <input v-model="serverImport.token" type="password" placeholder="服务器凭证" autocomplete="off" />
      <button class="btn" type="button" :disabled="!selectedIds.length || !uploadTargets.length || uploading" @click="uploadSelected">
        队列上传
      </button>
    </section>

    <section v-if="uploadResult" class="result-panel">
      <div v-for="(target, name) in uploadResult.targets" :key="String(name)">
        <strong>{{ uploadTargetLabel(String(name)) }}</strong>
        <span>total={{ target.summary.total || 0 }} ok={{ target.summary.ok || 0 }} fail={{ target.summary.fail || 0 }}</span>
      </div>
    </section>

    <section class="danger-panel">
      <div class="delete-targets">
        <strong>按池子删除</strong>
        <label v-for="status in deletableStatuses" :key="status.key">
          <input v-model="deleteStatuses" type="checkbox" :value="status.key" />
          {{ status.label }} ({{ counts[status.key] || 0 }})
        </label>
      </div>
      <button class="btn danger" type="button" :disabled="!deleteStatuses.length || deletingPools" @click="deleteSelectedPools">
        删除所选池子账号
      </button>
      <span class="muted">删除后会同步清理该池子的流转记录</span>
    </section>

    <section class="table-shell">
      <table>
        <thead>
          <tr>
            <th class="check-col"></th>
            <th>邮箱</th>
            <th>池子</th>
            <th>Plan</th>
            <th>RT</th>
            <th>账号标识</th>
            <th>任务</th>
            <th>最近状态</th>
            <th>更新</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="item.id" @click="openDetail(item.id)">
            <td class="check-col" @click.stop>
              <input type="checkbox" :checked="selected.has(item.id)" @change="toggleSelect(item.id)" />
            </td>
            <td>
              <div class="strong">{{ item.primary_email || item.email }}</div>
              <div class="muted">{{ item.email_source || 'manual' }} · #{{ item.id }}</div>
            </td>
            <td><span :class="['pill', statusClass(item.pool_status)]">{{ item.status_label }}</span></td>
            <td>{{ item.plan_type || '-' }}</td>
            <td>
              <span :class="['pill', item.has_refresh_token ? 'ok' : 'muted-pill']">
                {{ item.has_refresh_token ? '有 RT' : '无 RT' }}
              </span>
            </td>
            <td>
              <div>{{ item.account_id || item.team_account_id || '-' }}</div>
              <div class="muted">{{ item.payment_session_id || item.device_id || '' }}</div>
            </td>
            <td>
              <div>{{ item.round_id || '-' }}</div>
              <div class="muted">{{ item.task_id || '' }}</div>
            </td>
            <td>
              <div>{{ item.last_stage || '-' }}</div>
              <div class="error-text">{{ item.last_error || '' }}</div>
            </td>
            <td>{{ formatTime(item.updated_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="!items.length" class="empty-state">
        {{ loading ? '正在加载...' : '这个池子暂时没有账号' }}
      </div>
    </section>

    <footer class="pager">
      <button class="btn ghost" type="button" :disabled="filter.offset <= 0" @click="prevPage">上一页</button>
      <span>{{ pageStart }} - {{ pageEnd }} / {{ total }}</span>
      <button class="btn ghost" type="button" :disabled="filter.offset + filter.limit >= total" @click="nextPage">下一页</button>
    </footer>

    </template>

    <section v-else class="check-panel">
      <div class="check-head">
        <div>
          <strong>账号状态检测</strong>
          <span>使用 RT 刷新官方 OAuth token，解析 plan/account_id，并检测 Codex usage。</span>
        </div>
        <div class="check-actions">
          <select v-model="checkStatusFilter" @change="refreshCheckCandidates">
            <option value="plus_with_rt">已激活池</option>
            <option value="plus_missing_rt">待检测池</option>
            <option value="registered_pending_plus">待激活池</option>
            <option value="all">全部</option>
          </select>
          <input v-model="checkQuery" placeholder="搜索邮箱 / account_id" @keydown.enter="refreshCheckCandidates" />
          <button class="btn ghost" type="button" :disabled="checkingLoading" @click="refreshCheckCandidates">刷新</button>
          <button class="btn" type="button" :disabled="!checkSelectedIds.length || checkingAccounts" @click="checkSelectedAccounts">检测选中</button>
          <button v-if="checkStatusFilter === 'registered_pending_plus'" class="btn" type="button" :disabled="!checkSelectedIds.length || retryingAccounts" @click="retrySelected('payment', true)">重新支付执行</button>
          <button v-if="checkStatusFilter === 'all'" class="btn ghost" type="button" :disabled="!checkSelectedIds.length || retryingAccounts" @click="retrySelected('payment', true)">重跑待激活</button>
        </div>
      </div>
      <div class="check-summary">
        <span>候选 {{ checkCandidates.length }}</span>
        <span>已选 {{ checkSelectedIds.length }}</span>
        <span v-if="checkResult">OK {{ checkResult.summary?.ok || 0 }} / FAIL {{ checkResult.summary?.fail || 0 }} / UNKNOWN {{ checkResult.summary?.unknown || 0 }}</span>
      </div>
      <div class="check-list">
        <label v-for="item in checkCandidates" :key="item.id" class="check-row">
          <input type="checkbox" :checked="checkSelected.has(item.id)" @change="toggleCheckSelect(item.id)" />
          <span class="check-email">{{ item.primary_email || item.email }}</span>
          <span :class="['pill', statusClass(item.pool_status)]">{{ item.status_label }}</span>
          <span :class="['pill', item.has_refresh_token ? 'ok' : 'muted-pill']">{{ item.has_refresh_token ? '有 RT' : '无 RT' }}</span>
          <span class="muted">{{ item.plan_type || '-' }} {{ item.account_id || item.team_account_id || '' }}</span>
        </label>
        <div v-if="!checkCandidates.length" class="empty-state">
          {{ checkingLoading ? '正在加载...' : '暂无可检测账号' }}
        </div>
      </div>
      <div v-if="checkResult" class="check-results">
        <div v-for="result in checkResult.results || []" :key="result.id" class="check-result-row">
          <strong>{{ result.email || result.id }}</strong>
          <span :class="['pill', checkResultClass(result.status)]">{{ checkResultLabel(result.status) }}</span>
          <span>{{ result.plan || '-' }}</span>
          <span>{{ result.account_id || '' }}</span>
          <small>{{ result.message }}</small>
        </div>
      </div>
    </section>
      </main>
    </div>

    <aside v-if="detail" class="detail-drawer">
      <div class="drawer-card">
        <header>
          <div>
            <p class="eyebrow">Account Detail</p>
            <h2>{{ detail.primary_email }}</h2>
          </div>
          <button class="icon-btn" type="button" title="关闭" @click="detail = null">×</button>
        </header>
        <div class="detail-grid">
          <div><span>池子</span><strong>{{ detail.status_label }}</strong></div>
          <div><span>Plan</span><strong>{{ detail.plan_type || '-' }}</strong></div>
          <div><span>RT</span><strong>{{ detail.has_refresh_token ? '已获取' : '缺失' }}</strong></div>
          <div><span>尝试次数</span><strong>{{ detail.attempt_count || 0 }}</strong></div>
        </div>
        <div class="detail-section">
          <h3>完整字段</h3>
          <dl>
            <template v-for="field in detailFields" :key="field">
              <dt>{{ field }}</dt>
              <dd>{{ detail[field] || '-' }}</dd>
            </template>
          </dl>
        </div>
        <div class="detail-section">
          <h3>流转记录</h3>
          <div v-for="event in detail.events || []" :key="event.id" class="event-row">
            <strong>{{ statusLabel(event.from_status) || '新建' }} → {{ statusLabel(event.to_status) }}</strong>
            <span>{{ stageLabel(event.stage) }} · {{ reasonLabel(event.reason) }}</span>
            <small>{{ formatTime(event.ts) }}</small>
          </div>
        </div>
      </div>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useMessage } from "naive-ui";
import { useRouter } from "vue-router";
import { api } from "../api/client";

interface PoolStatus {
  key: string;
  label: string;
}

interface PoolItem {
  id: number;
  email: string;
  primary_email: string;
  status_label: string;
  pool_status: string;
  email_source: string;
  plan_type: string;
  has_refresh_token: boolean;
  account_id: string;
  team_account_id: string;
  payment_session_id: string;
  device_id: string;
  round_id: string;
  task_id: string;
  last_stage: string;
  last_error: string;
  updated_at: number;
  [key: string]: any;
}

const router = useRouter();
const message = useMessage();

const loading = ref(false);
const importing = ref(false);
const moving = ref(false);
const claiming = ref(false);
const savingRotation = ref(false);
const downloading = ref(false);
const uploading = ref(false);
const deletingPools = ref(false);
const checkingLoading = ref(false);
const checkingAccounts = ref(false);
const retryingAccounts = ref(false);
const showImport = ref(false);
const activeTab = ref<"accounts" | "checks">("accounts");
const importText = ref("");
const items = ref<PoolItem[]>([]);
const checkCandidates = ref<PoolItem[]>([]);
const checkSelected = ref<Set<number>>(new Set());
const checkResult = ref<any | null>(null);
const checkStatusFilter = ref("plus_with_rt");
const checkQuery = ref("");
const total = ref(0);
const counts = ref<Record<string, number>>({});
const statuses = ref<PoolStatus[]>([
  { key: "email_unused", label: "未激活池" },
  { key: "in_progress", label: "任务中" },
  { key: "plus_with_rt", label: "已激活池" },
  { key: "plus_missing_rt", label: "待检测池" },
  { key: "registered_pending_plus", label: "待激活池" },
  { key: "registration_failed", label: "失败池" },
]);
const selected = ref<Set<number>>(new Set());
const moveTarget = ref("");
const moveReason = ref("人工调整");
const downloadTarget = ref("cpa");
const uploadTargets = ref<string[]>([]);
const deleteStatuses = ref<string[]>([]);
const serverImport = ref({
  url: "https://mail.shfjkqhk.site/api/email-data",
  token: "sakuya1.2.3.",
});
const uploadResult = ref<any | null>(null);
const detail = ref<PoolItem | null>(null);
const filter = ref({
  status: "all",
  q: "",
  limit: 50,
  offset: 0,
});
const rotation = ref({
  enabled: false,
  interval: 100,
});

const moveStatuses = computed(() => [
  ...statuses.value,
  { key: "quarantined", label: "已隔离" },
]);
const deletableStatuses = computed(() =>
  [
    ...statuses.value,
    { key: "quarantined", label: "已隔离" },
  ]
);
const selectedIds = computed(() => Array.from(selected.value));
const checkSelectedIds = computed(() => Array.from(checkSelected.value));
const pageAllSelected = computed(() => items.value.length > 0 && items.value.every(item => selected.value.has(item.id)));
const selectedPageCount = computed(() => items.value.filter(item => selected.value.has(item.id)).length);
const pageStart = computed(() => total.value ? filter.value.offset + 1 : 0);
const pageEnd = computed(() => Math.min(filter.value.offset + items.value.length, total.value));
const detailFields = [
  "email",
  "chatgpt_email",
  "account_id",
  "team_account_id",
  "team_gpt_account_pk",
  "invite_permission",
  "payment_status",
  "payment_channel",
  "payment_session_id",
  "email_domain",
  "task_id",
  "round_id",
  "last_stage",
  "last_error",
  "source_registered_account_id",
  "source_card_result_id",
  "source_outlook_mail_id",
];

watch(
  () => [filter.value.status],
  () => {
    filter.value.offset = 0;
    selected.value = new Set();
    refresh();
  }
);

onMounted(async () => {
  await Promise.all([refresh(), loadRotation()]);
});

watch(activeTab, async tab => {
  if (tab === "checks" && !checkCandidates.value.length) {
    await refreshCheckCandidates();
  }
});

function setStatus(status: string) {
  filter.value.status = status;
}

async function refresh() {
  loading.value = true;
  try {
    const r = await api.get("/pool/accounts", {
      params: {
        status: filter.value.status,
        q: filter.value.q,
        limit: filter.value.limit,
        offset: filter.value.offset,
      },
    });
    items.value = r.data?.items || [];
    total.value = Number(r.data?.total || 0);
    counts.value = r.data?.counts || {};
    if (Array.isArray(r.data?.statuses)) statuses.value = r.data.statuses;
  } catch (e: any) {
    message.error(`加载规划池失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    loading.value = false;
  }
}

async function loadRotation() {
  try {
    const r = await api.get("/pool/rotation");
    rotation.value.enabled = Boolean(r.data?.enabled);
    rotation.value.interval = Number(r.data?.interval || 100);
  } catch {}
}

async function saveRotation() {
  savingRotation.value = true;
  try {
    const interval = Math.max(1, Number(rotation.value.interval || 100));
    const r = await api.post("/pool/rotation", {
      enabled: rotation.value.enabled,
      interval,
    });
    rotation.value.enabled = Boolean(r.data?.enabled);
    rotation.value.interval = Number(r.data?.interval || interval);
    message.success(rotation.value.enabled ? `已开启轮转：每 ${rotation.value.interval} 次任务处理待激活池` : "已关闭任务轮转");
  } catch (e: any) {
    message.error(`保存轮转失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    savingRotation.value = false;
  }
}

async function importEmails() {
  if (!importText.value.trim()) {
    message.warning("请先粘贴邮箱数据");
    return;
  }
  importing.value = true;
  try {
    const r = await api.post("/pool/emails/import", {
      text: importText.value,
      source: "manual_pool_import",
    });
    const d = r.data || {};
    message.success(`导入完成：新增 ${d.created || 0}，更新 ${d.updated || 0}`);
    importText.value = "";
    showImport.value = false;
    await refresh();
  } catch (e: any) {
    message.error(`导入失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    importing.value = false;
  }
}

function toggleSelect(id: number) {
  const next = new Set(selected.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selected.value = next;
}

function togglePageSelect() {
  const next = new Set(selected.value);
  if (pageAllSelected.value) items.value.forEach(item => next.delete(item.id));
  else items.value.forEach(item => next.add(item.id));
  selected.value = next;
}

async function moveSelected() {
  if (!selectedIds.value.length || !moveTarget.value) return;
  moving.value = true;
  try {
    const r = await api.post("/pool/accounts/move", {
      ids: selectedIds.value,
      to_status: moveTarget.value,
      reason: moveReason.value || "人工调整",
    });
    message.success(`已移动 ${r.data?.moved || 0} 个账号`);
    selected.value = new Set();
    moveTarget.value = "";
    await refresh();
  } catch (e: any) {
    message.error(`移动失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    moving.value = false;
  }
}

async function claimPreview() {
  claiming.value = true;
  try {
    const r = await api.post("/pool/accounts/claim", {
      limit: 5,
      task_id: `manual-${Date.now()}`,
      round_id: "manual-preview",
    });
    message.success(`已领取 ${r.data?.items?.length || 0} 个未激活邮箱`);
    await refresh();
  } catch (e: any) {
    message.error(`领取失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    claiming.value = false;
  }
}

async function downloadSelected() {
  if (!selectedIds.value.length) {
    message.warning("请先选择账号");
    return;
  }
  downloading.value = true;
  try {
    const r = await api.post(
      "/pool/accounts/export",
      { ids: selectedIds.value, target: downloadTarget.value },
      { responseType: "blob" }
    );
    const cd = String(r.headers?.["content-disposition"] || "");
    const m = /filename="?([^"]+)"?/i.exec(cd);
    const fallback = downloadTarget.value === "cpa" ? `pool-cpa-${Date.now()}.zip` : `pool-sub2api-${Date.now()}.json`;
    const name = m?.[1] || fallback;
    const url = URL.createObjectURL(r.data);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    message.success(`已下载 ${selectedIds.value.length} 个账号`);
  } catch (e: any) {
    message.error(`下载失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    downloading.value = false;
  }
}

async function uploadSelected() {
  if (!selectedIds.value.length) {
    message.warning("请先选择账号");
    return;
  }
  if (!uploadTargets.value.length) {
    message.warning("请选择上传目标");
    return;
  }
  if (uploadTargets.value.includes("server") && (!serverImport.value.url.trim() || !serverImport.value.token.trim())) {
    message.warning("请填写服务器 URL 和凭证");
    return;
  }
  uploading.value = true;
  uploadResult.value = null;
  try {
    const r = await api.post("/pool/accounts/upload", {
      ids: selectedIds.value,
      targets: uploadTargets.value,
      import_url: serverImport.value.url.trim(),
      import_token: serverImport.value.token.trim(),
      group_size: 10,
    });
    uploadResult.value = r.data || null;
    const targets = Object.entries(r.data?.targets || {})
      .map(([name, data]: any) => `${uploadTargetLabel(name)} ok=${data.summary?.ok || 0} fail=${data.summary?.fail || 0}`)
      .join("；");
    message.success(`队列上传完成：${targets || "无结果"}`);
  } catch (e: any) {
    message.error(`上传失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    uploading.value = false;
  }
}

async function deleteSelectedPools() {
  if (!deleteStatuses.value.length) return;
  const labels = deleteStatuses.value.map(statusLabel).join("、");
  const total = deleteStatuses.value.reduce((sum, key) => sum + Number(counts.value[key] || 0), 0);
  const ok = window.confirm(`确认删除 ${labels} 中的账号？预计 ${total} 个。此操作会同步删除流转记录。`);
  if (!ok) return;
  deletingPools.value = true;
  try {
    const r = await api.post("/pool/accounts/delete-by-status", {
      statuses: deleteStatuses.value,
    });
    message.success(`已删除 ${r.data?.deleted || 0} 个账号`);
    deleteStatuses.value = [];
    selected.value = new Set();
    await refresh();
  } catch (e: any) {
    message.error(`删除失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    deletingPools.value = false;
  }
}

async function openDetail(id: number) {
  try {
    const r = await api.get(`/pool/accounts/${id}`);
    detail.value = r.data || null;
  } catch (e: any) {
    message.error(`读取详情失败：${e?.response?.data?.detail || e?.message || e}`);
  }
}

async function refreshCheckCandidates() {
  checkingLoading.value = true;
  try {
    const r = await api.get("/pool/accounts", {
      params: {
        status: checkStatusFilter.value,
        q: checkQuery.value,
        limit: 200,
        offset: 0,
      },
    });
    checkCandidates.value = r.data?.items || [];
    checkSelected.value = new Set();
  } catch (e: any) {
    message.error(`加载检测账号失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    checkingLoading.value = false;
  }
}

function toggleCheckSelect(id: number) {
  const next = new Set(checkSelected.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  checkSelected.value = next;
}

async function checkSelectedAccounts() {
  if (!checkSelectedIds.value.length) {
    message.warning("请先选择要检测的账号");
    return;
  }
  checkingAccounts.value = true;
  checkResult.value = null;
  try {
    const r = await api.post("/pool/accounts/check", {
      ids: checkSelectedIds.value,
      timeout_s: 12,
      max_workers: 3,
    });
    checkResult.value = r.data || null;
    const s = r.data?.summary || {};
    message.success(`检测完成：OK ${s.ok || 0} / FAIL ${s.fail || 0} / UNKNOWN ${s.unknown || 0}`);
    await Promise.all([refresh(), refreshCheckCandidates()]);
  } catch (e: any) {
    message.error(`状态检测失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    checkingAccounts.value = false;
  }
}

async function retrySelected(type: "registration" | "payment", fromCheck = false) {
  const ids = fromCheck ? checkSelectedIds.value : selectedIds.value;
  if (!ids.length) {
    message.warning("请先选择要重新执行的账号");
    return;
  }
  const label = type === "registration" ? "重新注册" : "重新支付";
  const ok = window.confirm(`确认对 ${ids.length} 个账号执行${label}任务？当前只能同时运行一个任务。`);
  if (!ok) return;
  retryingAccounts.value = true;
  try {
    const r = await api.post("/pool/accounts/retry", {
      ids,
      retry_type: type,
      paypal: true,
      gopay: false,
      push_server: false,
    });
    const prepared = r.data?.prepared?.prepared || 0;
    message.success(`${label}任务已启动，已加入 ${prepared} 个账号`);
    selected.value = new Set();
    checkSelected.value = new Set();
    await Promise.all([refresh(), refreshCheckCandidates()]);
  } catch (e: any) {
    message.error(`${label}启动失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    retryingAccounts.value = false;
  }
}

function prevPage() {
  filter.value.offset = Math.max(0, filter.value.offset - filter.value.limit);
  refresh();
}

function nextPage() {
  filter.value.offset += filter.value.limit;
  refresh();
}

function statusClass(status: string) {
  if (status === "plus_with_rt") return "ok";
  if (status === "plus_missing_rt") return "warn";
  if (status === "registered_pending_plus") return "info";
  if (status === "registration_failed") return "danger";
  return "muted-pill";
}

function checkResultClass(status: string) {
  if (["ok", "quota_limited", "refreshed"].includes(status)) return "ok";
  if (["invalid", "no_refresh_token"].includes(status)) return "danger";
  return "warn";
}

function checkResultLabel(status: string) {
  const map: Record<string, string> = {
    ok: "可用",
    refreshed: "已刷新",
    quota_limited: "额度限制",
    invalid: "失效",
    no_refresh_token: "无 RT",
    unknown: "未知",
  };
  return map[status] || status || "-";
}

function statusLabel(status: string) {
  const found = [...statuses.value, { key: "quarantined", label: "已隔离" }]
    .find(item => item.key === status);
  return found?.label || status || "";
}

function stageLabel(stage: string) {
  const map: Record<string, string> = {
    upsert: "写入/更新",
    email_import: "导入邮箱",
    legacy_migration: "旧数据迁移",
    manual_move: "手动移动",
    claimed: "任务领取",
    mail_reserved: "邮箱被领取",
    registration_success: "注册成功",
    registration_failed: "注册失败",
    payment_succeeded_with_rt: "支付成功并获取 RT",
    payment_succeeded_missing_rt: "支付成功但缺少 RT",
    payment_not_succeeded: "支付未成功",
    auto_server_push_rt: "自动推送前获取 RT",
    free_backfill_rt: "补 RT 成功",
    free_register_rt: "注册后获取 RT",
    self_dealer_member: "成员账号获取 RT",
    team_probe: "团队权限检测",
    already_paid_rt_ok: "已付费账号 RT 检测成功",
    already_paid_rt_failed: "已付费账号 RT 检测失败",
    rotation_claimed: "轮转领取",
    rotation_skip_no_auth: "轮转跳过无凭证",
  };
  return map[stage] || stage || "-";
}

function reasonLabel(reason: string) {
  if (!reason) return "-";
  const map: Record<string, string> = {
    "manual email import": "手动导入邮箱",
    "manual legacy migration": "手动迁移旧账号",
    "manual outlook migration": "手动迁移 Outlook 邮箱",
    "manual move": "手动调整",
    "task claim": "任务领取",
    "Outlook mailbox reserved by registration task": "注册任务领取 Outlook 邮箱",
    "registered account waiting for Plus activation": "已注册，等待激活 Plus",
    "Plus activated and RT obtained": "Plus 已激活并已获取 RT",
    "Plus activated but RT missing": "Plus 已激活但缺少 RT",
    "RT obtained": "已获取 RT",
    "RT missing": "缺少 RT",
    "already paid and RT detected": "账号已付费并检测到 RT",
    "rotation picked pending activation account": "轮转领取待激活账号",
  };
  if (reason.startsWith("failed_plus:")) return `Plus 账号 RT 检测失败：${reason.slice("failed_plus:".length)}`;
  if (reason.startsWith("payment_status=")) return `支付状态：${reason.slice("payment_status=".length)}`;
  if (reason.startsWith("invite=")) return `邀请权限：${reason.slice("invite=".length)}`;
  return map[reason] || reason;
}

function uploadTargetLabel(target: string) {
  if (target === "cpa") return "CPA";
  if (target === "sub2api") return "sub2api";
  if (target === "server") return "服务器";
  return target;
}

function formatTime(value: number | string) {
  const n = Number(value || 0);
  if (!n) return "-";
  return new Date(n * 1000).toLocaleString();
}
</script>

<style scoped>
.pool-page {
  min-height: 100vh;
  padding: 24px;
  background: #f7faf9;
  color: #17211d;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.pool-header,
.toolbar,
.import-panel,
.bulk-bar,
.upload-panel,
.result-panel,
.table-shell,
.pager {
  margin: 0 auto 14px;
}

.pool-layout {
  max-width: 1480px;
  margin: 18px auto 0;
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  gap: 16px;
}

.pool-main {
  min-width: 0;
}

.side-tabs {
  position: sticky;
  top: 18px;
  align-self: start;
  display: grid;
  gap: 8px;
}

.side-tabs button {
  border: 1px solid #d4e3de;
  background: #fff;
  color: #25423a;
  border-radius: 8px;
  padding: 12px;
  text-align: left;
  cursor: pointer;
}

.side-tabs button.active {
  border-color: #0f6b57;
  background: #e7f5f0;
  box-shadow: 0 0 0 3px rgba(15, 107, 87, .1);
}

.side-tabs span,
.side-tabs small {
  display: block;
}

.side-tabs span {
  font-weight: 800;
}

.side-tabs small {
  margin-top: 4px;
  color: #668078;
}

.pool-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.eyebrow {
  margin: 0 0 4px;
  color: #5e756c;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
}

h1,
h2,
h3 {
  margin: 0;
  letter-spacing: 0;
}

h1 {
  font-size: 28px;
}

.header-actions,
.search-row,
.rotation-row,
.bulk-bar,
.tabs,
.panel-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.btn,
.tabs button,
.icon-btn {
  border: 1px solid #bfd4cb;
  background: #0f6b57;
  color: #fff;
  min-height: 36px;
  padding: 0 14px;
  border-radius: 6px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}

.btn.ghost,
.tabs button,
.icon-btn {
  background: #fff;
  color: #1f3a33;
}

.btn:disabled {
  opacity: .45;
  cursor: not-allowed;
}

.tabs button.active {
  background: #dff1eb;
  border-color: #71a696;
}

.stats-grid {
  margin: 0 0 14px;
  display: grid;
  grid-template-columns: repeat(6, minmax(140px, 1fr));
  gap: 12px;
}

.stat-card {
  text-align: left;
  border: 1px solid #d4e3de;
  background: #fff;
  padding: 16px;
  border-radius: 8px;
  cursor: pointer;
}

.stat-card.active {
  border-color: #0f6b57;
  box-shadow: 0 0 0 3px rgba(15, 107, 87, .12);
}

.stat-card span {
  display: block;
  color: #61776f;
  font-weight: 700;
  font-size: 13px;
}

.stat-card strong {
  display: block;
  margin-top: 8px;
  font-size: 30px;
}

.toolbar,
.import-panel,
.bulk-bar,
.upload-panel,
.result-panel,
.table-shell,
.pager {
  border: 1px solid #d4e3de;
  background: #fff;
  border-radius: 8px;
}

.toolbar {
  padding: 14px;
  display: grid;
  gap: 12px;
}

.search-row input,
.rotation-row input[type="number"],
.upload-panel input,
.bulk-bar input,
.bulk-bar select,
.import-panel textarea {
  border: 1px solid #cbded7;
  background: #fff;
  border-radius: 6px;
  min-height: 36px;
  padding: 0 12px;
  font: inherit;
}

.search-row input {
  min-width: 260px;
}

.rotation-row {
  padding: 10px 12px;
  background: #f8fbfa;
  border: 1px solid #dce9e5;
  border-radius: 8px;
}

.switch-line,
.interval-field {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
}

.interval-field input {
  width: 88px;
}

.import-panel {
  padding: 14px;
}

.panel-head {
  justify-content: space-between;
  margin-bottom: 12px;
}

.panel-head span,
.muted,
.bulk-info {
  color: #668078;
  font-size: 12px;
}

.import-panel textarea {
  width: 100%;
  min-height: 130px;
  padding: 12px;
  resize: vertical;
}

.bulk-bar {
  padding: 12px 14px;
}

.upload-panel,
.result-panel {
  padding: 12px 14px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.danger-panel {
  max-width: 1400px;
  margin: 0 auto 14px;
  padding: 12px 14px;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  border: 1px solid #f0c4c0;
  background: #fffafa;
  border-radius: 8px;
}

.delete-targets {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.delete-targets label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.btn.danger {
  background: #b42318;
  border-color: #b42318;
  color: #fff;
}

.upload-targets {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  font-weight: 700;
}

.upload-targets label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.upload-panel input {
  min-width: 220px;
}

.result-panel {
  background: #f8fbfa;
}

.check-panel {
  border: 1px solid #d4e3de;
  background: #fff;
  border-radius: 8px;
  padding: 14px;
}

.check-head,
.check-actions,
.check-summary,
.check-row,
.check-result-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.check-head {
  justify-content: space-between;
  margin-bottom: 12px;
}

.check-head span,
.check-summary {
  color: #668078;
  font-size: 12px;
}

.check-actions input,
.check-actions select {
  border: 1px solid #cbded7;
  background: #fff;
  border-radius: 6px;
  min-height: 36px;
  padding: 0 12px;
  font: inherit;
}

.check-actions input {
  min-width: 240px;
}

.check-summary {
  margin-bottom: 10px;
  font-weight: 700;
}

.check-list,
.check-results {
  display: grid;
  gap: 8px;
}

.check-row,
.check-result-row {
  border: 1px solid #e0ece8;
  background: #f9fcfb;
  border-radius: 8px;
  padding: 10px 12px;
}

.check-email {
  min-width: 220px;
  font-weight: 800;
  word-break: break-all;
}

.check-result-row small {
  color: #668078;
  flex-basis: 100%;
}

.result-panel div {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border: 1px solid #d6e6e0;
  border-radius: 6px;
}

.select-all {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
}

.table-shell {
  overflow: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th,
td {
  padding: 12px;
  border-bottom: 1px solid #e3eeea;
  text-align: left;
  vertical-align: top;
}

th {
  color: #52675f;
  background: #f1f7f5;
  font-size: 12px;
  text-transform: uppercase;
}

tbody tr {
  cursor: pointer;
}

tbody tr:hover {
  background: #f8fbfa;
}

.check-col {
  width: 36px;
}

.strong {
  font-weight: 800;
}

.pill {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 0 8px;
  border-radius: 999px;
  background: #edf4f1;
  color: #49645b;
  font-weight: 800;
  font-size: 12px;
}

.pill.ok {
  background: #dbf3e7;
  color: #11633e;
}

.pill.warn {
  background: #fff1cc;
  color: #855a00;
}

.pill.info {
  background: #e3efff;
  color: #1f5f9d;
}

.pill.danger {
  background: #ffe1df;
  color: #a82820;
}

.muted-pill {
  background: #edf2f0;
  color: #62776f;
}

.error-text {
  max-width: 280px;
  color: #9a332b;
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.empty-state {
  padding: 48px;
  text-align: center;
  color: #6c8179;
}

.pager {
  padding: 12px 14px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
}

.detail-drawer {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  justify-content: flex-end;
  background: rgba(10, 24, 19, .26);
}

.drawer-card {
  width: min(620px, 100vw);
  height: 100%;
  overflow: auto;
  background: #fff;
  box-shadow: -12px 0 32px rgba(7, 30, 23, .18);
  padding: 22px;
}

.drawer-card header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.icon-btn {
  width: 36px;
  padding: 0;
  font-size: 22px;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 18px;
}

.detail-grid div {
  border: 1px solid #dbe8e4;
  border-radius: 8px;
  padding: 12px;
  background: #f9fcfb;
}

.detail-grid span {
  display: block;
  color: #687f77;
  font-size: 12px;
}

.detail-grid strong {
  display: block;
  margin-top: 6px;
}

.detail-section {
  margin-top: 20px;
}

.detail-section h3 {
  margin-bottom: 10px;
  font-size: 15px;
}

dl {
  display: grid;
  grid-template-columns: 170px 1fr;
  gap: 8px 12px;
  margin: 0;
  font-size: 13px;
}

dt {
  color: #63776f;
  font-weight: 700;
}

dd {
  margin: 0;
  word-break: break-all;
}

.event-row {
  border-left: 3px solid #9ec7ba;
  padding: 8px 0 8px 12px;
  margin-bottom: 8px;
  background: #f8fbfa;
}

.event-row strong,
.event-row span,
.event-row small {
  display: block;
}

.event-row span,
.event-row small {
  color: #667b73;
  font-size: 12px;
}

@media (max-width: 900px) {
  .pool-page {
    padding: 14px;
  }
  .pool-header {
    align-items: flex-start;
    flex-direction: column;
  }
  .pool-layout {
    grid-template-columns: 1fr;
  }
  .side-tabs {
    position: static;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .stats-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .detail-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
