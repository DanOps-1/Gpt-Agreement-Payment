"""Outlook 账号池 — 4 段接码格式批量入库 + Run 时 claim 下一个未用的。

格式（每行一个）：
    email----password----client_id----refresh_token

DB 表 outlook_accounts，状态机：
    available → claim → in_use → mark_used (注册成功) | mark_dead (refresh_token 失效)
"""
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


# ──────────────────────── 解析 + 入库 ────────────────────────


def parse_lines(text: str) -> list[dict]:
    """解析多行 4 段格式 → list of dicts。无效行被跳过。"""
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
    """对单个号跑 RT-grant + IMAP XOAUTH2 双验证.

    返 (status, fail_reason):
      - ('available', '')                   RT 有效 + IMAP 通 → 真能用
      - ('dead', 'RT 失效: ...')             refresh_token grant 失败 (过期/封号)
      - ('dead', 'IMAP XOAUTH2 拒绝: ...')   RT 通但 IMAP scope 缺 (supplier client_id 限制)
      - ('dead', 'IMAP 连接异常: ...')       网络/代理问题
    """
    # Step 1: RT → access_token (v2 endpoint + IMAP scope)
    try:
        at = get_outlook_access_token(refresh_token, client_id)
    except Exception as e:
        err = str(e)[:180]
        # 区分常见原因
        if "service abuse" in err.lower() or "abuse mode" in err.lower():
            return "dead", f"账号被 Microsoft 封禁: {err}"
        if "400" in err or "invalid_grant" in err.lower():
            return "dead", f"refresh_token 失效或 client_id 不匹配: {err}"
        return "dead", f"RT 失效: {err}"

    # Step 2: 真 IMAP XOAUTH2 (~3-5s)
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
    """批量入库 + 默认并发跑 RT/IMAP 验证, 失败的入 DB 时就标 dead.

    validate=True 走 ThreadPoolExecutor 并发 N=8 (单号 ~3-8s, 100 号也只要 ~10s);
    concurrency 太高会被 Microsoft 限速 (HTTP 429 / IMAP banned), 8 是稳态;
    想纯入库 (跳过验证) 走 validate=False.
    """
    rows = parse_lines(text)
    db = get_db()
    con = db._conn()
    inserted = updated = skipped = 0
    valid = invalid = 0
    fail_reasons: dict[str, int] = {}
    now = time.time()

    # Step 1: 并发跑 validate, 收集 (idx, status, fail_reason)
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

    # Step 2: 串行写 DB (SQLite 不擅长并发写)
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
    """对池子里所有 (默认排除 status='used') 的号并发跑 RT + IMAP 验证, 写回 status + fail_reason.

    used 默认排除: 它们已被 OpenAI 标 used, RT 状态不影响后续 (除非 include_used=True).
    返 {scanned, valid_imap, invalid_imap, transitions, fail_reasons, elapsed}.
    """
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
        # claimed_at=0 因为可能之前 in_use 被释放但状态没改
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
    """原子 claim 下一个 available outlook 给 register 用；无可用返 None。"""
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
        # 并发场景被别人抢了，重试一次
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
    """原子 claim 指定 email（仅 status='available' 时成功）；其它状态返 None。

    跟 claim_next 的区别：调用方明确指定要用哪个号（webui UI 下拉选定），
    若该号已 in_use/used/dead 直接返 None 让上游报错，不擅自换号。
    """
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
        return None  # 并发被抢
    con.commit()
    return {
        "email": email,
        "password": row["password"],
        "client_id": row["client_id"],
        "refresh_token": row["refresh_token"],
    }


def mark_used(email: str, chatgpt_email: str = "") -> None:
    """注册成功；后续 pay-only 复用 (registered_accounts 表)。"""
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
    """claim 后没真注册（异常 / 用户取消）→ 还回 available。"""
    con = get_db()._conn()
    con.execute(
        "UPDATE outlook_accounts SET status='available', claimed_at=0 WHERE email=? AND status='in_use'",
        (email,),
    )
    con.commit()


# ──────────────────────── 列表 / 状态 ────────────────────────


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
    """阻塞拉 outlook OTP（OpenAI 来的最新邮件）。返回 6 位 OTP 或抛 TimeoutError。

    扫描多 folder：INBOX、Junk、Junk Email、Spam。outlook 反垃圾经常把 OpenAI
    第一次发给陌生收件人的验证码邮件直接 route 到 Junk，单查 INBOX 会假装"未收到"。
    """
    import email as _email
    deadline = time.time() + max(60, timeout)
    if not threshold_ts:
        threshold_ts = time.time() - 300  # 5min grace
    seen: set = set()
    cached_token = ""
    cached_at = 0.0
    folders_to_scan = ["INBOX", "Junk", "Junk Email", "Spam"]
    found_folders: list[str] | None = None  # LIST 探测一次就缓存
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
            # 第一次连接时探测真实 folder 名字（不同 outlook 区域 Junk 命名不同）
            if found_folders is None:
                try:
                    typ, listing = M.list()
                    names_lower: dict[str, str] = {}
                    for raw in (listing or []):
                        if not raw:
                            continue
                        s = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
                        # IMAP LIST 行末是带引号的 mailbox 名
                        m = re.search(r'"([^"]+)"\s*$', s) or re.search(r"\s(\S+)\s*$", s)
                        if m:
                            nm = m.group(1).strip('"')
                            names_lower[nm.lower()] = nm
                    picked = []
                    for cand in folders_to_scan:
                        real = names_lower.get(cand.lower())
                        if real and real not in picked:
                            picked.append(real)
                    # 兜底：模糊匹配 "junk" / "spam" / "bulk" 子串
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
                    # 带空格的 folder 名要加引号
                    sel_arg = f'"{folder}"' if " " in folder else folder
                    typ, _ = M.select(sel_arg, readonly=True)
                    if typ != "OK":
                        continue
                except Exception:
                    continue
                try:
                    # SEARCH ALL + python 层 From 校验. 之前用 5 层嵌套 OR 复合 query
                    # 在 Office365 IMAP 触发 'BAD Command Argument Error. 12' 然后被
                    # except 静默吞掉, 永远找不到邮件.
                    # 真实 From 实测可能是:
                    #   - ChatGPT <noreply@tm.openai.com>  (outlook.com 收件人)
                    #   - bounces+xxx@em7877.tm.open       (catch_all 域名收件人, SendGrid 中转)
                    # 用 python 层校验 (line 526) 涵盖两种, 不依赖 IMAP 复杂 query.
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
                    # 校验 From 字段, 必须是 OpenAI 域 (防伪造 / 系统邮件含 "OpenAI" 字样误判)
                    from_field = (msg.get("From") or "").lower()
                    if not any(d in from_field for d in (
                        # OpenAI 自家 + SendGrid 中转 (实测 from
                        # = "bounces+xxxxxxx-fdd4-isiner1988=lukyface.com@em7877.tm.open")
                        "openai.com", "auth.openai", "tm.openai", "chatgpt.com",
                        "tm.open",  # OpenAI SendGrid 中转子域 (em*.tm.open)
                    )):
                        logger.debug(f"[outlook-pool] skip non-OpenAI from={from_field[:80]}")
                        continue
                    # tm1.openai.com 是 OpenAI 当前坏掉的"影子"发码域: 跨所有账号都返
                    # 固定 OTP=493682, validate 100% 401 wrong_email_otp_code。每次
                    # sign-in OpenAI 同时发 tm.openai.com (真有效) + tm1.openai.com (坏)
                    # 两封, 按 IMAP id 倒序常先命中 tm1 的 493682 → 协议层登录全挂。
                    # 这里硬过滤 tm1.*, 只保留 tm.openai.com 域的真 OTP。
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
