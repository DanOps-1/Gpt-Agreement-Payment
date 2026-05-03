import json
import importlib.util
from pathlib import Path

from webui.backend.db import get_db


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


def _seed(tmp_path, monkeypatch):
    pay_ex = tmp_path / "CTF-pay" / "config.paypal.example.json"
    reg_ex = tmp_path / "CTF-reg" / "config.paypal-proxy.example.json"
    pay_ex.parent.mkdir(parents=True)
    reg_ex.parent.mkdir(parents=True)
    pay_ex.write_text(json.dumps({"paypal": {"email": ""}, "captcha": {"api_url": "", "api_key": ""}}))
    reg_ex.write_text(json.dumps({"mail": {"catch_all_domain": ""}, "captcha": {"client_key": ""}}))

    import webui.backend.settings as s
    monkeypatch.setattr(s, "PAY_EXAMPLE_PATH", pay_ex)
    monkeypatch.setattr(s, "REG_EXAMPLE_PATH", reg_ex)
    monkeypatch.setattr(s, "PAY_CONFIG_PATH", tmp_path / "CTF-pay" / "config.paypal.json")
    monkeypatch.setattr(s, "REG_CONFIG_PATH", tmp_path / "CTF-reg" / "config.paypal-proxy.json")
    # 注：conftest 已经把 WEBUI_DATA_DIR 设到 tmp_path，SQLite 会落到 tmp_path/webui.db。


def test_export_writes_two_files(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {
        "paypal": {"email": "you@example.com"},
        "cloudflare": {"cf_token": "tok-abc", "zone_names": ["a.com", "b.com"]},
        # Note: forward_to 已被 fallback_to 取代（在 cloudflare_kv 里）；这里
        # 顺带保证 _write_secrets 不再要求 forward_to。
        "cloudflare_kv": {
            "account_id": "acct-123",
            "kv_namespace_id": "kv-456",
            "worker_name": "otp-relay",
        },
        "captcha": {"api_url": "https://x", "api_key": "k", "client_key": "k"},
    }
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200

    pay = json.loads((tmp_path / "CTF-pay" / "config.paypal.json").read_text())
    reg = json.loads((tmp_path / "CTF-reg" / "config.paypal-proxy.json").read_text())
    assert pay["paypal"]["email"] == "you@example.com"
    assert pay["captcha"]["api_key"] == "k"
    # mail.catch_all_domain(s) 来自 cloudflare zone_names；不再有 imap 字段
    assert reg["mail"]["catch_all_domain"] == "a.com"
    assert reg["mail"]["catch_all_domains"] == ["a.com", "b.com"]
    assert "imap_server" not in reg["mail"]
    assert reg["captcha"]["client_key"] == "k"

    # Cloudflare 凭证应写入 SQLite runtime_meta[secrets]，不再落 secrets.json。
    secrets = get_db().get_runtime_json("secrets", {})
    cf = secrets["cloudflare"]
    assert not (tmp_path / "secrets.json").exists()
    assert cf["api_token"] == "tok-abc"
    assert cf["zone_names"] == ["a.com", "b.com"]
    assert cf["account_id"] == "acct-123"
    assert cf["otp_kv_namespace_id"] == "kv-456"
    assert cf["otp_worker_name"] == "otp-relay"


def test_export_backs_up_existing(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    pay_path = tmp_path / "CTF-pay" / "config.paypal.json"
    pay_path.parent.mkdir(parents=True, exist_ok=True)
    pay_path.write_text(json.dumps({"old": True}))

    client.post("/api/config/export", json={"answers": {}})

    backups = list((tmp_path / "CTF-pay").glob("config.paypal.json.bak.*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"old": True}


def test_export_writes_gopay_auto_otp(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {
        "payment": {"method": "gopay"},
        "gopay": {
            "country_code": "62",
            "phone_number": "81234567890",
            "pin": "123456",
            "otp_timeout": 240,
        },
    }
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200

    pay = json.loads((tmp_path / "CTF-pay" / "config.paypal.json").read_text())
    assert pay["gopay"]["country_code"] == "62"
    assert pay["gopay"]["phone_number"] == "81234567890"
    assert pay["gopay"]["otp"]["source"] == "auto"
    assert "path" not in pay["gopay"]["otp"]
    assert "url" not in pay["gopay"]["otp"]
    assert pay["gopay"]["otp"]["timeout"] == 240
    assert pay["gopay"]["otp"]["interval"] == 1


def test_export_writes_hosted_checkout_link_mode(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {
        "team_plan": {
            "plan_name": "chatgptteamplan",
            "billing_country": "JP",
            "billing_currency": "JPY",
            "checkout_ui_mode": "hosted",
            "output_url_mode": "provider",
            "is_coupon_from_query_param": True,
        },
    }
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200

    pay = json.loads((tmp_path / "CTF-pay" / "config.paypal.json").read_text())
    plan = pay["fresh_checkout"]["plan"]
    assert plan["billing_country"] == "JP"
    assert plan["billing_currency"] == "JPY"
    assert plan["checkout_ui_mode"] == "hosted"
    assert plan["output_url_mode"] == "provider"
    assert plan["is_coupon_from_query_param"] is True


def test_export_preserves_cpa_config(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {
        "cpa": {
            "enabled": True,
            "base_url": "https://cpa.example.com",
            "admin_key": "secret-admin-key",
            "oauth_client_id": "app_test_client",
            "plan_tag": "team",
            "free_plan_tag": "free",
        },
    }
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200

    pay = json.loads((tmp_path / "CTF-pay" / "config.paypal.json").read_text())
    assert pay["cpa"]["enabled"] is True
    assert pay["cpa"]["base_url"] == "https://cpa.example.com"
    assert pay["cpa"]["admin_key"] == "secret-admin-key"
    assert pay["cpa"]["oauth_client_id"] == "app_test_client"
    assert pay["cpa"]["plan_tag"] == "team"
    assert pay["cpa"]["free_plan_tag"] == "free"


def test_exported_reg_config_accepts_checkout_link_fields(client, tmp_path, monkeypatch):
    _login(client)
    _seed(tmp_path, monkeypatch)

    answers = {
        "team_plan": {
            "plan_name": "chatgptplusplan",
            "plan_type": "plus",
            "entry_point": "all_plans_pricing_modal",
            "billing_country": "ID",
            "billing_currency": "IDR",
            "checkout_ui_mode": "custom",
            "output_url_mode": "canonical",
            "is_coupon_from_query_param": False,
        },
    }
    r = client.post("/api/config/export", json={"answers": answers})
    assert r.status_code == 200

    spec = importlib.util.spec_from_file_location("ctf_reg_config_for_test", Path("CTF-reg/config.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cfg = module.Config.from_file(str(tmp_path / "CTF-reg" / "config.paypal-proxy.json"))
    assert cfg.team_plan.plan_name == "chatgptplusplan"
    assert cfg.team_plan.billing_country == "ID"
    assert cfg.team_plan.billing_currency == "IDR"
    assert cfg.team_plan.checkout_ui_mode == "custom"
    assert cfg.team_plan.output_url_mode == "canonical"
    assert cfg.team_plan.is_coupon_from_query_param is False


def test_export_requires_auth(client):
    r = client.post("/api/config/export", json={"answers": {}})
    assert r.status_code == 401
