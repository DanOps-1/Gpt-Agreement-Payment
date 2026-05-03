from __future__ import annotations

import json

from webui.backend.db import get_db


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


def _seed_configs(tmp_path, monkeypatch):
    pay_path = tmp_path / "CTF-pay" / "config.paypal.json"
    reg_path = tmp_path / "CTF-reg" / "config.paypal-proxy.json"
    pay_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.parent.mkdir(parents=True, exist_ok=True)

    pay_path.write_text(json.dumps({
        "paypal": {"email": "payer@example.org", "password": "secret123", "cookies": ""},
        "fresh_checkout": {
            "auth": {
                "session_token": "sess-123",
                "access_token": "at-123",
                "cookie_header": "cookie=1",
                "auto_register": {"config_path": str(reg_path)},
            }
        },
        "cpa": {"enabled": False},
    }), encoding="utf-8")

    reg_path.write_text(json.dumps({
        "mail": {
            "catch_all_domain": "catch.example.org",
            "catch_all_domains": ["catch.example.org"],
        },
        "captcha": {"client_key": "captcha-key"},
    }), encoding="utf-8")

    import webui.backend.settings as s
    monkeypatch.setattr(s, "PAY_CONFIG_PATH", pay_path)
    monkeypatch.setattr(s, "REG_CONFIG_PATH", reg_path)
    return pay_path, reg_path


def test_config_health_fails_without_cloudflare_secrets(client, tmp_path, monkeypatch):
    _login(client)
    _seed_configs(tmp_path, monkeypatch)

    from webui.backend.db import get_db
    get_db().clear_runtime_data()

    r = client.post("/api/config/health", json={"mode": "single", "paypal": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    names = {c["name"] for c in body["blocking"]}
    assert "cloudflare_kv_secrets" in names


def test_config_health_ok_with_cloudflare_secrets(client, tmp_path, monkeypatch):
    _login(client)
    _seed_configs(tmp_path, monkeypatch)

    db = get_db()
    db.clear_runtime_data()
    db.set_runtime_json("secrets", {
        "cloudflare": {
            "api_token": "tok-abc",
            "account_id": "acct-123",
            "otp_kv_namespace_id": "kv-123",
        }
    })

    r = client.post("/api/config/health", json={"mode": "single", "paypal": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert not body["blocking"]


def test_run_start_blocked_by_config_health(client, tmp_path, monkeypatch):
    _login(client)
    _seed_configs(tmp_path, monkeypatch)

    from webui.backend.db import get_db
    get_db().clear_runtime_data()

    r = client.post("/api/run/start", json={"mode": "single", "paypal": True})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "message" in detail
    assert "Cloudflare" in detail["message"] or "cloudflare" in detail["message"].lower()
    assert detail["health"]["blocking"]
