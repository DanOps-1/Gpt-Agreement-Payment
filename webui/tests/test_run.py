"""Run controller tests — mock subprocess so we don't actually spawn pipeline."""
import time
import pytest


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


def test_run_status_idle(client):
    _login(client)
    r = client.get("/api/run/status")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False


def test_run_preview_single(client):
    _login(client)
    r = client.post("/api/run/preview", json={"mode": "single"})
    assert r.status_code == 200
    body = r.json()
    assert "xvfb-run" in body["cmd_str"]
    assert "pipeline.py" in body["cmd_str"]
    assert "--paypal" in body["cmd_str"]


def test_run_preview_batch(client):
    _login(client)
    r = client.post("/api/run/preview", json={"mode": "batch", "batch": 5, "workers": 2})
    body = r.json()
    assert "--batch" in body["cmd_str"]
    assert "5" in body["cmd_str"]
    assert "--workers" in body["cmd_str"]


def test_run_preview_self_dealer(client):
    _login(client)
    r = client.post("/api/run/preview", json={"mode": "self_dealer", "self_dealer": 4})
    body = r.json()
    assert "--self-dealer" in body["cmd_str"]
    assert "4" in body["cmd_str"]


def test_run_preview_daemon(client):
    _login(client)
    r = client.post("/api/run/preview", json={"mode": "daemon"})
    body = r.json()
    assert "--daemon" in body["cmd_str"]


def test_run_invalid_mode(client):
    _login(client)
    r = client.post("/api/run/preview", json={"mode": "bogus"})
    assert r.status_code == 422


def test_run_requires_auth(client):
    r = client.get("/api/run/status")
    assert r.status_code == 401


def test_run_start_then_409(client, monkeypatch):
    """Mock 一个不会立即退出的 subprocess，确保第二次 start 返 409。

    We bypass the drain thread entirely by also monkeypatching _drain to a no-op,
    so the module-level _proc stays alive (poll() returns None).
    """
    _login(client)
    import webui.backend.runner as runner_mod

    class FakeProc:
        """Simulate a long-running process: poll() returns None until terminated."""
        def __init__(self):
            self.pid = 12345
            self.stdout = None
            self.returncode = None
            self._terminated = False
        def poll(self):
            return None if not self._terminated else 0
        def terminate(self):
            self._terminated = True
            self.returncode = 0
        def wait(self, timeout=None):
            self._terminated = True
            self.returncode = 0
        def kill(self):
            self._terminated = True
            self.returncode = -9

    fake_procs: list = []
    def fake_popen(cmd, **kwargs):
        p = FakeProc()
        fake_procs.append(p)
        return p

    # Patch _drain to no-op so the daemon thread doesn't call proc.wait()
    # and flip _proc to "terminated" before our assertions.
    monkeypatch.setattr(runner_mod, "_drain", lambda proc: None)
    monkeypatch.setattr(runner_mod.subprocess, "Popen", fake_popen)

    # Bypass config health gate — this test focuses on the runner state
    # machine, not config validation (covered separately in test_config_health).
    import webui.backend.routes.run as run_route
    monkeypatch.setattr(
        run_route,
        "build_config_health",
        lambda req: {"ok": True, "blocking": [], "checks": []},
    )

    # Reset module state from prior tests
    runner_mod._proc = None
    runner_mod._ended_at = None
    runner_mod._exit_code = None

    r = client.post("/api/run/start", json={"mode": "single"})
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True

    r = client.post("/api/run/start", json={"mode": "single"})
    assert r.status_code == 409

    r = client.post("/api/run/stop")
    assert r.status_code == 200

    # Cleanup module state
    runner_mod._proc = None


def test_gopay_auto_otp_skips_manual_fifo(tmp_path, monkeypatch):
    import json
    import webui.backend.runner as runner_mod

    cfg = tmp_path / "pay.json"
    cfg.write_text(json.dumps({
        "gopay": {
            "otp": {
                "source": "http",
                "url": "http://127.0.0.1:8765/latest",
            },
        },
    }))
    monkeypatch.setattr(runner_mod.s, "PAY_CONFIG_PATH", cfg)

    assert runner_mod._gopay_auto_otp_enabled() is True
