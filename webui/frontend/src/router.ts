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
 * Extract as a pure function for easier unit testing. Two IO operations are injected via callbacks:
 * - fetchInitialized: calls /api/setup/status, returns whether admin has been initialized
 * - fetchAuthed: calls /api/me, returns whether session is valid
 * If either throws an error, it is treated as backend unavailable, with fallback behavior: allow /login, redirect others to /login.
 *
 * Historical bugs:
 *   1) /login was always allowed: users without an account could access /login directly → login form + admin placeholder → attempt login
 *      → 401 error looks like "wrong password". Fix: force all paths to /setup when not initialized.
 *   2) /setup was always allowed: initialized users who landed on /setup (via bookmark / old history) would see
 *      creation form → POST /api/setup returns 409 "already initialized" → popup "creation failed".
 *      Fix: redirect /setup to /login when already initialized.
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
    // Cannot fetch setup status, backend may be down — allow /login / /setup so users can at least see the error
    if (toPath === "/login" || toPath === "/setup") return true;
    return "/login";
  }
  if (!initialized) return toPath === "/setup" ? true : "/setup";
  // Already initialized: reject further access to setup page (creation would return 409)
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