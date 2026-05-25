"""Outlook Account Pool — 4-segment format bulk import + claim next unused at runtime.

Format (one per line):
    email----password----client_id----refresh_token

DB table outlook_accounts, state machine:
    available → claim → in_use → mark_used (registration success) | mark_dead (refresh_token expired)"""
from __future__ import annotations

import imaplib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Optional

from .db import get_db

logger = logging.getLogger(__name__)

GRAPH_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
IMAP_HOST = "outlook.office365.com"


# ──────────────────────── Parse + Import ────────────────────────


def parse_lines(text: str) -> list[dict]:
    """Parse multi-line 4-segment format → list of dicts. Invalid lines skipped."""
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) != 4:
            continue
        email, password, client_id, refresh = (p.strip() for p in parts)
        if "@" not in email or not refresh.startswith("M."):
            continue
        out.append({
            "email": email.lower(),
            "password": password,
            "client_id": client_id,
            "refresh_token": refresh,
        })
    return out


def validate_account(email: str, refresh_token: str, client_id: str,
                     timeout: int = 12) -> tuple[str, str]:
    """Run RT-grant + IMAP XOAUTH2 dual validation on single account.

    Returns (status, fail_reason):
      - ('available', '')                   RT valid + IMAP pass → truly usable
      - ('dead', 'RT expired: ...')          refresh_token grant failed (expired/banned)
      - ('dead', 'IMAP XOAUTH2 rejected: ...') RT ok but IMAP scope missing (supplier client_id restriction)
      - ('dead', 'IMAP connection error: ...') network/proxy issue"""
    # Step 1: RT → access_token (v2 endpoint + IMAP scope)
    try:
        at = get_outlook_access_token(refresh_token, client_id)
    except Exception as e:
        err = str(e)[:180]
        # Distinguish common reasons
        if "service abuse" in err.lower() or "abuse mode" in err.lower():
            return "dead", f"账号被 Microsoft 封禁: {err}"
        if "400" in err or "invalid_grant" in err.lower():
            return "dead", f"refresh_token 失效或 client_id 不匹配: {err}"
        return "dead", f"RT 失效: {err}"

    # Step 2: Real IMAP XOAUTH2 (~3-5s)
    try:
        import imaplib
        M = imaplib.IMAP4_SSL(IMAP_HOST, 993)
        M.socket().settimeout(timeout)
        auth = f"user={email}\x01auth=Bearer {at}\x01\x01"
        typ, _ = M.authenticate("XOAUTH2", lambda x: auth.encode())
        try:
            M.logout()
        except Exception:
            pass
        if typ != "OK":
            return "dead", (f"IMAP XOAUTH2 拒绝 (supplier 注册 client_id 时可能未声明 "
                           f"v2 outlook.office.com/IMAP.AccessAsUser.All scope; "
                           f"建议走 Device Code Flow 用 Thunderbird client_id 重拿 RT)")
        return "available", ""
    except Exception as e:
        err = str(e)[:180]
        if "AUTHENTICATE" in err:
            return "dead", (f"IMAP XOAUTH2 拒绝 ({err}); "
                           f"多半 supplier client_id 没 v2 IMAP scope")
        return "dead", f"IMAP 连接/认证异常: {err}"


def import_lines(text: str, validate: bool = True, concurrency: int = 8) -> dict:
    """Bulk import + run RT/IMAP validation concurrently by default, mark failed ones as dead on DB write.

    validate=True uses ThreadPoolExecutor concurrency N=8 (single account ~3-8s, 100 accounts ~10s);
    too high concurrency triggers Microsoft rate limiting (HTTP 429 / IMAP banned), 8 is stable;
    use validate=False for pure import (skip validation)."""
    rows = parse_lines(text)
    db = get_db()
    con = db._conn()
    inserted = updated = skipped = 0
    valid = invalid = 0
    fail_reasons: dict[str, int] = {}
    now = time.time()

    # Step 1: Concurrent validate, collect (idx, status, fail_reason)
    results: dict[int, tuple[str, str]] = {}
    if validate and rows:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(rows)))) as ex:
            futures = {
                ex.submit(validate_account, r["email"], r["refresh_token"], r["client_id"]): i
                for i, r in enumerate(rows)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    status, fail = fut.result()
                except Exception as e:
                    status, fail = "dead", f"验证 worker 异常: {str(e)[:120]}"
                results[idx] = (status, fail)
                if status == "available":
                    valid += 1
                else:
                    invalid += 1
                    short = fail.split(":")[0][:60] if fail else "(unknown)"
                    fail_reasons[short] = fail_reasons.get(short, 0) + 1
    else:
        for i in range(len(rows)):
            results[i] = ("available", "")

    # Step 2: Serial DB write (SQLite not good at concurrent writes)
    for i, r in enumerate(rows):
        status, fail = results[i]
        cur = con.execute(
            "SELECT email, refresh_token FROM outlook_accounts WHERE email=?",
            (r["email"],),
        )
        existing = cur.fetchone()
        if existing is None:
            con.execute(
                "INSERT INTO outlook_accounts(email, password, client_id, refresh_token, "
                "status, fail_reason, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (r["email"], r["password"], r["client_id"], r["refresh_token"],
                 status, fail, now),
            )
            inserted += 1
        elif existing["refresh_token"] != r["refresh_token"]:
            con.execute(
                "UPDATE outlook_accounts SET refresh_token=?, password=?, client_id=?, "
                "status=?, fail_reason=?, imported_at=? WHERE email=?",
                (r["refresh_token"], r["password"], r["client_id"],
                 status, fail, now, r["email"]),
            )
            updated += 1
        else:
            skipped += 1
    con.commit()
    return {
        "parsed": len(rows),
        "inserted": inserted, "updated": updated, "skipped": skipped,
        "validated": validate,
        "valid_imap": valid, "invalid_imap": invalid,
        "fail_reasons": fail_reasons,
        "concurrency": concurrency,
    }


# ──────────────────────── claim / mark ────────────────────────


def revalidate_all(concurrency: int = 8, include_used: bool = False) -> dict:
    """Run concurrent RT + IMAP validation on all accounts in pool (default exclude status='used'), write back status + fail_reason.

    'used' excluded by default: already marked used by OpenAI, RT status irrelevant going forward (unless include_used=True).
    Returns {scanned, valid_imap, invalid_imap, transitions, fail_reasons, elapsed}."""
    import time as _t
    con = get_db()._conn()
    if include_used:
        cur = con.execute(
            "SELECT email, refresh_token, client_id, status FROM outlook_accounts"
        )
    else:
        cur = con.execute(
            "SELECT email, refresh_token, client_id, status FROM outlook_accounts WHERE status != 'used'"
        )
    rows = cur.fetchall()
    if not rows:
        return {"scanned": 0, "valid_imap": 0, "invalid_imap": 0, "transitions": [],
                "fail_reasons": {}, "elapsed": 0.0}

    t0 = _t.time()
    results: dict[int, tuple[str, str]] = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(rows)))) as ex:
        futures = {
            ex.submit(validate_account, r["email"], r["refresh_token"], r["client_id"]): i
            for i, r in enumerate(rows)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                results[i] = ("dead", f"验证 worker 异常: {str(e)[:120]}")

    valid = invalid = 0
    transitions: list[dict] = []
    fail_reasons: dict[str, int] = {}
    for i, r in enumerate(rows):
        new_status, new_fail = results[i]
        old_status = r["status"]
        if new_status == "available":
            valid += 1
        else:
            invalid += 1
            short = (new_fail.split(":")[0])[:60] if new_fail else "(unknown)"
            fail_reasons[short] = fail_reasons.get(short, 0) + 1
        if old_status != new_status:
            transitions.append({"email": r["email"], "from": old_status, "to": new_status})
        # claimed_at=0 because possibly released from in_use but status not updated
        if new_status == "available":
            con.execute(
                "UPDATE outlook_accounts SET status=?, fail_reason=?, claimed_at=0 WHERE email=?",
                (new_status, new_fail, r["email"]),
            )
        else:
            con.execute(
                "UPDATE outlook_accounts SET status=?, fail_reason=? WHERE email=?",
                (new_status, new_fail, r["email"]),
            )
    con.commit()

    elapsed = _t.time() - t0
    return {
        "scanned": len(rows),
        "valid_imap": valid,
        "invalid_imap": invalid,
        "transitions": transitions,
        "fail_reasons": fail_reasons,
        "elapsed": round(elapsed, 1),
        "concurrency": concurrency,
    }


def claim_next() -> Optional[dict]:
    """Atomically claim next available outlook for registration; return None if none available."""
    db = get_db()
    con = db._conn()
    cur = con.execute(
        "SELECT email, password, client_id, refresh_token FROM outlook_accounts "
        "WHERE status='available' ORDER BY imported_at ASC LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        return None
    email = row["email"]
    rc = con.execute(
        "UPDATE outlook_accounts SET status='in_use', claimed_at=? WHERE email=? AND status='available'",
        (time.time(), email),
    )
    if rc.rowcount != 1:
        # Concurrently taken by others, retry once
        con.commit()
        return claim_next()
    con.commit()
    return {
        "email": email,
        "password": row["password"],
        "client_id": row["client_id"],
        "refresh_token": row["refresh_token"],
    }


def claim_email(email: str) -> Optional[dict]:
    """Atomically claim specific email (succeeds only if status='available'); return None for other states.

    Differs from claim_next: caller explicitly specifies which account (UI dropdown selection),
    if account is already in_use/used/dead return None directly for upstream error, don't auto-switch."""
    email = (email or "").strip().lower()
    if not email:
        return None
    db = get_db()
    con = db._conn()
    cur = con.execute(
        "SELECT email, password, client_id, refresh_token, status FROM outlook_accounts "
        "WHERE email=?",
        (email,),
    )
    row = cur.fetchone()
    if not row or row["status"] != "available":
        return None
    rc = con.execute(
        "UPDATE outlook_accounts SET status='in_use', claimed_at=? "
        "WHERE email=? AND status='available'",
        (time.time(), email),
    )
    if rc.rowcount != 1:
        con.commit()
        return None  # Concurrently taken
    con.commit()
    return {
        "email": email,
        "password": row["password"],
        "client_id": row["client_id"],
        "refresh_token": row["refresh_token"],
    }


def mark_used(email: str, chatgpt_email: str = "") -> None:
    """Registration successful; later reused for pay-only (registered_accounts table)."""
    con = get_db()._conn()
    con.execute(
        "UPDATE outlook_accounts SET status='used', used_at=?, chatgpt_email=? WHERE email=?",
        (time.time(), chatgpt_email or email, email),
    )
    con.commit()


def mark_dead(email: str, reason: str = "") -> None:
    con = get_db()._conn()
    con.execute(
        "UPDATE outlook_accounts SET status='dead', fail_reason=? WHERE email=?",
        (reason[:500], email),
    )
    con.commit()


def release_unused(email: str) -> None:
    """Post-claim not actually registered (exception / user canceled) → return to available."""
    con = get_db()._conn()
    con.execute(
        "UPDATE outlook_accounts SET status='available', claimed_at=0 WHERE email=? AND status='in_use'",
        (email,),
    )
    con.commit()


# ──────────────────────── List / Status ────────────────────────


def list_accounts(limit: int = 200, status: str = "") -> list[dict]:
    con = get_db()._conn()
    if status:
        cur = con.execute(
            "SELECT email, status, imported_at, claimed_at, used_at, chatgpt_email, fail_reason "
            "FROM outlook_accounts WHERE status=? ORDER BY imported_at DESC LIMIT ?",
            (status, limit),
        )
    else:
        cur = con.execute(
            "SELECT email, status, imported_at, claimed_at, used_at, chatgpt_email, fail_reason "
            "FROM outlook_accounts ORDER BY imported_at DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in cur.fetchall()]


def stats() -> dict:
    con = get_db()._conn()
    cur = con.execute("SELECT status, COUNT(*) AS n FROM outlook_accounts GROUP BY status")
    out = {"available": 0, "in_use": 0, "used": 0, "dead": 0, "total": 0}
    for r in cur.fetchall():
        out[r["status"]] = r["n"]
        out["total"] += r["n"]
    return out


def delete(email: str) -> bool:
    con = get_db()._conn()
    rc = con.execute("DELETE FROM outlook_accounts WHERE email=?", (email,))
    con.commit()
    return rc.rowcount > 0


# ──────────────────────── outlook IMAP OAuth2 fetch OTP ────────────────────────


def get_outlook_access_token(refresh_token: str, client_id: str) -> str:
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "scope": IMAP_SCOPE,
    }).encode()
    req = urllib.request.Request(GRAPH_TOKEN_URL, data=body)
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read())
    if not data.get("access_token"):
        raise RuntimeError(f"outlook refresh failed: {data}")
    return data["access_token"]


def _is_hex_color_context(haystack: str, idx: int) -> bool:
    if idx > 0 and haystack[idx - 1] == "#":
        return True
    before = haystack[max(0, idx - 30):idx]
    return bool(re.search(r"(?:color|background|bgcolor|fill|stroke)\s*[:=]\s*[\"']?#?\s*$", before, re.IGNORECASE))


def _extract_otp_from_html(body: str) -> Optional[str]:
    for pat in (
        r"(?:code(?:\s*is)?|verification|one[-\s]*time|verify|kode|verifikasi|代码|验证码|驗證碼)[^\d<>]{0,80}(\d{6})\b",
        r"chatgpt[^\d<>]{0,80}(\d{6})",
        r"openai[^\d<>]{0,80}(\d{6})",
    ):
        for m in re.finditer(pat, body, re.IGNORECASE | re.DOTALL):
            if not _is_hex_color_context(body, m.start(1)):
                return m.group(1)
    for m in re.finditer(r"\b(\d{6})\b", body):
        if not _is_hex_color_context(body, m.start(1)):
            return m.group(1)
    return None


def fetch_otp_via_imap(email: str, refresh_token: str, client_id: str,
                       timeout: int = 240, threshold_ts: float = 0) -> str:
    """Block-pull outlook OTP (latest email from OpenAI). Return 6-digit OTP or raise TimeoutError.

    Scan multiple folders: INBOX, Junk, Junk Email, Spam. Outlook anti-spam often routes
    OpenAI OTP emails to strangers directly to Junk; single INBOX query pretends "not received"."""
    import email as _email
    deadline = time.time() + max(60, timeout)
    if not threshold_ts:
        threshold_ts = time.time() - 300  # 5min grace
    seen: set = set()
    cached_token = ""
    cached_at = 0.0
    folders_to_scan = ["INBOX", "Junk", "Junk Email", "Spam"]
    found_folders: list[str] | None = None  # LIST detection cached once
    while time.time() < deadline:
        try:
            if not cached_token or time.time() - cached_at > 3000:
                cached_token = get_outlook_access_token(refresh_token, client_id)
                cached_at = time.time()
            M = imaplib.IMAP4_SSL(IMAP_HOST, 993)
            auth_string = f"user={email}\x01auth=Bearer {cached_token}\x01\x01"
            typ, _ = M.authenticate("XOAUTH2", lambda x: auth_string.encode())
            if typ != "OK":
                raise RuntimeError("imap XOAUTH2 失败")
            # First connection probes real folder names (Junk naming differs by Outlook region)
            if found_folders is None:
                try:
                    typ, listing = M.list()
                    names_lower: dict[str, str] = {}
                    for raw in (listing or []):
                        if not raw:
                            continue
                        s = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
                        # IMAP LIST line end is quoted mailbox name
                        m = re.search(r'"([^"]+)"\s*$', s) or re.search(r"\s(\S+)\s*$", s)
                        if m:
                            nm = m.group(1).strip('"')
                            names_lower[nm.lower()] = nm
                    picked = []
                    for cand in folders_to_scan:
                        real = names_lower.get(cand.lower())
                        if real and real not in picked:
                            picked.append(real)
                    # Fallback: fuzzy match "junk" / "spam" / "bulk" substrings
                    for k, v in names_lower.items():
                        if any(x in k for x in ("junk", "spam", "bulk")) and v not in picked:
                            picked.append(v)
                    if "INBOX" not in picked:
                        picked.insert(0, "INBOX")
                    found_folders = picked
                    logger.info(f"[outlook-pool] {email} folders to scan: {found_folders}")
                except Exception as e:
                    logger.warning(f"[outlook-pool] LIST 失败，回退默认列表: {e}")
                    found_folders = list(folders_to_scan)

            for folder in found_folders:
                try:
                    # Folder names with spaces need quotes
                    sel_arg = f'"{folder}"' if " " in folder else folder
                    typ, _ = M.select(sel_arg, readonly=True)
                    if typ != "OK":
                        continue
                except Exception:
                    continue
                try:
                    # SEARCH ALL + python layer From validation. Previously used 5-level nested OR compound query
                    # Triggers 'BAD Command Argument Error. 12' on Office365 IMAP then
                    # silently swallowed by except, email never found.
                    # Actual From observed may be:
                    #   - ChatGPT <noreply@tm.openai.com>  (outlook.com recipient)
                    #   - bounces+xxx@em7877.tm.open       (catch_all domain recipient, SendGrid relay)
                    # Validate with python layer (line 526) covers both, no complex IMAP query dependency.
                    typ, data = M.search(None, "ALL")
                    ids = (data[0].split() if data and data[0] else [])
                except Exception as e:
                    logger.warning(f"[outlook-pool] SEARCH 失败 folder={folder}: {e}")
                    continue
                for mid in reversed(ids[-8:]):
                    key = (folder, mid)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        typ, raw = M.fetch(mid, "(BODY.PEEK[])")
                        msg = _email.message_from_bytes(raw[0][1])
                    except Exception:
                        continue
                    date_str = msg.get("Date") or ""
                    try:
                        import email.utils as eu
                        msg_ts = eu.parsedate_to_datetime(date_str).timestamp()
                    except Exception:
                        msg_ts = 0
                    if msg_ts and msg_ts < threshold_ts:
                        continue
                    # Validate From field, must be OpenAI domain (prevent forgery / system emails containing "OpenAI" false positive)
                    from_field = (msg.get("From") or "").lower()
                    if not any(d in from_field for d in (
                        # OpenAI own + SendGrid relay (tested from
                        # = "bounces+xxxxxxx-fdd4-isiner1988=lukyface.com@em7877.tm.open")
                        "openai.com", "auth.openai", "tm.openai", "chatgpt.com",
                        "tm.open",  # OpenAI SendGrid relay subdomain (em*.tm.open)
                    )):
                        logger.debug(f"[outlook-pool] skip non-OpenAI from={from_field[:80]}")
                        continue
                    # tm1.openai.com is OpenAI current broken "shadow" OTP domain: returns fixed
                    # OTP=493682 across all accounts, validate 100% 401 wrong_email_otp_code. Each
                    # OpenAI sign-in sends both tm.openai.com (genuine) + tm1.openai.com (broken)
                    # two emails, by IMAP id reverse order often hits tm1's 493682 first → protocol login hangs.
                    # Hard filter tm1.* here, keep only tm.openai.com domain genuine OTP.
                    if "tm1.openai" in from_field:
                        logger.info(
                            f"[outlook-pool] skip tm1.openai.com 影子发码: id={mid.decode()} "
                            f"from={from_field[:60]}"
                        )
                        continue
                    text_body = ""
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            try:
                                payload = part.get_payload(decode=True) or b""
                                text_body += payload.decode(part.get_content_charset() or "utf-8", errors="replace") + "\n"
                            except Exception:
                                continue
                    otp = _extract_otp_from_html(text_body)
                    if otp:
                        logger.info(
                            f"[outlook-pool] {email} OTP 命中 folder={folder!r} "
                            f"msg_ts={int(msg_ts)} otp={otp}"
                        )
                        try:
                            M.logout()
                        except Exception:
                            pass
                        return otp
            try:
                M.logout()
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[outlook-pool] fetch_otp 异常 (吃掉重试): {e}")
        time.sleep(4)
    raise TimeoutError(f"outlook OTP timeout {timeout}s for {email}")
