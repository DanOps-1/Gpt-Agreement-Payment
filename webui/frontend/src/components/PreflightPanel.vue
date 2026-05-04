<template>
  <aside class="preflight">
    <div class="pre-head">
      <span class="pre-prompt">$</span> Preflight Status <span class="pre-tail">{{ logCount }} logs</span>
    </div>

    <div class="pre-summary">
      <span v-for="row in summary" :key="row.name" class="pre-pill" :class="row.status" :title="row.message">
        <span class="pp-dot">{{ symbol(row.status) }}</span>
        <span class="pp-name">{{ row.name }}</span>
      </span>
    </div>

    <div class="pre-divider">// Log Stream</div>

    <div class="pre-stream" ref="streamEl">
      <div v-if="!log.length" class="pre-empty">
        Waiting for first preflight<span class="term-cursor"></span>
      </div>
      <transition-group name="logrow" tag="div">
        <div v-for="e in log" :key="e.ts + e.name" class="pre-line" :class="e.status">
          <span class="pl-ts">{{ formatTs(e.ts) }}</span>
          <span class="pl-arrow">›</span>
          <span class="pl-name">{{ e.name }}</span>
          <span class="pl-sep">::</span>
          <span class="pl-status">{{ e.status }}</span>
          <span class="pl-msg">{{ e.message }}</span>
        </div>
      </transition-group>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useWizardStore } from "../stores/wizard";

const store = useWizardStore();
const streamEl = ref<HTMLElement | null>(null);

const ROW_LABELS: { name: string; key: string }[] = [
  { name: "system", key: "system" },
  { name: "cf", key: "cloudflare" },
  { name: "cf-kv", key: "cloudflare_kv" },
  { name: "proxy", key: "proxy" },
  { name: "webshare", key: "webshare" },
  { name: "card", key: "card" },
  { name: "captcha", key: "captcha" },
  { name: "vlm", key: "vlm" },
  { name: "team", key: "team_system" },
  { name: "cpa", key: "cpa" },
];

const summary = computed(() =>
  ROW_LABELS.map((r) => {
    const result = store.preflight[r.key];
    return {
      name: r.name,
      status: result?.status ?? "pending",
      message: result?.message ?? "Not running",
    };
  })
);

const log = computed(() => [...store.preflightLog].reverse());
const logCount = computed(() => store.preflightLog.length);

function symbol(s: string) {
  return s === "ok" ? "●" : s === "fail" ? "✗" : s === "warn" ? "▲" : "○";
}
function formatTs(ts: number) {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}

watch(() => store.preflightLog.length, async () => {
  await nextTick();
  if (streamEl.value) streamEl.value.scrollTop = 0;
});
</script>

<style scoped>
.preflight {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  font-size: 12px;
  height: 100%;
  min-height: 0;
}
.pre-head {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  color: var(--accent);
  font-weight: 700;
  letter-spacing: 0.06em;
  display: flex;
  align-items: center;
  gap: 8px;
}
.pre-prompt { color: var(--fg-tertiary); }
.pre-tail { margin-left: auto; color: var(--fg-tertiary); font-size: 11px; font-weight: 400; }

.pre-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 10px 14px 8px;
  border-bottom: 1px dashed var(--border);
}
.pre-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  background: var(--bg-base);
  border: 1px solid var(--border);
  font-size: 11px;
  cursor: default;
}
.pre-pill .pp-dot { color: var(--fg-tertiary); }
.pre-pill .pp-name { color: var(--fg-secondary); }
.pre-pill.ok { border-color: var(--ok); }
.pre-pill.ok .pp-dot, .pre-pill.ok .pp-name { color: var(--ok); }
.pre-pill.fail { border-color: var(--err); }
.pre-pill.fail .pp-dot, .pre-pill.fail .pp-name { color: var(--err); }
.pre-pill.warn { border-color: var(--warn); }
.pre-pill.warn .pp-dot, .pre-pill.warn .pp-name { color: var(--warn); }

.pre-divider {
  padding: 10px 14px 4px;
  color: var(--fg-tertiary);
  font-size: 11px;
  letter-spacing: 0.05em;
}

.pre-stream {
  flex: 1;
  overflow-y: auto;
  padding: 4px 14px 12px;
  font-size: 11px;
  display: flex;
  flex-direction: column;
}
.pre-empty {
  color: var(--fg-tertiary);
  padding: 24px 0;
  text-align: center;
}
.pre-line {
  display: grid;
  grid-template-columns: auto auto auto auto auto 1fr;
  gap: 6px;
  padding: 3px 0;
  align-items: baseline;
  white-space: nowrap;
  overflow: hidden;
}
.pl-ts { color: var(--fg-tertiary); font-variant-numeric: tabular-nums; }
.pl-arrow { color: var(--accent); }
.pl-name { color: var(--fg-primary); font-weight: 700; }
.pl-sep { color: var(--fg-tertiary); }
.pl-status { font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }
.pl-msg { color: var(--fg-tertiary); overflow: hidden; text-overflow: ellipsis; }
.pre-line.ok .pl-status { color: var(--ok); }
.pre-line.fail .pl-status { color: var(--err); }
.pre-line.warn .pl-status { color: var(--warn); }

/* tail -f Scroll-in animation */
.logrow-enter-active { transition: all 220ms ease-out; }
.logrow-enter-from {
  opacity: 0;
  transform: translateX(-12px);
  background: var(--accent-dim);
}
.logrow-enter-to {
  background: transparent;
}

::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-strong); }
::-webkit-scrollbar-thumb:hover { background: var(--accent-dim); }
</style>
