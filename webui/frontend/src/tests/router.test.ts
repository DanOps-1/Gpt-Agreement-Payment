import { describe, it, expect } from "vitest";
import { decideRouteTarget } from "../router";

const okInit = () => Promise.resolve(true);
const noInit = () => Promise.resolve(false);
const okAuth = () => Promise.resolve(true);
const noAuth = () => Promise.resolve(false);
const reject = () => Promise.reject(new Error("network down"));

describe("decideRouteTarget", () => {
  it("未 init 时 /setup 放行——新实例唯一入口", async () => {
    expect(await decideRouteTarget("/setup", noInit, okAuth)).toBe(true);
  });

  it("regression: 已 init 时 /setup 必须跳 /login（防止 409 already initialized）", async () => {
    // 之前的 bug：/setup 被无脑放行，已建过 admin 的用户停在 /setup 页（书签
    // / 旧 history）会填表点创建 → 后端 409 → 弹"创建失败"，看着像 bug。
    expect(await decideRouteTarget("/setup", okInit, okAuth)).toBe("/login");
    expect(await decideRouteTarget("/setup", okInit, noAuth)).toBe("/login");
  });

  it("regression: 未 init 时 /login 必须跳 /setup", async () => {
    // 之前的 bug：/login 被无脑放行，没账号的人到登录页 → 试登录 → 401 →
    // 看着像"密码错"。修复后路由层就拦截掉。
    expect(await decideRouteTarget("/login", noInit, okAuth)).toBe("/setup");
  });

  it("未 init 时业务路径都跳 /setup", async () => {
    expect(await decideRouteTarget("/wizard", noInit, okAuth)).toBe("/setup");
    expect(await decideRouteTarget("/run", noInit, okAuth)).toBe("/setup");
  });

  it("已 init + 已登录：业务路径放行", async () => {
    expect(await decideRouteTarget("/wizard", okInit, okAuth)).toBe(true);
    expect(await decideRouteTarget("/run", okInit, okAuth)).toBe(true);
  });

  it("已 init + 未登录：业务路径跳 /login", async () => {
    expect(await decideRouteTarget("/wizard", okInit, noAuth)).toBe("/login");
  });

  it("已 init：/login 自身允许进入", async () => {
    expect(await decideRouteTarget("/login", okInit, okAuth)).toBe(true);
  });

  it("setup status 拿不到时：放行 /login + /setup 让用户看见错误，其它跳 /login", async () => {
    expect(await decideRouteTarget("/login", reject, okAuth)).toBe(true);
    expect(await decideRouteTarget("/setup", reject, okAuth)).toBe(true);
    expect(await decideRouteTarget("/wizard", reject, okAuth)).toBe("/login");
  });

  it("/me 抛错也视作未登录", async () => {
    expect(await decideRouteTarget("/wizard", okInit, reject)).toBe("/login");
  });
});
