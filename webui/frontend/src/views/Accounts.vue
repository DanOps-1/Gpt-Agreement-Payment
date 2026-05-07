<template>
  <div class="accounts-page">
    <header class="accounts-topbar">
      <div>
        <p class="eyebrow">Account Inventory</p>
        <h1>账号管理</h1>
      </div>
      <div class="top-actions">
        <TermBtn variant="ghost" @click="router.push('/run')">返回运行页</TermBtn>
        <TermBtn variant="ghost" :loading="loading" @click="refreshInventory">刷新</TermBtn>
      </div>
    </header>

    <section class="summary-band">
      <div class="metric">
        <span>账号总数</span>
        <strong>{{ inventory.accounts.length }}</strong>
      </div>
      <div class="metric">
        <span>有 RT</span>
        <strong>{{ inventory.counts.with_refresh_token || 0 }}</strong>
      </div>
      <div class="metric">
        <span>待补 RT</span>
        <strong>{{ managerBackfillIds.length }}</strong>
      </div>
      <div class="metric">
        <span>已下载</span>
        <strong>{{ downloadedIds.length }}</strong>
      </div>
      <div class="metric">
        <span>当前筛选</span>
        <strong>{{ managerFilteredAccounts.length }}</strong>
      </div>
      <div class="metric">
        <span>已选择</span>
        <strong>{{ managerSelectedFilteredCount }}</strong>
      </div>
    </section>

    <section class="control-band">
      <label class="field field-wide">
        <span>导入接口 URL</span>
        <input v-model="manager.importUrl" placeholder="http://127.0.0.1:8787/api/import" />
      </label>
      <label class="field">
        <span>Bearer Token</span>
        <input v-model="manager.importToken" type="password" autocomplete="off" />
      </label>
      <TermBtn variant="ghost" :loading="manager.savingConfig" @click="saveAccountImportServerConfig()">
        保存推送配置
      </TermBtn>
      <label class="field field-compact">
        <span>RT筛选</span>
        <select v-model="manager.rtFilter">
          <option value="all">全部RT</option>
          <option value="has_rt">有RT</option>
          <option value="no_rt">无RT</option>
        </select>
      </label>
      <label class="field field-compact">
        <span>类型</span>
        <select v-model="manager.planFilter">
          <option value="all">全部</option>
          <option value="plus">Plus</option>
        </select>
      </label>
    </section>

    <section class="action-band">
      <label class="select-page">
        <input type="checkbox" :checked="managerAllSelected" @change="toggleManagerSelectAll" />
        <span>选择本页 ({{ managerSelectedPageCount }} / {{ pagedManagerAccounts.length }})</span>
      </label>
      <TermBtn variant="ghost" :disabled="managerFilteredAccounts.length === 0" @click="selectAllManagerFiltered">
        全部选择 ({{ managerSelectedFilteredCount }} / {{ managerFilteredAccounts.length }})
      </TermBtn>
      <TermBtn :loading="manager.busy" :disabled="managerSelectedFilteredIds.length === 0" @click="detectSelectedRt">
        RT检测 ({{ managerSelectedFilteredIds.length }})
      </TermBtn>
      <TermBtn :loading="manager.busy" :disabled="managerSelectedFilteredIds.length === 0" @click="detectSelectedAccessToken">
        AccessToken检测 ({{ managerSelectedFilteredIds.length }})
      </TermBtn>
      <TermBtn :loading="manager.busy" :disabled="managerBackfillIds.length === 0" @click="backfillManagerSelectedRt">
        一键补RT ({{ managerBackfillIds.length }})
      </TermBtn>
      <TermBtn :loading="manager.busy" :disabled="managerServerPushIds.length === 0" @click="pushManagerSelectedToServer">
        一键推送至导入服务器 ({{ managerServerPushIds.length }})
      </TermBtn>
      <TermBtn :loading="manager.busy" :disabled="managerSelectedFilteredIds.length === 0" @click="downloadManagerSelected">
        下载勾选账号
      </TermBtn>
      <TermBtn variant="danger" :loading="manager.busy" :disabled="downloadedIds.length === 0" @click="deleteDownloadedAccounts">
        删除已下载账号 ({{ downloadedIds.length }})
      </TermBtn>
    </section>

    <section v-if="rtCheck" class="rt-result-band">
      <div>
        <strong>RT检测结果</strong>
        <span>
          total={{ rtCheck.summary.total || 0 }}
          has_rt={{ rtCheck.summary.has_rt || 0 }}
          missing={{ rtCheck.summary.missing || 0 }}
          retryable={{ rtCheck.summary.retryable || 0 }}
          cooldown={{ rtCheck.summary.cooldown || 0 }}
          dead={{ rtCheck.summary.dead || 0 }}
        </span>
      </div>
      <button class="text-button" type="button" @click="rtCheck = null">关闭</button>
    </section>

    <section v-if="accessCheck" class="rt-result-band">
      <div>
        <strong>AccessToken检测</strong>
        <span>
          total={{ accessCheck.summary.total || 0 }}
          refreshed={{ accessCheck.summary.refreshed || 0 }}
          ok={{ accessCheck.summary.ok || 0 }}
          quota={{ accessCheck.summary.quota_limited || 0 }}
          invalid={{ accessCheck.summary.invalid || 0 }}
          no_rt={{ accessCheck.summary.no_refresh_token || 0 }}
        </span>
        <span v-if="planSummary" class="result-extra">plan: {{ planSummary }}</span>
        <div class="result-list-inline">
          <span v-for="item in accessCheck.results.slice(0, 8)" :key="String(item.id)">
            {{ item.email || ("id=" + item.id) }} {{ item.plan || "-" }} {{ item.status }} {{ item.message || "" }}
          </span>
        </div>
      </div>
      <button class="text-button" type="button" @click="accessCheck = null">关闭</button>
    </section>

    <section class="table-wrap">
      <table class="account-table">
        <thead>
          <tr>
            <th class="check-col"></th>
            <th>邮箱</th>
            <th>计划</th>
            <th>RT状态</th>
            <th>支付状态</th>
            <th>账号资产</th>
            <th>下载</th>
            <th>推送</th>
            <th>最近错误</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="acc in pagedManagerAccounts" :key="acc.id">
            <td class="check-col">
              <input
                type="checkbox"
                :checked="managerSelectedIds.has(acc.id)"
                @change="toggleManagerSelect(acc.id)"
              />
            </td>
            <td>
              <div class="email-cell">{{ acc.email }}</div>
              <div class="subtle">#{{ acc.id }} · {{ acc.registered_at || "-" }}</div>
            </td>
            <td><span :class="['badge', planBadgeClass(acc.plan_tag)]">{{ planLabel(acc.plan_tag) }}</span></td>
            <td>
              <span :class="['badge', rtBadgeClass(acc)]">{{ rtLabel(acc) }}</span>
              <div v-if="acc.oauth_fail_reason" class="subtle">{{ acc.oauth_fail_reason }}</div>
              <div v-else-if="acc.oauth_cooldown_remaining_s" class="subtle">
                cooldown {{ acc.oauth_cooldown_remaining_s }}s
              </div>
            </td>
            <td>
              <span :class="['badge', payBadgeClass(acc.pay_state)]">{{ payStateLabel(acc.pay_state) }}</span>
              <div v-if="acc.latest_payment_status" class="subtle">{{ acc.latest_payment_status }}</div>
            </td>
            <td class="asset-cell">
              <span :class="['asset', { ok: acc.has_session_token }]">session</span>
              <span :class="['asset', { ok: acc.has_access_token }]">access</span>
              <span :class="['asset', { ok: acc.has_device_id }]">device</span>
              <div v-if="accessUsageBadges(acc).length" class="usage-badges">
                <span
                  v-for="badge in accessUsageBadges(acc)"
                  :key="badge.label"
                  :class="['badge', badge.kind === 'error' ? 'badge-danger' : badge.kind === 'warn' ? 'badge-warn' : 'badge-ok']"
                >
                  {{ badge.label }}
                </span>
              </div>
            </td>
            <td><span :class="['badge', acc.downloaded ? 'badge-ok' : 'badge-muted']">{{ acc.downloaded ? "已下载" : "未下载" }}</span></td>
            <td><span :class="['badge', acc.server_pushed ? 'badge-ok' : 'badge-muted']">{{ acc.server_pushed ? "已推送" : "可推送" }}</span></td>
            <td class="error-cell">{{ acc.latest_payment_error || "-" }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="!managerFilteredAccounts.length" class="empty-state">
        {{ inventory.accounts.length ? "没有匹配筛选条件的账号。" : "暂无账号库存。" }}
      </div>
    </section>

    <footer v-if="managerFilteredAccounts.length" class="pager">
      <button class="pager-btn" :disabled="manager.page <= 1" @click="manager.page -= 1">上一页</button>
      <span>第 {{ manager.page }} / {{ managerPageCount }} 页</span>
      <button class="pager-btn" :disabled="manager.page >= managerPageCount" @click="manager.page += 1">下一页</button>
    </footer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { h } from "vue";
import { useDialog, useMessage } from "naive-ui";
import { useRouter } from "vue-router";
import { api } from "../api/client";
import TermBtn from "../components/term/TermBtn.vue";

interface InventoryAccount {
  id: number;
  email: string;
  plan_tag: string;
  has_session_token: boolean;
  has_access_token: boolean;
  has_device_id: boolean;
  has_refresh_token: boolean;
  pay_state: string;
  rt_state: string;
  can_backfill_rt: boolean;
  oauth_status: string;
  oauth_fail_reason: string;
  oauth_cooldown_remaining_s: number;
  latest_payment_status: string;
  latest_payment_error: string;
  registered_at: string;
  downloaded: boolean;
  server_pushed: boolean;
}

interface InventoryResponse {
  accounts: InventoryAccount[];
  counts: Record<string, number>;
}

interface RtCheckResult {
  results: Array<Record<string, unknown>>;
  summary: Record<string, number>;
}

interface AccessCheckResult {
  results: Array<Record<string, unknown>>;
  summary: Record<string, any>;
}

interface UsageBadge {
  label: string;
  kind: "ok" | "warn" | "error";
}

const router = useRouter();
const message = useMessage();
const dialog = useDialog();
const loading = ref(false);
const inventory = ref<InventoryResponse>({ accounts: [], counts: {} });
const managerSelectedIds = ref<Set<number>>(new Set());
const rtCheck = ref<RtCheckResult | null>(null);
const accessCheck = ref<AccessCheckResult | null>(null);
const accessUsageById = ref<Record<number, Record<string, any>>>({});
const manager = ref({
  busy: false,
  savingConfig: false,
  page: 1,
  rtFilter: "all",
  planFilter: "all",
  importUrl: "http://127.0.0.1:8787/api/import",
  importToken: "dev-import-token",
});

const managerPageSize = 25;
const downloadedIds = computed(() =>
  inventory.value.accounts.filter(a => a.downloaded).map(a => a.id)
);
const managerFilteredAccounts = computed(() =>
  inventory.value.accounts.filter((a) => {
    const rtOk =
      manager.value.rtFilter === "all" ||
      (manager.value.rtFilter === "has_rt" && a.has_refresh_token) ||
      (manager.value.rtFilter === "no_rt" && !a.has_refresh_token);
    const planOk =
      manager.value.planFilter === "all" ||
      (manager.value.planFilter === "plus" && a.plan_tag === "plus");
    return rtOk && planOk;
  })
);
const managerPageCount = computed(() =>
  Math.max(1, Math.ceil(managerFilteredAccounts.value.length / managerPageSize))
);
const pagedManagerAccounts = computed(() => {
  const page = Math.min(Math.max(1, manager.value.page), managerPageCount.value);
  const start = (page - 1) * managerPageSize;
  return managerFilteredAccounts.value.slice(start, start + managerPageSize);
});
const managerAllSelected = computed(() => {
  const ids = pagedManagerAccounts.value.map(a => a.id).filter(Boolean);
  return ids.length > 0 && ids.every(id => managerSelectedIds.value.has(id));
});
const managerSelectedFilteredIds = computed(() => {
  const selected = new Set(managerSelectedIds.value);
  return managerFilteredAccounts.value
    .map(a => a.id)
    .filter(id => selected.has(id));
});
const managerSelectedFilteredCount = computed(() => managerSelectedFilteredIds.value.length);
const managerSelectedPageCount = computed(() => {
  const selected = new Set(managerSelectedIds.value);
  return pagedManagerAccounts.value
    .map(a => a.id)
    .filter(id => selected.has(id))
    .length;
});
const managerBackfillIds = computed(() => {
  const selected = new Set(managerSelectedIds.value);
  return managerFilteredAccounts.value
    .filter(a => selected.has(a.id) && a.can_backfill_rt)
    .map(a => a.id);
});
const managerServerPushIds = computed(() => {
  const selected = new Set(managerSelectedIds.value);
  return managerFilteredAccounts.value
    .filter(a => selected.has(a.id) && a.has_refresh_token)
    .map(a => a.id);
});
const planSummary = computed(() => {
  const plans = accessCheck.value?.summary?.plans;
  if (!plans || typeof plans !== "object") return "";
  return Object.entries(plans)
    .map(([k, v]) => `${k}:${v}`)
    .join(" ");
});

watch(
  () => [manager.value.rtFilter, manager.value.planFilter],
  () => {
    manager.value.page = 1;
  }
);
watch(managerPageCount, (count) => {
  if (manager.value.page > count) manager.value.page = count;
});

onMounted(async () => {
  await Promise.all([refreshInventory(), loadAccountImportServerConfig()]);
});

async function refreshInventory() {
  loading.value = true;
  try {
    const r = await api.get("/inventory/accounts");
    inventory.value = {
      accounts: r.data?.accounts || [],
      counts: r.data?.counts || {},
    };
  } catch (e: any) {
    message.error(`加载账号库存失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    loading.value = false;
  }
}

async function loadAccountImportServerConfig() {
  try {
    const r = await api.get("/config/account-import-server");
    const cfg = r.data || {};
    manager.value.importUrl = String(cfg.url || "http://127.0.0.1:8787/api/import");
    manager.value.importToken = String(cfg.token || "dev-import-token");
  } catch {}
}

async function saveAccountImportServerConfig(options?: { quiet?: boolean }) {
  if (!manager.value.importUrl.trim()) { message.warning("请填写导入接口 URL"); return false; }
  if (!manager.value.importToken.trim()) { message.warning("请填写 Bearer token"); return false; }
  manager.value.savingConfig = true;
  try {
    await api.post("/config/account-import-server", {
      url: manager.value.importUrl.trim(),
      token: manager.value.importToken.trim(),
      timeout_s: 30,
    });
    if (!options?.quiet) message.success("服务器推送配置已保存");
    return true;
  } catch (e: any) {
    message.error(`保存服务器推送配置失败：${e?.response?.data?.detail || e?.message || e}`);
    return false;
  } finally {
    manager.value.savingConfig = false;
  }
}

function toggleManagerSelect(id: number) {
  const next = new Set(managerSelectedIds.value);
  if (next.has(id)) next.delete(id); else next.add(id);
  managerSelectedIds.value = next;
}

function toggleManagerSelectAll() {
  const next = new Set(managerSelectedIds.value);
  if (managerAllSelected.value) {
    pagedManagerAccounts.value.forEach(a => next.delete(a.id));
  } else {
    pagedManagerAccounts.value.forEach(a => next.add(a.id));
  }
  managerSelectedIds.value = next;
}

function selectAllManagerFiltered() {
  const next = new Set(managerSelectedIds.value);
  managerFilteredAccounts.value.forEach(a => {
    if (a.id) next.add(a.id);
  });
  managerSelectedIds.value = next;
}

async function detectSelectedRt() {
  const ids = managerSelectedFilteredIds.value;
  if (!ids.length) { message.warning("请选择要检测RT的账号"); return; }
  manager.value.busy = true;
  try {
    const r = await api.post("/inventory/accounts/rt-check", { ids });
    rtCheck.value = r.data || null;
    const s = r.data?.summary || {};
    message.success(`RT检测完成：has_rt=${s.has_rt || 0} missing=${s.missing || 0} retryable=${s.retryable || 0} cooldown=${s.cooldown || 0} dead=${s.dead || 0}`);
    await refreshInventory();
  } catch (e: any) {
    message.error(`RT检测失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    manager.value.busy = false;
  }
}

async function detectSelectedAccessToken() {
  const ids = managerSelectedFilteredIds.value;
  if (!ids.length) { message.warning("请选择要检测AccessToken的账号"); return; }
  manager.value.busy = true;
  try {
    const r = await api.post("/inventory/accounts/oauth-usage-check", {
      ids,
      timeout_s: 12,
      max_workers: 3,
    });
    accessCheck.value = r.data || null;
    accessUsageById.value = Object.fromEntries(
      (r.data?.results || [])
        .filter((item: any) => Number(item?.id))
        .map((item: any) => [Number(item.id), item])
    );
    const s = r.data?.summary || {};
    message.success(`AccessToken检测完成：refreshed=${s.refreshed || 0} ok=${s.ok || 0} quota=${s.quota_limited || 0} invalid=${s.invalid || 0} no_rt=${s.no_refresh_token || 0}`);
    await refreshInventory();
  } catch (e: any) {
    message.error(`AccessToken检测失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    manager.value.busy = false;
  }
}

async function downloadManagerSelected() {
  const ids = managerSelectedFilteredIds.value;
  if (!ids.length) { message.warning("请选择要下载的账号"); return; }
  manager.value.busy = true;
  try {
    const r = await api.post("/inventory/accounts/export-cpa-zip", { ids }, { responseType: "blob" });
    const cd = String(r.headers?.["content-disposition"] || "");
    const m = /filename="?([^"]+)"?/i.exec(cd);
    const name = m?.[1] || `cpa-accounts-${Date.now()}.zip`;
    const url = URL.createObjectURL(r.data);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    message.success(`已下载 ${ids.length} 个账号`);
    managerSelectedIds.value = new Set();
    await refreshInventory();
  } catch (e: any) {
    message.error(`下载失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    manager.value.busy = false;
  }
}

async function backfillManagerSelectedRt() {
  const ids = managerBackfillIds.value;
  if (!ids.length) { message.warning("请选择RT待补或可重试账号"); return; }
  manager.value.busy = true;
  try {
    await api.post("/inventory/accounts/backfill-rt", { ids });
    message.success(`已启动补RT任务：${ids.length} 个账号，可在运行页查看日志`);
    await refreshInventory();
  } catch (e: any) {
    message.error(`补RT启动失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    manager.value.busy = false;
  }
}

async function pushManagerSelectedToServer() {
  const ids = managerServerPushIds.value;
  if (!ids.length) { message.warning("请选择有RT的账号推送至导入服务器"); return; }
  if (!manager.value.importUrl.trim()) { message.warning("请填写导入接口 URL"); return; }
  if (!manager.value.importToken.trim()) { message.warning("请填写 Bearer token"); return; }
  if (!(await saveAccountImportServerConfig({ quiet: true }))) return;
  manager.value.busy = true;
  try {
    const r = await api.post("/inventory/accounts/server-push", {
      ids,
      import_url: manager.value.importUrl.trim(),
      import_token: manager.value.importToken.trim(),
    });
    const s = r.data?.summary || {};
    message.success(`导入服务器推送完成：ok=${s.ok || 0} fail=${s.fail || 0} missing=${s.missing || 0}`);
    managerSelectedIds.value = new Set();
    await refreshInventory();
  } catch (e: any) {
    message.error(`导入服务器推送失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    manager.value.busy = false;
  }
}

function deleteDownloadedAccounts() {
  if (!downloadedIds.value.length) { message.warning("没有已下载账号"); return; }
  dialog.warning({
    title: "确认删除已下载账号？",
    content: () => h("div", { style: "font-size:12px; line-height:1.6" }, [
      h("div", `将删除 ${downloadedIds.value.length} 个已下载账号，审计记录会保留。`),
    ]),
    positiveText: "确认删除",
    negativeText: "取消",
    onPositiveClick: async () => {
      manager.value.busy = true;
      try {
        const r = await api.post("/inventory/accounts/delete-downloaded");
        message.success(`已删除 ${r.data?.deleted ?? 0} 个已下载账号`);
        managerSelectedIds.value = new Set();
        await refreshInventory();
      } catch (e: any) {
        message.error(`删除失败：${e?.response?.data?.detail || e?.message || e}`);
      } finally {
        manager.value.busy = false;
      }
    },
  });
}

function planLabel(p: string) {
  if (p === "team") return "team";
  if (p === "plus") return "plus";
  return "free";
}

function planBadgeClass(p: string) {
  if (p === "team") return "badge-team";
  if (p === "plus") return "badge-plus";
  return "badge-muted";
}

function rtLabel(acc: InventoryAccount) {
  if (acc.has_refresh_token) return "有RT";
  if (acc.rt_state === "cooldown") return "冷却";
  if (acc.rt_state === "retryable") return "可重试";
  if (acc.rt_state === "dead") return "失效";
  if (acc.rt_state === "oauth_succeeded") return "已处理";
  return "无RT";
}

function rtBadgeClass(acc: InventoryAccount) {
  if (acc.has_refresh_token) return "badge-ok";
  if (acc.rt_state === "cooldown") return "badge-warn";
  if (acc.rt_state === "retryable") return "badge-info";
  if (acc.rt_state === "dead") return "badge-danger";
  return "badge-muted";
}

function payStateLabel(state: string) {
  if (state === "reusable") return "可支付";
  if (state === "consumed") return "已支付";
  if (state === "no_auth") return "无授权";
  return state || "-";
}

function payBadgeClass(state: string) {
  if (state === "reusable") return "badge-ok";
  if (state === "consumed") return "badge-plus";
  if (state === "no_auth") return "badge-warn";
  return "badge-muted";
}

function accessUsageBadges(acc: InventoryAccount): UsageBadge[] {
  const result = accessUsageById.value[acc.id];
  if (!result) return [];

  const httpStatus = Number(result.http_status || 0);
  if (httpStatus === 401 || httpStatus === 403) {
    return [{ label: String(httpStatus), kind: "error" }];
  }
  if (result.status === "invalid") {
    return [{ label: httpStatus ? String(httpStatus) : "invalid", kind: "error" }];
  }
  if (result.status === "no_refresh_token") {
    return [{ label: "no_rt", kind: "warn" }];
  }

  const badges: UsageBadge[] = [];
  const fiveHour = quotaBadgeLabel("5h", result.five_hour_quota);
  const weekly = quotaBadgeLabel("week", result.weekly_quota);
  if (fiveHour) badges.push({ label: fiveHour, kind: "ok" });
  if (weekly) badges.push({ label: weekly, kind: "ok" });
  if (!badges.length && result.status === "quota_limited") {
    badges.push({ label: "quota", kind: "warn" });
  }
  if (!badges.length && result.status) {
    badges.push({ label: String(result.status), kind: result.oauth_valid ? "warn" : "error" });
  }
  return badges;
}

function quotaBadgeLabel(prefix: string, quota: any): string {
  if (!quota || typeof quota !== "object") return "";
  const percent = quota.percent_remaining ?? quota.remaining_percent;
  if (percent !== undefined && percent !== null && percent !== "") {
    const n = Number(percent);
    const value = Number.isFinite(n) ? Math.round(n <= 1 ? n * 100 : n) : String(percent);
    return `${prefix}:${value}%`;
  }
  const remaining = quota.remaining;
  const limit = quota.limit;
  if (remaining !== undefined && remaining !== null && limit !== undefined && limit !== null) {
    return `${prefix}:${remaining}/${limit}`;
  }
  return "";
}
</script>

<style scoped>
.accounts-page {
  min-height: 100vh;
  background: var(--bg-base);
  color: var(--fg-primary);
  padding: 24px;
}

.accounts-topbar,
.summary-band,
.control-band,
.action-band,
.rt-result-band,
.pager {
  border: 1px solid var(--border);
  background: var(--bg-panel);
}

.accounts-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px;
}

.eyebrow {
  margin: 0 0 4px;
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--fg-tertiary);
}

h1 {
  margin: 0;
  font-size: 24px;
  letter-spacing: 0;
}

.top-actions,
.action-band {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.summary-band {
  display: grid;
  grid-template-columns: repeat(6, minmax(120px, 1fr));
  margin-top: 14px;
}

.metric {
  padding: 14px 16px;
  border-right: 1px solid var(--border);
}

.metric:last-child {
  border-right: 0;
}

.metric span {
  display: block;
  color: var(--fg-tertiary);
  font-size: 12px;
}

.metric strong {
  display: block;
  margin-top: 5px;
  font-size: 22px;
}

.control-band {
  margin-top: 14px;
  padding: 14px;
  display: grid;
  grid-template-columns: minmax(260px, 1.5fr) minmax(190px, 1fr) auto 160px 130px;
  gap: 12px;
  align-items: end;
}

.field {
  display: grid;
  gap: 6px;
}

.field span {
  color: var(--fg-tertiary);
  font-size: 12px;
}

input,
select {
  width: 100%;
  box-sizing: border-box;
  background: var(--bg-base);
  color: var(--fg-primary);
  border: 1px solid var(--border-strong);
  padding: 9px 10px;
  font: inherit;
  border-radius: 0;
  outline: none;
}

input:focus,
select:focus {
  border-color: var(--accent);
}

.action-band {
  margin-top: 14px;
  padding: 12px 14px;
}

.select-page {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--fg-secondary);
  font-size: 13px;
  white-space: nowrap;
}

.rt-result-band {
  margin-top: 14px;
  padding: 12px 14px;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  color: var(--fg-secondary);
}

.rt-result-band strong {
  margin-right: 12px;
  color: var(--fg-primary);
}

.result-extra {
  margin-left: 12px;
}

.result-list-inline {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  margin-top: 8px;
  color: var(--fg-tertiary);
  font-size: 11px;
}

.result-list-inline span {
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.text-button {
  background: transparent;
  border: 0;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
}

.table-wrap {
  margin-top: 14px;
  border: 1px solid var(--border);
  overflow: auto;
  background: var(--bg-base);
}

.account-table {
  width: 100%;
  border-collapse: collapse;
  min-width: 1120px;
}

th,
td {
  border-bottom: 1px solid var(--border);
  padding: 10px 12px;
  text-align: left;
  vertical-align: top;
  font-size: 13px;
}

th {
  position: sticky;
  top: 0;
  background: var(--bg-panel);
  color: var(--fg-tertiary);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  z-index: 1;
}

.check-col {
  width: 38px;
}

.email-cell {
  font-weight: 700;
  word-break: break-all;
}

.subtle {
  margin-top: 4px;
  color: var(--fg-tertiary);
  font-size: 11px;
  max-width: 260px;
  word-break: break-all;
}

.badge {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  border: 1px solid var(--border);
  color: var(--fg-secondary);
  font-size: 12px;
  white-space: nowrap;
}

.badge-ok {
  color: var(--ok);
  border-color: color-mix(in srgb, var(--ok), transparent 55%);
}

.badge-plus {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent), transparent 55%);
}

.badge-team {
  color: #5aa7ff;
  border-color: rgba(90, 167, 255, 0.45);
}

.badge-warn {
  color: var(--warn);
  border-color: color-mix(in srgb, var(--warn), transparent 55%);
}

.badge-danger {
  color: var(--err);
  border-color: color-mix(in srgb, var(--err), transparent 55%);
}

.badge-info {
  color: #5aa7ff;
  border-color: rgba(90, 167, 255, 0.45);
}

.badge-muted {
  color: var(--fg-tertiary);
  border-color: var(--border);
}

.asset-cell {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.usage-badges {
  display: flex;
  flex-basis: 100%;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 4px;
}

.asset {
  color: var(--fg-tertiary);
  border: 1px solid var(--border);
  padding: 2px 6px;
  font-size: 11px;
}

.asset.ok {
  color: var(--ok);
  border-color: color-mix(in srgb, var(--ok), transparent 60%);
}

.error-cell {
  max-width: 260px;
  color: var(--fg-tertiary);
  word-break: break-word;
}

.empty-state {
  padding: 48px 18px;
  text-align: center;
  color: var(--fg-tertiary);
}

.pager {
  margin-top: 14px;
  padding: 12px;
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 12px;
  color: var(--fg-secondary);
}

.pager-btn {
  background: transparent;
  border: 1px solid var(--border-strong);
  color: var(--fg-secondary);
  padding: 7px 12px;
  cursor: pointer;
}

.pager-btn:disabled {
  color: var(--fg-tertiary);
  cursor: not-allowed;
}

@media (max-width: 980px) {
  .accounts-page {
    padding: 14px;
  }

  .accounts-topbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .summary-band {
    grid-template-columns: repeat(2, minmax(120px, 1fr));
  }

  .metric {
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }

  .control-band {
    grid-template-columns: 1fr;
  }

  .rt-result-band {
    flex-direction: column;
  }
}
</style>
