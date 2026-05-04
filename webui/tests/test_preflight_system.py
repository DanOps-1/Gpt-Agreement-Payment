def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


def test_system_preflight_returns_status(client):
    _login(client)
    r = client.post("/api/preflight/system", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "warn", "fail")
    assert "checks" in body
    names = {c["name"] for c in body["checks"]}
    assert {"python", "camoufox", "xvfb-run", "playwright"}.issubset(names)


def test_system_preflight_each_check_has_status(client):
    _login(client)
    body = client.post("/api/preflight/system", json={}).json()
    for c in body["checks"]:
        assert c["status"] in ("ok", "warn", "fail")
        assert "message" in c


def test_system_preflight_requires_auth(client):
    r = client.post("/api/preflight/system", json={})
    assert r.status_code == 401


def test_system_preflight_allows_missing_xvfb_on_macos(monkeypatch):
    import webui.backend.preflight.system as system_mod

    monkeypatch.setattr(system_mod.runtime_env.sys, "platform", "darwin")
    monkeypatch.setattr(system_mod.shutil, "which", lambda name: "/usr/local/bin/camoufox" if name == "camoufox" else None)
    monkeypatch.setattr(system_mod.runtime_env, "xvfb_check", lambda: ("ok", "macOS 不需要 xvfb-run；将直接启动 python"))

    body = system_mod.check()
    xvfb = next(c for c in body.checks if c.name == "xvfb-run")
    assert xvfb.status == "ok"
    assert "macOS" in xvfb.message
