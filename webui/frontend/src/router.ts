import { createRouter, createWebHistory } from "vue-router";

const BASE = import.meta.env.BASE_URL;

const router = createRouter({
  history: createWebHistory(BASE),
  routes: [
    { path: "/setup", component: () => import("./views/Setup.vue") },
    { path: "/login", component: () => import("./views/Login.vue") },
    { path: "/wizard", component: () => import("./views/Wizard.vue") },
    { path: "/run", component: () => import("./views/Run.vue") },
    { path: "/whatsapp", component: () => import("./views/Whatsapp.vue") },
    { path: "/", redirect: "/wizard" },
  ],
});

router.beforeEach(async (to) => {
  if (to.path === "/setup" || to.path === "/login") return true;
  try {
    const r = await fetch(BASE + "api/setup/status").then((x) => x.json());
    if (!r.initialized) return "/setup";
    const me = await fetch(BASE + "api/me", { credentials: "include" });
    if (me.status === 401) return "/login";
  } catch {
    return "/login";
  }
  return true;
});

export default router;
