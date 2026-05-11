"""Tests for auto_loop module + routes."""
from __future__ import annotations


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


def test_auto_loop_classify_kinds(client):
    from webui.backend import auto_loop

    cases = {
        "proxy_dead": [
            "curl_cffi.requests.exceptions.ProxyError: Failed to perform, curl: (97) cannot complete SOCKS5 connection to chatgpt.com.",
        ],
        "cf_429": ["[ERROR] GoPayError: midtrans linking unexpected status=429 body="],
        "otp_validate_400": [
            '  File "CTF-pay/gopay.py", line 488, in _gopay_validate_otp',
            "    r.raise_for_status()",
            "curl_cffi.requests.exceptions.HTTPError: HTTP Error 400:",
        ],
        "otp_timeout": ["OTPCancelled: OTP timeout after 300.0s"],
        "linked_exhausted": ["GoPayError: midtrans linking exhausted retries: account already linked"],
        "wallet_insufficient": ['{"errors":[{"code":"201","cause":"createAuth call to payment-switch failed for payment_method: GOPAY_WALLET"}]}'],
        "coupon_ineligible": ["RuntimeError: promo coupon 'plus-1-month-free' state=not_eligible"],
        "register_failed": ["pipeline.RegistrationError: 注册失败 (exit=1)"],
        "already_paid": ['FreshCheckoutAuthError: 生成 fresh checkout 失败: modern [400]: {"detail":"User is already paid"}'],
    }
    for expected, lines in cases.items():
        assert auto_loop._classify(lines) == expected, f"missed {expected} on {lines}"

    assert auto_loop._classify(["random log line"]) == "unknown"

    # 407 + SOCKS5 unreachable 都应分到 proxy_dead
    assert auto_loop._classify([
        '{"handler":"socks5","level":"error","msg":"route(retry=0) 407 Proxy Authentication Required"}',
    ]) == "proxy_dead"
    assert auto_loop._classify([
        "ProxyError: ... SOCKS5 connection ... Network unreachable",
    ]) == "proxy_dead"


def test_auto_loop_extract_email(client):
    from webui.backend import auto_loop
    lines = [
        "[batch] === pay-only × 3 串行 ===",
        "[pay-only] 复用最近未支付注册账号: ksuwo1pbm@lukyface.com session_token=yes",
    ]
    assert auto_loop._extract_email(lines) == "ksuwo1pbm@lukyface.com"

    lines2 = ["[reg] 06:29:41 [INFO] mail_provider: 邮箱已创建: cnayx8xgi@lukyface.com (路径: ...)"]
    assert auto_loop._extract_email(lines2) == "cnayx8xgi@lukyface.com"

    assert auto_loop._extract_email(["nothing relevant"]) == ""

    # 跨 iter 保留 buffer：取最新一条而不是最早
    lines3 = [
        "[reg] 邮箱已创建: old@619462.xyz (旧轮)",
        "[fresh] 当前账号: old@619462.xyz",
        "[reg] 邮箱已创建: new@lukyface.com (本轮)",
        "[fresh] 当前账号: new@lukyface.com",
    ]
    assert auto_loop._extract_email(lines3) == "new@lukyface.com"


def test_auto_loop_status_default(client):
    _login(client)
    r = client.get("/api/auto-loop/status")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert body["success_count"] == 0


def test_auto_loop_start_validates_input(client):
    _login(client)
    r = client.post("/api/auto-loop/start", json={"target_success": 0, "max_consec_fail": 5})
    assert r.status_code == 422  # pydantic validation

    r = client.post("/api/auto-loop/start", json={"target_success": 5, "max_consec_fail": 0})
    assert r.status_code == 422


def test_auto_loop_start_requires_auth(client):
    r = client.post("/api/auto-loop/start", json={"target_success": 1, "max_consec_fail": 1})
    assert r.status_code == 401


def test_auto_loop_scrap_account_deletes_inventory(client, tmp_path, monkeypatch):
    from webui.backend import auto_loop
    from webui.backend.db import get_db

    db = get_db()
    with db._conn() as c:
        c.execute(
            "INSERT INTO registered_accounts (email, ts, created_at) "
            "VALUES (?, ?, ?)",
            ("scrap-test@example.com", "2026-05-04T00:00:00Z", 1700000000.0),
        )
    assert auto_loop._scrap_account("scrap-test@example.com") is True
    # second time → already deleted, returns False
    assert auto_loop._scrap_account("scrap-test@example.com") is False
    assert auto_loop._scrap_account("") is False


def test_auto_loop_409_when_already_running(client, monkeypatch):
    _login(client)
    from webui.backend import auto_loop

    # Force running flag
    auto_loop._state["running"] = True
    try:
        r = client.post("/api/auto-loop/start", json={"target_success": 1, "max_consec_fail": 1})
        assert r.status_code == 409
        assert "运行" in r.json()["detail"]
    finally:
        auto_loop._state["running"] = False


def test_auto_loop_409_when_runner_running(client, monkeypatch):
    _login(client)
    from webui.backend import auto_loop, runner

    monkeypatch.setattr(runner, "status", lambda: {"running": True})
    r = client.post("/api/auto-loop/start", json={"target_success": 1, "max_consec_fail": 1})
    assert r.status_code == 409
    assert "pipeline" in r.json()["detail"].lower()


def test_auto_loop_stop_endpoint(client):
    _login(client)
    r = client.post("/api/auto-loop/stop")
    assert r.status_code == 200
    body = r.json()
    assert "running" in body


def test_auto_loop_status_exposes_zone_fields(client):
    _login(client)
    r = client.get("/api/auto-loop/status")
    assert r.status_code == 200
    body = r.json()
    for k in ("zone_list", "zone_idx", "current_zone", "zone_reg_fail_streak",
              "zone_ip_rotations", "total_zone_rotations",
              "zone_rotate_on_reg_fails", "zone_rotate_after_ip_rotations"):
        assert k in body, f"status 缺字段 {k}"


def test_auto_loop_load_zone_list_from_cardw(client):
    from webui.backend import auto_loop
    z = auto_loop._load_zone_list_from_cardw()
    # 不要求一定有，只要求是个 list
    assert isinstance(z, list)
