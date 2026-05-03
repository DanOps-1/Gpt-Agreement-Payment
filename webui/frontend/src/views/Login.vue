<template>
  <div class="auth-shell">
    <header class="auth-banner">
      <pre class="banner-art">
┌─────────────────────────────────────────────────────────────┐
│  GPT-AGREEMENT-PAYMENT // 身份认证                                       │
│  输入凭据访问配置向导                                       │
└─────────────────────────────────────────────────────────────┘</pre>
    </header>

    <main class="auth-main">
      <h1 class="auth-headline">$&nbsp;登录<span class="term-cursor"></span></h1>

      <form class="auth-form" @submit.prevent="submit">
        <label class="field-row">
          <span class="field-tag">用户</span>
          <input v-model="form.username" type="text" autofocus class="term-input" />
        </label>
        <label class="field-row">
          <span class="field-tag">密码</span>
          <input v-model="form.password" type="password" class="term-input" />
        </label>

        <div class="auth-actions">
          <button class="term-btn" :disabled="loading" type="submit">{{ loading ? '登录中…' : '登录' }}</button>
        </div>
      </form>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { useMessage } from "naive-ui";
import { api } from "../api/client";

const router = useRouter();
const message = useMessage();
const loading = ref(false);
const form = ref({ username: "admin", password: "" });

async function submit() {
  loading.value = true;
  try {
    await api.post("/login", form.value);
    router.push("/wizard");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "登录失败");
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
.auth-headline { font-size: 36px; font-weight: 700; letter-spacing: 0.04em; margin: 0 0 24px; color: var(--fg-primary); }
.auth-form { display: flex; flex-direction: column; gap: 18px; }
.field-row { display: grid; grid-template-columns: 80px 1fr; align-items: center; gap: 0; border: 1px solid var(--border); }
.field-row:focus-within { border-color: var(--accent); }
.field-tag { background: var(--bg-panel); color: var(--fg-tertiary); padding: 12px 14px; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; border-right: 1px solid var(--border); }
.term-input { background: var(--bg-base); border: 0; padding: 12px 14px; color: var(--fg-primary); font: inherit; font-size: 14px; outline: none; width: 100%; }
.term-input::placeholder { color: var(--fg-tertiary); }
.auth-actions { display: flex; justify-content: flex-end; margin-top: 8px; }
</style>
