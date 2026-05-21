import { createRouter, createWebHistory } from "vue-router";

const BASE = import.meta.env.BASE_URL;

const router = createRouter({
  history: createWebHistory(BASE),
  routes: [
    { path: "/setup", component: () => import("./views/Setup.vue") },
    { path: "/login", component: () => import("./views/Login.vue") },
    { path: "/wizard", component: () => import("./views/Wizard.vue") },
    { path: "/run", component: () => import("./views/Run.vue") },
    { path: "/outlook", component: () => import("./views/Outlook.vue") },
    { path: "/promo-links", component: () => import("./views/PromoLinks.vue") },
    { path: "/whatsapp", component: () => import("./views/Whatsapp.vue") },
    { path: "/", redirect: "/wizard" },
  ],
});

/**
 * 抽成纯函数便于单测。两个 IO 用 callback 注入：
 * - fetchInitialized: 调 /api/setup/status，返回是否已建管理员
 * - fetchAuthed: 调 /api/me，返回 session 是否有效
 * 任一抛错都视为后端不可达，按"放过 /login，其它跳 /login"兜底。
 *
 * 历史 bug：
 *   1) /login 永远放行：没账号的人直接打 /login → 登录框 + admin 占位 → 试登录
 *      401 → 看着像"密码错"。修复：未 init 时所有路径强制跳 /setup。
 *   2) /setup 永远放行：已 init 的人若停在 /setup（书签 / 旧 history）会看到
 *      创建表单 → POST /api/setup 返 409 "already initialized" → 弹"创建失败"。
 *      修复：已 init 时 /setup 跳 /login。
 */
export async function decideRouteTarget(
  toPath: string,
  fetchInitialized: () => Promise<boolean>,
  fetchAuthed: () => Promise<boolean>,
): Promise<true | string> {
  let initialized: boolean;
  try {
    initialized = await fetchInitialized();
  } catch {
    // setup status 拿不到，后端可能挂了——放过 /login / /setup 让用户至少看到错误
    if (toPath === "/login" || toPath === "/setup") return true;
    return "/login";
  }
  if (!initialized) return toPath === "/setup" ? true : "/setup";
  // 已初始化：拒绝再进 setup 页（创建会 409）
  if (toPath === "/setup") return "/login";
  if (toPath === "/login") return true;
  try {
    if (!(await fetchAuthed())) return "/login";
  } catch {
    return "/login";
  }
  return true;
}

router.beforeEach(async (to) => {
  return decideRouteTarget(
    to.path,
    async () => {
      const r = await fetch(BASE + "api/setup/status").then((x) => x.json());
      return !!r.initialized;
    },
    async () => {
      const me = await fetch(BASE + "api/me", { credentials: "include" });
      return me.status !== 401;
    },
  );
});

export default router;
