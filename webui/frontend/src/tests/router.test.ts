import { describe, it, expect } from "vitest";
import { decideRouteTarget } from "../router";

const okInit = () => Promise.resolve(true);
const noInit = () => Promise.resolve(false);
const okAuth = () => Promise.resolve(true);
const noAuth = () => Promise.resolve(false);
const reject = () => Promise.reject(new Error("network down"));

describe("decideRouteTarget", () => {
  it("When not initialized, /setup is allowed — the only entry point for new instances", async () => {
    expect(await decideRouteTarget("/setup", noInit, okAuth)).toBe(true);
  });

  it("regression: When initialized, /setup must redirect to /login (to prevent 409 already initialized)", async () => {
    // Previous bug: /setup was carelessly allowed, users who had created an admin account
    // would stay on /setup page (via bookmarks/old history) and fill the form to create → 
    // backend returns 409 → error popup appears as if it were a bug.
    expect(await decideRouteTarget("/setup", okInit, okAuth)).toBe("/login");
    expect(await decideRouteTarget("/setup", okInit, noAuth)).toBe("/login");
  });

  it("regression: When not initialized, /login must redirect to /setup", async () => {
    // Previous bug: /login was carelessly allowed, users without an account would go to login page → 
    // try to log in → get 401 → looks like "wrong password". After the fix, the router intercepts this.
    expect(await decideRouteTarget("/login", noInit, okAuth)).toBe("/setup");
  });

  it("When not initialized, all business routes redirect to /setup", async () => {
    expect(await decideRouteTarget("/wizard", noInit, okAuth)).toBe("/setup");
    expect(await decideRouteTarget("/run", noInit, okAuth)).toBe("/setup");
  });

  it("When initialized + logged in: business routes are allowed", async () => {
    expect(await decideRouteTarget("/wizard", okInit, okAuth)).toBe(true);
    expect(await decideRouteTarget("/run", okInit, okAuth)).toBe(true);
  });

  it("When initialized + not logged in: business routes redirect to /login", async () => {
    expect(await decideRouteTarget("/wizard", okInit, noAuth)).toBe("/login");
  });

  it("When initialized: /login itself is allowed", async () => {
    expect(await decideRouteTarget("/login", okInit, okAuth)).toBe(true);
  });

  it("When setup status cannot be retrieved: allow /login + /setup to show errors, redirect others to /login", async () => {
    expect(await decideRouteTarget("/login", reject, okAuth)).toBe(true);
    expect(await decideRouteTarget("/setup", reject, okAuth)).toBe(true);
    expect(await decideRouteTarget("/wizard", reject, okAuth)).toBe("/login");
  });

  it("When /me throws an error, treat as not logged in", async () => {
    expect(await decideRouteTarget("/wizard", okInit, reject)).toBe("/login");
  });
});