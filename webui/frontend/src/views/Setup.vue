<template>
  <div class="auth-shell">
    <header class="auth-banner">
      <pre class="banner-art">
┌─────────────────────────────────────────────────────────────┐
│  GPT-AGREEMENT-PAYMENT // 配置向导                                       │
│  初始化管理员凭据 // 仅首次运行                             │
└─────────────────────────────────────────────────────────────┘</pre>
    </header>

    <main class="auth-main">
      <h1 class="auth-headline">$&nbsp;初始化管理员<span class="term-cursor"></span></h1>
      <p class="auth-sub">未找到管理员账户。设置凭据以锁定本实例。</p>

      <form class="auth-form" @submit.prevent="submit">
        <label class="field-row">
          <span class="field-tag">用户</span>
          <input v-model="form.username" type="text" autofocus class="term-input" placeholder="admin" />
        </label>
        <label class="field-row">
          <span class="field-tag">密码</span>
          <input v-model="form.password" type="password" class="term-input" placeholder="至少 8 字符" />
        </label>

        <div class="auth-actions">
          <button class="term-btn" :disabled="loading" type="submit">{{ loading ? '创建中…' : '创建' }}</button>
        </div>
      </form>

      <footer class="auth-foot">
        // bcrypt cost=12 // session 走 httponly cookie
      </footer>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useRouter } from "vue-router";
import { useMessage } from "naive-ui";
import { api } from "../api/client";

const router = useRouter();
const message = useMessage();
const loading = ref(false);
const form = ref({ username: "admin", password: "" });

// 二次保险：router beforeEach 可能因为 fetch 失败把已初始化的用户也放进了
// /setup。组件挂载后再确认一次，已经有账号就立刻送去登录页，避免用户填完表
// 单点"创建"才看到 409 "already initialized"。
onMounted(async () => {
  try {
    const r = await api.get<{ initialized: boolean }>("/setup/status");
    if (r.data.initialized) router.replace("/login");
  } catch { /* status 拿不到就维持当前页让用户看到表单 */ }
});

async function submit() {
  if (form.value.password.length < 8) { message.error("密码至少 8 字符"); return; }
  loading.value = true;
  try {
    await api.post("/setup", form.value);
    message.success("管理员已创建，跳转登录…");
    setTimeout(() => router.push("/login"), 600);
  } catch (e: any) {
    // 409 already initialized 是常见的"用户走错路径"场景，直接送去登录
    if (e.response?.status === 409) {
      message.info("管理员已存在，转登录页…");
      setTimeout(() => router.replace("/login"), 600);
      return;
    }
    message.error(e.response?.data?.detail || "创建失败");
  } finally { loading.value = false; }
}
</script>

<style scoped>
.auth-shell { display: grid; place-items: center; min-height: 100vh; padding: 40px 16px; }
.auth-banner { width: 100%; max-width: 720px; margin-bottom: 32px; overflow-x: auto; }
.banner-art {
  color: var(--accent);
  font-size: 12px;
  line-height: 1.4;
  margin: 0;
  user-select: none;
  opacity: 0.75;
  white-space: pre;
  display: inline-block;
}
@media (max-width: 600px) {
  .banner-art { font-size: 9px; }
}
.auth-main { width: 100%; max-width: 540px; }
.auth-headline { font-size: 36px; font-weight: 700; letter-spacing: 0.04em; margin: 0 0 8px; color: var(--fg-primary); }
.auth-headline span.term-cursor { background: var(--accent); }
.auth-sub { color: var(--fg-secondary); font-size: 13px; margin: 0 0 32px; }
.auth-form { display: flex; flex-direction: column; gap: 18px; }
.field-row { display: grid; grid-template-columns: 80px 1fr; align-items: center; gap: 0; border: 1px solid var(--border); }
.field-row:focus-within { border-color: var(--accent); }
.field-tag { background: var(--bg-panel); color: var(--fg-tertiary); padding: 12px 14px; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; border-right: 1px solid var(--border); }
.term-input { background: var(--bg-base); border: 0; padding: 12px 14px; color: var(--fg-primary); font: inherit; font-size: 14px; outline: none; width: 100%; }
.term-input::placeholder { color: var(--fg-tertiary); }
.auth-actions { display: flex; justify-content: flex-end; margin-top: 8px; }
.auth-foot { color: var(--fg-tertiary); font-size: 11px; margin-top: 32px; user-select: none; }
</style>
