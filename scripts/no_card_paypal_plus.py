#!/usr/bin/env python3
"""Activate ChatGPT Plus using promo long links + PayPal no-card pure protocol flow.

This script breaks down page actions done in Tampermonkey into pure HTTP:

1. Read promo long links from inventory (promo_links.checkout_url / cs_live_xxx).
2. Stripe hosted checkout side:
   - payment_pages/init
   - elements / link lookup / address update
   - Create payment_method with type=paypal
   - payment_pages/<cs>/confirm
3. PayPal side:
   - Extract BA token from Stripe redirect
   - Replicate no-card signup GraphQL from /root/no_card_paypal_plus mitm dump:
     SMS OTP -> SignUpNewMember(no card) -> billing.authorize
4. Callback to Stripe, poll results; after success, mark promo link as used and refresh inventory plan with RT.

Strict "pure protocol" by default: will not launch Camoufox/Playwright or go through PayPal Web login.
If PayPal DataDome blocks 403 on current exit IP, this script will fail directly and preserve logs."""
from __future__ import annotations

import argparse
import atexit
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
CARD_DIR = ROOT / "CTF-pay"
REG_DIR = ROOT / "CTF-reg"
OUTPUT_DIR = ROOT / "output"
RUNTIME_DIR = CARD_DIR / ".runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
(OUTPUT_DIR / "logs").mkdir(parents=True, exist_ok=True)

for p in (ROOT, CARD_DIR, REG_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from webui.backend.db import get_db  # noqa: E402


def _resolve_worker_id(args_worker_id: str = "") -> str:
    """Unified worker identifier source: CLI > env > default 'w<pid>'.
    When multiple workers run concurrently, each worker must have a different id, 
    otherwise file paths and DB claims will conflict with themselves."""
    wid = (args_worker_id or "").strip()
    if not wid:
        wid = os.environ.get("NCPP_WORKER_ID", "").strip()
    if not wid:
        wid = f"w{os.getpid()}"
    # Restrict to only alphanumeric characters, underscores, and hyphens to avoid path traversal / SQL injection vulnerabilities
    wid = re.sub(r"[^A-Za-z0-9_\-]", "_", wid)[:32] or "w0"
    return wid


def _tmp_path(suffix: str) -> str:
    """Per-worker isolated /tmp path to prevent concurrent worker file conflicts.

    Single worker (NCPP_WORKER_ID not set) → /tmp/paypal_node_rpa_<suffix> (backward compatible)
    Multiple workers (env set) → /tmp/paypal_node_rpa_<worker>_<suffix>"""
    wid = re.sub(r"[^A-Za-z0-9_\-]", "", (os.environ.get("NCPP_WORKER_ID") or "").strip())
    base = "/tmp/paypal_node_rpa" + (f"_{wid}" if wid else "")
    return f"{base}_{suffix}"


COUNTRY_ADDRESS: dict[str, dict[str, str]] = {
    "US": {
        "country": "US", "line1": "123 Main St", "city": "New York",
        "state": "NY", "postal_code": "10001",
        # userscript v32 hides address autocomplete and fills the manual
        # billing fields, so PayPal signup should advertise MANUAL by default.
        "autoCompleteType": "MANUAL",
        "isUserModified": False,
    },
    "GB": {
        "country": "GB", "line1": "10 Downing Street", "city": "London",
        "state": "London", "postal_code": "SW1A 2AA",
    },
    "IE": {
        "country": "IE", "line1": "1 Dame Street", "city": "Dublin",
        "state": "Dublin", "postal_code": "D02 XH24",
    },
    "FR": {
        "country": "FR", "line1": "10 Rue de Rivoli", "city": "Paris",
        "state": "", "postal_code": "75004",
    },
    "DE": {
        "country": "DE", "line1": "Unter den Linden 1", "city": "Berlin",
        "state": "Berlin", "postal_code": "10117",
    },
    "ID": {
        "country": "ID", "line1": "Jl. Jend. Sudirman No. 1", "city": "Jakarta",
        "state": "DKI Jakarta", "postal_code": "10220",
    },
}

US_STATE_ABBR: dict[str, str] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT",
    "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL",
    "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY",
    "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
}


def _us_state_code(value: str) -> str:
    v = (value or "").strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return US_STATE_ABBR.get(v.upper(), v)


def _split_full_name(value: str) -> tuple[str, str]:
    """Return a PayPal-friendly first/last name from meiguodizhi Full_Name."""
    clean = re.sub(r"[^A-Za-z.\-\' ]+", " ", str(value or "")).strip()
    clean = re.sub(r"\s+", " ", clean)
    parts = [p for p in clean.split(" ") if p]
    if len(parts) >= 2:
        return parts[0].title(), " ".join(parts[1:]).title()
    # occasionally return word names from meiguodizhi; use local random English names as fallback.
    fallback = _rand_name().title().split()
    return fallback[0], fallback[-1]


def _parse_card_expiry(value: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in str(value or "").replace("/", " ").split() if p.strip()]
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        mm = parts[0].zfill(2)[:2]
        yyyy = ("20" + parts[1]) if len(parts[1]) == 2 else parts[1]
        yy = yyyy[-2:]
        return mm, yyyy, f"{mm}/{yyyy}"
    return "", "", ""


def _looks_like_card(number: str) -> bool:
    n = re.sub(r"\D+", "", str(number or ""))
    return 12 <= len(n) <= 19 and len(set(n)) > 1


def _fetch_userscript_us_address(timeout: int = 20, *, require_card: bool = False,
                                 attempts: int = 5) -> dict[str, str]:
    """Pure-protocol equivalent of userscript getAddr(path='/', method='address').

    meiguodizhi returns name/address/phone/card fields in a single response.
    When random card is needed, set require_card=True, which will retry until
    Credit_Card_Number/CVV2/Expires are available."""
    fallback = dict(COUNTRY_ADDRESS["US"])
    last_err = ""
    for i in range(max(1, attempts if require_card else 1)):
        try:
            r = requests.post(
                "https://www.meiguodizhi.com/api/v1/dz",
                json={"path": "/", "method": "address"},
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://www.meiguodizhi.com",
                    "Referer": "https://www.meiguodizhi.com/",
                },
                timeout=timeout,
            )
            r.raise_for_status()
            data = r.json()
            a = data.get("address") if isinstance(data, dict) else None
            if not isinstance(a, dict):
                last_err = "response.address missing"
                continue
            state = _us_state_code(str(a.get("State") or a.get("State_Full") or "NY"))
            first_name, last_name = _split_full_name(str(a.get("Full_Name") or ""))
            card_number = re.sub(r"\D+", "", str(a.get("Credit_Card_Number") or ""))
            exp_month, exp_year, card_expiry = _parse_card_expiry(str(a.get("Expires") or ""))
            card_cvv = re.sub(r"\D+", "", str(a.get("CVV2") or ""))
            card_ok = _looks_like_card(card_number) and bool(exp_month and exp_year and card_cvv)
            if require_card and not card_ok:
                last_err = "card fields missing/undefined"
                continue
            out = {
                "country": "US",
                "line1": str(a.get("Address") or fallback["line1"]),
                "city": str(a.get("City") or fallback["city"]),
                "state": state,
                "postal_code": str(a.get("Zip_Code") or fallback["postal_code"])[:5],
                # Retain complete persona for PayPal Node RPA use; Stripe side read-only
                # line1/city/state/postal_code/country, extra keys will not be included in the request fields.
                "first_name": first_name,
                "last_name": last_name,
                "full_name": f"{first_name} {last_name}",
                "telephone": str(a.get("Telephone") or ""),
                "source": "meiguodizhi",
                "autoCompleteType": "MANUAL",
                "isUserModified": False,
            }
            if card_ok:
                out.update({
                    "card_number": card_number,
                    "card_exp_month": exp_month,
                    "card_exp_year": exp_year,
                    "card_expiry": card_expiry,
                    "card_cvv": card_cvv,
                    "card_type": str(a.get("Credit_Card_Type") or ""),
                })
            return out
        except Exception as e:
            last_err = str(e)
    print(f"[addr] meiguodizhi path=/ 获取失败，使用 fallback: {last_err}")
    return fallback


def _rand_email() -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=16)) + "@gmail.com"


def _rand_name() -> str:
    first = random.choice(["James", "John", "Robert", "Michael", "William", "David"])
    last = random.choice(["Smith", "Johnson", "Brown", "Williams", "Miller", "Davis"])
    return f"{first} {last}".upper()


def _card_type(number: str) -> str:
    n = (number or "").strip().replace(" ", "")
    if n.startswith("4"):
        return "VISA"
    if n[:2].isdigit() and 51 <= int(n[:2]) <= 55:
        return "MASTERCARD"
    if n[:2] in {"34", "37"}:
        return "AMEX"
    return "VISA"


def _mask_url(url: str) -> str:
    m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", url or "")
    if not m:
        return (url or "")[:100]
    cs = m.group(1)
    masked = url.replace(cs, cs[:18] + "..." + cs[-6:])
    return masked[:160] + ("..." if len(masked) > 160 else "")


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是 JSON object")
    return data


_MICROSOFT_EMAIL_DOMAINS = ("outlook.com", "hotmail.com", "live.com", "msn.com")


def _load_catch_all_domains() -> list[str]:
    """Read configured catch_all_domains from CTF-reg config; fallback to common one."""
    candidates = [
        REG_DIR / "config.paypal-proxy.json",
        REG_DIR / "config.json",
    ]
    for path in candidates:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            mail = (data.get("mail") or {})
            doms = mail.get("catch_all_domains") or []
            if isinstance(doms, list) and doms:
                return [str(d).strip().lower() for d in doms if d]
            single = mail.get("catch_all_domain")
            if single:
                return [str(single).strip().lower()]
        except Exception:
            continue
    return []


def _pick_inventory_account_for_promo(
    args: argparse.Namespace,
    exclude_emails: set[str] | None = None,
) -> dict[str, Any] | None:
    """Pick a registered_accounts row eligible for promo_link auto-generation.

    Eligibility:
      - has access_token + cookie_header
      - last_plan_type not paid/deactivated
      - last_check_status not invalid/deactivated
      - no existing 'fresh' plus promo_link (avoid double-gen)
      - not in exclude_emails (for rotation re-pick)
      - --inventory-mail-source filters by email domain family

    With --email: respect that email only. Else newest-id eligible row.
    """
    exclude = {(e or "").strip().lower() for e in (exclude_emails or set()) if e}
    where: list[str] = [
        "length(coalesce(access_token,'')) > 0",
        "length(coalesce(cookie_header,'')) > 0",
        "(last_plan_type IS NULL OR lower(last_plan_type) NOT IN ("
        "'plus','team','pro','chatgptplusplan','chatgptteamplan','chatgptproplan','deactivated'))",
        "(last_check_status IS NULL OR lower(last_check_status) NOT IN ("
        "'deactivated','invalid','account_deactivated'))",
        # Plus promo_link with fresh or in_use status are regarded as the email being occupied by another worker,
        # Prevent two concurrent workers from simultaneously picking the same inventory account and each fetching promo_link duplicates.
        "NOT EXISTS ("
        " SELECT 1 FROM promo_links pl"
        " WHERE lower(pl.email) = lower(registered_accounts.email)"
        "   AND lower(pl.plan_name) LIKE '%plus%'"
        "   AND pl.status IN ('fresh', 'in_use'))",
    ]
    params: list[Any] = []
    if args.email:
        where.append("lower(email) = lower(?)")
        params.append(args.email.strip())

    source = (getattr(args, "inventory_mail_source", "any") or "any").strip().lower()
    if source == "outlook":
        clauses = " OR ".join(["lower(email) LIKE ?"] * len(_MICROSOFT_EMAIL_DOMAINS))
        where.append(f"({clauses})")
        params.extend(f"%@{d}" for d in _MICROSOFT_EMAIL_DOMAINS)
    elif source == "catch_all":
        domains = _load_catch_all_domains()
        if not domains:
            print("[auto-gen] inventory_mail_source=catch_all 但 CTF-reg config 没配 catch_all_domain")
            return None
        clauses = " OR ".join(["lower(email) LIKE ?"] * len(domains))
        where.append(f"({clauses})")
        params.extend(f"%@{d}" for d in domains)
    elif source not in ("any", ""):
        print(f"[auto-gen] 未知 inventory_mail_source={source!r}，按 any 处理")

    sql = f"""
        SELECT id, email, access_token, cookie_header, device_id, session_token,
               last_plan_type, last_check_status
        FROM registered_accounts
        WHERE {' AND '.join(where)}
        ORDER BY id DESC
        LIMIT 50
    """
    try:
        with get_db()._conn() as c:
            rows = [dict(r) for r in c.execute(sql, tuple(params)).fetchall()]
    except Exception as e:
        print(f"[auto-gen] 查询 inventory 失败: {e}")
        return None
    for r in rows:
        em = (r.get("email") or "").strip().lower()
        if em and em not in exclude:
            return r
    return None


def _reserve_promo_link_slot(
    *,
    email: str,
    worker_id: str,
    plan_name: str,
    promo_campaign_id: str,
    billing_country: str,
    billing_currency: str,
) -> int:
    """First INSERT a promo_link placeholder row (status='in_use', checkout_url=''),
    return the new row id. picker SQL excludes 'fresh'+'in_use' so concurrent workers
    immediately don't see this email. Won't be snatched during fetch. On failure, call
    release_promo_link(id, 'expired') or DELETE."""
    import time as _t
    with get_db()._conn() as c:
        cur = c.execute(
            """
            INSERT INTO promo_links(
              email, checkout_url, cs_id, processor_entity,
              plan_name, promo_campaign_id, billing_country, billing_currency,
              amount_due_cents, status, created_at, raw_response, claimed_by, claimed_at
            ) VALUES (?, '', '', '', ?, ?, ?, ?, 0, 'in_use', ?, '', ?, ?)
            """,
            (
                email,
                plan_name,
                promo_campaign_id,
                billing_country,
                billing_currency,
                _t.time(),
                worker_id,
                _t.time(),
            ),
        )
        return int(cur.lastrowid or 0)


def _release_reserved_slot(slot_id: int) -> None:
    """Delete the placeholder row when fetch fails (different from expired: this row didn't fetch the url at all, shouldn't be kept)."""
    if not slot_id:
        return
    try:
        with get_db()._conn() as c:
            c.execute("DELETE FROM promo_links WHERE id=? AND status='in_use' AND checkout_url=''", (int(slot_id),))
    except Exception:
        pass


def _finalize_reserved_slot(
    slot_id: int,
    *,
    checkout_url: str,
    cs_id: str,
    processor_entity: str,
    plan_name: str,
    promo_campaign_id: str,
    billing_country: str,
    billing_currency: str,
    amount_due_cents: int,
    raw_response: Any,
) -> bool:
    """After fetch succeeds, fill in the fields of the placeholder row, keep the status as in_use+claimed_by=self, and let _select_promo_link return directly."""
    if not slot_id:
        return False
    import json as _json
    try:
        with get_db()._conn() as c:
            cur = c.execute(
                """
                UPDATE promo_links
                   SET checkout_url=?, cs_id=?, processor_entity=?,
                       plan_name=?, promo_campaign_id=?,
                       billing_country=?, billing_currency=?,
                       amount_due_cents=?, raw_response=?
                 WHERE id=? AND status='in_use'
                """,
                (
                    checkout_url, cs_id, processor_entity,
                    plan_name, promo_campaign_id,
                    billing_country, billing_currency,
                    int(amount_due_cents),
                    _json.dumps(raw_response, ensure_ascii=False) if isinstance(raw_response, dict) else str(raw_response or ""),
                    int(slot_id),
                ),
            )
            return cur.rowcount > 0
    except Exception:
        return False


def _auto_generate_promo_link(
    args: argparse.Namespace,
    exclude_emails: set[str] | None = None,
    max_attempts: int = 6,
    worker_id: str = "",
) -> dict[str, Any] | None:
    """Use an inventory account's auth to call ChatGPT checkout API and persist
    the resulting promo long-URL to promo_links. Returns the inserted row dict
    or None on failure.

    When a single account's fetch_promo_link fails (network/AT invalid/upstream
    proxy not running), add it to exclude and retry with the next inventory account,
    up to max_attempts times, instead of giving up directly."""
    base_cfg: dict[str, Any] = {}
    try:
        if args.config and Path(args.config).exists():
            base_cfg = _load_json(Path(args.config))
    except Exception:
        base_cfg = {}
    proxy_url = (args.proxy or base_cfg.get("proxy") or "").strip()
    country = (args.billing_country or "GB").upper()
    currency = (args.billing_currency or ("GBP" if country == "GB" else "USD")).upper()
    campaign = args.promo_campaign_id or "plus-1-month-free"

    try:
        from pipeline.promo_link import fetch_promo_link  # type: ignore
    except Exception as e:
        print(f"[auto-gen] import pipeline.promo_link 失败: {e}")
        return None

    # Key: Pass the entire base_cfg so that _ensure_gost_alive can see the webshare section, otherwise
    # Unable to pull gost(127.0.0.1:18898) → fetch_promo_link will inevitably result in ConnectionError.
    _ensure_proxy_alive(base_cfg if base_cfg else ({"proxy": proxy_url} if proxy_url else {}))

    tried: set[str] = {(e or "").strip().lower() for e in (exclude_emails or set()) if e}
    last_err = ""
    slot_id = 0
    plan_name_final = "chatgptplusplan"
    wid_for_slot = (worker_id or os.environ.get("NCPP_WORKER_ID") or "").strip() or f"w{os.getpid()}"
    for attempt in range(1, max_attempts + 1):
        acc = _pick_inventory_account_for_promo(args, exclude_emails=tried)
        if not acc:
            if attempt == 1:
                print("[auto-gen] 库存里没有可用账号（需要 access_token+cookie 且未付费/未停用）")
            else:
                print(f"[auto-gen] 候选邮箱用尽 ({attempt - 1} 次尝试后)")
            return None
        email = acc.get("email") or ""
        tried.add(email.strip().lower())

        # Key: immediately INSERT placeholder after pick (status='in_use' + claimed_by=self),
        # Make other workers' picker NOT EXISTS check immediately see that this email has been "claimed",
        # Avoid two workers fetching the same email and generating duplicate promo_link.
        slot_id = _reserve_promo_link_slot(
            email=email,
            worker_id=wid_for_slot,
            plan_name="chatgptplusplan",
            promo_campaign_id=campaign,
            billing_country=country,
            billing_currency=currency,
        )

        print(
            f"[auto-gen] #{attempt}/{max_attempts} 用库存账号 {email} (占位 id={slot_id}) "
            f"生产 promo_link plan=plus region={country}/{currency} campaign={campaign}"
        )

        result = fetch_promo_link(
            access_token=str(acc.get("access_token") or ""),
            cookie_header=str(acc.get("cookie_header") or ""),
            device_id=str(acc.get("device_id") or ""),
            plan="plus",
            country=country,
            currency=currency,
            promo_campaign_id=campaign,
            proxy_url=proxy_url,
            timeout=30,
        )
        if not result.get("ok"):
            last_err = str(result.get("error") or "")[:200]
            _release_reserved_slot(slot_id)
            slot_id = 0
            # access_token expired (401 / authentication invalidated) → mark that email
            # last_check_status='invalid', picker will automatically skip next time. This way multiple workers can run concurrently
            # Won't repeatedly retry the same failed account.
            if "401" in last_err or "authentication" in last_err.lower() or "invalidated" in last_err.lower():
                try:
                    with get_db()._conn() as c:
                        c.execute(
                            "UPDATE registered_accounts SET last_check_status='invalid', "
                            "last_check_message=?, last_check_at=? "
                            "WHERE lower(email)=lower(?)",
                            ("access_token 401 from auto-gen", time.time(), email),
                        )
                    print(f"[auto-gen] {email} access_token 失效, 标记 invalid; 换下一个邮箱")
                except Exception as _e:
                    print(f"[auto-gen] 标记 invalid 失败: {_e}")
            else:
                print(f"[auto-gen] fetch_promo_link 失败: {last_err}; 换下一个邮箱重试")
            continue
        plan_name_final = result.get("plan_name") or "chatgptplusplan"
        # Success → break loop, finalize placeholder line below
        break
    else:
        print(f"[auto-gen] {max_attempts} 个邮箱全失败, 最后错误: {last_err}")
        return None

    # Fill the fetch result back into the placeholder row (status remains in_use, claimed_by remains self;
    # _select_promo_link receives this line and returns directly, no secondary claim_by_id).
    final_ok = _finalize_reserved_slot(
        slot_id,
        checkout_url=result.get("checkout_url") or "",
        cs_id=result.get("cs_id") or "",
        processor_entity=result.get("processor_entity") or "",
        plan_name=plan_name_final,
        promo_campaign_id=result.get("promo_campaign_id") or campaign,
        billing_country=result.get("billing_country") or country,
        billing_currency=result.get("billing_currency") or currency,
        amount_due_cents=int(result.get("amount_due_cents") or 0),
        raw_response=result.get("raw") or {},
    )
    if not final_ok:
        _release_reserved_slot(slot_id)
        print(f"[auto-gen] finalize 占位行失败 (slot id={slot_id} 被并发改了状态)")
        return None

    row_to_insert = {
        "id": slot_id,
        "email": email,
        "checkout_url": result.get("checkout_url") or "",
        "cs_id": result.get("cs_id") or "",
        "processor_entity": result.get("processor_entity") or "",
        "plan_name": plan_name_final,
        "promo_campaign_id": result.get("promo_campaign_id") or campaign,
        "billing_country": result.get("billing_country") or country,
        "billing_currency": result.get("billing_currency") or currency,
        "amount_due_cents": int(result.get("amount_due_cents") or 0),
        "status": "in_use",
        "raw_response": result.get("raw") or {},
    }
    # The placeholder row has been finalized, slot_id is the id of the new row; do not call add_promo_link again to avoid duplicate insertion.
    print(
        f"[auto-gen] ✓ promo_links.id={slot_id} email={row_to_insert['email']} "
        f"due={row_to_insert['amount_due_cents']} url=...{(row_to_insert['checkout_url'] or '')[-40:]}"
    )
    return row_to_insert


def _select_promo_link(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve the checkout URL from CLI / DB. Auto-gen from inventory account
    when DB has no fresh row available. Concurrency-safe: use atomic claim instead of SELECT,
    multiple workers won't contend for the same promo_link."""
    if args.checkout_url:
        m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", args.checkout_url)
        if not m:
            raise SystemExit("--checkout-url 里没有 cs_live/cs_test")
        return {
            "id": 0,
            "email": args.email or "",
            "checkout_url": args.checkout_url,
            "cs_id": m.group(1),
            "processor_entity": "",
            "plan_name": "chatgptplusplan",
            "promo_campaign_id": args.promo_campaign_id or "plus-1-month-free",
            "billing_country": args.billing_country or "US",
            "billing_currency": args.billing_currency or "USD",
            "amount_due_cents": int(args.expected_due),
            "status": "manual",
        }

    worker_id = _resolve_worker_id(getattr(args, "worker_id", ""))
    db = get_db()

    # Explicit --promo-link-id: force claim that one
    if args.promo_link_id:
        row = db.claim_promo_link_by_id(worker_id, int(args.promo_link_id))
        if not row:
            raise SystemExit(
                f"--promo-link-id {args.promo_link_id} 不存在或已被别的 worker 占用 (status≠fresh)"
            )
        return row

    # Default: iterate through claims, skip already paid accounts (if claim is found to be paid after claiming -> release expired, select next one)
    excluded: list[int] = []
    max_attempts = 10
    for _ in range(max_attempts):
        row = db.claim_next_fresh_promo_link(
            worker_id=worker_id,
            plan_like="plus",
            email=(args.email or "").strip(),
            max_due_cents=int(args.max_due) if not args.allow_full_price else 0,
            exclude_ids=excluded,
        )
        if not row:
            break
        plan = _latest_account_plan(row.get("email") or "")
        if plan in {"plus", "team", "pro"} and not args.allow_already_paid:
            # Already a paid plan, expiration prevents other workers from selecting it, continue searching for the next one
            db.release_promo_link(int(row["id"]), "expired")
            excluded.append(int(row["id"]))
            print(
                f"[claim] promo_link.id={row['id']} email={row['email']} 已是 paid plan={plan}, "
                f"标 expired 并跳过"
            )
            continue
        return row

    # Didn't get it → auto-gen one and then claim it
    print("[auto-gen] 没有可 claim 的 fresh promo_link，尝试从库存账号生产")
    generated = _auto_generate_promo_link(args, worker_id=worker_id)
    if generated and generated.get("id"):
        # auto-gen internal has already INSERT placeholder row status='in_use' + claimed_by=self,
        # Here we directly return, no longer claim_promo_link_by_id (which requires status='fresh').
        return generated

    # auto-gen no output → explicitly report error reason, no longer mislead as "already paid"
    if generated is None:
        raise SystemExit(
            "auto-gen 失败：库存里没有可用账号 (需要 access_token + cookie 且非 paid/deactivated)，"
            "或所有候选邮箱的 fetch_promo_link 都失败 (常见: gost 中继没起 / proxy 不通 / access_token 失效)。"
            "查上面的 [auto-gen] 详细错误。"
        )
    if args.email or args.allow_already_paid:
        raise SystemExit("没有找到可用 promo_links 长链接；请传 --promo-link-id 或 --checkout-url")
    raise SystemExit("找到的 fresh plus 长链接所属账号都已经是 paid plan；请传 --promo-link-id --allow-already-paid 强制执行")


def _latest_account_plan(email: str) -> str:
    if not email:
        return ""
    try:
        with get_db()._conn() as c:
            row = c.execute(
                """
                SELECT last_plan_type
                FROM registered_accounts
                WHERE lower(email)=lower(?)
                ORDER BY id DESC LIMIT 1
                """,
                (email,),
            ).fetchone()
        return str(row["last_plan_type"] if row else "").strip().lower()
    except Exception:
        return ""


def _latest_account_auth(email: str) -> dict[str, str]:
    """Return reusable ChatGPT auth material for a promo-link owner.

    Promo links in the DB only store the checkout URL.  If that Stripe session
    has gone inactive, card.run can generate a fresh checkout, but only if we
    provide the owning ChatGPT account's auth.  Reusing the matching
    registered_accounts row keeps refreshes on the intended inventory account
    instead of falling back to auto-registering a different account.
    """
    if not email:
        return {}
    try:
        with get_db()._conn() as c:
            row = c.execute(
                """
                SELECT email, session_token, access_token, cookie_header, device_id
                FROM registered_accounts
                WHERE lower(email)=lower(?)
                ORDER BY id DESC LIMIT 1
                """,
                (email.strip(),),
            ).fetchone()
        if not row:
            return {}
        return {k: (row[k] or "") for k in row.keys()}
    except Exception:
        return {}


def _ensure_proxy_alive(cfg: dict[str, Any]) -> None:
    proxy = str(cfg.get("proxy") or "").strip()
    if not proxy:
        return
    if "127.0.0.1:18898" not in proxy and "localhost:18898" not in proxy:
        return
    try:
        from pipeline import _ensure_gost_alive  # type: ignore
        _ensure_gost_alive(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] gost 保活失败/跳过: {e}")


def _build_temp_config(
    *,
    base_config: Path,
    row: dict[str, Any],
    args: argparse.Namespace,
) -> Path:
    cfg = _load_json(base_config)
    if getattr(args, "captcha_api_url", "") or getattr(args, "captcha_api_key", ""):
        cap = cfg.setdefault("captcha", {})
        if args.captcha_api_url:
            cap["api_url"] = args.captcha_api_url
        if args.captcha_api_key:
            cap["api_key"] = args.captcha_api_key
            cap["client_key"] = args.captcha_api_key

    # userscript v32 forces the Stripe billing country select to US even when
    # the promo checkout itself is priced in another region/currency.
    country = (args.billing_country or "US").upper()
    checkout_country = (row.get("billing_country") or "US").upper()
    currency = (args.billing_currency or row.get("billing_currency") or "USD").upper()
    wants_random_card = bool(getattr(args, "paypal_node_rpa", False))
    cli_card_number = str(getattr(args, "card_number", "") or "").replace(" ", "")
    prefer_fixed_card = bool(cli_card_number and getattr(args, "prefer_fixed_card", False))
    meiguo_persona = (
        _fetch_userscript_us_address(require_card=wants_random_card and not prefer_fixed_card)
        if country == "US" or args.paypal_country.upper() == "US"
        else {}
    )
    addr = dict(meiguo_persona) if country == "US" and meiguo_persona else dict(COUNTRY_ADDRESS.get(country) or COUNTRY_ADDRESS["US"])
    addr["country"] = country

    cfg["randomize_identity"] = False
    cfg["pre_solve_passive_captcha"] = False
    browser_cfg = cfg.setdefault("browser_challenge", {})
    browser_cfg["enabled"] = False
    browser_cfg["auto_launch_browser"] = False
    browser_cfg["headless"] = True

    random_card_number = str(meiguo_persona.get("card_number") or "").replace(" ", "")
    use_random_card = wants_random_card and not prefer_fixed_card and bool(random_card_number)
    exp_month = str(meiguo_persona.get("card_exp_month") or "12") if use_random_card else "12"
    exp_year = str(meiguo_persona.get("card_exp_year") or "2030") if use_random_card else "2030"
    expiry = str(getattr(args, "card_expiry", "") or "").strip()
    if expiry and not use_random_card:
        parts = [p.strip() for p in expiry.replace("/", " ").split() if p.strip()]
        if len(parts) >= 2:
            exp_month = parts[0].zfill(2)
            yy = parts[1]
            exp_year = ("20" + yy) if len(yy) == 2 else yy

    payment_card_number = (
        cli_card_number if prefer_fixed_card else
        random_card_number
        or cli_card_number
        or "4242424242424242"
    )
    payment_card_cvv = (
        str(meiguo_persona.get("card_cvv") or "").strip()
        if use_random_card and not prefer_fixed_card else
        str(getattr(args, "card_cvv", "") or "123").strip()
    )
    if use_random_card:
        print(
            "[persona] 使用 meiguodizhi 随机身份/地址/银行卡；"
            f"card={_card_type(payment_card_number)} ****{payment_card_number[-4:]} "
            f"name={addr.get('full_name', '')} addr={addr.get('city', '')}/{addr.get('state', '')}"
        )

    billing_name = args.billing_name or addr.get("full_name") or _rand_name()
    cfg["cards"] = [{
        # PayPal payment_method typically does not use card number; reserved field mainly satisfies the configuration structure of card.run.
        # If the subsequent process switches to card field/card fallback, use the value explicitly passed in via CLI.
        "number": payment_card_number,
        "cvc": payment_card_cvv,
        "exp_month": exp_month,
        "exp_year": exp_year,
        "address": addr,
        "name": billing_name,
        "email": args.billing_email or _rand_email(),
    }]

    paypal = dict(cfg.get("paypal") or {})
    card_number = cli_card_number if prefer_fixed_card else (random_card_number if use_random_card else cli_card_number)
    if card_number:
        # userscript calls getAddr() again on the PayPal checkoutweb page.
        paypal_signup_addr = (
            dict(meiguo_persona) if meiguo_persona else _fetch_userscript_us_address(require_card=wants_random_card)
            if args.paypal_country.upper() == "US"
            else dict(COUNTRY_ADDRESS.get(args.paypal_country.upper()) or COUNTRY_ADDRESS["US"])
        )
        paypal["signup_card"] = {
            "cardNumber": card_number,
            "expirationDate": f"{exp_month}/{exp_year}",
            "securityCode": payment_card_cvv,
            "type": _card_type(card_number),
        }
        paypal["signup_billing_address"] = paypal_signup_addr
        if paypal_signup_addr.get("first_name") and paypal_signup_addr.get("last_name"):
            paypal["signup_first_name"] = paypal_signup_addr["first_name"]
            paypal["signup_last_name"] = paypal_signup_addr["last_name"]
    paypal.update({
        "mode": "signup_no_card",
        "signup_no_card": True,
        # Default strict pure protocol; seed is only allowed with explicit --allow-camoufox-seed.
        "skip_camoufox_seed": not bool(args.allow_camoufox_seed),
        # After seed fails, directly using pure HTTP will generally pollute the current BA/EC and trigger hcaptchapassive.
        # Therefore, in allow-camoufox-seed mode, only seed is re-weighted by default without fallback; needs to be reproduced
        # Old behavior explicitly add --paypal-allow-http-fallback-on-seed-fail.
        "seed_retries": max(1, int(getattr(args, "paypal_seed_retries", 2) or 2)),
        "no_http_fallback_on_seed_fail": bool(
            args.allow_camoufox_seed
            and not getattr(args, "paypal_allow_http_fallback_on_seed_fail", False)
        ),
        "disable_idapps": bool(getattr(args, "paypal_disable_idapps", False)),
        "browser_form_warmup": bool(getattr(args, "paypal_browser_form_warmup", False)),
        "allow_browser_recaptcha": bool(getattr(args, "paypal_allow_browser_recaptcha", False)),
        "node_rpa": bool(getattr(args, "paypal_node_rpa", False)),
        "node_rpa_headless": bool(getattr(args, "paypal_node_rpa_headless", False)),
        "node_rpa_timeout_s": int(getattr(args, "paypal_node_rpa_timeout", 720) or 720),
        "phone": args.phone,
        "locale_country": args.paypal_country.upper(),
        "locale_lang": args.paypal_lang.lower(),
        "otp_timeout_s": int(args.otp_timeout),
        "signup_persona_retries": max(0, int(args.paypal_signup_retries)),
        # v32 Tampermonkey script's actual actions on the PayPal checkoutweb page are:
        #   fill('billingLine1'...) / fill('billingCity'...)
        #   fill('billingPostalCode'...) / fillSelect('billingState'...)
        # That is, hide/skip AddressAutocomplete and proceed with manual input MANUAL. Here taking the script as
        # Process truth value, defaults to not going through PayPal AddressAutocomplete; only when explicitly requested
        # Replicate the historical successful packet capture of the GOOGLE address branch.
        "signup_address_autocomplete": bool(args.paypal_autocomplete_address),
        "persist_to": str(args.persist_to or (OUTPUT_DIR / "no_card_paypal_plus_latest.json")),
    })
    # Avoid run() mistakenly thinking it needs to use the existing PayPal login.
    if paypal["signup_no_card"]:
        paypal.setdefault("email", "")
        paypal.setdefault("password", "")
        paypal.setdefault("cookies", "")
    cfg["paypal"] = paypal

    fresh = cfg.setdefault("fresh_checkout", {})
    fresh["enabled"] = True
    # The inventory long connection may have been set to inactive by previous rounds of OAS/PayPal requires_action attempts.
    # Allow card.run to reuse the same inventory account auth to regenerate fresh checkout; expected_due
    # Still locks 0 to prevent mistakenly continuing at full price after the discount expires.
    fresh["auto_refresh_on_inactive"] = True
    fresh["auto_refresh_on_due_mismatch"] = True
    fresh["max_due_mismatch_refreshes"] = int(fresh.get("max_due_mismatch_refreshes") or 2)
    fresh["expected_due"] = int(args.expected_due if args.expected_due is not None else row.get("amount_due_cents") or 0)
    # Let card_results record the real ChatGPT email instead of random billing email.
    if row.get("email"):
        fresh["_chatgpt_email"] = row["email"]
        acc_auth = _latest_account_auth(str(row["email"]))
        if acc_auth:
            auth = fresh.setdefault("auth", {})
            auth.update({
                "mode": "access_token",
                "access_token": acc_auth.get("access_token") or "",
                "session_token": acc_auth.get("session_token") or "",
                "cookie_header": acc_auth.get("cookie_header") or "",
                "device_id": acc_auth.get("device_id") or "",
                "oai_device_id": acc_auth.get("device_id") or "",
                "prefer_session_refresh": True,
            })
            auto_reg = auth.setdefault("auto_register", {})
            auto_reg["enabled"] = False
    plan = fresh.setdefault("plan", {})
    plan.update({
        "plan_name": "chatgptplusplan",
        "entry_point": "all_plans_pricing_modal",
        "promo_campaign_id": row.get("promo_campaign_id") or args.promo_campaign_id or "plus-1-month-free",
        # Fresh ChatGPT checkout must keep the promo-link region/currency.
        # The userscript only changes the hosted Stripe billing/tax address to
        # US after the checkout exists.
        "billing_country": checkout_country,
        "billing_currency": currency,
        "checkout_ui_mode": "hosted",
        "output_url_mode": "provider",
    })
    plan.pop("workspace_name", None)
    plan.pop("seat_quantity", None)

    runtime = cfg.setdefault("runtime", {})
    runtime["confirm_mode"] = "shared_payment_method"

    if args.proxy:
        cfg["proxy"] = args.proxy

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="no_card_paypal_plus_",
        dir=str(RUNTIME_DIR),
        delete=False,
        encoding="utf-8",
    )
    json.dump(cfg, tmp, ensure_ascii=False, indent=2)
    tmp.close()
    atexit.register(lambda p=tmp.name: os.path.exists(p) and os.unlink(p))
    return Path(tmp.name)


def _mark_link_used(row: dict[str, Any]) -> None:
    link_id = int(row.get("id") or 0)
    if link_id <= 0:
        return
    try:
        get_db().mark_promo_link_used(link_id)
        print(f"[db] promo_links.id={link_id} 已标记 used")
    except Exception as e:  # noqa: BLE001
        print(f"[db] 标记 used 失败: {e}")


def _refresh_inventory_plan(row: dict[str, Any]) -> None:
    email = (row.get("email") or "").strip()
    if not email:
        return
    try:
        from webui.backend.account_validator import refresh_rt_status_by_id
        with get_db()._conn() as c:
            acc = c.execute(
                "SELECT id FROM registered_accounts WHERE lower(email)=lower(?) ORDER BY id DESC LIMIT 1",
                (email,),
            ).fetchone()
        if not acc:
            return
        result = refresh_rt_status_by_id(int(acc["id"]), timeout_s=20)
        print(f"[db] RT 刷新库存 plan: {result.get('status')} plan={result.get('plan_type')}")
        if not str(result.get("plan_type") or "").strip():
            from webui.backend.account_validator import validate_account_by_id
            result = validate_account_by_id(int(acc["id"]), timeout_s=25, use_proxy=True)
            print(f"[db] access_token 实时刷新库存 plan: {result.get('status')} plan={result.get('plan_type')}")
    except Exception as e:  # noqa: BLE001
        print(f"[db] RT 刷新库存 plan 失败: {e}")


def _try_protocol_relogin(email: str, password: str) -> tuple[bool, str]:
    """When OpenAI changes plans (plus activation), it revokes both access_token and session_token.
    Only re-login can obtain tokens with the new plan claim. Execute AuthFlow.run_protocol_login
    (sentinel + email OTP), write to DB. One-time fix every 30-60s.

    Returns (ok, reason).
    ok=True  : re-login successful + new token written to database.
    ok=False : failed. reason distinguishes scenarios:
       - "account_deactivated" : OpenAI account deactivated (RPA triggered risk control), should not retry.
       - others : import/transient failure, caller may optionally retry."""
    if not email or not password:
        return False, "no_credentials"
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _reg = _Path("/app/CTF-reg")
        if not _reg.exists():
            _reg = _Path(__file__).resolve().parents[1] / "CTF-reg"
        if str(_reg) not in _sys.path:
            _sys.path.insert(0, str(_reg))
        from config import Config, MailConfig  # type: ignore
        from mail.provider import MailProvider  # type: ignore
        from drivers.protocol import AuthFlow  # type: ignore
    except Exception as e:
        print(f"[relogin] import 失败: {e}")
        return False, "import_failed"

    cfg = Config()
    cfg.mail = MailConfig(catch_all_domain="")
    cfg.proxy = "socks5://127.0.0.1:18898"

    os.environ.setdefault("OAUTH_REFRESH_ONLY", "1")
    os.environ.setdefault("OPENAI_SENTINEL_REQUIRE_QUICKJS", "1")

    mail = MailProvider(cfg.mail.catch_all_domain)
    # outlook pool IMAP creds injection (same pattern as exchange_refresh_token_protocol)
    try:
        with get_db()._conn() as c:
            row = c.execute(
                "SELECT email, refresh_token, client_id FROM outlook_accounts "
                "WHERE lower(email)=lower(?) OR lower(chatgpt_email)=lower(?) "
                "ORDER BY CASE WHEN lower(email)=lower(?) THEN 0 ELSE 1 END LIMIT 1",
                (email, email, email),
            ).fetchone()
        if row and (row["refresh_token"] or "") and (row["client_id"] or ""):
            mail._outlook_creds = {
                "email": row["email"],
                "refresh_token": row["refresh_token"],
                "client_id": row["client_id"],
            }
    except Exception as e:
        print(f"[relogin] 查询 outlook creds 失败 (不致命): {e}")

    print(f"[relogin] 走 AuthFlow.run_protocol_login email={email} ...")
    try:
        flow = AuthFlow(cfg)
        flow._is_existing_account = True
        result = flow.run_protocol_login(mail, email, password)
    except Exception as e:
        err = str(e)
        # OpenAI rejected (Account deactivated after RPA risk control triggered)
        if "account_deactivated" in err or "deleted or deactivated" in err:
            print(f"[relogin] OpenAI 已注销账号 (account_deactivated): {email}")
            return False, "account_deactivated"
        print(f"[relogin] run_protocol_login 失败: {err}")
        return False, "relogin_error"

    new_at = (result.access_token or "").strip()
    new_st = (result.session_token or "").strip()
    new_rt = (result.refresh_token or "").strip()
    if not new_at and not new_st:
        print("[relogin] 完成但 access_token + session_token 都为空")
        return False, "empty_tokens"
    try:
        with get_db()._conn() as c:
            sets = ["last_check_at=?"]
            args: list = [time.time()]
            if new_at:
                sets.append("access_token=?")
                args.append(new_at)
            if new_st:
                sets.append("session_token=?")
                args.append(new_st)
            if new_rt:
                sets.append("refresh_token=?")
                args.append(new_rt)
            args.append(email)
            c.execute(
                f"UPDATE registered_accounts SET {', '.join(sets)} "
                f"WHERE id=(SELECT id FROM registered_accounts WHERE lower(email)=lower(?) ORDER BY id DESC LIMIT 1)",
                args,
            )
        print(f"[relogin] 新凭证写库 OK at_len={len(new_at)} st_len={len(new_st)} rt_len={len(new_rt)}")
        return True, "ok"
    except Exception as e:
        print(f"[relogin] 写库失败: {e}")
        return False, "db_write_failed"


def _wait_inventory_paid_plan(row: dict[str, Any], *, timeout_s: int = 90) -> str:
    """Poll inventory live plan after browser checkout return.

    New Outlook registration currently often lacks Codex refresh_token, and simply
    calling refresh_rt_status will get invalid/empty plan; here we directly use the
    access_token + check/v4 real-time plan branch of validate_account_by_id, which
    is consistent with the frontend "refresh account status".

    When OpenAI changes the plan, it revokes both access_token and session_token.
    When the validator first returns 401/token_revoked, use password to perform
    protocol re-login to obtain new credentials (one-shot), then continue polling."""
    email = (row.get("email") or "").strip()
    if not email:
        return ""
    try:
        from webui.backend.account_validator import validate_account_by_id
        with get_db()._conn() as c:
            acc = c.execute(
                "SELECT id, password FROM registered_accounts WHERE lower(email)=lower(?) ORDER BY id DESC LIMIT 1",
                (email,),
            ).fetchone()
        if not acc:
            return ""
        aid = int(acc["id"])
        password = (acc["password"] or "").strip()
        deadline = time.time() + max(1, int(timeout_s))
        last_plan = ""
        relogin_done = False
        while time.time() < deadline:
            result = validate_account_by_id(aid, timeout_s=25, use_proxy=True)
            last_plan = str(result.get("plan_type") or "").strip().lower()
            msg = str(result.get("message") or "")
            print(f"[db] 实时刷新库存 plan: {result.get('status')} plan={last_plan or '-'}")
            if last_plan in {"plus", "team", "pro"}:
                return last_plan
            # session completely revoked (401 token + 401 cookie + NextAuth exposes old token)
            # → password reset, login again to get new credentials, then continue polling
            session_revoked = (
                ("token revoked" in msg or "session revoked" in msg or "401" in msg)
                and result.get("status") == "invalid"
            )
            if session_revoked and not relogin_done and password:
                relogin_done = True
                ok, reason = _try_protocol_relogin(email, password)
                if ok:
                    time.sleep(2)
                    continue
                if reason == "account_deactivated":
                    # OpenAI risk control account cancellation: RPA runs through but actually not activated plus.
                    # Write DB marker last_plan_type='deactivated' + status='deactivated',
                    # No longer poll (save time), let webui see the real state.
                    try:
                        with get_db()._conn() as c:
                            c.execute(
                                "UPDATE registered_accounts SET "
                                "last_plan_type='deactivated', "
                                "last_check_status='deactivated', "
                                "last_check_message=?, "
                                "last_check_at=? WHERE id=?",
                                (
                                    "RPA RPA succeeded but OpenAI deactivated account "
                                    "(account_deactivated on relogin). Plus NOT activated.",
                                    time.time(),
                                    aid,
                                ),
                            )
                        print(f"[db] 账号被 OpenAI 注销, 标 deactivated, 跳出 poll")
                    except Exception as e:
                        print(f"[db] 标 deactivated 失败: {e}")
                    return "deactivated"
            time.sleep(8)
        return last_plan
    except Exception as e:  # noqa: BLE001
        print(f"[db] 实时刷新库存 plan 失败: {e}")
        return ""


def _run_node_full_checkout_rpa(row: dict[str, Any], tmp_config: Path,
                                args: argparse.Namespace) -> dict[str, Any]:
    """Run the Tampermonkey-equivalent full browser path.

    This opens the stored hosted checkout URL directly in Chromium, fills the
    Stripe/OpenAI page, then continues into PayPal in the *same* browser
    context.  It deliberately avoids the older hybrid path:

        Stripe pure protocol -> pm-redirect -> browser only for PayPal

    because the successful Tampermonkey evidence shows the decisive difference
    is likely the full page runtime context before PayPal receives the BA/EC
    handoff.
    """
    cfg = _load_json(tmp_config)
    paypal = cfg.get("paypal") or {}
    signup_card = paypal.get("signup_card") if isinstance(paypal.get("signup_card"), dict) else {}
    signup_addr = paypal.get("signup_billing_address") if isinstance(paypal.get("signup_billing_address"), dict) else {}
    if not signup_card:
        cards = cfg.get("cards") or []
        if cards and isinstance(cards[0], dict):
            signup_card = {
                "cardNumber": str(cards[0].get("number") or ""),
                "expirationDate": f"{cards[0].get('exp_month') or '03'}/{cards[0].get('exp_year') or '2030'}",
                "securityCode": str(cards[0].get("cvc") or ""),
            }
            if not signup_addr and isinstance(cards[0].get("address"), dict):
                signup_addr = dict(cards[0]["address"])
    card_number = re.sub(r"\s+", "", str(signup_card.get("cardNumber") or signup_card.get("number") or ""))
    if not card_number:
        raise RuntimeError("Node full-checkout RPA 缺少 PayPal signup card")

    helper = CARD_DIR / "scripts" / "paypal_node_rpa.js"
    if not helper.exists():
        raise RuntimeError(f"Node RPA helper 不存在: {helper}")
    node_bin = (
        os.environ.get("OPENAI_SENTINEL_NODE_PATH", "").strip()
        or shutil.which("node")
        or "node"
    )

    # Each PayPal signup/approval must start from an empty profile like an incognito window.
    # Cannot reuse node_rpa_profile_dir from config; otherwise the previous round PayPal new account,
    # Cookies, localStorage, and risk control cache will affect the next round of BA/EC.
    profile_dir = tempfile.mkdtemp(prefix="paypal_full_checkout_rpa_")
    payload = {
        "checkoutUrl": row.get("checkout_url") or args.checkout_url,
        "redirectUrl": row.get("checkout_url") or args.checkout_url,
        "fullCheckout": True,
        "proxy": args.proxy or cfg.get("proxy") or "",
        "phone": args.phone,
        "cardNumber": card_number,
        "cardExpiry": signup_card.get("expirationDate") or "03/30",
        "cardCvv": signup_card.get("securityCode") or signup_card.get("cvc") or signup_card.get("cvv") or "",
        "expectedDueCents": int(getattr(args, "expected_due", 0) or 0),
        "address": signup_addr,
        "firstName": paypal.get("signup_first_name") or signup_addr.get("first_name") or "James",
        "lastName": paypal.get("signup_last_name") or signup_addr.get("last_name") or "Smith",
        "smsApiUrl": (
            paypal.get("sms_api_url")
            or getattr(args, "sms_api_url", "")
            or os.environ.get("PPS_SMS_API_URL")
            or os.environ.get("PAYPAL_SMS_API_URL")
            or ""
        ),
        "timeoutMs": int(paypal.get("node_rpa_timeout_s") or args.paypal_node_rpa_timeout or 720) * 1000,
        "otpTimeoutMs": int(paypal.get("otp_timeout_s") or args.otp_timeout or 180) * 1000,
        # PayPal fallback billing page can render the consent button before
        # the hidden wallet/agreement state is fully hydrated.  Tampermonkey
        # runs with human-scale latency here; keep a short delay before the
        # final "Agree and Continue" click to match that behavior.
        "fallbackConsentDelayMs": int(
            paypal.get("fallback_consent_delay_ms")
            or os.environ.get("PPS_PAYPAL_FALLBACK_CONSENT_DELAY_MS")
            or 12000
        ),
        "headless": bool(paypal.get("node_rpa_headless") or args.paypal_node_rpa_headless),
        "profileDir": profile_dir,
        "keepProfile": bool(os.environ.get("PPS_PAYPAL_KEEP_PROFILE")),
    }

    # Prevent reading the structured results from the previous round of PayPal RPA.
    for p in (
        _tmp_path("result.json"),
        _tmp_path("state.json"),
        _tmp_path("live.log"),
        _tmp_path("last.json"),
    ):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    print(
        "[node-rpa-full] 启动整页浏览器 checkout+PayPal RPA "
        f"card=****{card_number[-4:]} phone={str(args.phone)[-4:].rjust(len(str(args.phone)), '*')} "
        f"headless={payload['headless']}"
    )

    env = os.environ.copy()
    node_paths = [
        env.get("NODE_PATH", ""),
        "/app/webui/frontend/node_modules",
        str(ROOT / "webui" / "frontend" / "node_modules"),
        "/usr/local/lib/node_modules",
    ]
    env["NODE_PATH"] = ":".join([p for p in node_paths if p])
    proxy_url = str(payload.get("proxy") or "")
    if proxy_url:
        env.setdefault("HTTPS_PROXY", proxy_url)
        env.setdefault("HTTP_PROXY", proxy_url)
        env.setdefault("ALL_PROXY", proxy_url)
    # Pass the current worker id to the Node child process, so it puts /tmp/paypal_node_rpa_*
    # Write to independent file names (T_BASE), otherwise concurrent workers will have interleaved logs/results.
    wid_env = os.environ.get("NCPP_WORKER_ID", "").strip()
    if wid_env:
        env["NCPP_WORKER_ID"] = wid_env

    cmd: list[str] = [node_bin, str(helper)]
    use_xvfb = (
        not bool(payload["headless"])
        and not bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        and bool(shutil.which("xvfb-run"))
    )
    if use_xvfb:
        cmd = [
            shutil.which("xvfb-run") or "xvfb-run",
            "-a",
            "-s",
            "-screen 0 1440x900x24",
            *cmd,
        ]
        print("[node-rpa-full] 无 DISPLAY，使用 xvfb-run 跑 headed Chromium")

    timeout_s = int(payload["timeoutMs"] / 1000) + 120
    stderr_lines: list[str] = []
    raw = ""
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(ROOT),
            bufsize=1,
        )

        def _drain_stderr() -> None:
            try:
                assert proc is not None and proc.stderr is not None
                for line in proc.stderr:
                    line = line.rstrip("\n")
                    stderr_lines.append(line)
                    if line.strip():
                        print("      " + line[:500], flush=True)
            except Exception:
                pass

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()
        if proc.stdin is not None:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False))
            proc.stdin.close()
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            print(f"[node-rpa-full] 超时 {timeout_s}s，终止 Node/Chromium")
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            raw = (proc.stdout.read() if proc.stdout is not None else "") or ""
        except Exception:
            raw = ""
        t.join(timeout=2)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"spawn failed: {type(e).__name__}: {e}"}
    finally:
        if not payload.get("keepProfile"):
            try:
                shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception:
                pass

    result: dict[str, Any] = {}
    raw = raw.strip()
    if raw:
        try:
            result = json.loads(raw)
        except Exception:
            result = {}
    if not result:
        try:
            with open(_tmp_path("result.json"), "r", encoding="utf-8") as f:
                result = json.load(f)
        except Exception:
            result = {}
    if not result:
        m = re.search(r"(\{\s*\"success\"\s*:\s*(?:true|false).*?\})\s*$", raw, re.S)
        if m:
            try:
                result = json.loads(m.group(1))
            except Exception:
                result = {}
    if not result:
        result = {"success": False, "error": f"no JSON result; stdout={raw[:300]}"}

    try:
        with open(_tmp_path("last.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "returncode": proc.returncode if proc is not None else None,
                    "result": result,
                    "stderr_tail": stderr_lines[-200:],
                    "stdout_tail": raw.splitlines()[-200:],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        pass

    if proc is not None and proc.returncode not in (0, None) and result.get("success") is not True:
        result.setdefault("error", f"node rc={proc.returncode}")
    return result


def _node_rpa_retryable_failure(result: dict[str, Any]) -> tuple[bool, str]:
    """Return whether a full-checkout RPA failure should try a fresh persona/card."""
    result_hay = json.dumps(result or {}, ensure_ascii=False)
    # First check the structured result. For example, INSTRUMENT_SHARING_LIMIT_EXCEEDED is only a PayPal
    # Prerequisites console error for entering Hermes `/pay/billing` fallback; if subsequently already
    # After clicking Agree and Continue, the real failure reason should be the generic-error on the finalUrl /
    # INVALID_REQUEST, rather than treating the preceding console error as the endpoint.
    prioritized = [
        # meiguodizhi card pool reuse, PayPal side remembers the bound full account → switch persona/card and re-draw
        ("paypal_cc_linked_to_full_account", r"paypal_cc_linked_to_full_account|CC_LINKED_TO_FULL_ACCOUNT"),
        # PayPal createMember validation rejection (persona/card combo triggers risk control rules) → switch persona and retry
        ("paypal_create_card_account_validation_error", r"paypal_create_card_account_validation_error|CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR"),
        ("paypal_generic_error_after_agree_continue", r"paypal_generic_error_after_agree_continue"),
        ("paypal_invalid_request_after_fallback", r"INVALID_REQUEST|SU5WQUxJRF9SRVFVRVNU|paypal generic-error|/pay/generic-error"),
        ("paypal_missing_agree_continue", r"missing_agree_continue|no_agree_continue|没有 Agree|no Agree and Continue"),
        ("paypal_onboarding_email_stuck", r"onboarding_email_stuck"),
    ]
    for name, pat in prioritized:
        if re.search(pat, result_hay, re.I):
            return True, name

    hay = result_hay
    try:
        with open(_tmp_path("live.log"), "r", encoding="utf-8", errors="ignore") as f:
            hay += "\n" + f.read()[-20000:]
    except Exception:
        pass
    patterns = [
        # These are cases where the finalUrl was not reached midway, so we need to view the complete live log.
        ("paypal_cc_linked_to_full_account", r"CC_LINKED_TO_FULL_ACCOUNT"),
        ("paypal_create_card_account_validation_error", r"CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR"),
        ("paypal_onboarding_email_stuck", r"onboarding_email_stuck"),
        ("paypal_missing_agree_continue", r"missing_agree_continue|no_agree_continue|没有 Agree|no Agree and Continue"),
        ("paypal_invalid_request_after_fallback", r"INVALID_REQUEST|SU5WQUxJRF9SRVFVRVNU|paypal generic-error|/pay/generic-error"),
        ("issuer_decline", r"ISSUER_DECLINE"),
        ("paypal_card_generic", r"CARD_GENERIC_ERROR"),
        # INSTRUMENT_SHARING_LIMIT_EXCEEDED itself often just enters Agree and
        # Continue fallback page signal; only serves as fallback when there is no more specific final result
        # Retryable funding-source signal.
        ("instrument_sharing_limit_before_fallback", r"INSTRUMENT_SHARING_LIMIT_EXCEEDED"),
    ]
    for name, pat in patterns:
        if re.search(pat, hay, re.I):
            return True, name
    return False, ""


def main() -> int:
    p = argparse.ArgumentParser(
        description="promo 长链接 + PayPal no-card 纯协议开通 ChatGPT Plus",
    )
    p.add_argument("--promo-link-id", type=int, default=0, help="promo_links.id；不传则取最新 fresh plus 链接")
    p.add_argument("--email", default="", help="按 ChatGPT 邮箱筛选最新 fresh plus 链接；配 --checkout-url 时只用于记录")
    p.add_argument("--checkout-url", default="", help="手工传 hosted checkout 长链接，绕过 promo_links DB")
    p.add_argument("--config", default=str(CARD_DIR / "config.paypal-plus.json"), help="基础 card config")
    p.add_argument("--proxy", default="", help="覆盖 config.proxy")
    p.add_argument("--phone", default=os.environ.get("PPS_PAYPAL_PHONE", ""), help="PayPal signup SMS 手机号；也可用 PPS_PAYPAL_PHONE")
    p.add_argument("--sms-api-url", default=os.environ.get("PPS_SMS_API_URL") or os.environ.get("PAYPAL_SMS_API_URL") or "", help="PayPal signup SMS 拉码接口；也可用 PPS_SMS_API_URL/PAYPAL_SMS_API_URL 或 config.paypal.sms_api_url")
    p.add_argument("--paypal-country", default="US", help="PayPal signup locale country")
    p.add_argument("--paypal-lang", default="en", help="PayPal signup locale lang")
    p.add_argument("--otp-timeout", type=int, default=180)
    p.add_argument("--billing-country", default="", help="覆盖 Stripe billing country")
    p.add_argument("--billing-currency", default="", help="覆盖记录用 currency")
    p.add_argument("--billing-name", default="")
    p.add_argument("--billing-email", default="")
    p.add_argument("--card-number", default="", help="可选固定卡；--paypal-node-rpa 下默认优先用 meiguodizhi 随机卡，固定卡仅作兜底")
    p.add_argument("--card-expiry", default="", help="可选固定卡有效期；随机卡模式下由 meiguodizhi Expires 提供")
    p.add_argument("--card-cvv", default="", help="可选固定卡 CVV；随机卡模式下由 meiguodizhi CVV2 提供")
    p.add_argument("--prefer-fixed-card", action="store_true", help="--paypal-node-rpa 下显式优先使用 --card-number/--card-expiry/--card-cvv，只从 meiguodizhi 随机姓名/地址")
    p.add_argument("--captcha-api-url", default="", help="可选：覆盖 createTask/getTaskResult 打码 API base URL")
    p.add_argument("--captcha-api-key", default="", help="可选：覆盖打码 API key")
    p.add_argument("--paypal-manual-address", action="store_true", help="兼容旧参数：现在默认就是手填 MANUAL")
    p.add_argument("--paypal-autocomplete-address", action="store_true", help="实验参数：PayPal signup 改走 AddressAutocomplete/GOOGLE")
    p.add_argument("--paypal-signup-retries", type=int, default=3, help="PayPal createMemberAccount/OAS_ERROR 无 EUAT 时，按油猴随机 persona/address 重试次数")
    p.add_argument("--paypal-seed-retries", type=int, default=2, help="允许 Camoufox seed 时，同一 BA token 最多 seed 重试次数")
    p.add_argument(
        "--paypal-allow-http-fallback-on-seed-fail",
        action="store_true",
        help="危险兼容开关：Camoufox seed 失败后仍回退纯 HTTP（默认不回退，避免直接触发 hcaptchapassive）",
    )
    p.add_argument("--paypal-disable-idapps", action="store_true", help="跳过 PayPal idapps OTP challenge 预热（用于验证是否反而触发 authchallenge）")
    p.add_argument("--paypal-browser-form-warmup", action="store_true", help="实验：用 seed 浏览器只做表单字段/风控 beacon 预热，最终提交仍走协议")
    p.add_argument("--paypal-allow-browser-recaptcha", action="store_true", help="实验：允许浏览器执行 PayPal passive reCAPTCHA v3 取 token 后协议提交 validatecaptcha")
    p.add_argument("--paypal-node-rpa", action="store_true", help="PayPal 段改用 Node/Chromium RPA，让 PayPal 自己前端完成风控/注册/授权")
    p.add_argument("--paypal-node-rpa-paypal-only", action="store_true", help="保留旧模式：仅 PayPal 段进浏览器；默认不建议，整页 checkout 更接近油猴脚本")
    p.add_argument("--paypal-node-rpa-headless", action="store_true", help="Node RPA 使用 headless Chromium；默认用 xvfb headed，风控更接近油猴/RPA")
    p.add_argument("--paypal-node-rpa-timeout", type=int, default=720, help="Node PayPal RPA 总超时秒数")
    p.add_argument("--promo-campaign-id", default="plus-1-month-free")
    p.add_argument(
        "--inventory-mail-source",
        choices=["any", "outlook", "catch_all"],
        default="any",
        help="auto-gen 选库存账号时的邮箱来源过滤：any=不限；outlook=只挑 @outlook/@hotmail/@live/@msn；catch_all=只挑 CTF-reg config 里的 catch_all_domain(s)",
    )
    p.add_argument("--expected-due", type=int, default=0, help="期望 Stripe due；优惠命中通常为 0")
    p.add_argument("--max-due", type=int, default=100, help="超过该金额拒绝执行，防止全价开通")
    p.add_argument("--allow-full-price", action="store_true")
    p.add_argument("--allow-already-paid", action="store_true", help="允许对已由 RT 标记为 plus/team/pro 的账号继续执行")
    p.add_argument("--allow-camoufox-seed", action="store_true", help="非纯协议兜底：允许 Camoufox 只做 DataDome seed")
    p.add_argument("--persist-to", default="")
    p.add_argument("--no-mark-used", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只生成临时 config 并打印目标，不执行支付")
    p.add_argument(
        "--worker-id",
        default="",
        help="并发跑多 worker 时各自的唯一标识；空 = env NCPP_WORKER_ID 或 'w<pid>'。"
        " 影响 DB claim 标签 + /tmp 输出文件路径 + log 前缀, 防多 worker 串文件",
    )
    args = p.parse_args()

    # Resolve worker id once and export to env, so all downstream child processes (Node RPA / gost) see consistent values.
    _worker_id = _resolve_worker_id(getattr(args, "worker_id", ""))
    os.environ["NCPP_WORKER_ID"] = _worker_id
    print(f"[worker] id={_worker_id} pid={os.getpid()}")

    base_config = Path(args.config)
    if not base_config.exists():
        raise SystemExit(f"基础配置不存在: {base_config}")

    row = _select_promo_link(args)
    def _bail_release(msg: str) -> None:
        """Release already claimed promo_link before SystemExit to avoid deadlock."""
        try:
            link_id = int(row.get("id") or 0)
            if link_id > 0:
                get_db().release_promo_link(link_id, "fresh")
        except Exception:
            pass
        raise SystemExit(msg)

    current_plan = _latest_account_plan(row.get("email") or "")
    if current_plan in {"plus", "team", "pro"} and not args.allow_already_paid:
        _bail_release(
            f"拒绝执行：{row.get('email')} 当前库存 RT 状态已是 {current_plan}。"
            "如确认要继续，显式加 --allow-already-paid。"
        )
    due = int(row.get("amount_due_cents") or 0)
    if not args.allow_full_price and due > int(args.max_due):
        _bail_release(
            f"拒绝执行：promo link due={due} > max_due={args.max_due}。"
            "如果确认要全价，显式加 --allow-full-price。"
        )

    tmp_config = _build_temp_config(base_config=base_config, row=row, args=args)
    print("[target]")
    print(f"  promo_link_id: {row.get('id')}")
    print(f"  email:         {row.get('email') or args.email or '-'}")
    print(f"  plan:          {row.get('plan_name')}")
    print(f"  campaign:      {row.get('promo_campaign_id') or args.promo_campaign_id}")
    print(f"  region:        {row.get('billing_country')}/{row.get('billing_currency')}")
    print(f"  due:           {due}")
    print(f"  checkout:      {_mask_url(row.get('checkout_url') or '')}")
    print(f"  temp_config:   {tmp_config}")
    print(f"  pure_protocol: {not args.allow_camoufox_seed and not args.paypal_node_rpa}")
    if args.paypal_node_rpa:
        print("  paypal_mode:   node_rpa_full_checkout" if not args.paypal_node_rpa_paypal_only else "  paypal_mode:   node_rpa_paypal_only")

    if args.dry_run:
        print("[dry-run] 未执行支付。")
        # Release claim to prevent dry-run from locking promo_link
        try:
            link_id = int(row.get("id") or 0)
            if link_id > 0:
                get_db().release_promo_link(link_id, "fresh")
        except Exception:
            pass
        return 0

    _ensure_proxy_alive(_load_json(tmp_config))

    if args.paypal_node_rpa and not args.paypal_node_rpa_paypal_only:
        started = time.time()
        max_attempts = max(1, int(args.paypal_signup_retries or 1))
        # Outer promo_link rotation limit (when stripe_due_mismatch_before_submit is triggered, switch to the next fresh one)
        max_rotations = max(1, int(getattr(args, "max_promo_rotations", 0) or 5))
        seen_link_ids: set[int] = set()
        seen_link_emails: set[str] = set()
        attempts: list[dict[str, Any]] = []
        result: dict[str, Any] = {}

        for rotation in range(1, max_rotations + 1):
            link_id_cur = int(row.get("id") or 0)
            if link_id_cur > 0:
                seen_link_ids.add(link_id_cur)
            link_email_cur = str(row.get("email") or "").strip().lower()
            if link_email_cur:
                seen_link_emails.add(link_email_cur)
            if rotation > 1:
                print(
                    f"[rotate] 第 {rotation}/{max_rotations} 个 promo_link → "
                    f"id={link_id_cur} email={row.get('email')}"
                )

            # Inner layer: Same as signup retries on promo_link (random persona/card)
            for attempt in range(1, max_attempts + 1):
                if attempt > 1:
                    print(f"[node-rpa-full] 第 {attempt}/{max_attempts} 次：上一张卡/PayPal 新号失败，重新随机 persona/card 后重试")
                    tmp_config = _build_temp_config(base_config=base_config, row=row, args=args)
                    _ensure_proxy_alive(_load_json(tmp_config))
                result = _run_node_full_checkout_rpa(row, tmp_config, args)
                attempts.append({
                    "rotation": rotation,
                    "promo_link_id": link_id_cur,
                    "attempt": attempt,
                    "success": bool(result.get("success")),
                    "error": str(result.get("error") or "")[:300],
                    "finalUrl": _mask_url(str(result.get("finalUrl") or "")),
                })
                if bool(result.get("success")):
                    break
                retryable, reason = _node_rpa_retryable_failure(result)
                if not retryable or attempt >= max_attempts:
                    if retryable and reason:
                        result.setdefault("retry_reason", reason)
                    break
                print(f"[node-rpa-full] retryable={reason}，准备换随机卡/身份重试")

            if bool(result.get("success")):
                break

            # Detect "promo not matched" — guard throws stripe_due_mismatch_before_submit
            err_text = str(result.get("error") or "")
            due_mismatch = (
                "stripe_due_mismatch_before_submit" in err_text
                or "stripe_due_mismatch" in err_text
            )
            if not due_mismatch:
                # Other failures (RESTRICTED_USER / DataDome / OTP etc.), switching promo_link won't help, stop
                break
            # Mark expired to prevent auto-pick next time
            if link_id_cur > 0 and not args.no_mark_used:
                try:
                    ok = get_db().mark_promo_link_status(link_id_cur, "expired")
                    print(
                        f"[rotate] promo_link.id={link_id_cur} 优惠不命中 (due > 0), "
                        f"标 expired {'✓' if ok else '✗'}"
                    )
                except Exception as e:
                    print(f"[rotate] mark_promo_link_status 失败: {e}")
            if args.promo_link_id:
                # User explicitly specified single link, no auto-poll to next
                print("[rotate] 显式 --promo-link-id 模式, 不自动轮询下一个 fresh")
                break
            if rotation >= max_rotations:
                print(f"[rotate] 已达上限 {max_rotations}, 停止")
                break
            # Pick next fresh one excluding seen, use atomic claim (concurrent workers don't contend same row)
            try:
                next_row = get_db().claim_next_fresh_promo_link(
                    worker_id=_worker_id,
                    plan_like="plus",
                    max_due_cents=int(args.max_due) if not args.allow_full_price else 0,
                    exclude_ids=list(seen_link_ids),
                )
            except Exception as e:
                print(f"[rotate] claim 下一个 fresh promo_link 失败: {e}")
                next_row = None
            if not next_row:
                print("[rotate] 没有更多 fresh promo_link 可用, 尝试从库存账号自动生产")
                generated = _auto_generate_promo_link(
                    args, exclude_emails=seen_link_emails, worker_id=_worker_id,
                )
                # auto-gen returned row is already in_use + claimed_by=self, no second claim
                next_row = generated if (generated and generated.get("id")) else None
                if not next_row:
                    print("[rotate] auto-gen 失败, 停止")
                    break
            row = dict(next_row)
            print(
                f"[rotate] 换下一个 promo_link → id={row['id']} email={row['email']} "
                f"region={row.get('billing_country')}/{row.get('billing_currency')}"
            )
            tmp_config = _build_temp_config(base_config=base_config, row=row, args=args)
            _ensure_proxy_alive(_load_json(tmp_config))

        if len(attempts) > 1:
            result = dict(result)
            result["attempts"] = attempts
            result["rotations"] = len({a.get("promo_link_id") for a in attempts if a.get("promo_link_id")})
        state = "succeeded" if bool(result.get("success")) else "failed"
        print("[result]")
        print(json.dumps({
            "state": state,
            "elapsed_s": round(time.time() - started, 1),
            "raw": result,
        }, ensure_ascii=False, indent=2))
        if state == "succeeded":
            if not args.no_mark_used:
                _mark_link_used(row)
            # Wait for webhook / accounts check side plan refresh to land.
            plan = _wait_inventory_paid_plan(row, timeout_s=120)
            if not plan:
                _refresh_inventory_plan(row)
            return 0
        # Failure: revert in_use back to fresh so other workers (or next run) can reuse same long link,
        # But stripe_due_mismatch already marked expired in rotation, here only release current row that still in_use.
        try:
            link_id = int(row.get("id") or 0)
            if link_id > 0:
                released = get_db().release_promo_link(link_id, "fresh")
                if released:
                    print(f"[release] promo_link.id={link_id} 失败回滚 in_use → fresh")
        except Exception as e:
            print(f"[release] 失败: {e}")
        return 2

    from card import run as card_run

    started = time.time()
    result = card_run(
        row["checkout_url"],
        card_index=0,
        config_path=str(tmp_config),
        manual_token="",
        force_fresh=False,
        fresh_only=False,
        offline_replay=False,
        local_mock=False,
        use_paypal=True,
        use_gopay=False,
    )
    state = str((result or {}).get("state") or "").lower()
    print("[result]")
    print(json.dumps({
        "state": state,
        "elapsed_s": round(time.time() - started, 1),
        "raw": result,
    }, ensure_ascii=False, indent=2))

    if state == "succeeded":
        if not args.no_mark_used:
            _mark_link_used(row)
        _refresh_inventory_plan(row)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
