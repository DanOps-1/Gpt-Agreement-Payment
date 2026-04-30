"""Tests for WhatsApp relay routes + state file parsing.

We don't actually launch the Node sidecar in tests (it would require a
WhatsApp account + Chromium download). Instead we mock subprocess.Popen and
write fake state files to verify the Python orchestration layer.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


def test_status_requires_auth(client):
    r = client.get("/api/whatsapp/status")
    assert r.status_code == 401


def test_status_when_stopped(client):
    _login(client)
    r = client.get("/api/whatsapp/status")
    assert r.status_code == 200
    data = r.json()
    assert data["running"] is False
    assert data.get("status") in ("stopped", None)


def test_start_requires_pairing_phone_in_pairing_mode(client):
    _login(client)
    r = client.post("/api/whatsapp/start", json={"mode": "pairing"})
    assert r.status_code == 400
    assert "pairing_phone" in r.json()["detail"] or "国家码" in r.json()["detail"]


def test_start_rejects_missing_relay(client, monkeypatch, tmp_path):
    """If relay sidecar dir is missing, start returns 400."""
    _login(client)
    import webui.backend.settings as s
    monkeypatch.setattr(s, "WA_RELAY_DIR", tmp_path / "nope")
    r = client.post("/api/whatsapp/start", json={"mode": "qr"})
    assert r.status_code == 400
    assert "缺失" in r.json()["detail"] or "missing" in r.json()["detail"].lower()


def test_start_rejects_missing_node_modules(client, monkeypatch, tmp_path):
    """When relay dir exists but node_modules missing, must error helpfully."""
    _login(client)
    relay = tmp_path / "relay"
    relay.mkdir()
    (relay / "index.js").write_text("// fake")
    import webui.backend.settings as s
    monkeypatch.setattr(s, "WA_RELAY_DIR", relay)
    r = client.post("/api/whatsapp/start", json={"mode": "qr"})
    assert r.status_code == 400
    assert "npm install" in r.json()["detail"]


def test_start_spawns_subprocess_and_reports_state(client, monkeypatch, tmp_path):
    """Mock Popen → start returns running=true and state file is parsed."""
    _login(client)

    relay = tmp_path / "relay"
    relay.mkdir()
    (relay / "index.js").write_text("// fake")
    (relay / "node_modules").mkdir()

    import webui.backend.settings as s
    import webui.backend.wa_relay as wa
    monkeypatch.setattr(s, "WA_RELAY_DIR", relay)

    captured = {}
    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env", {})
        # Simulate sidecar writing state file before Popen returns
        state_file = Path(kw["env"]["WA_STATE_FILE"])
        state_file.write_text(json.dumps({
            "status": "awaiting_qr_scan",
            "login_mode": "qr",
            "qr_data_url": "data:image/png;base64,FAKE",
            "ts": int(time.time() * 1000),
        }))
        class FakeProc:
            pid = 99999
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): return 0
            def kill(self): pass
        return FakeProc()
    monkeypatch.setattr("webui.backend.wa_relay.subprocess.Popen", fake_popen)

    r = client.post("/api/whatsapp/start", json={"mode": "qr"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["running"] is True
    assert data["pid"] == 99999
    assert data["status"] == "awaiting_qr_scan"
    assert data["qr_data_url"].startswith("data:image/png")

    # Cmd contains node + index.js path
    assert captured["cmd"][0] == "node"
    assert captured["cmd"][1].endswith("index.js")
    # Env injected
    assert captured["env"]["WA_LOGIN_MODE"] == "qr"
    assert captured["env"]["WA_STATE_FILE"].endswith("wa_state.json")
    assert captured["env"]["WA_OTP_FILE"].endswith("wa_otp.txt")


def test_start_pairing_mode_passes_phone(client, monkeypatch, tmp_path):
    _login(client)
    relay = tmp_path / "relay"
    relay.mkdir()
    (relay / "index.js").write_text("// fake")
    (relay / "node_modules").mkdir()
    import webui.backend.settings as s
    monkeypatch.setattr(s, "WA_RELAY_DIR", relay)

    captured = {}
    def fake_popen(cmd, **kw):
        captured["env"] = kw.get("env", {})
        Path(kw["env"]["WA_STATE_FILE"]).write_text(json.dumps({
            "status": "awaiting_pairing_code",
            "login_mode": "pairing",
            "code": "ABCD1234",
            "phone": "8617788949030",
        }))
        class FakeProc:
            pid = 1; poll = lambda self: None; terminate = lambda self: None
            wait = lambda self, timeout=None: 0; kill = lambda self: None
        return FakeProc()
    monkeypatch.setattr("webui.backend.wa_relay.subprocess.Popen", fake_popen)

    r = client.post("/api/whatsapp/start", json={
        "mode": "pairing",
        "phone": "+86 177 8894 9030",  # spaces and + should get stripped
    })
    assert r.status_code == 200, r.text
    assert captured["env"]["WA_PAIRING_PHONE"] == "8617788949030"
    assert captured["env"]["WA_LOGIN_MODE"] == "pairing"
    assert r.json()["status"] == "awaiting_pairing_code"


def test_logout_clears_session_dir(client, monkeypatch, tmp_path):
    _login(client)
    monkeypatch.setenv("WEBUI_DATA_DIR", str(tmp_path))
    # Create fake session contents
    sd = tmp_path / "wa_session"
    sd.mkdir()
    (sd / "fake-cookie").write_text("xxx")
    state = tmp_path / "wa_state.json"
    state.write_text("{}")

    r = client.post("/api/whatsapp/logout")
    assert r.status_code == 200
    assert r.json()["status"] == "logged_out"
    # session contents wiped
    assert not (sd / "fake-cookie").exists()
    # session dir recreated empty
    assert sd.exists()
    # state file gone
    assert not state.exists()
