import json
import sys
import types

import pipeline
from webui.backend.db import get_db


def _reset_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBUI_DATA_DIR", str(tmp_path))
    db = get_db()
    db.clear_runtime_data()
    return db


def test_pay_only_selects_latest_registered_unpaid_account(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)

    for row in [
        {
            "ts": "2026-05-03T01:00:00+00:00",
            "email": "paid@example.com",
            "session_token": "sess-paid",
            "access_token": "at-paid",
            "device_id": "dev-paid",
        },
        {
            "ts": "2026-05-03T02:00:00+00:00",
            "email": "retry@example.com",
            "session_token": "sess-retry",
            "access_token": "at-retry",
            "device_id": "dev-retry",
        },
        {
            "ts": "2026-05-03T03:00:00+00:00",
            "email": "no-auth@example.com",
            "session_token": "",
            "access_token": "",
            "device_id": "dev-noauth",
        },
    ]:
        db.add_registered_account(row)
    db.add_pipeline_result({
        "registration": {"status": "ok", "email": "paid@example.com"},
        "payment": {"status": "succeeded", "email": "paid@example.com"},
    })
    db.add_pipeline_result({
        "registration": {"status": "ok", "email": "retry@example.com"},
        "payment": {"status": "error", "email": "retry@example.com", "error": "OTP timeout"},
    })

    selected = pipeline._select_recent_registered_account_for_pay_only()
    assert selected is not None
    assert selected["email"] == "retry@example.com"
    assert selected["session_token"] == "sess-retry"
    assert selected["access_token"] == "at-retry"


def test_pay_only_treats_already_paid_error_as_consumed(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)

    db.add_registered_account({"email": "older@example.com", "session_token": "sess-older", "access_token": ""})
    db.add_registered_account({"email": "latest@example.com", "session_token": "sess-latest", "access_token": ""})
    db.add_pipeline_result({
        "registration": {"status": "ok", "email": "latest@example.com"},
        "payment": {
            "status": "error",
            "email": "latest@example.com",
            "error": '生成 fresh checkout 失败: modern [400]: {"detail":"User is already paid"}',
        },
    })

    selected = pipeline._select_recent_registered_account_for_pay_only()
    assert selected is not None
    assert selected["email"] == "older@example.com"


def test_pay_only_success_imports_cpa_with_plus_tag(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"

    db.add_registered_account({
        "email": "retry@example.com",
        "session_token": "sess-retry",
        "access_token": "at-retry",
        "device_id": "dev-retry",
    })
    card_config.write_text(json.dumps({
        "fresh_checkout": {"plan": {"plan_name": "chatgptplusplan"}},
        "cpa": {
            "enabled": True,
            "base_url": "https://cpa.example.com",
            "admin_key": "adm",
            "oauth_client_id": "app_test",
            "plan_tag": "team",
        },
    }), encoding="utf-8")

    calls = []

    def fake_pay(*args, **kwargs):
        return {
            "status": "succeeded",
            "raw": {
                "session_id": "cs_test",
                "chatgpt_email": "retry@example.com",
            },
        }

    def fake_cpa(email, sid, cpa_cfg, **kwargs):
        calls.append((email, sid, cpa_cfg, kwargs))
        return "ok"

    monkeypatch.setattr(pipeline, "pay", fake_pay)
    monkeypatch.setattr(pipeline, "_cpa_import_after_team", fake_cpa)

    result = pipeline.pay_only(str(card_config), use_gopay=True)

    assert result["status"] == "succeeded"
    assert calls
    email, sid, cpa_cfg, kwargs = calls[0]
    assert email == "retry@example.com"
    assert sid == "cs_test"
    assert cpa_cfg["plan_tag"] == "plus"
    rows = get_db().iter_pipeline_results()
    assert rows[-1]["cpa_import"] == "ok"


def test_cpa_import_falls_back_to_access_token_without_refresh_token(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    db.add_registered_account({
        "email": "fallback@example.com",
        "access_token": "eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjdF8xMjMifSwiZXhwIjoyNTM0MDk0NDAwfQ.sig",
    })
    monkeypatch.setattr(pipeline, "_find_latest_refresh_token_for_email", lambda *args, **kwargs: "")

    fake_calls = []

    class FakeResponse:
        status_code = 200
        text = ""

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.proxies = {}
            self.trust_env = False

        def post(self, url, params=None, json=None, headers=None, timeout=None):
            fake_calls.append({"url": url, "params": params, "json": json, "headers": headers, "timeout": timeout})
            return FakeResponse()

    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.Session = lambda impersonate=None: FakeSession()
    fake_pkg = types.ModuleType("curl_cffi")
    fake_pkg.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_pkg)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)

    status = pipeline._cpa_import_after_team(
        "fallback@example.com",
        "cs_test",
        {
            "enabled": True,
            "base_url": "https://cpa.example.com",
            "admin_key": "secret-admin-key",
            "oauth_client_id": "app_test_client",
            "plan_tag": "team",
            "free_plan_tag": "free",
        },
    )

    assert status == "ok"
    assert fake_calls
    body = fake_calls[0]["json"]
    assert body["email"] == "fallback@example.com"
    assert body["access_token"].startswith("eyJhbGciOiJub25lIn0.")
    assert body["refresh_token"] == ""
    assert body["account_id"] == "acct_123"
