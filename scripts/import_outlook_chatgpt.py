#!/usr/bin/env python3
"""One-time script: use outlook.jp OTP reception account + current gost (JP IP) to register a ChatGPT account + write access_token/session_token to webui registered_accounts table, so webui pay-only --qris can directly reuse it.

OTP reception account 4-segment format (separated by ----):
    email----password----client_id----microsoft_refresh_token

Usage:
    python scripts/import_outlook_chatgpt.py 'CharlesXxx@outlook.jp----wnwuc...----9e5f94bc-...----M.C538_...'"""
from __future__ import annotations

import base64
import email as _email
import imaplib
import json
import logging
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "CTF-reg"))

from config import Config  # noqa: E402
from mail.provider import MailProvider  # noqa: E402  # Wave H: mail_provider.py → mail/provider.py
from drivers.protocol import AuthFlow  # noqa: E402  # Wave H: auth_flow.py → drivers/protocol.py
from drivers.browser import browser_register  # noqa: E402  # Wave H: browser_register.py → drivers/browser.py
from webui.backend.db import get_db  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


class OutlookMailProvider(MailProvider):
    """Reuse a fixed outlook mailbox + IMAP OAuth2 to pull OpenAI OTP."""

    def __init__(self, email: str, refresh_token: str, client_id: str):
        super().__init__(catch_all_domain=email.split("@", 1)[1])
        self.email = email
        self.refresh = refresh_token
        self.client_id = client_id
        self._reuse_email = email
        self._cached_access: Optional[str] = None
        self._cached_at: float = 0.0

    def _outlook_access_token(self) -> str:
        # outlook access_token valid for 1 day, cache to avoid refreshing every time
        if self._cached_access and time.time() - self._cached_at < 3000:
            return self._cached_access
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.refresh,
            "client_id": self.client_id,
            "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
        }).encode()
        req = urllib.request.Request(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data=body,
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        if not data.get("access_token"):
            raise RuntimeError(f"outlook refresh failed: {data}")
        self._cached_access = data["access_token"]
        self._cached_at = time.time()
        if data.get("refresh_token"):
            # outlook rolling refresh_token, update cache (don't rewrite disk)
            self.refresh = data["refresh_token"]
        return self._cached_access

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 180,
        issued_after: Optional[float] = None,
    ) -> str:
        # outlook occasionally delivers emails in 60-180s (especially OpenAI → outlook path);
        # auth_flow caller sets 60s which is too strict, need at least 240s
        timeout = max(int(timeout), 240)
        deadline = time.time() + timeout
        if issued_after is None:
            issued_after = time.time()
        # accept emails from the past 5 minutes as this round's OTP (IMAP time precision is poor, so provide redundancy)
        threshold = issued_after - 300
        last_seen_uids: set[str] = set()
        logger.info(f"[outlook] 等 OTP key={email_addr} timeout={timeout}s threshold={threshold:.0f}")

        while time.time() < deadline:
            try:
                access = self._outlook_access_token()
                M = imaplib.IMAP4_SSL("outlook.office365.com", 993)
                auth_string = f"user={self.email}\x01auth=Bearer {access}\x01\x01"
                typ, _ = M.authenticate("XOAUTH2", lambda x: auth_string.encode())
                if typ != "OK":
                    raise RuntimeError("imap XOAUTH2 失败")
                M.select("INBOX")
                # search for recent emails from OpenAI/ChatGPT
                typ, data = M.search(None, '(OR FROM "openai" FROM "chatgpt")')
                ids = data[0].split()
                # also fallback to search all latest (prevent sender mismatch)
                if not ids:
                    typ, data = M.search(None, "ALL")
                    ids = data[0].split()
                # check in reverse order the last 8 emails
                for mid in reversed(ids[-8:]):
                    if mid in last_seen_uids:
                        continue
                    last_seen_uids.add(mid)
                    typ, raw = M.fetch(mid, "(BODY.PEEK[])")
                    msg = _email.message_from_bytes(raw[0][1])
                    date_str = msg.get("Date") or ""
                    msg_ts = _parse_imap_date(date_str)
                    if msg_ts and msg_ts < threshold:
                        continue
                    # get text/plain or text/html from multipart, avoid SMTP headers + base64 encoded segments
                    text_body = ""
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct in ("text/plain", "text/html"):
                            try:
                                payload = part.get_payload(decode=True) or b""
                                text_body += payload.decode(part.get_content_charset() or "utf-8", errors="replace") + "\n"
                            except Exception:
                                continue
                    if not text_body:
                        continue
                    # OTP extraction: first semantic ("code is 123456" / chatgpt / openai context),
                    # then fallback \b\d{6}\b but exclude OpenAI brand hex colors (#353740 / #10A37F etc)
                    otp = _extract_otp_from_html(text_body)
                    if otp:
                        logger.info(f"[outlook] 收到 OTP={otp} from msg uid={mid.decode()} date={date_str[:30]}")
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
                logger.warning(f"[outlook] poll 异常: {e}")
            time.sleep(4)
        raise TimeoutError(f"outlook OTP timeout {timeout}s key={email_addr}")


def _is_hex_color_context(haystack: str, idx: int) -> bool:
    """Same as worker.js: 6-digit immediately followed by #xxxxxx or css color/background context → treat as hex"""
    if idx > 0 and haystack[idx - 1] == "#":
        return True
    before = haystack[max(0, idx - 30):idx]
    if re.search(r"(?:color|background|bgcolor|fill|stroke)\s*[:=]\s*[\"']?#?\s*$", before, re.IGNORECASE):
        return True
    return False


def _extract_otp_from_html(body: str) -> Optional[str]:
    """Extract 6-digit OTP from HTML/plain text body, exclude hex colors + SMTP header numbers."""
    semantic = [
        r"(?:code(?:\s*is)?|verification|one[-\s]*time|verify|kode|verifikasi|代码|验证码|驗證碼)[^\d<>]{0,80}(\d{6})\b",
        r"chatgpt[^\d<>]{0,80}(\d{6})",
        r"openai[^\d<>]{0,80}(\d{6})",
    ]
    for pat in semantic:
        for m in re.finditer(pat, body, re.IGNORECASE | re.DOTALL):
            cand = m.group(1)
            # find candidate position in original text
            cand_pos = m.start(1)
            if not _is_hex_color_context(body, cand_pos):
                return cand
    # fallback: pure text \b\d{6}\b exclude hex
    for m in re.finditer(r"\b(\d{6})\b", body):
        cand = m.group(1)
        if _is_hex_color_context(body, m.start(1)):
            continue
        return cand
    return None


def _parse_imap_date(s: str) -> Optional[float]:
    if not s:
        return None
    import email.utils as eu
    try:
        ts = eu.parsedate_to_datetime(s).timestamp()
        return ts
    except Exception:
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python import_outlook_chatgpt.py 'email----password----client_id----refresh_token'", file=sys.stderr)
        sys.exit(2)
    parts = sys.argv[1].split("----")
    if len(parts) != 4:
        print(f"4 段格式错: 拿到 {len(parts)} 段", file=sys.stderr)
        sys.exit(2)
    email, password, client_id, refresh = parts
    logger.info(f"账号: {email}  client_id={client_id[:8]}…  refresh_token len={len(refresh)}")

    # switch to JP IP (promo hit key)
    from pipeline import _read_card_cfg, _rotate_webshare_ip
    pay_cfg = _read_card_cfg(str(ROOT / "CTF-pay" / "config.paypal.json"))
    px = _rotate_webshare_ip(pay_cfg, force=True)
    logger.info(f"[ip] 当前出口: {px.get('proxy_address')} {px.get('country_code')}/{px.get('city_name')}")

    cardw = Config.from_file(str(ROOT / "CTF-reg" / "config.paypal-proxy.json"))
    mail = OutlookMailProvider(email, refresh, client_id)
    # use Camoufox real browser path (Turnstile / DataDome / anti-fraud treated as genuine user behavior
    # easier to pass OpenAI risk control than pure auth_flow protocol)
    use_browser = bool(int(__import__("os").environ.get("REG_VIA_BROWSER", "1")))
    if use_browser:
        logger.info("[browser_register] Camoufox 启动 (outlook 邮箱 + JP IP) ...")
        d = browser_register(cardw, mail)
    else:
        flow = AuthFlow(cardw)
        logger.info("[auth_flow] run_register 启动 (outlook 邮箱 + JP IP) ...")
        result = flow.run_register(mail)
        d = result.to_dict()
    logger.info(
        f"[register] 完成 email={d.get('email')} "
        f"access_token=len{len(d.get('access_token') or '')} "
        f"session_token=len{len(d.get('session_token') or '')}"
    )

    # write webui registered_accounts table
    db = get_db()
    if hasattr(db, "save_registered_account"):
        db.save_registered_account(d)
        logger.info("[db] save_registered_account 已写")
    else:
        # fallback: direct SQL insert
        con = db._conn()
        con.execute(
            "INSERT INTO registered_accounts (email, ts, password, session_token, access_token, "
            "device_id, csrf_token, id_token, refresh_token, cookie_header, created_at) "
            "VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))",
            (
                d.get("email"), d.get("password", password),
                d.get("session_token", ""), d.get("access_token", ""),
                d.get("device_id", ""), d.get("csrf_token", ""),
                d.get("id_token", ""), d.get("refresh_token", ""),
                d.get("cookie_header", ""),
            ),
        )
        con.commit()
        logger.info("[db] 直接 SQL insert 已写")
    print(f"\n=== DONE ===\nimport: {d.get('email')} 已塞入 webui inventory，去 webui Run 页选 QRIS + --pay-only 跑")


if __name__ == "__main__":
    main()
