import json
import sys
import types

import pytest

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


def test_pay_only_claim_skips_in_flight_email(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    pipeline._PAY_ONLY_IN_FLIGHT_EMAILS.clear()

    db.add_registered_account({"email": "older@example.com", "session_token": "sess-older", "access_token": ""})
    db.add_registered_account({"email": "latest@example.com", "session_token": "sess-latest", "access_token": ""})

    first = pipeline._claim_recent_registered_account_for_pay_only()
    second = pipeline._claim_recent_registered_account_for_pay_only()

    try:
        assert first is not None
        assert second is not None
        assert first["email"] == "latest@example.com"
        assert second["email"] == "older@example.com"
    finally:
        pipeline._release_pay_only_account_claim(first)
        pipeline._release_pay_only_account_claim(second)

    again = pipeline._claim_recent_registered_account_for_pay_only()
    try:
        assert again is not None
        assert again["email"] == "latest@example.com"
    finally:
        pipeline._release_pay_only_account_claim(again)


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


def test_pay_only_push_server_only_for_plus_success(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"

    db.add_registered_account({
        "email": "retry@example.com",
        "password": "pw",
        "session_token": "sess-retry",
        "access_token": "at-retry",
        "device_id": "dev-retry",
    })
    card_config.write_text(json.dumps({
        "fresh_checkout": {"plan": {"plan_name": "chatgptplusplan"}},
    }), encoding="utf-8")

    calls = []

    monkeypatch.setattr(pipeline, "pay", lambda *args, **kwargs: {
        "status": "succeeded",
        "raw": {"session_id": "cs_test", "chatgpt_email": "retry@example.com"},
    })
    monkeypatch.setattr(
        pipeline,
        "_push_plus_account_to_server",
        lambda email, card_cfg, account=None: calls.append((email, card_cfg, account)) or "ok",
    )

    result = pipeline.pay_only(str(card_config), use_gopay=True, push_server=True)

    assert result["status"] == "succeeded"
    assert calls
    assert calls[0][0] == "retry@example.com"
    rows = get_db().iter_pipeline_results()
    assert rows[-1]["server_push"] == "ok"


def test_push_plus_account_to_server_posts_account_import_payload(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    aid = db.add_registered_account({
        "email": "retry@example.com",
        "password": "pw",
        "access_token": "at",
        "refresh_token": "rt",
        "id_token": "id",
    })
    calls = []

    class FakeResponse:
        status_code = 200
        text = '{"ok":true}'

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append({"init": kwargs})

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(Client=FakeClient))

    result = pipeline._push_plus_account_to_server(
        "retry@example.com",
        {"account_import_server": {"url": "http://127.0.0.1:8787/api/import", "token": "dev-import-token"}},
    )

    assert result == "ok"
    req = calls[-1]
    assert req["url"] == "http://127.0.0.1:8787/api/import"
    assert req["headers"] == {"Authorization": "Bearer dev-import-token"}
    assert req["json"]["type"] == "accounts"
    item = req["json"]["items"][0]
    assert item["email"] == "retry@example.com"
    assert item["password"] == "pw"
    assert item["enabled"] is True
    assert item["access_token"] == "at"
    assert item["id_token"] == "id"
    assert item["refresh_token"] == "rt"
    assert json.loads(item["extra"])["refresh_token"] == "rt"
    assert get_db().get_registered_account(aid)["server_pushed_at"] > 0


def test_auto_server_push_skips_when_rt_hits_add_phone_cooldown(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"
    db.add_registered_account({
        "email": "retry@example.com",
        "password": "pw",
        "session_token": "sess",
        "access_token": "at",
    })
    card_config.write_text(json.dumps({
        "fresh_checkout": {"plan": {"plan_name": "chatgptplusplan"}},
        "mail": {},
        "account_import_server": {"url": "http://127.0.0.1:8787/api/import", "token": "dev-import-token"},
    }), encoding="utf-8")

    push_calls = []
    monkeypatch.setattr(pipeline, "pay", lambda *args, **kwargs: {
        "status": "succeeded",
        "raw": {"session_id": "cs_test", "chatgpt_email": "retry@example.com"},
    })
    monkeypatch.setattr(pipeline, "_exchange_rt_with_classification", lambda *args, **kwargs: ("", "add_phone_blocked"))
    monkeypatch.setattr(
        pipeline,
        "_push_plus_account_to_server",
        lambda *args, **kwargs: push_calls.append((args, kwargs)) or "ok",
    )

    result = pipeline.pay_only(str(card_config), use_gopay=True, push_server=True)

    assert result["status"] == "succeeded"
    assert not push_calls
    rows = get_db().iter_pipeline_results()
    assert rows[-1]["server_push"] == "rt_cooldown"
    oauth = get_db().load_oauth_status_map()["retry@example.com"]
    assert oauth["status"] == "transient_failed"
    assert oauth["fail_reason"] == "add_phone_blocked"


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


def test_worker_card_config_assigns_distinct_gopay_and_proxy(tmp_path):
    card_config = tmp_path / "config.paypal.json"
    card_config.write_text(json.dumps({
        "proxy": "http://base-proxy",
        "gopay": {
            "country_code": "+62",
            "midtrans_client_id": "midtrans-base",
            "accounts": [
                {"label": "a", "phone_number": "111", "pin": "111111"},
                {"label": "b", "phone_number": "222", "pin": "222222"},
            ],
        },
        "proxies": {
            "enabled": True,
            "list": ["http://proxy-a", "http://proxy-b"],
        },
    }), encoding="utf-8")
    card_cfg = pipeline._read_card_cfg(str(card_config))
    proxy_pool = pipeline._build_proxy_pool_from_card_cfg(card_cfg)

    path0, px0 = pipeline._worker_card_config(str(card_config), card_cfg, 0, 2, proxy_pool, True)
    path1, px1 = pipeline._worker_card_config(str(card_config), card_cfg, 1, 2, proxy_pool, True)

    try:
        data0 = json.loads(open(path0, encoding="utf-8").read())
        data1 = json.loads(open(path1, encoding="utf-8").read())
        assert px0 == "http://proxy-a"
        assert px1 == "http://proxy-b"
        assert data0["proxy"] == "http://proxy-a"
        assert data1["proxy"] == "http://proxy-b"
        assert data0["gopay"]["phone_number"] == "111"
        assert data1["gopay"]["phone_number"] == "222"
        assert "accounts" not in data0["gopay"]
        assert "accounts" not in data1["gopay"]
    finally:
        pipeline.os.unlink(path0)
        pipeline.os.unlink(path1)


def test_batch_worker_continues_after_single_pipeline_error(monkeypatch):
    calls = []

    def fake_pipeline(card_path, **kwargs):
        calls.append((card_path, kwargs))
        if len(calls) == 1:
            raise RuntimeError("register stuck at about-you")
        return {
            "registration": {"status": "ok", "email": "next@example.com"},
            "payment": {"status": "succeeded"},
        }

    monkeypatch.setattr(pipeline, "pipeline", fake_pipeline)

    results = pipeline._run_batch_worker((
        0,
        range(0, 4, 2),
        "config.paypal.json",
        {},
        1,
        False,
        pipeline.ProxyPool(),
        {},
    ))

    assert len(calls) == 2
    assert [r["batch_index"] for r in results] == [0, 2]
    assert results[0]["status"] == "error"
    assert results[0]["payment"]["status"] == "error"
    assert "about-you" in results[0]["error"]
    assert results[1]["payment"]["status"] == "succeeded"


def test_batch_worker_scopes_gopay_otp_file_per_task(monkeypatch):
    calls = []

    def fake_pipeline(card_path, **kwargs):
        calls.append(kwargs)
        return {
            "registration": {"status": "ok", "email": "next@example.com"},
            "payment": {"status": "succeeded"},
        }

    monkeypatch.setattr(pipeline, "pipeline", fake_pipeline)

    results = pipeline._run_batch_worker((
        1,
        range(1, 5, 2),
        "config.paypal.json",
        {"gopay_otp_file": "/tmp/gopay_otp.txt"},
        2,
        False,
        pipeline.ProxyPool(),
        {},
    ))

    assert [r["batch_index"] for r in results] == [1, 3]
    assert [c["gopay_otp_file"] for c in calls] == [
        "/tmp/gopay_otp_w1_t2.txt",
        "/tmp/gopay_otp_w1_t4.txt",
    ]
    assert [c["log_label"] for c in calls] == ["w1:t2", "w1:t4"]


def test_pay_only_worker_scopes_gopay_otp_file_per_task(monkeypatch):
    calls = []

    def fake_pay_only(card_path, **kwargs):
        calls.append(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr(pipeline, "pay_only", fake_pay_only)

    results = pipeline._run_pay_only_worker((
        0,
        range(0, 4, 2),
        "config.paypal.json",
        False,
        True,
        "/tmp/gopay_otp.txt",
        False,
        2,
        pipeline.ProxyPool(),
        {},
    ))

    assert [r["batch_index"] for r in results] == [0, 2]
    assert [c["gopay_otp_file"] for c in calls] == [
        "/tmp/gopay_otp_w0_t1.txt",
        "/tmp/gopay_otp_w0_t3.txt",
    ]
    assert [c["log_label"] for c in calls] == ["w0:t1", "w0:t3"]


def test_validate_worker_resources_rejects_duplicate_proxy_or_gopay():
    with pytest.raises(RuntimeError, match="GoPay accounts"):
        pipeline._validate_worker_resources(
            {"gopay": {"accounts": [{"country_code": "+62", "phone_number": "111", "pin": "111111"}]}},
            2,
            use_gopay=True,
        )

    with pytest.raises(RuntimeError, match="HTTP 代理"):
        pipeline._validate_worker_resources(
            {"proxies": {"enabled": True, "list": ["http://proxy-a"]}},
            2,
            use_gopay=False,
        )
