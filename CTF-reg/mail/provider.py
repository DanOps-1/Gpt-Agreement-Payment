"""Email service (Outlook pool + CF Email Routing fallback).

Historically this module pulled QQ mailbox via IMAP to intercept OTP (5s polling + forwarding path 30–90s latency).
catch-all mailbox now switched to Cloudflare Email Worker → KV path:

    Sender → CF MX (catch-all) → otp-relay Worker → KV
                                                       ↓
                                            cf_kv_otp_provider read

Outlook pool accounts only use IMAP XOAUTH2 pure protocol to receive codes, Outlook Web scrape prohibited.
OTP extraction done on Worker side (see scripts/otp_email_worker.js),
this module responsible for:
  1. Select Outlook pool account or generate catch-all random mailbox address (`create_mailbox`)
  2. Block-wait for OTP via IMAP XOAUTH2 / CloudflareKVOtpProvider (`wait_for_otp`)

KV credential read order: environment variables `CF_API_TOKEN/CF_ACCOUNT_ID/CF_OTP_KV_NAMESPACE_ID`
→ SQLite runtime_meta[secrets] cloudflare section. See cf_kv_otp_provider.py for details."""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

logger = logging.getLogger(__name__)


# —— Realistic human email prefix generation ——
# Keep same English/American common name pool as browser_register._gen_name; OpenAI's anti-fraud system scores
# "random string prefix" lower, first/last combinations more closely match real new user distribution.
_FIRST_NAMES = [
    "james", "john", "emily", "sophia", "michael", "oliver", "emma",
    "william", "amelia", "lucas", "mia", "ethan", "noah", "ava", "liam",
    "isabella", "mason", "charlotte", "logan", "harper", "elijah", "evelyn",
    "benjamin", "abigail", "jacob", "ella", "alexander", "scarlett", "henry",
    "grace", "daniel", "chloe", "matthew", "lily", "samuel", "zoe",
    "david", "hannah", "joseph", "aria", "ryan", "nora",
]
_LAST_NAMES = [
    "smith", "johnson", "williams", "brown", "jones", "garcia",
    "miller", "davis", "rodriguez", "martinez", "wilson", "anderson",
    "taylor", "thomas", "moore", "jackson", "martin", "lee", "walker",
    "hall", "allen", "young", "king", "wright", "scott", "green",
    "baker", "adams", "nelson", "carter",
]


def _humanlike_local_part(rng: random.Random | None = None) -> str:
    """Generate realistic human-like email prefixes, e.g., emma.davis, jsmith92, liam_wilson03.

    Sampling modes (weighted):
      - first.last                       (common professional email)
      - firstlast                        (no separator)
      - first_last                       (underscore)
      - first.last + 1-2 digits
      - firstlast + 2-4 digits (with year)
      - first initial + last + digits (jsmith92)
      - first + last initial + digits (emmas01)
      - first + birth year (1985-2003)

    All results contain only [a-z0-9._], length 5-22, compliant with RFC + most email service local-part requirements."""
    r = rng or random
    first = r.choice(_FIRST_NAMES)
    last = r.choice(_LAST_NAMES)

    pattern = r.choices(
        population=[
            "first.last", "firstlast", "first_last",
            "first.last+num", "firstlast+num",
            "f.last+num", "first.l+num", "first+year",
        ],
        weights=[14, 10, 6, 18, 16, 14, 10, 12],
        k=1,
    )[0]

    if pattern == "first.last":
        local = f"{first}.{last}"
    elif pattern == "firstlast":
        local = f"{first}{last}"
    elif pattern == "first_last":
        local = f"{first}_{last}"
    elif pattern == "first.last+num":
        n = r.randint(1, 99)
        local = f"{first}.{last}{n:02d}"
    elif pattern == "firstlast+num":
        # Favor 4-digit year style (more human-like)
        if r.random() < 0.55:
            n = r.randint(1985, 2003)
            local = f"{first}{last}{n}"
        else:
            n = r.randint(1, 999)
            local = f"{first}{last}{n}"
    elif pattern == "f.last+num":
        n = r.randint(1, 99)
        local = f"{first[0]}{last}{n:02d}"
    elif pattern == "first.l+num":
        n = r.randint(1, 99)
        local = f"{first}{last[0]}{n:02d}"
    else:  # first+year
        n = r.randint(1985, 2003)
        local = f"{first}{n}"

    # Fallback length (rare long surnames like rodriguez+full year reach 22)
    if len(local) > 22:
        local = local[:22]
    return local


class MailProvider:
    """Generate catch-all subdomain random mailbox + delegate CF KV provider to fetch OTP.

    `last_persona` exposes the complete persona from last `create_mailbox()` call
    (mailbox / first / last / password), for `browser_register` reuse,
    ensuring "email first-name matches registration display name" — OpenAI's anti-fraud system
    penalizes mismatch."""

    def __init__(self, catch_all_domain: str = ""):
        self.catch_all_domain = catch_all_domain
        self._reuse_email: Optional[str] = None  # Backward compatible with register-only resume
        # Algorithmic persona generator (syllable synthesis, see persona.py)
        from persona import PersonaGenerator, Persona
        self._persona_gen = PersonaGenerator(catch_all_domain)
        self.last_persona: Optional[Persona] = None
        # Outlook pool claim saves these three segments, wait_for_otp uses IMAP OAuth2
        self._outlook_creds: Optional[dict] = None  # {email, refresh_token, client_id}

    @staticmethod
    def _random_name() -> str:
        # Maintain old API compatibility; new flow uses persona generator
        return _humanlike_local_part()

    def create_mailbox(self) -> str:
        """Pick one: Outlook pool / domain catch-all. Strictly mutually exclusive, no mixing no fallback.

        env passthrough (webui runner writes, CLI users use cfg.mail.source default):
          WEBUI_MAIL_SOURCE = "outlook" | "catch_all"
          WEBUI_OUTLOOK_EMAIL = "<email>" (only when source=outlook, empty = claim_next)

        failure behavior (design intent: let user immediately know which pool to refill):
          - source=outlook pool empty → raise error ("go to /outlook page to import mailboxes")
          - source=outlook specified email not in pool → raise error
          - source=catch_all no domain configured → raise error ("go to wizard to configure catch_all_domain")"""
        if self._reuse_email:
            addr = self._reuse_email
            self._reuse_email = None
            logger.info(f"复用邮箱: {addr}")
            self.last_persona = None
            return addr

        import os as _os
        # source priority: env > cfg.mail.source > default catch_all (historical CLI backward compat)
        _source = (_os.environ.get("WEBUI_MAIL_SOURCE", "") or "").strip().lower()
        if not _source:
            # backward compat old env: WEBUI_MAIL_MODE=outlook equivalent to source=outlook
            _legacy = (_os.environ.get("WEBUI_MAIL_MODE", "") or "").strip().lower()
            if _legacy == "outlook":
                _source = "outlook"
            else:
                # default catch_all: use if catch_all_domain configured, raise error if not
                _source = "catch_all"
        _outlook_email = (_os.environ.get("WEBUI_OUTLOOK_EMAIL", "") or "").strip()

        if _source == "outlook":
            return self._create_outlook_mailbox(_outlook_email)
        elif _source == "catch_all":
            return self._create_catchall_mailbox()
        else:
            raise RuntimeError(
                f"未知 mail source: {_source!r} (合法: outlook / catch_all)"
            )

    def _create_outlook_mailbox(self, target_email: str = "") -> str:
        """Claim one account from Outlook pool. Pool empty / specified account unavailable → raise error, no fallback."""
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _root = _Path(__file__).resolve().parents[2]  # repository root
            if str(_root) not in _sys.path:
                _sys.path.insert(0, str(_root))
            from webui.backend import outlook_pool
        except Exception as e:
            raise RuntimeError(f"outlook_pool 模块不可用: {e}") from e

        if target_email:
            claimed = outlook_pool.claim_email(target_email)
            if not claimed:
                raise RuntimeError(
                    f"outlook 模式指定账号 {target_email} 不可用 "
                    f"(已 in_use/used/dead 或不在池子里)"
                )
            origin = "指定"
        else:
            claimed = outlook_pool.claim_next()
            if not claimed:
                raise RuntimeError(
                    "outlook 模式池空 (无 available 账号). "
                    "去 /outlook 页粘贴 4 段格式批量导入 (Thunderbird client_id 才能自动 IMAP)."
                )
            origin = "claim_next"

        self._outlook_creds = claimed
        self.last_persona = None  # Outlook doesn't use algorithmic persona
        logger.info(
            f"邮箱已创建: {claimed['email']} | (源=outlook池 {origin}, IMAP OAuth2 收 OTP)"
        )
        return claimed["email"]

    def _create_catchall_mailbox(self) -> str:
        """Algorithmically generate persona@domain from catch_all_domain. domain not configured → raise error."""
        if not self.catch_all_domain:
            raise RuntimeError(
                "catch_all 模式但 catch_all_domain 没配. "
                "去 webui /wizard 邮箱段配置 catch_all_domain + CF Email Worker."
            )
        persona = self._persona_gen.next()
        self.last_persona = persona
        logger.info(
            f"邮箱已创建: {persona.email} | persona={persona.first} {persona.last} "
            f"(源=catch_all → CF Email Worker → KV 收 OTP)"
        )
        return persona.email

    def mark_outlook_dead(self, reason: str = "") -> None:
        """Called by auth_flow when detecting OpenAI 'account exists' branch: current Outlook already registered with OpenAI,
        mark dead to prevent future claim."""
        creds = self._outlook_creds
        if not creds:
            return
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _root = _Path(__file__).resolve().parents[2]  # Wave H bug: mail/provider.py adds one more level, parents[1] now points to CTF-reg/ not repository root
            if str(_root) not in _sys.path:
                _sys.path.insert(0, str(_root))
            from webui.backend import outlook_pool as _op
            _op.mark_dead(creds["email"], reason=reason or "OpenAI 已识别为已注册")
            logger.info(f"[mail] outlook 已 mark dead: {creds['email']}  reason={reason!r}")
        except Exception as e:
            logger.warning(f"[mail] mark_outlook_dead 失败 (不致命): {e}")

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """Block-wait for OTP.

        - Outlook pool accounts: only use IMAP XOAUTH2 pure protocol to receive codes, no longer call Outlook Web scrape.
        - catch-all mailbox: use CF Email Worker → KV pure protocol to read codes.

        Raise TimeoutError / RuntimeError on failure; never return None."""
        # Current mailbox from Outlook pool → IMAP OAuth2 fetch (pure protocol, no web fallback)
        creds = self._outlook_creds
        if creds and creds.get("email", "").lower() == (email_addr or "").lower():
            try:
                import sys as _sys
                from pathlib import Path as _Path
                _root = _Path(__file__).resolve().parents[2]  # repository root
                if str(_root) not in _sys.path:
                    _sys.path.insert(0, str(_root))
                from webui.backend import outlook_pool as _op
            except Exception as e:
                raise RuntimeError(f"outlook_pool 模块不可用: {e}")

            # protocol path Outlook pool expects results within 60-120s.
            # OpenAI typically delivers OTP in 5-30s; no email after 90s usually means OTP not delivered / account unavailable.
            timeout = max(int(timeout), 90)
            # strict threshold: only accept emails arriving after issued_after, avoid post-retry-resend
            # fetching server-side stale codes → verify 401 wrong_email_otp_code.
            strict_threshold = (issued_after - 5) if issued_after else (time.time() - 5)

            logger.info(
                f"[mail] outlook IMAP OAuth2 纯协议取 OTP -> {email_addr} "
                f"(timeout={timeout}s threshold>={int(strict_threshold)})"
            )
            try:
                otp = _op.fetch_otp_via_imap(
                    creds["email"], creds["refresh_token"], creds["client_id"],
                    timeout=timeout, threshold_ts=strict_threshold,
                )
            except Exception as e:
                reason = f"IMAP XOAUTH2 纯协议失败: {e}"
                try:
                    self.mark_outlook_dead(reason)
                except Exception:
                    pass
                self.outlook_exhausted = True
                raise TimeoutError(
                    f"outlook OTP timeout (IMAP-only pure protocol, no web fallback) "
                    f"for {email_addr}; {reason}"
                ) from e

            if not otp:
                reason = "IMAP XOAUTH2 纯协议未收到 OTP"
                try:
                    self.mark_outlook_dead(reason)
                except Exception:
                    pass
                self.outlook_exhausted = True
                raise TimeoutError(
                    f"outlook OTP timeout (IMAP-only pure protocol, no web fallback) "
                    f"for {email_addr}; {reason}"
                )

            # OpenAI actually sent OTP and received → mailbox recognized by OpenAI, mark used to prevent reuse.
            try:
                _op.mark_used(creds["email"], chatgpt_email=creds["email"])
            except Exception as e:
                logger.warning(f"[mail] outlook mark_used 失败（不致命）: {e}")
            return otp

        # fallback: catch-all mailbox uses CF KV
        from mail.cf_kv import CloudflareKVOtpProvider  # Wave H: cf_kv_otp_provider.py → mail/cf_kv.py
        logger.info(
            f"[mail] 走 CF KV 取 OTP -> {email_addr} (timeout={timeout}s)"
        )
        provider = CloudflareKVOtpProvider.from_env_or_secrets()
        return provider.wait_for_otp(
            email_addr, timeout=timeout, issued_after=issued_after
        )
