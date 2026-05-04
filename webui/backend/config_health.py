"""Mode-aware configuration health checks for pipeline starts.

The goal is to fail fast before a real registration/payment run consumes time
or external state.  The checks are intentionally local and side-effect free:
they inspect SQLite runtime config, exported JSON config, and local inventory,
but they do not call Cloudflare/OpenAI/Stripe/PayPal/GoPay.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import settings as s
from .account_inventory import build_accounts_inventory
from .db import get_db
from . import wa_relay


_PLACEHOLDER_MARKERS = (
    "your_",
    "your-",
    "example.com",
    "example street",
    "tester@example.com",
    "you@your-catch-all-zone.com",
    "your_paypal_password",
    "your_6_digit_gopay_pin",
    "subdomain.example.com",
    "change_me",
    "changeme",
    "todo",
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _is_missing(value: Any, *, allow_example: bool = False) -> bool:
    text = _text(value)
    if not text:
        return True
    if allow_example:
        return False
    low = text.lower()
    if low in {"", "none", "null", "undefined"}:
        return True
    return any(marker in low for marker in _PLACEHOLDER_MARKERS)


def _get(obj: Any, path: str, default: Any = None) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
    return default if cur is None else cur


def _load_json(path: Path) -> tuple[dict, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, f"File not found: {path}"
    except Exception as e:
        return {}, f"JSON parse error: {path}: {e}"
    if not isinstance(data, dict):
        return {}, f"JSON top-level is not object: {path}"
    return data, ""


def _resolve_reg_config_path(pay_cfg: dict) -> Path:
    raw = _text(_get(pay_cfg, "fresh_checkout.auth.auto_register.config_path"))
    if not raw:
        return s.REG_CONFIG_PATH
    p = Path(raw)
    return p if p.is_absolute() else (s.ROOT / p)


def _check(
    checks: list[dict],
    name: str,
    status: str,
    message: str,
    *,
    missing: list[str] | None = None,
    blocking: bool | None = None,
    details: str = "",
    action: str = "",
) -> None:
    if blocking is None:
        blocking = status == "fail"
    checks.append({
        "name": name,
        "status": status,
        "message": message,
        "missing": missing or [],
        "blocking": bool(blocking),
        "details": details,
        "action": action,
    })


def _effective_cloudflare_secret_presence() -> dict[str, bool]:
    secrets = get_db().get_runtime_json("secrets", {})
    cf = (secrets.get("cloudflare") or {}) if isinstance(secrets, dict) else {}
    return {
        "cloudflare.api_token": bool(
            _text(os.getenv("CF_API_TOKEN"))
            or _text(cf.get("kv_api_token"))
            or _text(cf.get("api_token"))
        ),
        "cloudflare.account_id": bool(
            _text(os.getenv("CF_ACCOUNT_ID"))
            or _text(cf.get("account_id"))
        ),
        "cloudflare.otp_kv_namespace_id": bool(
            _text(os.getenv("CF_OTP_KV_NAMESPACE_ID"))
            or _text(cf.get("otp_kv_namespace_id"))
        ),
    }


def _missing_paths(obj: dict, paths: list[str]) -> list[str]:
    return [p for p in paths if _is_missing(_get(obj, p))]


def _requires_registration(req: dict) -> bool:
    mode = _text(req.get("mode")) or "single"
    if mode == "free_register":
        return True
    if mode == "free_backfill_rt":
        return False
    return not bool(req.get("pay_only"))


def _requires_email_otp(req: dict) -> bool:
    mode = _text(req.get("mode")) or "single"
    # free_backfill_rt does not create a new mailbox, but OAuth login still
    # needs the OpenAI email OTP provider for existing accounts.
    return _requires_registration(req) or mode == "free_backfill_rt" or bool(req.get("register_only"))


def _payment_kind(req: dict) -> str:
    mode = _text(req.get("mode")) or "single"
    if mode in {"free_register", "free_backfill_rt"} or bool(req.get("register_only")):
        return "none"
    if bool(req.get("gopay")):
        return "gopay"
    if bool(req.get("paypal", True)):
        return "paypal"
    return "card"


def _config_has_embedded_auth(pay_cfg: dict) -> bool:
    auth = _get(pay_cfg, "fresh_checkout.auth", {})
    if not isinstance(auth, dict):
        return False
    return any(
        not _is_missing(auth.get(key), allow_example=True)
        for key in ("session_token", "access_token", "cookie_header")
    )


def _check_config_files(checks: list[dict], req: dict) -> tuple[dict, dict, Path]:
    pay_cfg, pay_err = _load_json(s.PAY_CONFIG_PATH)
    if pay_err:
        _check(
            checks,
            "pay_config",
            "fail",
            "Payment/runtime config file unavailable",
            missing=[str(s.PAY_CONFIG_PATH)],
            details=pay_err,
            action="Export config in wizard, or fix CTF-pay/config.paypal.json",
        )
        return {}, {}, s.REG_CONFIG_PATH
    _check(
        checks,
        "pay_config",
        "ok",
        "Payment/runtime config file readable",
        details=str(s.PAY_CONFIG_PATH),
        blocking=False,
    )

    reg_path = _resolve_reg_config_path(pay_cfg)
    reg_cfg: dict = {}
    if _requires_registration(req):
        reg_cfg, reg_err = _load_json(reg_path)
        if reg_err:
            _check(
                checks,
                "reg_config",
                "fail",
                "Registration config file unavailable",
                missing=[str(reg_path)],
                details=reg_err,
                action="Re-export config wizard, ensure fresh_checkout.auth.auto_register.config_path points to real CTF-reg config",
            )
        else:
            _check(
                checks,
                "reg_config",
                "ok",
                "Registration config file readable",
                details=str(reg_path),
                blocking=False,
            )
    return pay_cfg, reg_cfg, reg_path


def _check_cloudflare_kv(checks: list[dict], req: dict) -> None:
    presence = _effective_cloudflare_secret_presence()
    missing = [name for name, ok in presence.items() if not ok]
    if not missing:
        _check(
            checks,
            "cloudflare_kv_secrets",
            "ok",
            "Cloudflare KV OTP credentials configured",
            details="Source: env vars or SQLite runtime_meta[secrets].cloudflare",
            blocking=False,
        )
        return

    if _requires_email_otp(req):
        _check(
            checks,
            "cloudflare_kv_secrets",
            "fail",
            "Registration/OAuth email OTP requires Cloudflare KV credentials",
            missing=missing,
            action="Re-save in wizard Cloudflare KV step, or write to SQLite runtime_meta[secrets].cloudflare",
        )
    else:
        _check(
            checks,
            "cloudflare_kv_secrets",
            "warn",
            "Cloudflare KV OTP credentials missing; pay-only can continue, but subsequent RT/CPA may fail",
            missing=missing,
            blocking=False,
            action="If auto refresh_token is needed, please fill in Cloudflare KV credentials first",
        )


def _check_registration_config(checks: list[dict], req: dict, reg_cfg: dict) -> None:
    if not _requires_registration(req):
        return
    mail = reg_cfg.get("mail") if isinstance(reg_cfg.get("mail"), dict) else {}
    domains = mail.get("catch_all_domains")
    has_domain = False
    if isinstance(domains, list):
        has_domain = any(not _is_missing(x) for x in domains)
    has_domain = has_domain or not _is_missing(mail.get("catch_all_domain"))
    if not has_domain:
        _check(
            checks,
            "mail_domains",
            "fail",
            "Registration requires catch-all email domain",
            missing=["mail.catch_all_domain or mail.catch_all_domains"],
            action="Fill in zone_names in wizard Cloudflare step, then re-export config",
        )
    else:
        _check(
            checks,
            "mail_domains",
            "ok",
            "Registration email domain configured",
            blocking=False,
        )

    captcha_key = _text(_get(reg_cfg, "captcha.client_key"))
    if not captcha_key:
        _check(
            checks,
            "captcha",
            "warn",
            "Registration captcha client_key not configured; may fail on CAPTCHA challenge",
            missing=["captcha.client_key"],
            blocking=False,
            action="If recent registration triggers CAPTCHA, fill in CAPTCHA service config in wizard first",
        )


def _check_payment_config(checks: list[dict], req: dict, pay_cfg: dict) -> None:
    kind = _payment_kind(req)
    if kind == "none":
        _check(checks, "payment_config", "ok", "Current mode disables payment", blocking=False)
        return

    if kind == "gopay":
        gp = pay_cfg.get("gopay") if isinstance(pay_cfg.get("gopay"), dict) else {}
        missing = [
            key for key in ("country_code", "phone_number", "pin")
            if _is_missing(gp.get(key))
        ]
        if missing:
            _check(
                checks,
                "gopay_config",
                "fail",
                "GoPay payment config incomplete",
                missing=[f"gopay.{x}" for x in missing],
                action="Fill in country code, phone number, and 6-digit PIN in wizard GoPay step, then re-export",
            )
        else:
            _check(checks, "gopay_config", "ok", "GoPay payment config configured", blocking=False)

        wa = wa_relay.status()
        if wa.get("status") == "connected":
            _check(
                checks,
                "whatsapp_relay",
                "ok",
                "WhatsApp relay connected, can auto-receive GoPay OTP",
                details=f"engine={wa.get('engine')}",
                blocking=False,
            )
        else:
            _check(
                checks,
                "whatsapp_relay",
                "warn",
                "WhatsApp relay not connected; GoPay OTP will wait for auto-relay or manual entry",
                details=f"status={wa.get('status')}",
                blocking=False,
                action="To auto-receive GoPay OTP, open WhatsApp login entry and scan QR code",
            )
        return

    if kind == "paypal":
        pp = pay_cfg.get("paypal") if isinstance(pay_cfg.get("paypal"), dict) else {}
        missing = [
            key for key in ("email", "password")
            if _is_missing(pp.get(key))
        ]
        if missing:
            _check(
                checks,
                "paypal_config",
                "fail",
                "PayPal payment config incomplete",
                missing=[f"paypal.{x}" for x in missing],
                action="Fill in email and password in wizard PayPal step, then re-export",
            )
        else:
            _check(checks, "paypal_config", "ok", "PayPal payment config configured", blocking=False)
        return

    cards = pay_cfg.get("cards") if isinstance(pay_cfg.get("cards"), list) else []
    usable = [
        c for c in cards
        if isinstance(c, dict)
        and all(not _is_missing(c.get(k), allow_example=True) for k in ("number", "cvc", "exp_month", "exp_year"))
    ]
    if not usable:
        _check(
            checks,
            "card_config",
            "fail",
            "Card payment config incomplete",
            missing=["cards[0].number", "cards[0].cvc", "cards[0].exp_month", "cards[0].exp_year"],
            action="Fill in card info in wizard card step, then re-export",
        )
    else:
        first = str(usable[0].get("number") or "")
        if first.startswith("424242"):
            _check(
                checks,
                "card_config",
                "warn",
                "Detected likely Stripe test card number; confirm real card before actual payment",
                blocking=False,
            )
        else:
            _check(checks, "card_config", "ok", "Card payment config configured", blocking=False)


def _check_pay_only_inventory(checks: list[dict], req: dict, pay_cfg: dict) -> None:
    if not bool(req.get("pay_only")):
        return
    inv = build_accounts_inventory()
    eligible = int((inv.get("counts") or {}).get("pay_only_eligible", 0) or 0)
    if eligible > 0:
        _check(
            checks,
            "pay_only_inventory",
            "ok",
            f"Reusable account inventory: {eligible}",
            blocking=False,
        )
        return
    if _config_has_embedded_auth(pay_cfg):
        _check(
            checks,
            "pay_only_inventory",
            "warn",
            "No reusable accounts in DB, will fall back to config auth",
            blocking=False,
        )
        return
    _check(
        checks,
        "pay_only_inventory",
        "fail",
        "pay-only has no reusable accounts and no fallback auth in config",
        missing=["registered_accounts reusable accounts", "fresh_checkout.auth.session_token/access_token/cookie_header"],
        action="Run register-only/registration first, or fill usable session/access token in config",
    )


def _check_cpa(checks: list[dict], req: dict, pay_cfg: dict) -> None:
    cpa = pay_cfg.get("cpa") if isinstance(pay_cfg.get("cpa"), dict) else {}
    mode = _text(req.get("mode")) or "single"
    if not cpa.get("enabled"):
        if mode in {"free_register", "free_backfill_rt"}:
            _check(
                checks,
                "cpa_config",
                "warn",
                "CPA not enabled; free mode will register/backfill RT but won't push to CPA",
                blocking=False,
            )
        return

    required = ["base_url", "admin_key"]
    missing = [f"cpa.{p}" for p in required if _is_missing(cpa.get(p), allow_example=True)]

    if missing:
        _check(
            checks,
            "cpa_config",
            "fail" if mode in {"free_register", "free_backfill_rt"} else "warn",
            "CPA config incomplete",
            missing=missing,
            blocking=mode in {"free_register", "free_backfill_rt"},
            action="Fill in base_url/admin_key in wizard CPA step, then re-export",
        )
    else:
        _check(checks, "cpa_config", "ok", "CPA config configured", blocking=False)


def _check_team_system(checks: list[dict], req: dict, pay_cfg: dict) -> None:
    mode = _text(req.get("mode")) or "single"
    if mode != "daemon":
        return
    ts = pay_cfg.get("team_system") if isinstance(pay_cfg.get("team_system"), dict) else {}
    missing = []
    if not ts.get("enabled"):
        missing.append("team_system.enabled")
    for key in ("base_url", "username", "password"):
        if _is_missing(ts.get(key), allow_example=True):
            missing.append(f"team_system.{key}")
    if missing:
        _check(
            checks,
            "team_system",
            "fail",
            "daemon requires team_system config",
            missing=missing,
            action="Fill in team system config in wizard, then re-export",
        )
    else:
        _check(checks, "team_system", "ok", "team_system config configured", blocking=False)


def _check_free_backfill_inventory(checks: list[dict], req: dict) -> None:
    if _text(req.get("mode")) != "free_backfill_rt":
        return
    inv = build_accounts_inventory()
    counts = inv.get("counts") or {}
    total = int(counts.get("registered_total", 0) or 0)
    candidates = int(counts.get("rt_missing", 0) or 0) + int(counts.get("rt_retryable", 0) or 0)
    if total <= 0:
        _check(
            checks,
            "backfill_inventory",
            "fail",
            "No old accounts in DB to backfill RT",
            missing=["registered_accounts"],
            action="Run registration to build account inventory first",
        )
    elif candidates <= 0:
        _check(
            checks,
            "backfill_inventory",
            "warn",
            "Account inventory exists but no RT pending/retryable accounts",
            blocking=False,
        )
    else:
        _check(checks, "backfill_inventory", "ok", f"RT pending/retryable accounts: {candidates}", blocking=False)


def build_config_health(req: dict | None = None) -> dict:
    req = dict(req or {})
    req.setdefault("mode", "single")
    req.setdefault("paypal", True)
    checks: list[dict] = []

    pay_cfg, reg_cfg, reg_path = _check_config_files(checks, req)
    if pay_cfg:
        _check_cloudflare_kv(checks, req)
        _check_registration_config(checks, req, reg_cfg)
        _check_payment_config(checks, req, pay_cfg)
        _check_pay_only_inventory(checks, req, pay_cfg)
        _check_cpa(checks, req, pay_cfg)
        _check_team_system(checks, req, pay_cfg)
        _check_free_backfill_inventory(checks, req)

    blocking = [c for c in checks if c.get("blocking") and c.get("status") == "fail"]
    return {
        "ok": not blocking,
        "mode": req.get("mode"),
        "payment_kind": _payment_kind(req),
        "requires_registration": _requires_registration(req),
        "requires_email_otp": _requires_email_otp(req),
        "paths": {
            "pay_config": str(s.PAY_CONFIG_PATH),
            "reg_config": str(reg_path),
            "database": str(get_db().path),
        },
        "checks": checks,
        "blocking": blocking,
    }


def health_error_message(health: dict) -> str:
    blocking = health.get("blocking") or []
    if not blocking:
        return ""
    head = blocking[0].get("message") or "Config health check failed"
    missing: list[str] = []
    for check in blocking:
        missing.extend(check.get("missing") or [])
    suffix = f"; missing: {', '.join(missing[:6])}" if missing else ""
    if len(missing) > 6:
        suffix += f" and {len(missing)} more items"
    return head + suffix