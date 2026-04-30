<template>
  <div class="wa-root">
    <header class="wizard-header">
      <div class="brand">
        <span class="brand-prompt">$</span>
        <span class="brand-name">gpt-pay</span>
        <span class="brand-sub">// WhatsApp 集成</span>
        <span class="brand-clock">{{ clock }}</span>
      </div>
      <div class="run-nav">
        <RouterLink to="/wizard" class="nav-link">配置向导</RouterLink>
        <RouterLink to="/run" class="nav-link">运行</RouterLink>
        <RouterLink to="/whatsapp" class="nav-link active">WhatsApp</RouterLink>
        <button class="header-btn" @click="logout">退出</button>
      </div>
    </header>

    <main class="wa-main">
      <section class="wa-status">
        <div class="term-divider" data-tail="──────────">状态</div>
        <div class="status-card" :class="`status-${connectClass}`">
          <div class="status-line">
            <span class="status-dot"></span>
            <span class="status-text">{{ statusLabel }}</span>
          </div>
          <div v-if="status.pushname" class="status-meta">登录账号：{{ status.pushname }}</div>
          <div v-if="status.wid" class="status-meta">wid：{{ status.wid }}</div>
          <div v-if="status.error" class="status-meta status-meta--err">{{ status.error }}</div>
          <div class="status-meta">PID {{ status.pid ?? '—' }} · mode {{ status.mode || '—' }}</div>
        </div>
      </section>

      <section class="wa-login" v-if="status.status !== 'connected'">
        <div class="term-divider" data-tail="──────────">登录方式</div>

        <div class="tabs">
          <button :class="{ tab: true, active: tab === 'qr' }" @click="tab = 'qr'">扫码</button>
          <button :class="{ tab: true, active: tab === 'pairing' }" @click="tab = 'pairing'">关联码</button>
        </div>

        <div v-if="tab === 'qr'" class="tab-pane">
          <p class="hint">手机 WhatsApp → 设置 → <strong>已链接的设备</strong> → 链接设备 → 扫描下面的二维码。</p>
          <div class="qr-area">
            <img v-if="status.qr_data_url" :src="status.qr_data_url" alt="WhatsApp QR" class="qr-img" />
            <div v-else class="qr-placeholder">
              <span v-if="status.status === 'starting' || status.status === 'loading'">
                启动中... ({{ status.percent ?? 0 }}%)
              </span>
              <span v-else>点 [▶ 启动 QR 模式] 生成二维码</span>
            </div>
          </div>
          <div class="actions">
            <TermBtn :loading="starting" @click="start('qr')" v-if="status.status !== 'awaiting_qr_scan'">▶ 启动 QR 模式</TermBtn>
            <TermBtn variant="danger" @click="stop" v-if="status.running">■ 停止</TermBtn>
          </div>
        </div>

        <div v-if="tab === 'pairing'" class="tab-pane">
          <p class="hint">手机 WhatsApp → 设置 → 已链接的设备 → <strong>使用电话号码链接</strong> → 输入下面的关联码。</p>
          <TermField v-model="pairingPhone" label="完整手机号 · 含国家码" placeholder="例如 8617788949030" />
          <div v-if="status.code" class="pairing-code">
            <span class="pairing-label">关联码</span>
            <span class="pairing-value">{{ formattedCode }}</span>
            <span class="pairing-phone">→ {{ status.phone }}</span>
          </div>
          <div class="actions">
            <TermBtn :loading="starting" @click="start('pairing')" v-if="status.status !== 'awaiting_pairing_code'">▶ 启动 + 拿关联码</TermBtn>
            <TermBtn variant="danger" @click="stop" v-if="status.running">■ 停止</TermBtn>
          </div>
        </div>
      </section>

      <section v-else class="wa-connected">
        <div class="term-divider" data-tail="──────────">已连接</div>
        <p class="hint">
          WhatsApp Web 已链接。当 GoPay 把 OTP 发到 <code>{{ status.pushname || '当前账号' }}</code> 时，
          <code>gopay.py</code> 会自动从 sidecar 读到 OTP 并继续 —— /run 页面的手动模态框只是兜底。
        </p>
        <div class="actions">
          <TermBtn variant="danger" :loading="loggingOut" @click="logoutWa">退出 WhatsApp 登录（清 session）</TermBtn>
        </div>
      </section>

      <section class="wa-logs">
        <div class="term-divider" data-tail="──────────">原始 state</div>
        <pre class="state-dump">{{ statusJson }}</pre>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { useRouter, RouterLink } from "vue-router";
import { useMessage } from "naive-ui";
import { api } from "../api/client";
import TermBtn from "../components/term/TermBtn.vue";
import TermField from "../components/term/TermField.vue";

const router = useRouter();
const message = useMessage();

interface WaStatus {
  running: boolean;
  pid?: number | null;
  mode?: string;
  status?: string;
  qr?: string;
  qr_ascii?: string;
  qr_data_url?: string;
  code?: string;
  phone?: string;
  wid?: string;
  pushname?: string;
  reason?: string;
  error?: string;
  percent?: number;
}

const tab = ref<"qr" | "pairing">("qr");
const status = ref<WaStatus>({ running: false, status: "stopped" });
const pairingPhone = ref("");
const starting = ref(false);
const loggingOut = ref(false);
const clock = ref("");
let clockTimer: ReturnType<typeof setInterval> | undefined;
let pollTimer: ReturnType<typeof setInterval> | undefined;

const statusLabel = computed(() => {
  switch (status.value.status) {
    case "stopped": return "未启动";
    case "starting": return "启动中...";
    case "loading": return `加载中 (${status.value.percent ?? 0}%)`;
    case "awaiting_qr_scan": return "等待扫码";
    case "awaiting_pairing_code": return "等待输入关联码";
    case "authenticated": return "已认证（连接中）";
    case "connected": return "已连接 ✓";
    case "disconnected": return `已断开 (${status.value.reason || "未知"})`;
    case "auth_failure": return `认证失败`;
    case "error": return "错误";
    default: return status.value.status || "未知";
  }
});

const connectClass = computed(() => {
  switch (status.value.status) {
    case "connected": return "ok";
    case "disconnected":
    case "auth_failure":
    case "error": return "err";
    case "awaiting_qr_scan":
    case "awaiting_pairing_code": return "warn";
    default: return "idle";
  }
});

const formattedCode = computed(() => {
  const c = status.value.code || "";
  // WhatsApp pairing code format is XXXX-XXXX
  if (c.length === 8 && !c.includes("-")) return `${c.slice(0,4)}-${c.slice(4)}`;
  return c;
});

const statusJson = computed(() => JSON.stringify(status.value, null, 2));

function tick() {
  const d = new Date();
  clock.value = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

async function refresh() {
  try {
    const r = await api.get<WaStatus>("/whatsapp/status");
    status.value = r.data;
    if (r.data.code && tab.value !== "pairing") tab.value = "pairing";
    if (r.data.qr_data_url && tab.value !== "qr") tab.value = "qr";
  } catch (e: any) {
    // silently ignore polling errors
  }
}

async function start(mode: "qr" | "pairing") {
  if (mode === "pairing") {
    const digits = pairingPhone.value.replace(/[^\d]/g, "");
    if (digits.length < 10) {
      message.warning("手机号要包含国家码，至少 10 位数字");
      return;
    }
  }
  starting.value = true;
  try {
    await api.post("/whatsapp/start", { mode, phone: pairingPhone.value });
    await refresh();
  } catch (e: any) {
    message.error(e.response?.data?.detail || "启动失败");
  } finally {
    starting.value = false;
  }
}

async function stop() {
  try {
    await api.post("/whatsapp/stop");
    await refresh();
  } catch (e: any) {
    message.error(e.response?.data?.detail || "停止失败");
  }
}

async function logoutWa() {
  loggingOut.value = true;
  try {
    await api.post("/whatsapp/logout");
    await refresh();
    message.success("已退出 WhatsApp 登录");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "退出失败");
  } finally {
    loggingOut.value = false;
  }
}

async function logout() {
  await api.post("/logout");
  router.push("/login");
}

onMounted(async () => {
  tick();
  clockTimer = setInterval(tick, 1000);
  await refresh();
  pollTimer = setInterval(refresh, 1500);
});

onBeforeUnmount(() => {
  if (clockTimer) clearInterval(clockTimer);
  if (pollTimer) clearInterval(pollTimer);
});
</script>

<style scoped>
.wa-root { min-height: 100vh; display: flex; flex-direction: column; }

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

.wa-main {
  flex: 1;
  max-width: 720px;
  margin: 0 auto;
  width: 100%;
  padding: 24px;
  overflow-y: auto;
}

.term-divider { color: var(--fg-tertiary); font-size: 11px; letter-spacing: 0.08em; margin: 16px 0 8px; }
.term-divider::after { content: " " attr(data-tail); color: var(--border-strong); }

.status-card { padding: 14px 16px; border: 1px solid var(--border); background: var(--bg-panel); }
.status-line { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 13px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--fg-tertiary); }
.status-ok .status-dot { background: var(--ok); animation: pulse 1.4s infinite; }
.status-err .status-dot { background: var(--err); }
.status-warn .status-dot { background: var(--warn); animation: pulse 1.4s infinite; }
.status-meta { color: var(--fg-tertiary); font-size: 11px; margin-top: 4px; }
.status-meta--err { color: var(--err); }
@keyframes pulse { 0%,100% { opacity: 0.5; } 50% { opacity: 1; } }

.tabs { display: flex; gap: 0; border: 1px solid var(--border-strong); margin: 12px 0; width: max-content; }
.tab { background: transparent; border: 0; border-right: 1px solid var(--border); padding: 8px 18px; font: inherit; font-size: 12px; cursor: pointer; color: var(--fg-secondary); }
.tab:last-child { border-right: 0; }
.tab.active { background: var(--accent); color: #fff; }

.hint { color: var(--fg-secondary); font-size: 12px; line-height: 1.7; margin: 12px 0; }
.hint code { background: var(--bg-panel); padding: 1px 5px; border: 1px solid var(--border); }

.qr-area { display: flex; align-items: center; justify-content: center; min-height: 280px; padding: 16px; border: 1px dashed var(--border-strong); background: #fff; }
.qr-img { max-width: 280px; max-height: 280px; }
.qr-placeholder { color: var(--fg-tertiary); font-size: 12px; }

.pairing-code { margin: 18px 0; padding: 18px; border: 1px dashed var(--accent); background: var(--bg-panel); display: grid; gap: 6px; }
.pairing-label { color: var(--fg-tertiary); font-size: 11px; letter-spacing: 0.1em; }
.pairing-value { font-size: 32px; font-weight: 700; letter-spacing: 0.2em; color: var(--accent); font-variant-numeric: tabular-nums; }
.pairing-phone { color: var(--fg-tertiary); font-size: 11px; }

.actions { margin-top: 16px; display: flex; gap: 12px; }

.state-dump {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  padding: 12px 14px;
  font-size: 11px;
  color: var(--fg-tertiary);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
