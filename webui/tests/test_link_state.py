"""Tests for the GoPay link-state module + HTTP API."""
from __future__ import annotations

import pytest


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


# ---- module-level helpers ---------------------------------------------------


def test_link_state_normalize_strips_non_digits(client):
    from webui.backend import link_state
    assert link_state._normalize("+86 138-1234-5678") == "8613812345678"
    assert link_state._normalize("") == ""


def test_link_state_phone_from_config(client):
    from webui.backend import link_state
    cfg = {"gopay": {"country_code": "86", "phone_number": "13812345678"}}
    assert link_state.phone_from_gopay_config(cfg) == "8613812345678"
    assert link_state.phone_from_gopay_config({"gopay": {}}) == ""
    assert link_state.phone_from_gopay_config({}) == ""
    assert link_state.phone_from_gopay_config(None) == ""


def test_link_state_mark_and_query_roundtrip(client):
    from webui.backend import link_state
    link_state.reset()

    assert link_state.is_linked("8613800000000") is False
    assert link_state.get_status("8613800000000")["linked"] is False

    item = link_state.mark_linked("86 138-0000-0000", payment_ref="ref-XYZ")
    assert item["linked"] is True
    assert item["phone"] == "8613800000000"
    assert item["payment_ref"] == "ref-XYZ"

    assert link_state.is_linked("8613800000000") is True

    item2 = link_state.mark_unlinked("8613800000000", source="some-worker")
    assert item2["linked"] is False
    assert item2["last_changed_by"] == "some-worker"
    assert link_state.is_linked("8613800000000") is False


def test_link_state_list_all(client):
    from webui.backend import link_state
    link_state.reset()
    link_state.mark_linked("8611111111111")
    link_state.mark_linked("8622222222222", payment_ref="abc")
    items = link_state.list_all()
    phones = sorted(it["phone"] for it in items)
    assert phones == ["8611111111111", "8622222222222"]


def test_link_state_mark_empty_phone_raises(client):
    from webui.backend import link_state
    with pytest.raises(ValueError):
        link_state.mark_linked("")
    with pytest.raises(ValueError):
        link_state.mark_unlinked("non-digits-only")  # normalizes to "" → raises


# ---- HTTP API ---------------------------------------------------------------


def test_link_state_list_requires_auth(client):
    r = client.get("/api/gopay/link-state")
    assert r.status_code == 401


def test_link_state_list_with_session(client):
    _login(client)
    from webui.backend import link_state
    link_state.reset()
    link_state.mark_linked("8613800000001")

    r = client.get("/api/gopay/link-state")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(it["phone"] == "8613800000001" and it["linked"] for it in items)


def test_link_state_list_with_relay_token(client):
    from webui.backend import link_state, wa_relay
    link_state.reset()
    link_state.mark_linked("8613800000002")
    token = wa_relay.relay_token()

    r = client.get(f"/api/gopay/link-state?token={token}")
    assert r.status_code == 200


def test_link_state_get_phone_with_token_header(client):
    from webui.backend import link_state, wa_relay
    link_state.reset()
    link_state.mark_linked("8613800000003", payment_ref="xyz")
    token = wa_relay.relay_token()

    r = client.get(
        "/api/gopay/link-state/8613800000003",
        headers={"X-WA-Relay-Token": token},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is True
    assert body["payment_ref"] == "xyz"


def test_link_state_get_unknown_phone_returns_unlinked(client):
    _login(client)
    r = client.get("/api/gopay/link-state/8699999999999")
    assert r.status_code == 200
    assert r.json()["linked"] is False


def test_link_state_unlink_requires_token(client):
    _login(client)  # session is NOT enough for unlink — we want explicit token
    r = client.post(
        "/api/gopay/link-state/unlink",
        json={"phone": "8613800000004"},
    )
    assert r.status_code == 403


def test_link_state_unlink_with_token(client):
    from webui.backend import link_state, wa_relay
    link_state.reset()
    link_state.mark_linked("8613800000005", payment_ref="r5")
    token = wa_relay.relay_token()

    r = client.post(
        "/api/gopay/link-state/unlink",
        json={"phone": "8613800000005", "source": "external-test"},
        headers={"X-WA-Relay-Token": token},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is False
    assert body["last_changed_by"] == "external-test"
    assert link_state.is_linked("8613800000005") is False


def test_link_state_unlink_empty_phone_returns_400(client):
    from webui.backend import wa_relay
    token = wa_relay.relay_token()
    r = client.post(
        "/api/gopay/link-state/unlink",
        json={"phone": ""},
        headers={"X-WA-Relay-Token": token},
    )
    assert r.status_code == 400


def test_link_state_set_with_session_marks_linked(client):
    """WebUI 用 session 鉴权能强制 mark linked（外部回调失败时手动覆盖）。"""
    _login(client)
    from webui.backend import link_state
    link_state.reset()

    r = client.post(
        "/api/gopay/link-state/set",
        json={"phone": "8613800000010", "linked": True, "source": "webui_manual"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is True
    assert body["last_changed_by"] == "webui_manual"
    assert link_state.is_linked("8613800000010") is True


def test_link_state_set_with_session_marks_unlinked(client):
    """同一 endpoint 也支持反向（unlink）。"""
    _login(client)
    from webui.backend import link_state
    link_state.reset()
    link_state.mark_linked("8613800000011", payment_ref="r11")

    r = client.post(
        "/api/gopay/link-state/set",
        json={"phone": "8613800000011", "linked": False},
    )
    assert r.status_code == 200
    assert r.json()["linked"] is False
    assert link_state.is_linked("8613800000011") is False


def test_link_state_set_requires_auth(client):
    r = client.post(
        "/api/gopay/link-state/set",
        json={"phone": "8613800000012", "linked": True},
    )
    assert r.status_code == 401


def test_link_state_set_with_token(client):
    """relay token 也能调（外部服务也复用这条入口）。"""
    from webui.backend import link_state, wa_relay
    link_state.reset()
    token = wa_relay.relay_token()

    r = client.post(
        "/api/gopay/link-state/set",
        json={"phone": "8613800000013", "linked": True},
        headers={"X-WA-Relay-Token": token},
    )
    assert r.status_code == 200
    assert link_state.is_linked("8613800000013") is True


# ---- Runner integration -----------------------------------------------------


def test_runner_start_blocked_when_phone_linked(client, tmp_path, monkeypatch):
    """runner.start(gopay=True) raises RuntimeError when phone is linked."""
    from webui.backend import link_state, runner
    import webui.backend.settings as settings_mod
    import json

    link_state.reset()

    # Write a minimal config with a gopay block
    cfg = {"gopay": {"country_code": "86", "phone_number": "13800000006", "pin": "123456"}}
    cfg_path = tmp_path / "config.paypal.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "PAY_CONFIG_PATH", cfg_path)

    link_state.mark_linked("8613800000006")

    with pytest.raises(RuntimeError) as exc:
        runner.start(mode="single", paypal=False, gopay=True)
    assert "linked" in str(exc.value).lower()


def test_runner_start_route_returns_409_when_linked(client, tmp_path, monkeypatch):
    """The /api/run/start route surfaces the link-block as 409."""
    _login(client)
    from webui.backend import link_state
    from webui.backend.routes import run as run_routes
    import webui.backend.settings as settings_mod
    import json

    link_state.reset()
    cfg = {"gopay": {"country_code": "86", "phone_number": "13800000007", "pin": "123456"}}
    cfg_path = tmp_path / "config.paypal.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "PAY_CONFIG_PATH", cfg_path)

    # Bypass the config-health gate that runs before runner.start.
    monkeypatch.setattr(run_routes, "build_config_health", lambda *a, **kw: {"ok": True, "checks": []})

    link_state.mark_linked("8613800000007")

    r = client.post("/api/run/start", json={"mode": "single", "paypal": False, "gopay": True})
    assert r.status_code == 409
    detail = r.json().get("detail")
    text = detail if isinstance(detail, str) else str(detail)
    assert "linked" in text.lower()


def test_runner_drain_marks_phone_linked_on_charge_settled(client, monkeypatch):
    """When _drain sees [gopay] charge settled, it marks the active phone linked."""
    from webui.backend import link_state, runner

    link_state.reset()

    # Simulate the runner state mid-flight
    runner._active_gopay_phone = "8613800000008"

    # Build a fake proc that emits the success line then EOF.
    class FakeStdout:
        def __init__(self, lines):
            self._iter = iter(lines)
        def readline(self):
            return next(self._iter, "")

    class FakeProc:
        stdout = FakeStdout([
            "[gopay] midtrans linking ok reference=ref-LINK-1\n",
            "[gopay] charge settled\n",
            "",
        ])
        returncode = 0
        def wait(self):
            return 0

    runner._drain(FakeProc())

    item = link_state.get_status("8613800000008")
    assert item["linked"] is True
    assert item["payment_ref"] == "ref-LINK-1"
    runner._active_gopay_phone = ""


def test_runner_drain_auto_marks_linked_on_midtrans_406(client):
    """Even when payment fails, a 406 from Midtrans means the phone is linked
    server-side. Our local state must be updated to match — otherwise the next
    run will blindly hit the same 406."""
    from webui.backend import link_state, runner

    link_state.reset()
    runner._active_gopay_phone = "8613800000020"

    class FakeStdout:
        def __init__(self, lines):
            self._iter = iter(lines)
        def readline(self):
            return next(self._iter, "")

    class FakeProc:
        # Real-world line shape: pipeline + card prefixes + gopay msg.
        # Pipeline aborts after retries exhausted, no charge-settled.
        stdout = FakeStdout([
            "  [pay] [12:00:00.001] [gopay] midtrans linking 406 (errors=...), 冷却 12s 再重试 1/2\n",
            "  [pay] [12:00:12.001] [gopay] midtrans linking 406 (errors=...), 冷却 12s 再重试 2/2\n",
            "  [pay] [ERROR] GoPayError: midtrans linking exhausted retries\n",
            "",
        ])
        returncode = 1
        def wait(self):
            return 1

    runner._drain(FakeProc())

    item = link_state.get_status("8613800000020")
    assert item["linked"] is True, "406 should auto-mark linked even when payment fails"
    assert item["last_changed_by"] == "pipeline_406_detect"
    runner._active_gopay_phone = ""
