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
          (命中 <code>plus-1-month-free</code> 时 <code>amount_due ≤ 1 currency unit</code>; 全价 ~349k IDR / $20 USD)。
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
              <th>id</th>
              <th>email</th>
              <th>plan / promo</th>
              <th>amount_due</th>
              <th>状态</th>
              <th>时间</th>
              <th class="url-col">URL</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in items" :key="row.id" :class="`row-${row.status}`">
              <td>#{{ row.id }}</td>
              <td><code>{{ row.email }}</code></td>
              <td class="plan-cell">
                <div>{{ row.plan_name || "—" }}</div>
                <div class="meta">{{ row.promo_campaign_id }}</div>
              </td>
              <td>
                <span :class="amountClass(row)">
                  {{ row.amount_due_cents }} {{ row.billing_currency }} cents
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
import { ref, onMounted, onBeforeUnmount } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api/client";

const router = useRouter();

const items = ref<any[]>([]);
const stats = ref({ fresh: 0, used: 0, expired: 0, total: 0 });
const busy = ref(false);
const statusFilter = ref("");
const copiedId = ref<number | null>(null);

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
  } catch (e: any) {
    console.warn("loadList fail", e);
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
  if (!amt) return "amt-unknown";
  if (amt <= 100) return "amt-promo-hit";
  return "amt-fullprice";
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
.pl-filter { display: flex; gap: 8px; align-items: center; margin: 12px 0; }
.pl-filter select { padding: 6px 10px; font-family: inherit; border: 1px solid var(--border); background: var(--bg-input); }
.pl-empty { padding: 30px; text-align: center; color: var(--fg-secondary); border: 1px dashed var(--border); }
.pl-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.pl-table th, .pl-table td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
.pl-table th { background: var(--bg-secondary); font-weight: 600; }
.pl-table code { font-family: JetBrains Mono, ui-monospace, monospace; }
.plan-cell .meta { font-size: 10px; color: var(--fg-secondary); margin-top: 2px; }
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
