<template>
  <div class="pl-root">
    <header class="wizard-header">
      <div class="brand">
        <span class="brand-prompt">$</span>
        <span class="brand-name">gpt-pay</span>
        <span class="brand-sub">// Promo 长链接池</span>
        <span class="brand-clock">{{ clock }}</span>
      </div>
      <div class="run-nav">
        <RouterLink to="/wizard" class="nav-link">配置向导</RouterLink>
        <RouterLink to="/run" class="nav-link">运行</RouterLink>
        <RouterLink to="/outlook" class="nav-link">Outlook 池</RouterLink>
        <RouterLink to="/whatsapp" class="nav-link">WhatsApp</RouterLink>
        <button class="header-btn" @click="logout">退出</button>
      </div>
    </header>

    <main class="pl-main">
      <section class="pl-panel">
        <div class="term-divider" data-tail="──────────">优惠长链接 (promo_links)</div>
        <h2 class="pl-title">ChatGPT promo 命中的 hosted long URL<span class="term-cursor"></span></h2>
        <p class="pl-sub">
          <code>mode=promo_link</code> 跑出来的 URL 存这里。打开 <code>checkout_url</code> 即可走 promo 价格付款
          (命中 <code>plus-1-month-free</code> 时 <code>amount_due ≤ 1 currency unit</code>)。
          现在可以自由选择 <code>country/currency</code> 生成新链接，也可以把库存里的链接用同账号重建到其它区域。
          点 "复制" 拿 URL, 用完按 "标 used" 防重复用; URL 一般 30 分钟过期, 过期按 "标 expired"。
        </p>

        <div class="pl-stats">
          <div class="stat" :class="{ ok: stats.fresh > 0 }">
            <strong>{{ stats.fresh }}</strong><span>fresh</span>
          </div>
          <div class="stat"><strong>{{ stats.used }}</strong><span>used</span></div>
          <div class="stat err"><strong>{{ stats.expired }}</strong><span>expired</span></div>
          <div class="stat"><strong>{{ stats.total }}</strong><span>total</span></div>
        </div>

        <div class="pl-actions">
          <button class="header-btn ghost" @click="loadList">刷新列表</button>
          <button class="header-btn ghost" :disabled="busy || stats.used === 0" @click="bulkDelete('used')">
            清 used ({{ stats.used }})
          </button>
          <button class="header-btn ghost" :disabled="busy || stats.expired === 0" @click="bulkDelete('expired')">
            清 expired ({{ stats.expired }})
          </button>
          <RouterLink to="/run?mode=promo_link" class="header-btn">▶ 去运行抓新链接</RouterLink>
        </div>

        <div class="convert-panel">
          <div class="convert-head">
            <strong>区域转换 / 重建 checkout</strong>
            <span class="convert-meta">
              选中库存行后，用该 email 在 <code>registered_accounts</code> 里的 access_token
              重新调用 ChatGPT checkout；不是简单替换 URL 文本。
            </span>
          </div>
          <div class="region-presets">
            <button
              v-for="preset in regionPresets"
              :key="preset.country"
              class="chip"
              :class="{ active: convertForm.country === preset.country && convertForm.currency === preset.currency }"
              @click="applyRegion(preset.country, preset.currency)"
            >{{ preset.label }}</button>
          </div>
          <div class="convert-form">
            <label>
              plan
              <select v-model="convertForm.plan">
                <option value="">沿用原行</option>
                <option value="plus">plus</option>
                <option value="team">team</option>
              </select>
            </label>
            <label>
              country
              <input v-model="convertForm.country" maxlength="2" placeholder="ID" @blur="normalizeRegion" />
            </label>
            <label>
              currency
              <input v-model="convertForm.currency" maxlength="3" placeholder="IDR" @blur="normalizeRegion" />
            </label>
            <label class="wide">
              campaign
              <input v-model="convertForm.promo_campaign_id" placeholder="空=沿用原行优惠码；原行空则 plus/team 默认" />
            </label>
            <label class="check-row" title="建议保持开启：目标区域优惠不命中时不写入库存，避免误用全价链接">
              <input type="checkbox" v-model="convertForm.require_promo_hit" />
              只保存优惠命中
            </label>
            <label>
              写入方式
              <select v-model="convertForm.mode">
                <option value="clone">新增副本</option>
                <option value="replace">覆盖原行</option>
              </select>
            </label>
            <button
              class="header-btn"
              :disabled="convertBusy || selectedIds.size === 0 || !convertRegionOk"
              @click="convertSelected"
            >
              {{ convertBusy ? "转换中..." : `转换选中 (${selectedIds.size})` }}
            </button>
          </div>
          <div v-if="convertMsg" class="convert-msg" :class="{ err: convertMsgIsErr }">{{ convertMsg }}</div>
        </div>

        <div class="term-divider" data-tail="──────────">列表</div>
        <div class="pl-filter">
          <label>状态过滤：</label>
          <select v-model="statusFilter" @change="loadList">
            <option value="">全部</option>
            <option value="fresh">fresh</option>
            <option value="used">used</option>
            <option value="expired">expired</option>
          </select>
        </div>
        <div v-if="items.length === 0" class="pl-empty">
          {{ statusFilter ? `无 ${statusFilter} 状态` : "池为空，去 /run 选 mode=promo_link 抓一批" }}
        </div>
        <table v-else class="pl-table">
          <thead>
            <tr>
              <th class="sel-col">
                <input type="checkbox" :checked="allSelected" @change="toggleAll" />
              </th>
              <th>id</th>
              <th>email</th>
              <th>plan / promo</th>
              <th>区域</th>
              <th>amount_due</th>
              <th>状态</th>
              <th>时间</th>
              <th class="url-col">URL</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in items" :key="row.id" :class="`row-${row.status}`">
              <td>
                <input type="checkbox" :checked="selectedIds.has(row.id)" @change="toggleSelected(row.id)" />
              </td>
              <td>#{{ row.id }}</td>
              <td><code>{{ row.email }}</code></td>
              <td class="plan-cell">
                <div>{{ row.plan_name || "—" }}</div>
                <div class="meta">{{ row.promo_campaign_id }}</div>
              </td>
              <td class="region-cell">
                <strong>{{ row.billing_country || "—" }}/{{ row.billing_currency || "—" }}</strong>
                <div class="meta">{{ row.processor_entity || "—" }}</div>
              </td>
              <td>
                <span :class="amountClass(row)">
                  {{ amountLabel(row) }}
                </span>
              </td>
              <td><span class="badge" :class="`badge-${row.status}`">{{ row.status }}</span></td>
              <td>{{ formatTs(row.created_at) }}</td>
              <td class="url-cell">
                <a :href="row.checkout_url" target="_blank" rel="noopener" :title="row.checkout_url">
                  {{ truncateUrl(row.checkout_url) }}
                </a>
              </td>
              <td class="ops">
                <button class="link-btn" @click="copy(row.checkout_url, row.id)">
                  {{ copiedId === row.id ? "✓ 已复制" : "复制" }}
                </button>
                <button class="link-btn" v-if="row.status === 'fresh'" @click="markUsed(row.id)">标 used</button>
                <button class="link-btn" v-if="row.status === 'fresh'" @click="setStatus(row.id, 'expired')">标 expired</button>
                <button class="link-btn" v-if="row.status !== 'fresh'" @click="setStatus(row.id, 'fresh')">复活</button>
                <button class="link-btn" :disabled="convertBusy || !convertRegionOk" @click="convertOne(row)">转区域</button>
                <button class="link-btn danger" @click="doDelete(row.id, row.email)">删</button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api/client";

const router = useRouter();

const items = ref<any[]>([]);
const stats = ref({ fresh: 0, used: 0, expired: 0, total: 0 });
const busy = ref(false);
const statusFilter = ref("");
const copiedId = ref<number | null>(null);
const selectedIds = ref<Set<number>>(new Set());
const convertBusy = ref(false);
const convertMsg = ref("");
const convertMsgIsErr = ref(false);
const regionPresets = [
  { country: "ID", currency: "IDR", label: "ID/IDR" },
  { country: "US", currency: "USD", label: "US/USD" },
  { country: "JP", currency: "JPY", label: "JP/JPY" },
  { country: "GB", currency: "GBP", label: "GB/GBP" },
  { country: "IE", currency: "EUR", label: "IE/EUR" },
  { country: "FR", currency: "EUR", label: "FR/EUR" },
  { country: "DE", currency: "EUR", label: "DE/EUR" },
  { country: "CA", currency: "CAD", label: "CA/CAD" },
  { country: "AU", currency: "AUD", label: "AU/AUD" },
  { country: "SG", currency: "SGD", label: "SG/SGD" },
  { country: "HK", currency: "HKD", label: "HK/HKD" },
  { country: "TW", currency: "TWD", label: "TW/TWD" },
  { country: "KR", currency: "KRW", label: "KR/KRW" },
  { country: "IN", currency: "INR", label: "IN/INR" },
];
const currencyByCountry: Record<string, string> = Object.fromEntries(
  regionPresets.map((x) => [x.country, x.currency])
);
const convertForm = ref({
  plan: "",
  country: (localStorage.getItem("webui.promo.convert_country") || "ID").toUpperCase(),
  currency: (localStorage.getItem("webui.promo.convert_currency") || "IDR").toUpperCase(),
  promo_campaign_id: localStorage.getItem("webui.promo.convert_campaign") || "",
  require_promo_hit: localStorage.getItem("webui.promo.require_hit") !== "0",
  mode: (localStorage.getItem("webui.promo.convert_mode") || "clone") as "clone" | "replace",
});
const convertRegionOk = computed(() =>
  /^[A-Z]{2}$/.test((convertForm.value.country || "").trim().toUpperCase())
  && /^[A-Z]{3}$/.test((convertForm.value.currency || "").trim().toUpperCase())
);
const allSelected = computed(() =>
  items.value.length > 0 && items.value.every((row) => selectedIds.value.has(row.id))
);
watch(
  () => [
    convertForm.value.country,
    convertForm.value.currency,
    convertForm.value.promo_campaign_id,
    convertForm.value.require_promo_hit,
    convertForm.value.mode,
  ],
  persistConvertForm
);

const clock = ref("");
let clockTimer: any = null;
function tick() {
  const d = new Date();
  clock.value = `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}:${d.getSeconds().toString().padStart(2,'0')}`;
}

async function loadList() {
  try {
    const r = await api.get("/promo-links/list", { params: { limit: 500, status: statusFilter.value } });
    items.value = r.data.items || [];
    stats.value = r.data.stats || stats.value;
    const visibleIds = new Set(items.value.map((x) => x.id));
    selectedIds.value = new Set([...selectedIds.value].filter((id) => visibleIds.has(id)));
  } catch (e: any) {
    console.warn("loadList fail", e);
  }
}

function toggleSelected(id: number) {
  const next = new Set(selectedIds.value);
  if (next.has(id)) next.delete(id); else next.add(id);
  selectedIds.value = next;
}

function toggleAll() {
  if (allSelected.value) {
    selectedIds.value = new Set();
  } else {
    selectedIds.value = new Set(items.value.map((x) => x.id));
  }
}

function applyRegion(country: string, currency: string) {
  convertForm.value.country = country.toUpperCase();
  convertForm.value.currency = currency.toUpperCase();
  persistConvertForm();
}

function normalizeRegion() {
  const country = (convertForm.value.country || "").trim().toUpperCase();
  convertForm.value.country = country;
  convertForm.value.currency = ((convertForm.value.currency || "").trim().toUpperCase() || currencyByCountry[country] || "");
  persistConvertForm();
}

function persistConvertForm() {
  try {
    localStorage.setItem("webui.promo.convert_country", (convertForm.value.country || "").trim().toUpperCase());
    localStorage.setItem("webui.promo.convert_currency", (convertForm.value.currency || "").trim().toUpperCase());
    localStorage.setItem("webui.promo.convert_campaign", convertForm.value.promo_campaign_id || "");
    localStorage.setItem("webui.promo.require_hit", convertForm.value.require_promo_hit ? "1" : "0");
    localStorage.setItem("webui.promo.convert_mode", convertForm.value.mode);
  } catch {}
}

function convertPayload() {
  normalizeRegion();
  return {
    plan: convertForm.value.plan,
    country: convertForm.value.country,
    currency: convertForm.value.currency,
    promo_campaign_id: convertForm.value.promo_campaign_id,
    require_promo_hit: convertForm.value.require_promo_hit,
    mode: convertForm.value.mode,
  };
}

function detailText(e: any): string {
  const detail = e?.response?.data?.detail;
  if (!detail) return e?.message || String(e);
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}

async function convertOne(row: any) {
  convertBusy.value = true;
  convertMsg.value = "";
  convertMsgIsErr.value = false;
  try {
    const r = await api.post(`/promo-links/${row.id}/convert`, convertPayload());
    const out = r.data || {};
    convertMsg.value = `#${row.id} 已转换为 ${out.billing_country}/${out.billing_currency}，campaign=${out.promo_campaign_id || "—"}，amount=${out.amount_due_cents}，${out.mode === "replace" ? "已覆盖原行" : `新行 #${out.id}`}`;
    await loadList();
  } catch (e: any) {
    convertMsgIsErr.value = true;
    convertMsg.value = "转换失败：" + detailText(e);
  } finally {
    convertBusy.value = false;
  }
}

async function convertSelected() {
  const ids = [...selectedIds.value];
  if (!ids.length) return;
  if (convertForm.value.mode === "replace" && !confirm(`确认覆盖 ${ids.length} 条长链接？旧 URL 会被替换为新区 checkout。`)) return;
  convertBusy.value = true;
  convertMsg.value = "";
  convertMsgIsErr.value = false;
  try {
    const r = await api.post("/promo-links/convert-bulk", { ...convertPayload(), ids });
    const ok = r.data?.converted?.length || 0;
    const errors = r.data?.errors || [];
    convertMsgIsErr.value = errors.length > 0;
    convertMsg.value = errors.length
      ? `转换完成 ${ok} 条，失败 ${errors.length} 条：${errors.slice(0, 3).map((x: any) => `#${x.id} ${x.error}`).join("；")}`
      : `已转换 ${ok} 条到 ${convertForm.value.country}/${convertForm.value.currency}`;
    await loadList();
  } catch (e: any) {
    convertMsgIsErr.value = true;
    convertMsg.value = "批量转换失败：" + detailText(e);
  } finally {
    convertBusy.value = false;
  }
}

async function markUsed(id: number) {
  try {
    await api.post(`/promo-links/${id}/mark-used`);
    await loadList();
  } catch (e: any) {
    alert("标 used 失败：" + (e?.response?.data?.detail || e.message));
  }
}

async function setStatus(id: number, status: "fresh" | "used" | "expired") {
  try {
    await api.post(`/promo-links/${id}/status`, { status });
    await loadList();
  } catch (e: any) {
    alert(`改 ${status} 失败：` + (e?.response?.data?.detail || e.message));
  }
}

async function doDelete(id: number, email: string) {
  if (!confirm(`确定删 #${id} (${email})？`)) return;
  try {
    await api.delete(`/promo-links/${id}`);
    await loadList();
  } catch (e: any) {
    alert("删除失败：" + (e?.response?.data?.detail || e.message));
  }
}

async function bulkDelete(status: "used" | "expired") {
  if (!confirm(`确定删所有 ${status} 状态？`)) return;
  busy.value = true;
  try {
    const r = await api.delete(`/promo-links?status=${status}`);
    await loadList();
    alert(`已删 ${r.data?.deleted ?? 0} 条`);
  } catch (e: any) {
    alert("批量删失败：" + (e?.response?.data?.detail || e.message));
  } finally {
    busy.value = false;
  }
}

async function copy(url: string, id: number) {
  try {
    await navigator.clipboard.writeText(url);
    copiedId.value = id;
    setTimeout(() => { if (copiedId.value === id) copiedId.value = null; }, 1500);
  } catch (e: any) {
    // fallback: temp textarea
    const ta = document.createElement("textarea");
    ta.value = url; document.body.appendChild(ta);
    ta.select(); document.execCommand("copy");
    document.body.removeChild(ta);
    copiedId.value = id;
    setTimeout(() => { if (copiedId.value === id) copiedId.value = null; }, 1500);
  }
}

function truncateUrl(u: string): string {
  if (!u) return "";
  if (u.length <= 60) return u;
  return u.slice(0, 30) + "..." + u.slice(-25);
}

function amountClass(row: any): string {
  const amt = row.amount_due_cents;
  if (amt === null || amt === undefined || amt === "") return "amt-unknown";
  if (amt <= 100) return "amt-promo-hit";
  return "amt-fullprice";
}

function amountLabel(row: any): string {
  const cur = row.billing_currency || "";
  const amount = row.amount_due_cents ?? 0;
  return `${amount} ${cur} minor`;
}

function formatTs(ts: number): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${d.getMonth()+1}/${d.getDate()} ${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}`;
}

async function logout() {
  try { await api.post("/logout"); } catch {}
  router.push("/login");
}

let pollTimer: any = null;
onMounted(() => {
  tick();
  clockTimer = setInterval(tick, 1000);
  loadList();
  // 自动每 10s 刷新 (promo_link mode 跑的时候有新条目进来)
  pollTimer = setInterval(loadList, 10000);
});
onBeforeUnmount(() => {
  if (clockTimer) clearInterval(clockTimer);
  if (pollTimer) clearInterval(pollTimer);
});
</script>

<style scoped>
.pl-root { min-height: 100vh; background: var(--bg-secondary, #f0ece1); display: flex; flex-direction: column; }
.wizard-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 20px; background: var(--bg-panel, #fff); border-bottom: 1px solid var(--border, #d4cdb9); }
.brand { display: flex; gap: 10px; align-items: center; font-family: JetBrains Mono, ui-monospace, monospace; }
.brand-prompt { color: var(--accent, #b25e1f); font-weight: bold; }
.brand-name { font-weight: bold; color: var(--fg, #1c1a15); }
.brand-sub { color: var(--fg-secondary, #7a7363); }
.brand-clock { margin-left: 16px; color: var(--fg-secondary); font-size: 12px; }
.run-nav { display: flex; gap: 8px; }
.nav-link { padding: 6px 12px; color: var(--fg-secondary); text-decoration: none; border: 1px solid transparent; }
.nav-link:hover { color: var(--accent); border-color: var(--accent); }
.header-btn { padding: 6px 14px; background: var(--accent, #b25e1f); color: white; border: 1px solid var(--accent); cursor: pointer; font-family: inherit; text-decoration: none; display: inline-block; }
.header-btn:hover { background: var(--accent-hover, #8a4413); }
.header-btn.ghost { background: transparent; color: var(--accent); }
.header-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.pl-main { flex: 1; padding: 24px; }
.pl-panel { max-width: 1400px; margin: 0 auto; background: var(--bg-panel, #fff); padding: 20px; border: 1px solid var(--border, #d4cdb9); }
.pl-title { font-size: 20px; margin: 0 0 8px; }
.pl-sub { color: var(--fg-secondary); font-size: 13px; margin-bottom: 16px; line-height: 1.6; }
.pl-sub code { background: var(--bg-secondary); padding: 2px 6px; border: 1px solid var(--border); }
.pl-stats { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.stat { display: flex; flex-direction: column; align-items: center; padding: 8px 16px; border: 1px solid var(--border); background: var(--bg-secondary); min-width: 80px; }
.stat strong { font-size: 22px; color: var(--fg); }
.stat span { font-size: 11px; color: var(--fg-secondary); }
.stat.ok { border-color: var(--success, #1f6638); }
.stat.ok strong { color: var(--success); }
.stat.err strong { color: var(--error, #b91c1c); }
.pl-actions { display: flex; gap: 12px; align-items: center; margin: 12px 0 24px; flex-wrap: wrap; }
.convert-panel {
  border: 1px dashed var(--border);
  background: var(--bg-secondary);
  padding: 12px;
  margin: 8px 0 18px;
}
.convert-head {
  display: flex;
  gap: 10px;
  align-items: baseline;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.convert-head strong { color: var(--fg); }
.convert-meta {
  color: var(--fg-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.convert-meta code {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  padding: 1px 5px;
}
.region-presets {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.chip {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--fg-secondary);
  padding: 4px 8px;
  cursor: pointer;
  font-family: inherit;
  font-size: 11px;
}
.chip:hover,
.chip.active {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--bg-panel);
}
.convert-form {
  display: flex;
  gap: 8px;
  align-items: flex-end;
  flex-wrap: wrap;
}
.convert-form label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--fg-secondary);
  font-size: 11px;
}
.convert-form label.check-row {
  flex-direction: row;
  align-items: center;
  min-height: 32px;
  padding: 0 8px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  cursor: pointer;
  user-select: none;
}
.convert-form label.check-row input {
  min-height: 0;
  accent-color: var(--accent);
}
.convert-form label.wide {
  flex: 1 1 260px;
}
.convert-form input,
.convert-form select {
  min-height: 32px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  color: var(--fg);
  padding: 5px 8px;
  font-family: inherit;
  font-size: 12px;
}
.convert-form input:focus,
.convert-form select:focus {
  outline: none;
  border-color: var(--accent);
}
.convert-msg {
  margin-top: 8px;
  font-size: 12px;
  color: var(--success, #1f6638);
  line-height: 1.5;
}
.convert-msg.err { color: var(--error, #b91c1c); }
.pl-filter { display: flex; gap: 8px; align-items: center; margin: 12px 0; }
.pl-filter select { padding: 6px 10px; font-family: inherit; border: 1px solid var(--border); background: var(--bg-input); }
.pl-empty { padding: 30px; text-align: center; color: var(--fg-secondary); border: 1px dashed var(--border); }
.pl-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.pl-table th, .pl-table td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
.pl-table th { background: var(--bg-secondary); font-weight: 600; }
.pl-table code { font-family: JetBrains Mono, ui-monospace, monospace; }
.sel-col { width: 28px; }
.pl-table input[type="checkbox"] { accent-color: var(--accent); cursor: pointer; }
.plan-cell .meta { font-size: 10px; color: var(--fg-secondary); margin-top: 2px; }
.region-cell strong { white-space: nowrap; }
.region-cell .meta { font-size: 10px; color: var(--fg-secondary); margin-top: 2px; }
.url-col { width: 35%; }
.url-cell { word-break: break-all; }
.url-cell a { color: var(--accent); text-decoration: none; font-family: JetBrains Mono, monospace; font-size: 11px; }
.url-cell a:hover { text-decoration: underline; }
.ops { white-space: nowrap; }
.amt-promo-hit { color: var(--success, #1f6638); font-weight: 600; }
.amt-fullprice { color: var(--error, #b91c1c); }
.amt-unknown { color: var(--fg-secondary); }
.badge { padding: 2px 8px; font-size: 11px; }
.badge-fresh { background: #d6eedc; color: #1f6638; border: 1px solid #1f6638; }
.badge-used { background: #e3e3e3; color: #555; border: 1px solid #888; }
.badge-expired { background: #fde0e0; color: #b91c1c; border: 1px solid #b91c1c; }
.link-btn { background: transparent; border: none; cursor: pointer; color: var(--fg-secondary); padding: 4px 6px; font-family: inherit; font-size: 11px; }
.link-btn:hover { color: var(--fg); }
.link-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.link-btn.danger:hover { color: var(--error, #b91c1c); }
.term-divider { font-family: JetBrains Mono, monospace; color: var(--fg-secondary); margin: 16px 0 8px; font-size: 12px; }
.term-divider::after { content: " " attr(data-tail); color: var(--border); }
.term-cursor::after { content: "_"; animation: blink 1s infinite; }
@keyframes blink { 50% { opacity: 0; } }
</style>
