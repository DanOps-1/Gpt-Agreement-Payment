<template>
  <div class="run-root">
    <header class="wizard-header">
      <div class="brand">
        <span class="brand-prompt">$</span>
        <span class="brand-name">gpt-pay</span>
        <span class="brand-sub">// RUN CONTROL</span>
        <span class="brand-clock">{{ clock }}</span>
      </div>
      <div class="run-nav">
        <RouterLink to="/wizard" class="nav-link">Wizard</RouterLink>
        <RouterLink to="/run" class="nav-link active">Run</RouterLink>
        <button class="header-btn" @click="logout">Logout</button>
      </div>
    </header>

    <div class="run-body">
      <section class="run-controls">
        <div class="term-divider" data-tail="──────────">PARAMS</div>
        <div class="form-stack">
          <div class="ctl-row">
            <span class="ctl-label">MODE</span>
            <div class="mode-pills">
              <button
                v-for="m in modes"
                :key="m.value"
                class="mode-pill"
                :class="{ active: form.mode === m.value }"
                :disabled="status.running"
                @click="form.mode = m.value"
              >{{ m.label }}</button>
            </div>
          </div>

          <div v-if="form.mode === 'batch'" class="ctl-row sub">
            <TermField v-model.number="form.batch" label="batch N" type="number" />
            <TermField v-model.number="form.workers" label="workers" type="number" />
          </div>
          <div v-if="form.mode === 'self_dealer'" class="ctl-row sub">
            <TermField v-model.number="form.self_dealer" label="member N" type="number" />
          </div>
          <div v-if="form.mode === 'free_register'" class="ctl-row sub">
            <TermField v-model.number="form.count" label="Registration Count (0=unlimited)" type="number" />
          </div>

          <div v-if="!isFreeMode" class="ctl-row toggles">
            <TermToggle v-model="form.paypal" :disabled="form.gopay">PayPal Payment</TermToggle>
            <TermToggle v-model="form.gopay" @update:modelValue="onGoPayToggle">GoPay (IDN)</TermToggle>
          </div>
          <div v-if="!isFreeMode" class="ctl-row toggles">
            <TermToggle v-model="form.pay_only">--pay-only</TermToggle>
            <TermToggle v-model="form.register_only" :disabled="form.pay_only">--register-only</TermToggle>
          </div>
          <p v-if="!isFreeMode" class="ctl-hint">
            <code>--pay-only</code> Skips registration, prioritizes reusing recent unpaid accounts;
            <code>--register-only</code> Only register without payment.
          </p>
          <p v-else class="ctl-hint">
            <code>{{ form.mode }}</code> Skips payment; OTP retrieved via CF KV, OAuth to get RT then push to CPA using <code>cpa.free_plan_tag</code>.
          </p>
        </div>

        <div class="term-divider" data-tail="──────────">COMMAND</div>
        <pre class="cmd-preview">{{ cmdPreview }}</pre>

        <div v-if="configHealth" class="health-panel" :class="{ ok: configHealth.ok, fail: !configHealth.ok }">
          <div class="health-head">
            <span class="health-title">Config Health Check</span>
            <span class="badge" :class="configHealth.ok ? 'badge-ok' : 'badge-err'">
              {{ configHealth.ok ? "Startable" : "Blocked" }}
            </span>
            <span class="health-meta">{{ configHealth.payment_kind }} / {{ configHealth.requires_email_otp ? "Requires Email OTP" : "No Email OTP" }}</span>
          </div>
          <div class="health-list">
            <div v-for="chk in visibleHealthChecks" :key="chk.name" class="health-row" :class="`health-${chk.status}`">
              <span class="health-status">{{ healthStatusLabel(chk.status) }}</span>
              <div class="health-body">
                <strong>{{ chk.message }}</strong>
                <div v-if="chk.missing?.length" class="health-sub">Missing: {{ chk.missing.join(", ") }}</div>
                <div v-if="chk.action" class="health-sub">Action: {{ chk.action }}</div>
                <div v-if="chk.details" class="health-sub">Details: {{ chk.details }}</div>
              </div>
            </div>
          </div>
        </div>

        <div class="step-actions">
          <TermBtn variant="ghost" :loading="configHealthLoading" @click="checkConfigHealth">Check Config</TermBtn>
          <TermBtn v-if="!status.running" :loading="starting" @click="start">▶ Start Run</TermBtn>
          <TermBtn v-else variant="danger" :loading="stopping" @click="stop">■ Stop</TermBtn>
        </div>

        <div class="status-line" :class="{ running: status.running }">
          <span v-if="status.running">
            <span class="status-dot">●</span>
            Running PID {{ status.pid }} // Mode {{ status.mode }} // {{ runtimeText }}
          </span>
          <span v-else-if="status.ended_at">
            <span class="status-dot ok" v-if="status.exit_code === 0">●</span>
            <span class="status-dot err" v-else>●</span>
            Last run exited // Exit Code {{ status.exit_code }} //
            {{ runtimeText }}
          </span>
          <span v-else>
            <span class="status-dot idle">○</span> Idle
          </span>
        </div>
      </section>

      <section class="run-inventory">
        <div class="term-divider inventory-divider" data-tail="──────────">Account Inventory</div>
        <div class="inventory-head">
          <div class="inventory-meta">
            <span class="inventory-label">Last Refresh</span>
            <span class="inventory-value">{{ inventoryUpdatedText }}</span>
          </div>
          <TermBtn variant="ghost" :loading="inventoryLoading" @click="refreshInventory">Refresh Inventory</TermBtn>
        </div>
        <div v-if="inventoryError" class="inventory-error">
          Inventory refresh failed: {{ inventoryError }} . If you just updated the code, restart the backend <code>python -m webui.server</code>.
        </div>

        <div class="inventory-stats">
          <div class="inventory-stat">
            <span class="inventory-stat-label">Total Accounts</span>
            <strong>{{ inventory.counts.registered_total }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">Reusable</span>
            <strong>{{ inventory.counts.pay_only_eligible }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">Consumed</span>
            <strong>{{ inventory.counts.pay_only_consumed }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">Missing auth</span>
            <strong>{{ inventory.counts.pay_only_no_auth }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">Has RT</span>
            <strong>{{ inventory.counts.with_refresh_token }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">RT Missing</span>
            <strong>{{ inventory.counts.rt_missing }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">RT Cooldown</span>
            <strong>{{ inventory.counts.rt_cooldown }}</strong>
          </div>
        </div>

        <div v-if="inventory.accounts.length" class="inventory-toolbar">
          <label class="inventory-toolbar-check">
            <input type="checkbox" :checked="allSelected" @change="toggleSelectAll" />
            <span>Select All ({{ selectedIds.size }} / {{ inventory.accounts.length }})</span>
          </label>
          <div class="inventory-toolbar-actions">
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="selectedIds.size === 0" @click="verifySelected">Verify Selected</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="unknownOrUncheckedIds.length === 0" @click="verifyAllUnknown">Verify All Unknown ({{ unknownOrUncheckedIds.length }})</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="selectedIds.size === 0" @click="pushSelectedToCpa">Push Selected → CPA</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="unpushedIds.length === 0" @click="pushAllUnpushed">Push All Unpushed ({{ unpushedIds.length }})</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="selectedIds.size === 0" @click="deleteSelected">Delete Selected</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="invalidIds.length === 0" @click="deleteAllInvalid">Delete All Invalid ({{ invalidIds.length }})</TermBtn>
          </div>
        </div>

        <div v-if="inventory.accounts.length" class="inventory-list">
          <div v-for="acc in inventory.accounts" :key="acc.id || acc.email" class="inventory-row" :class="{ 'inventory-row--selected': isSelected(acc.id) }">
            <div class="inventory-row-top">
              <input type="checkbox" class="inventory-row-check" :checked="isSelected(acc.id)" @change="toggleSelect(acc.id)" />
              <span class="inventory-email">{{ acc.email }}</span>
              <span class="badge" :class="planBadgeClass(acc.plan_tag)">{{ planLabel(acc.plan_tag) }}</span>
              <span class="badge" :class="checkBadgeClass(acc.last_check_status)" :title="acc.last_check_message">
                <template v-if="checkingIds.has(acc.id)">⟳ Verifying...</template>
                <template v-else>{{ checkLabel(acc.last_check_status) }}</template>
              </span>
              <span class="badge" :class="payBadgeClass(acc.pay_state)">{{ payStateLabel(acc) }}</span>
              <span class="badge" :class="rtBadgeClass(acc.rt_state)">{{ rtStateLabel(acc) }}</span>
              <span class="badge" :class="cpaBadgeClass(acc)" :title="acc.cpa_status">{{ cpaLabel(acc) }}</span>
              <button v-if="!acc.cpa_pushed" class="inventory-row-action" :disabled="inventoryBusy" @click="pushOneToCpa(acc.id)">Push → CPA</button>
            </div>
            <div class="inventory-row-sub">
              <span>Registered {{ formatInventoryTs(acc.registered_at) }}</span>
              <span>attempts {{ acc.attempts }}</span>
              <span>auth {{ authSummary(acc) }}</span>
            </div>
            <div class="inventory-row-detail">
              <span>payment {{ acc.latest_payment_status || "—" }}</span>
              <span v-if="acc.latest_payment_source">source {{ acc.latest_payment_source }}</span>
              <span v-if="acc.latest_payment_error">error {{ acc.latest_payment_error }}</span>
              <span v-if="acc.oauth_status">oauth {{ acc.oauth_status }}<template v-if="acc.oauth_fail_reason"> ({{ acc.oauth_fail_reason }})</template></span>
              <span v-if="acc.oauth_cooldown_remaining_s">cooldown {{ formatCooldown(acc.oauth_cooldown_remaining_s) }}</span>
              <span v-if="acc.latest_payment_is_already_paid" class="inventory-inline-flag">already paid</span>
              <span v-if="acc.can_backfill_rt" class="inventory-inline-flag">can backfill rt</span>
            </div>
          </div>
        </div>
        <div v-else class="inventory-empty">
          No account inventory; Run a registration/payment first, then refresh after database sync.
        </div>
      </section>

      <Teleport to="body">
        <div v-if="otpDialog.open" class="otp-overlay" @click.self="() => {}">
          <div class="otp-modal">
            <div class="otp-head">
              <span class="otp-prompt">$</span> GoPay WhatsApp OTP
            </div>
            <p class="otp-desc">Check WhatsApp and enter the 6-digit OTP you just received. gopay.py will continue automatically after submission.</p>
            <input
              class="otp-input"
              v-model="otpDialog.value"
              maxlength="8"
              autofocus
              :disabled="otpDialog.submitting"
              @keyup.enter="submitOtp"
              placeholder="000000"
            />
            <div class="otp-actions">
              <TermBtn :loading="otpDialog.submitting" @click="submitOtp">Submit</TermBtn>
            </div>
          </div>
        </div>
      </Teleport>

      <section class="run-logs">
        <div class="logs-head">
          <span class="pre-prompt">$</span> Realtime Logs
          <span class="logs-meta">{{ lines.length }} lines</span>
          <label class="auto-scroll-toggle">
            <input type="checkbox" v-model="autoScroll" />
            <span>Auto-scroll</span>
          </label>
        </div>
        <div class="logs-stream" ref="streamEl">
          <div v-if="!lines.length" class="logs-empty">
            Waiting for run<span class="term-cursor"></span>
          </div>
          <div v-for="entry in lines" :key="entry.seq" class="log-line" :class="logClass(entry.line)">
            <span class="log-ts">{{ formatTs(entry.ts) }}</span>
            <span class="log-msg">{{ entry.line }}</span>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from "vue";
import { useRouter, RouterLink } from "vue-router";
import { useMessage, useDialog } from "naive-ui";
import { h } from "vue";
import { api } from "../api/client";
import TermField from "../components/term/TermField.vue";
import TermBtn from "../components/term/TermBtn.vue";
import TermToggle from "../components/term/TermToggle.vue";

const router = useRouter();
const message = useMessage();
const dialog = useDialog();

const modes = [
  { value: "single", label: "single — 1×" },
  { value: "batch", label: "batch — N×" },
  { value: "self_dealer", label: "self-dealer" },
  { value: "daemon", label: "daemon ∞" },
  { value: "free_register", label: "free_register — Free Account+RT+CPA" },
  { value: "free_backfill_rt", label: "free_backfill_rt — Account RT Backfill" },
];

import { useWizardStore } from "../stores/wizard";
const store = useWizardStore();

interface RunStatus {
  running: boolean;
  pid: number | null;
  mode: string | null;
  cmd: string[] | null;
  started_at: number | null;
  ended_at: number | null;
  exit_code: number | null;
  log_count: number;
  otp_pending?: boolean;
}

interface InventoryAccount {
  id: number;
  email: string;
  registered_at: string;
  attempts: number;
  has_session_token: boolean;
  has_access_token: boolean;
  has_device_id: boolean;
  has_refresh_token: boolean;
  pay_state: "reusable" | "consumed" | "no_auth";
  pay_only_eligible: boolean;
  rt_state: "has_rt" | "oauth_succeeded" | "dead" | "cooldown" | "retryable" | "missing";
  can_backfill_rt: boolean;
  oauth_status: string;
  oauth_fail_reason: string;
  oauth_updated_at: string;
  oauth_cooldown_remaining_s: number;
  latest_payment_status: string;
  latest_payment_source: string;
  latest_payment_error: string;
  latest_payment_is_already_paid: boolean;
  last_check_status: "" | "valid" | "invalid" | "unknown";
  last_check_message: string;
  last_check_at: number;
  plan_tag: "free" | "plus" | "team" | string;
  cpa_status: string;
  cpa_pushed: boolean;
}

interface InventoryResponse {
  generated_at: string;
  files: Record<string, string>;
  counts: {
    registered_total: number;
    raw_registered_rows: number;
    with_auth: number;
    pay_only_eligible: number;
    pay_only_consumed: number;
    pay_only_no_auth: number;
    with_refresh_token: number;
    rt_missing: number;
    rt_processed: number;
    rt_retryable: number;
    rt_cooldown: number;
    rt_dead: number;
  };
  accounts: InventoryAccount[];
}

interface ConfigHealthCheck {
  name: string;
  status: "ok" | "warn" | "fail";
  message: string;
  missing: string[];
  blocking: boolean;
  details: string;
  action: string;
}

interface ConfigHealthResponse {
  ok: boolean;
  mode: string;
  payment_kind: string;
  requires_registration: boolean;
  requires_email_otp: boolean;
  paths: Record<string, string>;
  checks: ConfigHealthCheck[];
  blocking: ConfigHealthCheck[];
}

const form = ref({
  mode: (router.currentRoute.value.query.mode as string) || "single",
  paypal: true,
  gopay: false,
  pay_only: false,
  register_only: false,
  batch: 5,
  workers: 3,
  self_dealer: 4,
  count: 0, // free_register mode: how many to register before stopping (0 = unlimited)
});

const otpDialog = ref({ open: false, value: "", submitting: false });

function onGoPayToggle(v: boolean) {
  if (v) form.value.paypal = false;
}

const status = ref<RunStatus>({
  running: false, pid: null, mode: null, cmd: null,
  started_at: null, ended_at: null, exit_code: null, log_count: 0,
});

const cmdPreview = ref("xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal");
const lines = ref<{ seq: number; ts: number; line: string }[]>([]);
const starting = ref(false);
const stopping = ref(false);
const configHealth = ref<ConfigHealthResponse | null>(null);
const configHealthLoading = ref(false);
const inventory = ref<InventoryResponse>({
  generated_at: "",
  files: {},
  counts: {
    registered_total: 0,
    raw_registered_rows: 0,
    with_auth: 0,
    pay_only_eligible: 0,
    pay_only_consumed: 0,
    pay_only_no_auth: 0,
    with_refresh_token: 0,
    rt_missing: 0,
    rt_processed: 0,
    rt_retryable: 0,
    rt_cooldown: 0,
    rt_dead: 0,
  },
  accounts: [],
});
const selectedIds = ref<Set<number>>(new Set());
const checkingIds = ref<Set<number>>(new Set());
const inventoryBusy = ref(false);
const autoScroll = ref(true);
const inventoryLoading = ref(false);
const inventoryError = ref("");
const streamEl = ref<HTMLElement | null>(null);
const clock = ref("");
let clockTimer: ReturnType<typeof setInterval> | undefined;
let statusTimer: ReturnType<typeof setInterval> | undefined;
let inventoryTimer: ReturnType<typeof setInterval> | undefined;
let eventSource: EventSource | null = null;

function tick() {
  const d = new Date();
  clock.value = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

const runtimeText = computed(() => {
  if (status.value.running && status.value.started_at) {
    const elapsed = Math.floor((Date.now() / 1000) - status.value.started_at);
    return formatElapsed(elapsed);
  }
  if (status.value.started_at && status.value.ended_at) {
    const elapsed = Math.floor(status.value.ended_at - status.value.started_at);
    return `Elapsed ${formatElapsed(elapsed)}`;
  }
  return "";
});

const inventoryUpdatedText = computed(() =>
  inventory.value.generated_at ? formatInventoryTs(inventory.value.generated_at) : "Not Refreshed"
);

const visibleHealthChecks = computed(() => {
  const checks = configHealth.value?.checks || [];
  const important = checks.filter((c) => c.status !== "ok" || c.blocking);
  return important.length ? important : checks;
});

function formatElapsed(s: number) {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const ss = s % 60;
  if (m < 60) return `${m}m${String(ss).padStart(2,'0')}s`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2,'0')}m`;
}

function formatTs(ts: number) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

function formatInventoryTs(ts: string) {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}

function formatCooldown(seconds: number) {
  return formatElapsed(Math.max(0, Math.floor(seconds)));
}

function authSummary(acc: InventoryAccount) {
  const parts: string[] = [];
  if (acc.has_session_token) parts.push("session");
  if (acc.has_access_token) parts.push("access");
  if (acc.has_device_id) parts.push("device");
  if (acc.has_refresh_token) parts.push("rt");
  return parts.length ? parts.join(" / ") : "none";
}

function payStateLabel(acc: InventoryAccount) {
  if (acc.pay_state === "reusable") return "Reusable";
  if (acc.pay_state === "consumed") return "Consumed";
  return "Auth missing";
}

function rtStateLabel(acc: InventoryAccount) {
  switch (acc.rt_state) {
    case "has_rt":
      return "RT available";
    case "oauth_succeeded":
      return "RT processed";
    case "dead":
      return "dead";
    case "cooldown":
      return "RT cooldown";
    case "retryable":
      return "Retryable";
    default:
      return "RT missing";
  }
}

function payBadgeClass(state: InventoryAccount["pay_state"]) {
  if (state === "reusable") return "badge-ok";
  if (state === "consumed") return "badge-err";
  return "badge-warn";
}

function rtBadgeClass(state: InventoryAccount["rt_state"]) {
  if (state === "has_rt" || state === "oauth_succeeded") return "badge-ok";
  if (state === "dead") return "badge-err";
  if (state === "missing") return "badge-ghost";
  return "badge-warn";
}

function healthStatusLabel(status: ConfigHealthCheck["status"]) {
  if (status === "ok") return "OK";
  if (status === "warn") return "WARN";
  return "FAIL";
}

function healthErrorText(payload: any) {
  const detail = payload?.response?.data?.detail;
  if (!detail) return payload?.message || "Config health check failed";
  if (typeof detail === "string") return detail;
  return detail.message || "Config health check failed";
}

function logClass(line: string) {
  if (/\b(ERROR|FAIL|TRACE|Traceback)\b/i.test(line)) return "log-err";
  if (/\b(WARN|WARNING)\b/i.test(line)) return "log-warn";
  if (/\b(OK|SUCCESS|✓|Complete|Success)\b/i.test(line)) return "log-ok";
  return "";
}

// ── Inventory: Select + Verify + Delete ─────────────────────────────────
function checkLabel(s: InventoryAccount["last_check_status"]) {
  if (s === "valid") return "✓ Valid";
  if (s === "invalid") return "✗ Invalid";
  if (s === "unknown") return "? Unknown";
  return "○ Unchecked";
}
function checkBadgeClass(s: InventoryAccount["last_check_status"]) {
  if (s === "valid") return "badge-ok";
  if (s === "invalid") return "badge-err";
  if (s === "unknown") return "badge-warn";
  return "badge-ghost";
}
function isSelected(id: number) { return selectedIds.value.has(id); }
function toggleSelect(id: number) {
  const next = new Set(selectedIds.value);
  if (next.has(id)) next.delete(id); else next.add(id);
  selectedIds.value = next;
}
const allSelected = computed(() => {
  const ids = inventory.value.accounts.map(a => a.id).filter(Boolean);
  return ids.length > 0 && ids.every(id => selectedIds.value.has(id));
});
function toggleSelectAll() {
  if (allSelected.value) {
    selectedIds.value = new Set();
  } else {
    selectedIds.value = new Set(inventory.value.accounts.map(a => a.id).filter(Boolean));
  }
}
const invalidIds = computed(() =>
  inventory.value.accounts.filter(a => a.last_check_status === "invalid").map(a => a.id)
);
const unknownOrUncheckedIds = computed(() =>
  inventory.value.accounts
    .filter(a => a.last_check_status === "" || a.last_check_status === "unknown")
    .map(a => a.id)
);

async function runCheck(ids: number[], label: string) {
  if (!ids.length) { message.warning(`No accounts available to ${label}`); return; }
  inventoryBusy.value = true;
  ids.forEach(id => checkingIds.value.add(id));
  try {
    const r = await api.post("/inventory/accounts/check", { ids });
    const s = r.data?.summary || {};
    message.success(`${label} completed: valid=${s.valid || 0}  invalid=${s.invalid || 0}  unknown=${s.unknown || 0}`);
    await refreshInventory();
  } catch (e: any) {
    message.error(`${label} failed: ${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    ids.forEach(id => checkingIds.value.delete(id));
    inventoryBusy.value = false;
  }
}
function verifySelected() {
  runCheck(Array.from(selectedIds.value), "Verify selected");
}
function verifyAllUnknown() {
  runCheck(unknownOrUncheckedIds.value, "Verify unchecked/unknown");
}

function emailsForIds(ids: number[]): string[] {
  const map = new Map(inventory.value.accounts.map(a => [a.id, a.email]));
  return ids.map(i => map.get(i) || `id=${i}`);
}
function confirmAndDelete(ids: number[], label: string) {
  if (!ids.length) { message.warning(`No accounts available to ${label}`); return; }
  const emails = emailsForIds(ids);
  const preview = emails.slice(0, 5).join(", ") + (emails.length > 5 ? `, ... total ${emails.length} accounts` : "");
  dialog.warning({
    title: `Confirm ${label}?`,
    content: () => h("div", { style: "font-size:12px; line-height:1.6" }, [
      h("div", `Will permanently delete ${ids.length}  accounts (pipeline_results / card_results audit rows are kept).`),
      h("div", { style: "margin-top:6px; color:#7a7363; word-break:break-all" }, preview),
    ]),
    positiveText: "Confirm Delete",
    negativeText: "Cancel",
    onPositiveClick: async () => {
      inventoryBusy.value = true;
      try {
        const r = await api.post("/inventory/accounts/delete", { ids });
        message.success(`Deleted ${r.data?.deleted ?? 0} `);
        selectedIds.value = new Set();
        await refreshInventory();
      } catch (e: any) {
        message.error(`Delete failed: ${e?.response?.data?.detail || e?.message || e}`);
      } finally {
        inventoryBusy.value = false;
      }
    },
  });
}
function deleteSelected() {
  confirmAndDelete(Array.from(selectedIds.value), "Delete selected");
}
function deleteAllInvalid() {
  confirmAndDelete(invalidIds.value, "Delete all invalid");
}

// ── plan + CPA push ─────────────────────────────────
function planLabel(p: string) {
  if (p === "team") return "team";
  if (p === "plus") return "plus";
  return "free";
}
function planBadgeClass(p: string) {
  if (p === "team") return "badge-team";
  if (p === "plus") return "badge-plus";
  return "badge-ghost";
}
function cpaLabel(acc: InventoryAccount) {
  if (acc.cpa_pushed) return "✓ CPA Pushed";
  if (acc.cpa_status && acc.cpa_status !== "ok") return `✗ ${acc.cpa_status}`;
  return "○ CPA Not Pushed";
}
function cpaBadgeClass(acc: InventoryAccount) {
  if (acc.cpa_pushed) return "badge-ok";
  if (acc.cpa_status && acc.cpa_status !== "ok") return "badge-err";
  return "badge-ghost";
}
const unpushedIds = computed(() =>
  inventory.value.accounts.filter(a => !a.cpa_pushed).map(a => a.id)
);

async function pushCpa(ids: number[], label: string) {
  if (!ids.length) { message.warning(`No accounts available to ${label}`); return; }
  inventoryBusy.value = true;
  try {
    const r = await api.post("/inventory/accounts/cpa-push", { ids });
    const s = r.data?.summary || {};
    message.success(`${label} completed: ok=${s.ok || 0}  no_rt=${s.no_rt || 0}  fail=${s.fail || 0}`);
    await refreshInventory();
  } catch (e: any) {
    message.error(`${label} failed: ${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    inventoryBusy.value = false;
  }
}
function pushOneToCpa(id: number) { pushCpa([id], "Push CPA"); }
function pushSelectedToCpa() { pushCpa(Array.from(selectedIds.value), "Batch push selected"); }
function pushAllUnpushed() { pushCpa(unpushedIds.value, "Push all unpushed"); }

async function refreshInventory() {
  if (inventoryLoading.value) return;
  inventoryLoading.value = true;
  inventoryError.value = "";
  try {
    const r = await api.get<InventoryResponse>("/inventory/accounts");
    inventory.value = r.data;
  } catch (e: any) {
    inventoryError.value = e?.response?.status
      ? `HTTP ${e.response.status}${e.response.data?.detail ? `: ${e.response.data.detail}` : ""}`
      : (e?.message || "Request failed");
  }
  finally {
    inventoryLoading.value = false;
  }
}

async function refreshPreview() {
  try {
    const r = await api.post("/run/preview", form.value);
    cmdPreview.value = r.data.cmd_str;
  } catch {}
}

async function refreshStatus() {
  try {
    const r = await api.get<RunStatus>("/run/status");
    status.value = r.data;
  } catch {}
}

async function checkConfigHealth() {
  if (configHealthLoading.value) return configHealth.value;
  configHealthLoading.value = true;
  try {
    const r = await api.post<ConfigHealthResponse>("/config/health", form.value);
    configHealth.value = r.data;
    return r.data;
  } catch (e: any) {
    message.error(healthErrorText(e));
    return null;
  } finally {
    configHealthLoading.value = false;
  }
}

async function start() {
  starting.value = true;
  try {
    const health = await checkConfigHealth();
    if (!health?.ok) {
      const first = health?.blocking?.[0];
      message.error(first?.message || "Config health check failed, startup blocked");
      return;
    }
    await api.post("/run/start", form.value);
    message.success("Started");
    lines.value = [];
    await refreshStatus();
    await refreshInventory();
    openStream();
  } catch (e: any) {
    message.error(healthErrorText(e) || "Start failed");
  } finally {
    starting.value = false;
  }
}

async function stop() {
  stopping.value = true;
  try {
    await api.post("/run/stop");
    message.success("SIGTERM sent");
    await refreshStatus();
  } catch (e: any) {
    message.error(e.response?.data?.detail || "Stop failed");
  } finally {
    stopping.value = false;
  }
}

function openStream() {
  if (eventSource) eventSource.close();
  const url = import.meta.env.BASE_URL + "api/run/stream";
  eventSource = new EventSource(url, { withCredentials: true });
  eventSource.addEventListener("line", (e) => {
    try {
      const entry = JSON.parse((e as MessageEvent).data);
      lines.value.push(entry);
      if (lines.value.length > 5000) lines.value.splice(0, 1000);
      if (autoScroll.value) {
        nextTick(() => {
          if (streamEl.value) streamEl.value.scrollTop = streamEl.value.scrollHeight;
        });
      }
    } catch {}
  });
  eventSource.addEventListener("otp_pending", () => {
    if (!otpDialog.value.open) {
      otpDialog.value.open = true;
      otpDialog.value.value = "";
    }
  });
  eventSource.addEventListener("done", async () => {
    eventSource?.close();
    eventSource = null;
    otpDialog.value.open = false;
    await refreshStatus();
    await refreshInventory();
  });
  eventSource.onerror = () => {
    // Connection closed, no auto retry
    eventSource?.close();
    eventSource = null;
  };
}

async function logout() {
  await api.post("/logout");
  router.push("/login");
}

async function submitOtp() {
  const v = otpDialog.value.value.trim();
  if (!v) {
    message.warning("Please enter OTP");
    return;
  }
  otpDialog.value.submitting = true;
  try {
    await api.post("/run/otp", { otp: v });
    otpDialog.value.open = false;
    otpDialog.value.value = "";
    message.success("OTP submitted");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "Submit failed");
  } finally {
    otpDialog.value.submitting = false;
  }
}

const isFreeMode = computed(() =>
  form.value.mode === "free_register" || form.value.mode === "free_backfill_rt"
);

watch(
  () => [form.value.mode, form.value.paypal, form.value.gopay, form.value.pay_only, form.value.register_only, form.value.batch, form.value.workers, form.value.self_dealer, form.value.count],
  () => {
    configHealth.value = null;
    refreshPreview();
  },
  { immediate: false }
);

onMounted(async () => {
  tick();
  clockTimer = setInterval(tick, 1000);

  // Infer default payment method from wizard store: card without --paypal, others with it
  try {
    await store.loadFromServer();
    const pm = (store.answers.payment as any)?.method;
    if (pm === "gopay") {
      form.value.gopay = true;
      form.value.paypal = false;
    } else if (pm === "card") {
      form.value.paypal = false;
    } else if (pm === "paypal" || pm === "both") {
      form.value.paypal = true;
    }
  } catch {}

  await refreshStatus();
  await refreshPreview();
  await checkConfigHealth();
  await refreshInventory();
  if (status.value.running) {
    openStream();
  }
  statusTimer = setInterval(refreshStatus, 5000);
  inventoryTimer = setInterval(refreshInventory, 15000);
});

onBeforeUnmount(() => {
  if (clockTimer) clearInterval(clockTimer);
  if (statusTimer) clearInterval(statusTimer);
  if (inventoryTimer) clearInterval(inventoryTimer);
  if (eventSource) eventSource.close();
});
</script>

<style scoped>
.run-root { height: 100vh; overflow: hidden; display: flex; flex-direction: column; }

.wizard-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px;
  border-bottom: 1px solid var(--border);
}
.brand { display: flex; align-items: baseline; gap: 10px; }
.brand-prompt { color: var(--accent); }
.brand-name { font-weight: 700; font-size: 18px; letter-spacing: 0.04em; }
.brand-sub { color: var(--fg-tertiary); font-size: 12px; }
.brand-clock { color: var(--fg-tertiary); font-size: 11px; margin-left: 16px; font-variant-numeric: tabular-nums; }

.run-nav { display: flex; align-items: center; gap: 4px; }
.nav-link { padding: 6px 14px; color: var(--fg-secondary); text-decoration: none; font-size: 12px; letter-spacing: 0.06em; border: 1px solid transparent; transition: all 80ms; }
.nav-link:hover { color: var(--fg-primary); background: var(--bg-panel); }
.nav-link.active { color: var(--accent); border-color: var(--accent); background: var(--bg-panel); }
.header-btn { background: transparent; border: 1px solid var(--border-strong); color: var(--fg-secondary); padding: 4px 12px; font: inherit; font-size: 11px; letter-spacing: 0.08em; cursor: pointer; transition: all 60ms; margin-left: 12px; }
.header-btn:hover { background: var(--bg-raised); color: var(--fg-primary); border-color: var(--accent); }

.run-body { flex: 1; display: grid; grid-template-columns: 420px minmax(420px, 1fr) minmax(360px, 1fr); gap: 0; min-height: 0; overflow: hidden; }
.run-controls { padding: 24px; overflow-y: auto; border-right: 1px solid var(--border); }
.run-inventory { padding: 20px 22px; overflow-y: auto; border-right: 1px solid var(--border); background: var(--bg-base); min-height: 0; }
.run-logs { display: flex; flex-direction: column; min-height: 0; overflow: hidden; background: var(--bg-panel); }
@media (max-width: 1280px) {
  .run-body { grid-template-columns: 380px 1fr; grid-template-rows: minmax(0, 1fr) minmax(0, 1fr); }
  .run-controls { grid-row: 1 / span 2; }
  .run-inventory { border-right: 0; border-bottom: 1px solid var(--border); }
}

.form-stack { display: flex; flex-direction: column; gap: 12px; margin-bottom: 8px; }
.ctl-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.ctl-row.sub { padding-left: 8px; border-left: 2px solid var(--border-strong); }
.ctl-row.toggles { margin-top: 4px; gap: 16px; flex-wrap: wrap; }
.ctl-hint { color: var(--fg-tertiary); font-size: 11px; line-height: 1.6; margin: 4px 0 0; }
.ctl-hint code { background: var(--bg-panel); padding: 1px 5px; border: 1px solid var(--border); font-size: 11px; }
.ctl-label { font-size: 11px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: var(--fg-secondary); min-width: 60px; }

.mode-pills { display: flex; gap: 0; border: 1px solid var(--border-strong); flex-wrap: wrap; }
.mode-pill { background: #fff; border: 0; border-right: 1px solid var(--border); padding: 8px 14px; font: inherit; font-size: 12px; cursor: pointer; color: var(--fg-secondary); transition: all 80ms; }
.mode-pill:last-child { border-right: 0; }
.mode-pill:hover:not(:disabled) { background: var(--bg-raised); color: var(--fg-primary); }
.mode-pill.active { background: var(--accent); color: #fff; }
.mode-pill:disabled { cursor: not-allowed; opacity: 0.5; }

.cmd-preview {
  background: var(--bg-panel);
  border: 1px solid var(--border-strong);
  padding: 12px 14px;
  font-size: 12px;
  color: var(--fg-primary);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  line-height: 1.6;
}

.step-actions { margin-top: 16px; margin-bottom: 0; }

.health-panel {
  margin-top: 12px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  padding: 12px;
  font-size: 12px;
}
.health-panel.ok { border-color: color-mix(in srgb, var(--ok) 45%, var(--border)); }
.health-panel.fail { border-color: color-mix(in srgb, var(--err) 55%, var(--border)); }
.health-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.health-title {
  color: var(--fg-primary);
  font-weight: 700;
}
.health-meta {
  margin-left: auto;
  color: var(--fg-tertiary);
  font-size: 11px;
}
.health-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.health-row {
  display: grid;
  grid-template-columns: 54px 1fr;
  gap: 10px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}
.health-status {
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.08em;
}
.health-ok .health-status { color: var(--ok); }
.health-warn .health-status { color: var(--warn); }
.health-fail .health-status { color: var(--err); }
.health-body strong {
  color: var(--fg-primary);
  font-weight: 700;
}
.health-sub {
  margin-top: 3px;
  color: var(--fg-tertiary);
  line-height: 1.55;
  word-break: break-word;
}

.status-line { margin-top: 16px; padding: 10px 12px; background: var(--bg-panel); border: 1px solid var(--border); font-size: 12px; color: var(--fg-secondary); }
.status-line.running { border-color: var(--accent); }
.status-dot { color: var(--fg-tertiary); margin-right: 6px; }
.status-line.running .status-dot { color: var(--accent); animation: pulse 1.2s ease-in-out infinite; }
.status-dot.ok { color: var(--ok); }
.status-dot.err { color: var(--err); }
.status-dot.idle { color: var(--fg-tertiary); }

.inventory-divider { margin-top: 22px; }
.inventory-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.inventory-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.inventory-label {
  color: var(--fg-tertiary);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.inventory-value {
  color: var(--fg-secondary);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.inventory-error {
  margin-bottom: 12px;
  border: 1px solid var(--warn);
  background: color-mix(in srgb, var(--warn) 12%, var(--bg-panel));
  color: var(--warn);
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.6;
}
.inventory-stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}
.inventory-stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  padding: 10px 12px;
}
.inventory-stat-label {
  color: var(--fg-tertiary);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.inventory-stat strong {
  color: var(--fg-primary);
  font-size: 18px;
  font-variant-numeric: tabular-nums;
}
.inventory-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 8px 0 4px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  flex-wrap: wrap;
}
.inventory-toolbar-check {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--fg-secondary);
  cursor: pointer;
  user-select: none;
}
.inventory-toolbar-check input { accent-color: var(--accent); }
.inventory-toolbar-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.inventory-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 4px;
}
.inventory-row {
  border: 1px solid var(--border);
  background: var(--bg-panel);
  padding: 12px;
}
.inventory-row--selected {
  border-color: var(--accent);
  background: #fff7ec;
}
.inventory-row-check {
  margin-right: 4px;
  accent-color: var(--accent);
  cursor: pointer;
}
.inventory-row-top {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.inventory-email {
  font-weight: 700;
  color: var(--fg-primary);
  word-break: break-all;
}
.inventory-row-sub,
.inventory-row-detail {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  color: var(--fg-tertiary);
  font-size: 11px;
  line-height: 1.6;
  word-break: break-word;
}
.inventory-row-detail {
  margin-top: 6px;
}
.inventory-inline-flag {
  color: var(--accent);
}
.inventory-empty {
  border: 1px dashed var(--border);
  background: var(--bg-panel);
  color: var(--fg-tertiary);
  padding: 14px;
  font-size: 12px;
  line-height: 1.7;
}
.badge {
  display: inline-flex;
  align-items: center;
  min-height: 20px;
  padding: 0 8px;
  border: 1px solid var(--border);
  color: var(--fg-secondary);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  white-space: nowrap;
}
.badge-ok { border-color: var(--ok); color: var(--ok); }
.badge-warn { border-color: var(--warn); color: var(--warn); }
.badge-err { border-color: var(--err); color: var(--err); }
.badge-ghost { border-color: var(--border); color: var(--fg-tertiary); }
.badge-plus { border-color: #2563eb; color: #2563eb; }
.badge-team { border-color: #7c3aed; color: #7c3aed; }
.inventory-row-action {
  margin-left: auto;
  padding: 3px 9px;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px;
  background: transparent;
  border: 1px solid var(--accent);
  color: var(--accent);
  cursor: pointer;
  transition: background .15s, color .15s;
}
.inventory-row-action:hover:not(:disabled) {
  background: var(--accent);
  color: #fff;
}
.inventory-row-action:disabled {
  opacity: .5;
  cursor: not-allowed;
}
@keyframes pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 1; }
}

.logs-head { padding: 12px 16px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; color: var(--accent); font-weight: 700; font-size: 12px; letter-spacing: 0.06em; }
.pre-prompt { color: var(--fg-tertiary); }
.logs-meta { color: var(--fg-tertiary); font-size: 11px; font-weight: 400; }
.auto-scroll-toggle { margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--fg-secondary); cursor: pointer; user-select: none; font-weight: 400; letter-spacing: 0; }
.auto-scroll-toggle input { accent-color: var(--accent); }

.logs-stream { flex: 1; overflow-y: auto; padding: 8px 16px 12px; font-size: 11px; background: var(--bg-base); }
.logs-empty { color: var(--fg-tertiary); padding: 32px 0; text-align: center; }
.log-line { display: grid; grid-template-columns: 70px 1fr; gap: 10px; padding: 1px 0; align-items: baseline; }
.log-ts { color: var(--fg-tertiary); font-variant-numeric: tabular-nums; font-size: 10px; }
.log-msg { color: var(--fg-primary); white-space: pre-wrap; word-break: break-all; }
.log-line.log-err .log-msg { color: var(--err); }
.log-line.log-warn .log-msg { color: var(--warn); }
.log-line.log-ok .log-msg { color: var(--ok); }


/* GoPay OTP modal */
.otp-overlay {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
}
.otp-modal {
  background: var(--bg-base);
  border: 1px solid var(--accent);
  padding: 24px 28px;
  width: min(420px, 90vw);
  font-family: inherit;
  box-shadow: 0 10px 40px rgba(0,0,0,0.25);
}
.otp-head {
  font-weight: 700;
  font-size: 14px;
  letter-spacing: 0.06em;
  color: var(--accent);
  margin-bottom: 4px;
}
.otp-prompt { color: var(--fg-tertiary); margin-right: 6px; }
.otp-desc { color: var(--fg-secondary); font-size: 12px; line-height: 1.6; margin: 8px 0 16px; }
.otp-input {
  width: 100%; box-sizing: border-box;
  padding: 12px 14px;
  border: 1px solid var(--border-strong);
  background: var(--bg-panel);
  font: inherit; font-size: 22px;
  letter-spacing: 0.4em;
  text-align: center;
  color: var(--fg-primary);
  outline: none;
  font-variant-numeric: tabular-nums;
}
.otp-input:focus { border-color: var(--accent); }
.otp-actions { margin-top: 16px; display: flex; justify-content: flex-end; }

@media (max-width: 1024px) {
  .inventory-stats { grid-template-columns: 1fr; }
}
@media (max-width: 900px) {
  .run-body { grid-template-columns: 1fr; grid-template-rows: auto auto 1fr; }
  .run-controls, .run-inventory { grid-row: auto; border-right: 0; border-bottom: 1px solid var(--border); }
  .inventory-head { align-items: flex-start; flex-direction: column; }
}
</style>
