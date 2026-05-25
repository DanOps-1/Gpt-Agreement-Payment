<template>
  <div class="ol-root">
    <header class="wizard-header">
      <div class="brand">
        <span class="brand-prompt">$</span>
        <span class="brand-name">gpt-pay</span>
        <span class="brand-sub">// Outlook Account Pool</span>
        <span class="brand-clock">{{ clock }}</span>
      </div>
      <div class="run-nav">
        <RouterLink to="/wizard" class="nav-link">Configuration Wizard</RouterLink>
        <RouterLink to="/run" class="nav-link">Run</RouterLink>
        <RouterLink to="/promo-links" class="nav-link">Promo Long Links</RouterLink>
        <RouterLink to="/whatsapp" class="nav-link">WhatsApp</RouterLink>
        <button class="header-btn" @click="logout">Logout</button>
      </div>
    </header>

    <main class="ol-main">
      <section class="ol-panel">
        <div class="term-divider" data-tail="──────────">Batch Import</div>
        <h2 class="ol-title">Outlook SMS Reception Pool<span class="term-cursor"></span></h2>
        <p class="ol-sub">
          One entry per line in 4-segment format (SMS service standard): <code>email----password----client_id----refresh_token</code>.
          Empty lines / lines starting with <code>#</code> are automatically skipped. When QRIS+pay-only is selected on the Run page, an available account is automatically claimed from here to register ChatGPT and proceed with 1 IDR trial.
        </p>

        <div class="ol-stats">
          <div class="stat" :class="{ ok: stats.available > 0 }">
            <strong>{{ stats.available }}</strong><span>available</span>
          </div>
          <div class="stat"><strong>{{ stats.in_use }}</strong><span>in_use</span></div>
          <div class="stat"><strong>{{ stats.used }}</strong><span>used</span></div>
          <div class="stat err"><strong>{{ stats.dead }}</strong><span>dead</span></div>
          <div class="stat"><strong>{{ stats.total }}</strong><span>total</span></div>
        </div>

        <textarea
          v-model="text"
          class="ol-input"
          rows="10"
          placeholder="email1@outlook.jp----pwd1----9e5f...----M.C5...
email2@outlook.com----pwd2----9e5f...----M.C5...
# Comment lines can be added"
        />

        <div class="ol-actions">
          <button class="header-btn" :disabled="busy || !text.trim()" @click="doImport">
            Import to DB
          </button>
          <button class="header-btn ghost" @click="loadList">Refresh List</button>
          <button class="header-btn ghost" :disabled="busy || revalidating" @click="doRevalidateAll">
            {{ revalidating ? "Validating..." : "Revalidate All Pool (RT+IMAP)" }}
          </button>
          <button class="header-btn ghost" :disabled="!!refreshing || deadCount === 0" @click="doBatchRefresh">
            <template v-if="refreshing">
              Batch Refreshing {{ refreshProgress.done }}/{{ refreshProgress.total }} ({{ refreshing }})
            </template>
            <template v-else>
              Batch Refresh Invalid RT ({{ deadCount }} dead)
            </template>
          </button>
          <span v-if="lastImport" class="ol-imp-msg">
            Parsed {{ lastImport.parsed }} / New {{ lastImport.inserted }} / Updated {{ lastImport.updated }} / Skipped {{ lastImport.skipped }}
            <template v-if="lastImport.validated">
              · <span :class="lastImport.invalid_imap ? 'ol-imp-bad' : 'ol-imp-good'">
                IMAP Validation {{ lastImport.valid_imap }} passed / {{ lastImport.invalid_imap }} rejected
              </span>
            </template>
          </span>
          <div v-if="lastImport && lastImport.fail_reasons && Object.keys(lastImport.fail_reasons).length" class="ol-imp-reasons">
            <div class="ol-imp-reasons-title">Failure Reasons Summary:</div>
            <div v-for="(n, reason) in lastImport.fail_reasons" :key="reason" class="ol-imp-reasons-row">
              <span class="ol-imp-reasons-n">{{ n }} items</span>
              <span>{{ reason }}</span>
            </div>
          </div>
        </div>
        <div v-if="refreshLog.length" class="ol-refresh-log">
          <div v-for="(line, i) in refreshLog.slice().reverse()" :key="i" :class="`refresh-line refresh-${line.kind}`">
            [{{ line.time }}] {{ line.text }}
          </div>
        </div>

        <div class="term-divider" data-tail="──────────">List</div>
        <div class="ol-filter">
          <label>Status Filter:</label>
          <select v-model="statusFilter" @change="loadList">
            <option value="">All</option>
            <option value="available">available</option>
            <option value="in_use">in_use</option>
            <option value="used">used</option>
            <option value="dead">dead</option>
          </select>
        </div>
        <div v-if="items.length === 0" class="ol-empty">
          {{ statusFilter ? `No ${statusFilter} status accounts` : "Pool is empty, import first" }}
        </div>
        <table v-else class="ol-table">
          <thead>
            <tr>
              <th>email</th>
              <th>status</th>
              <th>import time</th>
              <th>usage time</th>
              <th>chatgpt email</th>
              <th>error</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in items" :key="row.email" :class="`row-${row.status}`">
              <td><code>{{ row.email }}</code></td>
              <td><span class="badge" :class="`badge-${row.status}`">{{ row.status }}</span></td>
              <td>{{ formatTs(row.imported_at) }}</td>
              <td>{{ formatTs(row.used_at) }}</td>
              <td>{{ row.chatgpt_email || "—" }}</td>
              <td>{{ row.fail_reason || "—" }}</td>
              <td>
                <button class="link-btn" :disabled="refreshing === row.email" @click="doRefreshOne(row.email)" :title="`OAuth Code Flow to get new refresh_token (~30s)`">
                  {{ refreshing === row.email ? "Refreshing..." : "Refresh RT" }}
                </button>
                <button class="link-btn danger" @click="doDelete(row.email)" :disabled="!!refreshing">Delete</button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api/client";

const router = useRouter();

const text = ref("");
const items = ref<any[]>([]);
const stats = ref({ available: 0, in_use: 0, used: 0, dead: 0, total: 0 });
const lastImport = ref<any>(null);
const busy = ref(false);
const statusFilter = ref("");

// OAuth refresh-rt status
const revalidating = ref(false);
const refreshing = ref<string>("");  // Currently running email; empty = idle
const refreshProgress = ref({ done: 0, total: 0 });
const refreshLog = ref<Array<{ time: string; text: string; kind: "ok" | "err" | "info" }>>([]);
const deadCount = computed(() => stats.value.dead || 0);

function pushLog(text: string, kind: "ok" | "err" | "info" = "info") {
  const d = new Date();
  const time = `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}:${d.getSeconds().toString().padStart(2,'0')}`;
  refreshLog.value.push({ time, text, kind });
  if (refreshLog.value.length > 50) refreshLog.value.shift();
}

async function doRevalidateAll() {
  if (revalidating.value) return;
  revalidating.value = true;
  pushLog(`Starting pool revalidation (concurrency 8, non-used emails)...`, "info");
  try {
    const r = await api.post<{
      scanned: number; valid_imap: number; invalid_imap: number;
      transitions: Array<{ email: string; from: string; to: string }>;
      fail_reasons: Record<string, number>; elapsed: number;
    }>("/outlook/revalidate-all", { include_used: false, concurrency: 8 }, { timeout: 600000 });
    const d = r.data;
    pushLog(`✓ Completed ${d.scanned} accounts / ${d.valid_imap} passed / ${d.invalid_imap} rejected (${d.elapsed}s, concurrency 8)`,
            d.invalid_imap > 0 ? "info" : "ok");
    for (const t of d.transitions.slice(0, 20)) {
      const kind = t.to === "available" ? "ok" : (t.to === "dead" ? "err" : "info");
      pushLog(`  ${t.email}: ${t.from} → ${t.to}`, kind);
    }
    if (d.transitions.length > 20) {
      pushLog(`  ...and ${d.transitions.length - 20} more status changes not listed`, "info");
    }
    if (Object.keys(d.fail_reasons).length) {
      pushLog(`Failure reason distribution:`, "info");
      for (const [reason, n] of Object.entries(d.fail_reasons)) {
        pushLog(`  [${n}] ${reason}`, "err");
      }
    }
    await loadList();
  } catch (e: any) {
    pushLog(`✗ Validation failed: ${e?.response?.data?.detail || e.message}`, "err");
  } finally {
    revalidating.value = false;
  }
}

async function doRefreshOne(email: string): Promise<boolean> {
  if (refreshing.value) {
    alert("A refresh task is already running, please wait for it to complete");
    return false;
  }
  refreshing.value = email;
  pushLog(`${email}: Starting OAuth Code Flow (~30s)...`, "info");
  try {
    const r = await api.post<{ ok: boolean; email: string; error?: string; new_rt_prefix?: string; imap_alive?: boolean; status?: string }>(
      "/outlook/refresh-rt",
      { email },
      { timeout: 120000 },
    );
    const d = r.data;
    if (d.ok) {
      pushLog(`${email}: ✓ Success → status=available, IMAP passed, RT=${d.new_rt_prefix}`, "ok");
      await loadList();
      return true;
    } else if (d.new_rt_prefix && d.imap_alive === false) {
      pushLog(`${email}: △ Got RT but IMAP still rejected → marked dead (${d.error})`, "err");
      await loadList();
      return false;
    } else {
      pushLog(`${email}: ✗ ${d.error}`, "err");
      return false;
    }
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e.message || String(e);
    pushLog(`${email}: ✗ HTTP/Network ${detail}`, "err");
    return false;
  } finally {
    refreshing.value = "";
  }
}

async function doBatchRefresh() {
  // Get all current dead emails; run individually (avoid container Firefox resource contention)
  const r = await api.get("/outlook/list", { params: { limit: 500, status: "dead" } });
  const deadEmails: string[] = (r.data?.items || []).map((x: any) => x.email);
  if (!deadEmails.length) {
    pushLog("No dead emails to refresh", "info");
    return;
  }
  if (!confirm(`Will run OAuth Code Flow individually to refresh refresh_token for ${deadEmails.length} dead emails, estimated ~${Math.ceil(deadEmails.length * 30 / 60)} minutes. Accounts that successfully pass IMAP will be restored to available. Continue?`)) {
    return;
  }
  refreshProgress.value = { done: 0, total: deadEmails.length };
  pushLog(`Batch refresh started: ${deadEmails.length} dead emails`, "info");
  let ok = 0;
  for (const email of deadEmails) {
    await doRefreshOne(email);
    refreshProgress.value.done += 1;
    if (refreshLog.value[refreshLog.value.length - 1]?.kind === "ok") ok += 1;
  }
  pushLog(`Batch refresh completed: ${ok}/${deadEmails.length} successful (others IMAP still rejected, see details above)`, "info");
  refreshProgress.value = { done: 0, total: 0 };
}

const clock = ref("");
let clockTimer: any = null;
function tick() {
  const d = new Date();
  clock.value = `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}:${d.getSeconds().toString().padStart(2,'0')}`;
}

async function loadList() {
  try {
    const r = await api.get("/outlook/list", { params: { limit: 500, status: statusFilter.value } });
    items.value = r.data.items || [];
    stats.value = r.data.stats || stats.value;
  } catch (e: any) {
    console.warn("loadList fail", e);
  }
}

async function doImport() {
  if (!text.value.trim()) return;
  busy.value = true;
  try {
    // Automatically runs RT + IMAP validation during import (~5s/account), N accounts ~N*5s blocking, timeout set generously
    const r = await api.post("/outlook/import", { text: text.value }, { timeout: 600000 });
    lastImport.value = r.data;
    text.value = "";
    await loadList();
  } catch (e: any) {
    alert("Import failed: " + (e?.response?.data?.detail || e.message));
  } finally {
    busy.value = false;
  }
}

async function doDelete(email: string) {
  if (!confirm(`Confirm deleting ${email}?`)) return;
  try {
    await api.delete(`/outlook/${encodeURIComponent(email)}`);
    await loadList();
  } catch (e: any) {
    alert("Delete failed: " + (e?.response?.data?.detail || e.message));
  }
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

onMounted(() => {
  tick();
  clockTimer = setInterval(tick, 1000);
  loadList();
});
onBeforeUnmount(() => {
  if (clockTimer) clearInterval(clockTimer);
});
</script>

<style scoped>
.ol-root { min-height: 100vh; background: var(--bg-secondary, #f0ece1); display: flex; flex-direction: column; }
.wizard-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 20px; background: var(--bg-panel, #fff); border-bottom: 1px solid var(--border, #d4cdb9); }
.brand { display: flex; gap: 10px; align-items: center; font-family: JetBrains Mono, ui-monospace, monospace; }
.brand-prompt { color: var(--accent, #b25e1f); font-weight: bold; }
.brand-name { font-weight: bold; color: var(--fg, #1c1a15); }
.brand-sub { color: var(--fg-secondary, #7a7363); }
.brand-clock { margin-left: 16px; color: var(--fg-secondary); font-size: 12px; }
.run-nav { display: flex; gap: 8px; }
.nav-link { padding: 6px 12px; color: var(--fg-secondary); text-decoration: none; border: 1px solid transparent; }
.nav-link:hover { color: var(--accent); border-color: var(--accent); }
.header-btn { padding: 6px 14px; background: var(--accent, #b25e1f); color: white; border: 1px solid var(--accent); cursor: pointer; font-family: inherit; }
.header-btn:hover { background: var(--accent-hover, #8a4413); }
.header-btn.ghost { background: transparent; color: var(--accent); }
.header-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.ol-main { flex: 1; padding: 24px; }
.ol-panel { max-width: 1200px; margin: 0 auto; background: var(--bg-panel, #fff); padding: 20px; border: 1px solid var(--border, #d4cdb9); }
.ol-title { font-size: 20px; margin: 0 0 8px; }
.ol-sub { color: var(--fg-secondary); font-size: 13px; margin-bottom: 16px; line-height: 1.6; }
.ol-sub code { background: var(--bg-secondary); padding: 2px 6px; border: 1px solid var(--border); }
.ol-stats { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.stat { display: flex; flex-direction: column; align-items: center; padding: 8px 16px; border: 1px solid var(--border); background: var(--bg-secondary); min-width: 80px; }
.stat strong { font-size: 22px; color: var(--fg); }
.stat span { font-size: 11px; color: var(--fg-secondary); }
.stat.ok { border-color: var(--success, #1f6638); }
.stat.ok strong { color: var(--success); }
.stat.err strong { color: var(--error, #b91c1c); }
.ol-input { width: 100%; padding: 12px; font-family: JetBrains Mono, ui-monospace, monospace; font-size: 12px; border: 1px solid var(--border); background: var(--bg-input, #fff); resize: vertical; box-sizing: border-box; }
.ol-actions { display: flex; gap: 12px; align-items: center; margin: 12px 0 24px; }
.ol-imp-msg { color: var(--fg-secondary); font-size: 12px; }
.ol-imp-good { color: var(--success, #1f6638); font-weight: 600; }
.ol-imp-bad { color: var(--error, #b91c1c); font-weight: 600; }
.ol-imp-reasons { margin: 8px 0; padding: 8px 12px; background: var(--bg-secondary); border-left: 3px solid var(--error, #b91c1c); font-size: 12px; }
.ol-imp-reasons-title { font-weight: 600; margin-bottom: 4px; }
.ol-imp-reasons-row { padding: 2px 0; color: var(--fg-secondary); }
.ol-imp-reasons-n { display: inline-block; min-width: 40px; color: var(--error, #b91c1c); font-weight: 600; }
.ol-filter { display: flex; gap: 8px; align-items: center; margin: 12px 0; }
.ol-filter select { padding: 6px 10px; font-family: inherit; border: 1px solid var(--border); background: var(--bg-input); }
.ol-empty { padding: 30px; text-align: center; color: var(--fg-secondary); border: 1px dashed var(--border); }
.ol-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.ol-table th, .ol-table td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); }
.ol-table th { background: var(--bg-secondary); font-weight: 600; }
.ol-table code { font-family: JetBrains Mono, ui-monospace, monospace; }
.badge { padding: 2px 8px; font-size: 11px; }
.badge-available { background: #d6eedc; color: #1f6638; border: 1px solid #1f6638; }
.badge-in_use { background: #f7e9d6; color: #b25e1f; border: 1px solid #b25e1f; }
.badge-used { background: #e3e3e3; color: #555; border: 1px solid #888; }
.badge-dead { background: #fde0e0; color: #b91c1c; border: 1px solid #b91c1c; }
.link-btn { background: transparent; border: none; cursor: pointer; color: var(--fg-secondary); padding: 4px 8px; font-family: inherit; }
.link-btn:hover { color: var(--fg); }
.link-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.link-btn.danger:hover { color: var(--error, #b91c1c); }
.ol-refresh-log { background: #1c1a15; color: #d4cdb9; font-family: JetBrains Mono, ui-monospace, monospace; font-size: 11px; padding: 8px 12px; max-height: 220px; overflow-y: auto; margin: 8px 0 16px; border: 1px solid var(--border); }
.refresh-line { line-height: 1.5; }
.refresh-ok { color: #5ec77a; }
.refresh-err { color: #f06464; }
.refresh-info { color: #d4cdb9; }
.term-divider { font-family: JetBrains Mono, monospace; color: var(--fg-secondary); margin: 16px 0 8px; font-size: 12px; }
.term-divider::after { content: " " attr(data-tail); color: var(--border); }
.term-cursor::after { content: "_"; animation: blink 1s infinite; }
@keyframes blink { 50% { opacity: 0; } }
</style>