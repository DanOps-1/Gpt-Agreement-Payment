<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 07: GoPay 账号</div>
    <h2 class="step-h">$&nbsp;GoPay (印尼 e-wallet)<span class="term-cursor"></span></h2>
    <p class="step-sub">每个 ChatGPT Plus 订阅消耗 1 次 WhatsApp OTP + 2 次 PIN 输入。Lite 账号 (无印尼 KYC) 月限额约 IDR 2M ≈ 5-6 单。</p>

    <div class="form-stack">
      <TermField v-model="form.country_code" label="国家码 · country_code" placeholder="86 (中国大陆) / 62 (印尼)" />
      <TermField v-model="form.phone_number" label="手机号 · phone_number" placeholder="不带国家码，11 位数字" />
      <TermField v-model="form.pin" label="6 位 PIN · pin" type="password" placeholder="登录 GoJek/GoPay 时设的 PIN" />
      <TermField v-model.number="form.otp_timeout" label="OTP 等待超时秒数" type="number" />
      <TermSelect
        v-model="form.whatsapp_engine"
        label="WhatsApp 引擎"
        :options="engineOptions"
      />
    </div>

    <RouterLink class="wa-login-entry" to="/whatsapp">
      <span class="wa-login-prompt">$</span>
      WhatsApp 登录 / 扫码接收 GoPay OTP
    </RouterLink>

    <div class="hint-box">
      <p>前端只保留上面的 WhatsApp 登录入口。扫码连接后，后台会自动监听 WhatsApp 消息并把 GoPay OTP 写给支付流程读取。</p>
      <p>PIN 配置后自动用，绑定 + 扣款各用一次。</p>
      <p>同号重复绑定时第一次会返 406「account already linked」，gopay.py 会自动重试一次。</p>
    </div>

    <details class="ingest-box">
      <summary>外部服务推送 OTP（HTTP 接口）</summary>
      <p class="ingest-desc">
        让任意第三方服务（短信网关、邮件解析、运营商回执等）把验证码 POST 到下面这个接口。
        <strong>窗口期机制</strong>：接口默认关闭，只有当运行页面弹出 OTP 输入框（即 pipeline 在等 OTP）时才打开；
        其余时间 POST 会返 409。POST 成功后，运行页会自动填入并提交，pipeline 立即继续。
      </p>
      <div class="ingest-status" :class="ingestInfo?.active ? 'on' : 'off'">
        <span class="status-dot">●</span>
        <span>{{ ingestInfo?.active ? "接口窗口已开（pipeline 正在等 OTP）" : "接口窗口已关（启动一次 GoPay 流程后才会开）" }}</span>
        <TermBtn variant="ghost" class="refresh-btn" @click="loadIngestInfo(true)">刷新</TermBtn>
      </div>
      <div class="ingest-meta">
        <div><span class="ingest-label">方法</span><code>POST</code></div>
        <div><span class="ingest-label">URL</span><code>{{ ingestUrl || "加载中…" }}</code></div>
        <div><span class="ingest-label">鉴权</span><code>X-WA-Relay-Token</code> 头 或 <code>?token=</code> 查询参数</div>
        <div><span class="ingest-label">请求体</span><code>{"otp":"123456"}</code>（非数字字符会被自动剔除）</div>
        <div v-if="ingestInfo?.token"><span class="ingest-label">Token</span><code class="ingest-token">{{ ingestInfo.token }}</code></div>
      </div>
      <pre class="ingest-pre">{{ ingestCurl || "加载中…" }}</pre>
      <div v-if="ingestError" class="ingest-error">
        加载失败：{{ ingestError }}
        <TermBtn variant="ghost" @click="loadIngestInfo(true)">重试</TermBtn>
      </div>
      <div class="ingest-actions">
        <TermBtn variant="ghost" :disabled="!ingestCurl" @click="copyIngestCurl">复制 curl</TermBtn>
        <TermBtn variant="ghost" :disabled="!ingestInfo?.token" @click="copyIngestToken">仅复制 Token</TermBtn>
      </div>
      <p class="ingest-note">
        Token 与 WhatsApp sidecar / <code>/latest-otp</code> 共用同一个；在 WebUI「WhatsApp 登录」页点「退出 WhatsApp 登录」不会重置它。
      </p>
    </details>

    <details class="ingest-box">
      <summary>GoPay 链接状态（HTTP 接口）</summary>
      <p class="ingest-desc">
        每次成功扣款后，本系统把当前手机号标记为 <strong>linked</strong>。
        下次启动 GoPay 流程会先查这个状态：如果还 linked，运行页直接 409 拒绝启动，避免命中
        GoPay 的 406「account already linked」。
        外部服务在 GoPay 侧手动解绑后必须 POST <code>/unlink</code> 把状态翻回去，pipeline 才能继续。
      </p>
      <div class="ingest-status" :class="linkStatus?.linked ? 'on' : 'off'">
        <span class="status-dot">●</span>
        <span v-if="!form.phone_number">未配置手机号</span>
        <span v-else-if="linkStatus?.linked">已 linked（{{ formatTs(linkStatus.linked_at) }}）— 启动 GoPay 流程将被拒绝</span>
        <span v-else>未 linked — pipeline 可以启动</span>
        <TermBtn variant="ghost" class="refresh-btn" @click="loadLinkStatus(true)">刷新</TermBtn>
      </div>
      <div class="ingest-meta">
        <div><span class="ingest-label">手机号</span><code>{{ currentPhoneKey || "未配置" }}</code></div>
        <div v-if="linkStatus?.payment_ref"><span class="ingest-label">支付 ref</span><code>{{ linkStatus.payment_ref }}</code></div>
        <div v-if="linkStatus?.linked_at"><span class="ingest-label">linked at</span><code>{{ formatTs(linkStatus.linked_at) }}</code></div>
        <div v-if="linkStatus?.unlinked_at"><span class="ingest-label">unlinked at</span><code>{{ formatTs(linkStatus.unlinked_at) }}</code></div>
        <div v-if="linkStatus?.last_changed_by"><span class="ingest-label">改动方</span><code>{{ linkStatus.last_changed_by }}</code></div>
      </div>
      <div class="ingest-meta">
        <div><span class="ingest-label">查询</span><code>GET {{ ingestOrigin }}/api/gopay/link-state</code> 或 <code>/{phone}</code>（接受 session 或 token）</div>
        <div><span class="ingest-label">解绑</span><code>POST {{ ingestOrigin }}/api/gopay/link-state/unlink</code>（仅 token）</div>
        <div><span class="ingest-label">请求体</span><code>{"phone":"86138...","source":"my-worker"}</code></div>
      </div>
      <pre class="ingest-pre">{{ unlinkCurl || "加载中…" }}</pre>
      <div v-if="linkError" class="ingest-error">
        加载失败：{{ linkError }}
        <TermBtn variant="ghost" @click="loadLinkStatus(true)">重试</TermBtn>
      </div>
      <div class="ingest-actions">
        <TermBtn variant="ghost" :disabled="!unlinkCurl" @click="copyUnlinkCurl">复制 unlink curl</TermBtn>
        <TermBtn
          variant="danger"
          :loading="unlinking"
          :disabled="!linkStatus?.linked"
          @click="unlinkNow"
        >立即 unlink（仅本机）</TermBtn>
      </div>
      <p class="ingest-note">
        手动 unlink 仅清除本系统的标记；GoPay 服务端的实际链接需要你或外部服务在 GoPay/Midtrans 侧自行处理。
      </p>
    </details>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import { useMessage } from "naive-ui";
import { useWizardStore } from "../../stores/wizard";
import { api } from "../../api/client";
import TermBtn from "../term/TermBtn.vue";
import TermField from "../term/TermField.vue";
import TermSelect from "../term/TermSelect.vue";

const store = useWizardStore();
const message = useMessage();
const init = store.answers.gopay ?? {};
const initOtp = init.otp ?? {};
const form = ref({
  country_code: init.country_code ?? "86",
  phone_number: init.phone_number ?? "",
  pin: init.pin ?? "",
  otp_timeout: init.otp_timeout ?? initOtp.timeout ?? 300,
  whatsapp_engine: init.whatsapp_engine ?? "baileys",
});

const engineOptions = [
  { value: "baileys", label: "Baileys (推荐)", desc: "直连 WhatsApp multi-device socket，启动更轻" },
  { value: "wwebjs", label: "whatsapp-web.js", desc: "Chromium 路径，兼容旧环境 / 调试用" },
];

interface IngestInfo {
  path: string;
  method: string;
  token: string;
  header_name: string;
  query_name: string;
  active: boolean;
}
const ingestInfo = ref<IngestInfo | null>(null);
const ingestError = ref("");
let ingestLoading = false;

const apiBase = computed(() => {
  // import.meta.env.BASE_URL is "/webui/" or "/" depending on how the build
  // was served. The path returned by the backend already starts with "/api"
  // and assumes that base, so we strip the trailing slash from BASE_URL.
  const base = (import.meta.env.BASE_URL || "/").replace(/\/$/, "");
  return `${window.location.origin}${base}`;
});

const ingestUrl = computed(() => {
  if (!ingestInfo.value) return "";
  return `${apiBase.value}${ingestInfo.value.path}`;
});

const ingestCurl = computed(() => {
  if (!ingestInfo.value) return "";
  return `curl -X ${ingestInfo.value.method} '${ingestUrl.value}' \\
  -H 'Content-Type: application/json' \\
  -H '${ingestInfo.value.header_name}: ${ingestInfo.value.token}' \\
  -d '{"otp":"123456"}'`;
});

async function loadIngestInfo(force = false) {
  if (ingestLoading) return;
  if (!force && ingestInfo.value) return;
  ingestLoading = true;
  ingestError.value = "";
  try {
    const r = await api.get<IngestInfo>("/whatsapp/ingest-info");
    ingestInfo.value = r.data;
  } catch (e: any) {
    ingestError.value = e?.response?.data?.detail || e?.message || "未知错误";
  } finally {
    ingestLoading = false;
  }
}

onMounted(() => {
  loadIngestInfo();
  loadLinkStatus(true);
  // Both ingest-info.active (OTP API window) and link-state are live values:
  // ingest-info flips when the pipeline starts/finishes asking for an OTP, and
  // link-state flips after a successful charge. Poll both so the page reflects
  // current state without requiring a manual refresh click.
  linkPollTimer = setInterval(() => {
    loadLinkStatus(false);
    loadIngestInfo(true);
  }, 3000);
});

onBeforeUnmount(() => {
  if (linkPollTimer) clearInterval(linkPollTimer);
});

async function copyIngestCurl() {
  if (!ingestCurl.value) return;
  await navigator.clipboard.writeText(ingestCurl.value);
  message.success("已复制 curl 示例");
}

async function copyIngestToken() {
  if (!ingestInfo.value?.token) return;
  await navigator.clipboard.writeText(ingestInfo.value.token);
  message.success("已复制 Token");
}

interface LinkStatus {
  phone: string;
  linked: boolean;
  linked_at: number | null;
  unlinked_at: number | null;
  payment_ref: string;
  last_changed_by: string;
}
const linkStatus = ref<LinkStatus | null>(null);
const linkError = ref("");
const unlinking = ref(false);
let linkLoading = false;
let linkPollTimer: ReturnType<typeof setInterval> | undefined;

const ingestOrigin = computed(() => apiBase.value);

const currentPhoneKey = computed(() => {
  const cc = String(form.value.country_code || "").replace(/\D/g, "");
  const pn = String(form.value.phone_number || "").replace(/\D/g, "");
  if (!cc || !pn) return "";
  return cc + pn;
});

const unlinkCurl = computed(() => {
  if (!ingestInfo.value?.token) return "";
  const phone = currentPhoneKey.value || "<phone_digits>";
  return `curl -X POST '${apiBase.value}/api/gopay/link-state/unlink' \\
  -H 'Content-Type: application/json' \\
  -H '${ingestInfo.value.header_name}: ${ingestInfo.value.token}' \\
  -d '{"phone":"${phone}","source":"my-worker"}'`;
});

function formatTs(ts: number | null | undefined): string {
  if (!ts) return "—";
  try {
    return new Date(Number(ts) * 1000).toLocaleString();
  } catch {
    return String(ts);
  }
}

async function loadLinkStatus(force = false) {
  const phone = currentPhoneKey.value;
  if (!phone) {
    linkStatus.value = null;
    return;
  }
  if (linkLoading && !force) return;
  linkLoading = true;
  linkError.value = "";
  try {
    const r = await api.get<LinkStatus>(`/gopay/link-state/${encodeURIComponent(phone)}`);
    linkStatus.value = r.data;
  } catch (e: any) {
    linkError.value = e?.response?.data?.detail || e?.message || "未知错误";
  } finally {
    linkLoading = false;
  }
}

async function copyUnlinkCurl() {
  if (!unlinkCurl.value) return;
  await navigator.clipboard.writeText(unlinkCurl.value);
  message.success("已复制 unlink curl");
}

async function unlinkNow() {
  if (!ingestInfo.value?.token) {
    message.error("Token 未加载");
    return;
  }
  const phone = currentPhoneKey.value;
  if (!phone) {
    message.warning("先填手机号");
    return;
  }
  unlinking.value = true;
  try {
    await api.post(
      "/gopay/link-state/unlink",
      { phone, source: "webui_manual" },
      { headers: { [ingestInfo.value.header_name]: ingestInfo.value.token } },
    );
    await loadLinkStatus(true);
    message.success("已 unlink");
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "unlink 失败");
  } finally {
    unlinking.value = false;
  }
}

watch(() => currentPhoneKey.value, () => {
  loadLinkStatus(true);
});

watch(form, () => {
  store.setAnswer("gopay", form.value);
  store.saveToServer();
}, { deep: true });
</script>

<style scoped>
.hint-box {
  margin-top: 24px;
  padding: 12px 14px;
  border: 1px dashed var(--border);
  background: var(--bg-panel);
  font-size: 12px;
  color: var(--fg-tertiary);
}
.hint-box p { margin: 4px 0; }
.wa-login-entry {
  margin-top: 18px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--accent);
  color: var(--accent);
  background: rgba(93, 255, 174, 0.06);
  text-decoration: none;
  padding: 10px 14px;
  font-size: 13px;
  font-weight: 700;
}
.wa-login-entry:hover {
  background: rgba(93, 255, 174, 0.12);
}
.wa-login-prompt {
  color: var(--fg-primary);
}
.ingest-box {
  margin-top: 18px;
  border: 1px dashed var(--border);
  background: var(--bg-panel);
  padding: 12px 14px;
  font-size: 12px;
  color: var(--fg-secondary);
}
.ingest-box > summary {
  cursor: pointer;
  font-weight: 700;
  color: var(--accent);
  list-style: none;
}
.ingest-box > summary::-webkit-details-marker { display: none; }
.ingest-box > summary::before {
  content: "▸ ";
}
.ingest-box[open] > summary::before {
  content: "▾ ";
}
.ingest-desc {
  margin: 10px 0;
  color: var(--fg-tertiary);
  line-height: 1.7;
}
.ingest-meta {
  display: grid;
  grid-template-columns: 1fr;
  gap: 4px;
  margin: 8px 0;
}
.ingest-meta > div { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.ingest-label {
  display: inline-block;
  width: 56px;
  color: var(--fg-tertiary);
  flex-shrink: 0;
}
.ingest-token {
  word-break: break-all;
  user-select: all;
}
.ingest-pre {
  margin: 10px 0;
  background: var(--bg-base);
  border: 1px solid var(--border);
  padding: 10px 12px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--fg-primary);
}
.ingest-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.ingest-error {
  margin: 8px 0;
  padding: 8px 10px;
  border: 1px solid var(--err);
  color: var(--err);
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.ingest-status {
  margin: 10px 0;
  padding: 8px 10px;
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.ingest-status.on { border-color: var(--ok); color: var(--ok); }
.ingest-status.off { border-color: var(--fg-tertiary); color: var(--fg-tertiary); }
.ingest-status .status-dot { font-size: 14px; }
.ingest-status .refresh-btn { margin-left: auto; }
.ingest-note {
  margin-top: 10px;
  color: var(--fg-tertiary);
  line-height: 1.6;
}
.ingest-box code { color: var(--accent); }
</style>
