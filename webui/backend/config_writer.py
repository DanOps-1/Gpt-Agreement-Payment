from __future__ import annotations

import json
import time
from pathlib import Path
from . import settings as s
from .db import get_db


def _deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    bak = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
    bak.write_bytes(path.read_bytes())
    return bak


def _payment_method(answers: dict) -> str:
    return (answers.get("payment") or {}).get("method", "both")


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _gopay_account_disabled(value: dict) -> bool:
    return _truthy(
        value.get("disabled")
        or value.get("disable")
        or value.get("enabled") is False
        or value.get("gopay_disabled")
        or value.get("account_disabled")
    )


def _normalize_gopay_accounts(gp: dict) -> list[dict]:
    raw_accounts = gp.get("accounts") if isinstance(gp.get("accounts"), list) else []
    accounts: list[dict] = []
    for idx, item in enumerate(raw_accounts):
        if not isinstance(item, dict):
            continue
        account = {
            "label": str(item.get("label") or item.get("name") or f"account-{idx + 1}"),
            "country_code": str(item.get("country_code") or gp.get("country_code") or "").lstrip("+"),
            "phone_number": str(item.get("phone_number") or ""),
            "pin": str(item.get("pin") or ""),
        }
        if _gopay_account_disabled(item):
            account["disabled"] = True
        if item.get("use_sms_otp") or item.get("sms_otp"):
            account["use_sms_otp"] = True
        sms_otp_poll_url = str(item.get("sms_otp_poll_url") or item.get("sms_otp_url") or "").strip()
        if sms_otp_poll_url:
            account["sms_otp_poll_url"] = sms_otp_poll_url
        auto_login_phone = str(item.get("auto_login_phone") or item.get("login_phone") or "").strip()
        if auto_login_phone:
            account["auto_login_phone"] = auto_login_phone
        if item.get("midtrans_client_id") or gp.get("midtrans_client_id"):
            account["midtrans_client_id"] = str(item.get("midtrans_client_id") or gp.get("midtrans_client_id"))
        for key in (
            "headers", "qris_headers", "qris_raw_headers", "qris", "qris_base_url",
            "qris_hmac_key", "qris_service_id", "qris_merchant_id",
            "qr_pre_linking", "qris_pre_linking", "linking_before_qr",
            "login_headers", "auto_login_headers", "auto_login_client_id",
            "auto_login_client_secret", "auto_login_flow", "auto_login_method",
            "auto_login_verify_flow", "auto_login_account_id",
            "auto_login_otp_poll_url", "auto_login_otp_headers", "auto_login_otp_params",
            "auto_login_otp_timeout", "auto_login_otp_interval", "auto_login_otp_regex",
            "auto_login_otp_json_path", "auto_login_otp_filter_phone",
            "auto_login_token_dir", "auto_login_keep_authorization",
            "auto_login_setup_pin", "auto_login_pin", "auto_login_pin_flow",
            "auto_login_pin_method", "auto_login_pin_client_id",
            "auto_login_pin_challenge_id",
            "login_otp_poll_url", "login_token_dir",
            "login_hmac_key", "login_nonce_marker",
        ):
            has_value = key in item or key in gp
            value = item.get(key) if key in item else gp.get(key)
            if has_value and (value or key in ("qr_pre_linking", "qris_pre_linking", "linking_before_qr")):
                account[key] = value
        auto_unbind = _gopay_auto_unbind_from_value(item, fallback=gp)
        if auto_unbind:
            account["auto_unbind"] = auto_unbind
        if all(account.get(k) for k in ("country_code", "phone_number", "pin")):
            accounts.append(account)

    if accounts:
        return accounts
    if raw_accounts:
        return []

    if all(gp.get(k) for k in ("country_code", "phone_number", "pin")):
        account = {
            "label": str(gp.get("label") or gp.get("name") or "default"),
            "country_code": str(gp["country_code"]).lstrip("+"),
            "phone_number": str(gp["phone_number"]),
            "pin": str(gp["pin"]),
        }
        if _gopay_account_disabled(gp):
            account["disabled"] = True
        if gp.get("use_sms_otp") or gp.get("sms_otp"):
            account["use_sms_otp"] = True
        sms_otp_poll_url = str(gp.get("sms_otp_poll_url") or gp.get("sms_otp_url") or "").strip()
        if sms_otp_poll_url:
            account["sms_otp_poll_url"] = sms_otp_poll_url
        auto_login_phone = str(gp.get("auto_login_phone") or gp.get("login_phone") or "").strip()
        if auto_login_phone:
            account["auto_login_phone"] = auto_login_phone
        if gp.get("midtrans_client_id"):
            account["midtrans_client_id"] = str(gp["midtrans_client_id"])
        for key in (
            "headers", "qris_headers", "qris_raw_headers", "qris", "qris_base_url",
            "qris_hmac_key", "qris_service_id", "qris_merchant_id",
            "qr_pre_linking", "qris_pre_linking", "linking_before_qr",
            "login_headers", "auto_login_headers", "auto_login_client_id",
            "auto_login_client_secret", "auto_login_flow", "auto_login_method",
            "auto_login_verify_flow", "auto_login_account_id",
            "auto_login_otp_poll_url", "auto_login_otp_headers", "auto_login_otp_params",
            "auto_login_otp_timeout", "auto_login_otp_interval", "auto_login_otp_regex",
            "auto_login_otp_json_path", "auto_login_otp_filter_phone",
            "auto_login_token_dir", "auto_login_keep_authorization",
            "auto_login_setup_pin", "auto_login_pin", "auto_login_pin_flow",
            "auto_login_pin_method", "auto_login_pin_client_id",
            "auto_login_pin_challenge_id",
            "login_otp_poll_url", "login_token_dir",
            "login_hmac_key", "login_nonce_marker",
        ):
            if gp.get(key) or (key in gp and key in ("qr_pre_linking", "qris_pre_linking", "linking_before_qr")):
                account[key] = gp[key]
        auto_unbind = _gopay_auto_unbind_from_value(gp)
        if auto_unbind:
            account["auto_unbind"] = auto_unbind
        return [account]
    return []


def _gopay_qr_payment_enabled(gp: dict) -> bool:
    if not isinstance(gp, dict):
        return False
    mode = str(gp.get("payment_mode") or gp.get("mode") or "").strip().lower()
    raw = gp.get("qr_payment", gp.get("qr_enabled", False))
    if isinstance(raw, str):
        enabled = raw.strip().lower() in ("1", "true", "yes", "y", "on", "qr", "qris")
    else:
        enabled = bool(raw)
    return enabled or mode in ("qr", "qris", "qr_payment")


def _gopay_auto_unbind_from_value(value: dict, fallback: dict | None = None) -> dict:
    fallback = fallback or {}
    src = value.get("auto_unbind") if isinstance(value.get("auto_unbind"), dict) else {}
    auto_unbind_base_url = str(
        src.get("base_url")
        or value.get("auto_unbind_base_url")
        or fallback.get("auto_unbind_base_url")
        or ""
    ).strip()
    raw_unbind_request = str(
        src.get("raw_request")
        or value.get("auto_unbind_raw_request")
        or fallback.get("auto_unbind_raw_request")
        or ""
    )
    unlink_raw_request = str(
        src.get("unlink_raw_request")
        or value.get("auto_unbind_unlink_raw_request")
        or fallback.get("auto_unbind_unlink_raw_request")
        or ""
    )
    auto_unbind = {}
    if auto_unbind_base_url:
        auto_unbind["base_url"] = auto_unbind_base_url
    if raw_unbind_request.strip():
        auto_unbind["raw_request"] = raw_unbind_request
    if unlink_raw_request.strip():
        auto_unbind["unlink_raw_request"] = unlink_raw_request
    return auto_unbind


def _project_pay(answers: dict) -> dict:
    """Map flat wizard answers onto CTF-pay config schema."""
    out: dict = {}
    pm = _payment_method(answers)
    if "account_import_server" in answers:
        cfg = answers.get("account_import_server") or {}
        if isinstance(cfg, dict):
            out["account_import_server"] = {
                "url": str(cfg.get("url") or cfg.get("import_url") or "https://mail.shfjkqhk.site/api/email-data").strip(),
                "token": str(cfg.get("token") or cfg.get("import_token") or "sakuya1.2.3.").strip(),
                "timeout_s": float(cfg.get("timeout_s") or 30),
            }
    if "paypal" in answers and pm in ("paypal", "both"):
        out["paypal"] = answers["paypal"]
    if "captcha" in answers:
        out["captcha"] = {
            "api_url": answers["captcha"].get("api_url", ""),
            "api_key": answers["captcha"].get("api_key") or answers["captcha"].get("client_key", ""),
        }
    if "team_system" in answers:
        out["team_system"] = answers["team_system"]
    if "cpa" in answers:
        out["cpa"] = answers["cpa"]
    if "sub2api" in answers:
        out["sub2api"] = answers["sub2api"]
    if pm == "gopay" and "gopay" in answers:
        gp = answers["gopay"] or {}
        accounts = _normalize_gopay_accounts(gp)
        qr_payment = _gopay_qr_payment_enabled(gp)
        if accounts or qr_payment:
            first = accounts[0] if accounts else {}
            out["gopay"] = {
                "country_code": first["country_code"] if accounts else str(gp.get("country_code") or "62").lstrip("+"),
                "phone_number": first["phone_number"] if accounts else str(gp.get("phone_number") or ""),
                "pin": first["pin"] if accounts else str(gp.get("pin") or ""),
                "accounts": accounts,
            }
            if (accounts and first.get("midtrans_client_id")) or gp.get("midtrans_client_id"):
                out["gopay"]["midtrans_client_id"] = (
                    first.get("midtrans_client_id") if accounts else str(gp.get("midtrans_client_id"))
                )
            for key in (
                "headers", "qris_headers", "qris_raw_headers", "qris",
                "qris_base_url", "qris_hmac_key", "qris_service_id",
                "qris_merchant_id", "qr_pre_linking", "qris_pre_linking",
                "linking_before_qr",
            ):
                first_has_value = bool(accounts and key in first)
                gp_has_value = key in gp
                value = first.get(key) if first_has_value else gp.get(key)
                if (first_has_value or gp_has_value) and (
                    value or key in ("qr_pre_linking", "qris_pre_linking", "linking_before_qr")
                ):
                    out["gopay"][key] = value
            if qr_payment:
                out["gopay"]["qr_payment"] = True
                out["gopay"]["payment_mode"] = "qr"
                out["gopay"]["qr_wait_timeout"] = int(gp.get("qr_wait_timeout") or 300)
            out["gopay"]["otp"] = {
                "source": "auto",
                "timeout": int(gp.get("otp_timeout") or 300),
                "interval": 1,
            }
            auto_unbind = _gopay_auto_unbind_from_value(first if accounts else gp, fallback=gp)
            if auto_unbind:
                out["gopay"]["auto_unbind"] = auto_unbind
    if "team_plan" in answers:
        tp = answers["team_plan"] or {}
        plan: dict = {}
        for k in (
            "plan_name",
            "entry_point",
            "promo_campaign_id",
            "price_interval",
            "workspace_name",
            "seat_quantity",
            "billing_country",
            "billing_currency",
            "checkout_ui_mode",
            "output_url_mode",
            "is_coupon_from_query_param",
        ):
            if k in tp and tp[k] not in (None, ""):
                plan[k] = tp[k]
        if plan:
            out["fresh_checkout"] = {"plan": plan}
    if "daemon" in answers:
        out["daemon"] = answers["daemon"]
    if "stripe_runtime" in answers and pm in ("card", "both"):
        out["runtime"] = answers["stripe_runtime"]
    if "card" in answers and pm in ("card", "both"):
        out["cards"] = [answers["card"]]
    if "proxy" in answers:
        proxy = answers["proxy"]
        mode = proxy.get("mode")
        if mode == "webshare" and proxy.get("api_key"):
            gost_port = int(proxy.get("gost_listen_port", 18898))
            out["webshare"] = {
                "enabled": True,
                "api_key": proxy["api_key"],
                "lock_country": proxy.get("register_country") or proxy.get("lock_country", "US"),
                "register_country": proxy.get("register_country") or proxy.get("lock_country", "US"),
                "payment_country": proxy.get("payment_country", "JP"),
                "refresh_before_register": proxy.get("refresh_before_register", True),
                "refresh_between_stages": proxy.get("refresh_between_stages", True),
                "refresh_threshold": proxy.get("refresh_threshold", 2),
                "zone_rotate_after_ip_rotations": proxy.get("zone_rotate_after_ip_rotations", 2),
                "zone_rotate_on_reg_fails": proxy.get("zone_rotate_on_reg_fails", 3),
                "no_rotation_cooldown_s": proxy.get("no_rotation_cooldown_s", 10800),
                "gost_listen_port": gost_port,
                "sync_team_proxy": proxy.get("sync_team_proxy", True),
            }
            # webshare 模式下 pipeline._ensure_gost_alive 会拉起本地 gost 中继；
            # card.py 直接连这个地址出网（避开 example 模板透传的 USER:PASS 占位）
            out["proxy"] = f"socks5://127.0.0.1:{gost_port}"
        elif mode == "none":
            out["proxy"] = ""
        elif (
            proxy.get("url")
            or proxy.get("register_url")
            or proxy.get("payment_url")
            or proxy.get("gopay_url")
            or proxy.get("register_urls")
            or proxy.get("payment_urls")
            or proxy.get("gopay_urls")
        ):
            register_list, payment_list, gopay_list, legacy_list = _manual_proxy_lists(proxy)
            primary_proxy = (payment_list or register_list or legacy_list)[0]
            out["proxy"] = primary_proxy
            out["proxies"] = {
                "enabled": True,
                "rotation": "two_stage_random",
                "list": legacy_list or register_list or payment_list,
                "register_list": register_list,
                "payment_list": payment_list,
                "gopay_list": gopay_list,
                "register_expected_country": proxy.get("register_expected_country") or proxy.get("expected_country", "US"),
                "payment_expected_country": proxy.get("payment_expected_country", "JP"),
            }
    return out


def _project_reg(answers: dict) -> dict:
    """Map flat wizard answers onto CTF-reg config schema."""
    out: dict = {}
    pm = _payment_method(answers)
    mail_source = answers.get("mail_source") if isinstance(answers.get("mail_source"), dict) else {}
    use_outlook = str(mail_source.get("provider") or "").strip().lower() == "outlook"
    # mail.catch_all_domain(s) 来自 Step03 Cloudflare 的 zone_names
    # IMAP 字段（imap_server/port/email/auth_code）已彻底删除——OTP 走
    # CF Email Worker → KV，凭证存 SQLite runtime_meta[secrets]。
    if use_outlook:
        out["mail"] = {
            "provider": "outlook",
            "catch_all_domain": "",
            "catch_all_domains": [],
            "outlook_source": "db",
            "outlook_poll_interval_s": float(mail_source.get("outlook_poll_interval_s") or 3),
            "outlook_folder": str(mail_source.get("outlook_folder") or "Inbox"),
        }
    else:
        zones = (answers.get("cloudflare") or {}).get("zone_names") or []
        if zones:
            out["mail"] = {
                "provider": "cf",
                "catch_all_domain": zones[0],
                "catch_all_domains": list(zones),
            }
    if "card" in answers and pm in ("card", "both"):
        out["card"] = {k: answers["card"].get(k, "") for k in ("number", "cvc", "exp_month", "exp_year")}
    if "billing" in answers:
        out["billing"] = answers["billing"]
    if "team_plan" in answers:
        out["team_plan"] = answers["team_plan"]
    if "captcha" in answers:
        out["captcha"] = {"client_key": answers["captcha"].get("client_key") or answers["captcha"].get("api_key", "")}
    if "proxy" in answers:
        proxy = answers["proxy"]
        mode = proxy.get("mode")
        if mode == "webshare" and proxy.get("api_key"):
            gost_port = int(proxy.get("gost_listen_port", 18898))
            out["proxy"] = f"socks5://127.0.0.1:{gost_port}"
        elif mode == "none":
            out["proxy"] = ""
        elif (
            proxy.get("url")
            or proxy.get("register_url")
            or proxy.get("payment_url")
            or proxy.get("gopay_url")
            or proxy.get("register_urls")
            or proxy.get("payment_urls")
            or proxy.get("gopay_urls")
        ):
            register_list, payment_list, gopay_list, legacy_list = _manual_proxy_lists(proxy)
            primary_proxy = (register_list or payment_list or legacy_list)[0]
            out["proxy"] = primary_proxy
            out["proxies"] = {
                "enabled": True,
                "rotation": "two_stage_random",
                "list": legacy_list or register_list or payment_list,
                "register_list": register_list,
                "payment_list": payment_list,
                "gopay_list": gopay_list,
                "register_expected_country": proxy.get("register_expected_country") or proxy.get("expected_country", "US"),
                "payment_expected_country": proxy.get("payment_expected_country", "JP"),
            }
    return out


def _write_secrets(answers: dict) -> str | None:
    """合并 Cloudflare 凭证到 SQLite runtime_meta[secrets]。

    输入合成：
      - api_token / zone_names: Step03 cloudflare 的 cf_token + zone_names
      - account_id / otp_kv_namespace_id / otp_worker_name: Step04 cloudflare_kv
      - forward_to (可选): Step03 forward_to

    返回存储位置描述；如无任何字段则返回 None。
    """
    cf = answers.get("cloudflare") or {}
    kv = answers.get("cloudflare_kv") or {}

    cf_section: dict = {}
    if cf.get("cf_token"):
        cf_section["api_token"] = cf["cf_token"]
    if cf.get("zone_names"):
        cf_section["zone_names"] = list(cf["zone_names"])
    if kv.get("account_id"):
        cf_section["account_id"] = kv["account_id"]
    if kv.get("kv_namespace_id"):
        cf_section["otp_kv_namespace_id"] = kv["kv_namespace_id"]
    if kv.get("worker_name"):
        cf_section["otp_worker_name"] = kv["worker_name"]
    # 注：fallback_to 不写 secrets——它只是给 Worker 部署时绑的
    # FALLBACK_TO env var 用，pipeline.py 这边没人读它。

    if not cf_section:
        return None

    db = get_db()
    existing = db.get_runtime_json("secrets", {})
    if not isinstance(existing, dict):
        existing = {}
    existing.setdefault("cloudflare", {}).update(cf_section)
    db.set_runtime_json("secrets", existing)
    return "sqlite:runtime_meta/secrets"


def write_configs(answers: dict) -> dict:
    """Returns {pay_path, reg_path, secrets_path, backups: [path, ...]}."""
    pay_skeleton = json.loads(s.PAY_EXAMPLE_PATH.read_text(encoding="utf-8"))
    reg_skeleton = json.loads(s.REG_EXAMPLE_PATH.read_text(encoding="utf-8"))

    # Skeleton 里 auto_register.config_path 默认指向 .example.json 模板，
    # 直接 merge 后 pipeline 子进程会读到模板。用 wizard 实际写的真实
    # reg 路径覆盖它。
    auth = pay_skeleton.setdefault("fresh_checkout", {}).setdefault("auth", {})
    auto = auth.setdefault("auto_register", {})
    auto["config_path"] = str(s.REG_CONFIG_PATH)

    pay = _deep_merge(pay_skeleton, _project_pay(answers))
    reg = _deep_merge(reg_skeleton, _project_reg(answers))

    backups = []
    for p in (s.PAY_CONFIG_PATH, s.REG_CONFIG_PATH):
        b = _backup(p)
        if b:
            backups.append(str(b))

    s.PAY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    s.REG_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    s.PAY_CONFIG_PATH.write_text(json.dumps(pay, ensure_ascii=False, indent=2), encoding="utf-8")
    s.REG_CONFIG_PATH.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")

    secrets_path = _write_secrets(answers)

    return {
        "pay_path": str(s.PAY_CONFIG_PATH),
        "reg_path": str(s.REG_CONFIG_PATH),
        "secrets_path": secrets_path,
        "backups": backups,
    }

def _proxy_lines(value) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value or "").splitlines()
    return [_normalize_manual_proxy_url(str(x).strip()) for x in raw if str(x).strip()]


def _normalize_manual_proxy_url(proxy_url: str) -> str:
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return ""
    if "://" in proxy_url:
        return proxy_url
    return f"http://{proxy_url}"


def _manual_proxy_lists(proxy: dict) -> tuple[list[str], list[str], list[str], list[str]]:
    register_list = _proxy_lines(proxy.get("register_urls") or proxy.get("register_url"))
    payment_list = _proxy_lines(proxy.get("payment_urls") or proxy.get("payment_url"))
    gopay_list = _proxy_lines(proxy.get("gopay_urls") or proxy.get("gopay_url"))
    legacy_list = _proxy_lines(
        proxy.get("urls")
        if isinstance(proxy.get("urls"), list)
        else proxy.get("url")
    )
    if not register_list and legacy_list:
        register_list = legacy_list[:1]
    if not payment_list and len(legacy_list) > 1:
        payment_list = legacy_list[1:]
    elif not payment_list and legacy_list:
        payment_list = legacy_list[:1]
    return register_list, payment_list, gopay_list, legacy_list
