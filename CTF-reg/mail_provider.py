"""Email service (CF Email Routing path).

Historically this module used IMAP to pull QQ mailbox to receive OTP (5s polling + forwarding link 30–90s latency).
Now completely switched to Cloudflare Email Worker → KV path:

    Sender → CF MX (catch-all) → otp-relay Worker → KV
                                                       ↓
                                            cf_kv_otp_provider reads

OTP extraction is done on the Worker side (see scripts/otp_email_worker.js),
this module only has two responsibilities:
  1. Generate random recipient address using catch-all domain (`create_mailbox`)
  2. Delegate to `CloudflareKVOtpProvider` to block-wait for OTP (`wait_for_otp`)

KV credential read order: environment variables `CF_API_TOKEN/CF_ACCOUNT_ID/CF_OTP_KV_NAMESPACE_ID`
→ SQLite runtime_meta[secrets] cloudflare section. See cf_kv_otp_provider.py for details."""
from __future__ import annotations

import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)


# —— Human-like email prefix generation ——
# Keep same common English/American name pool as browser_register._gen_name; OpenAI anti-fraud system scores
# "random string prefix" lower, first/last combination more closely matches real new user distribution.
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
    """Generate human-like email prefix, e.g. emma.davis, jsmith92, liam_wilson03.

    Sampling modes (weighted):
      - first.last                       (common professional email)
      - firstlast                        (no separator)
      - first_last                       (underscore)
      - first.last + 1-2 digits
      - firstlast + 2-4 digits (including year)
      - first initial + last + number (jsmith92)
      - first + last initial + number (emmas01)
      - first + birth year (1985-2003)

    All results contain only [a-z0-9._], length 5-22, compliant with RFC + local part requirements of most mail services."""
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
        # Biased toward 4-digit year style (more human-like)
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

    # Fallback length (rare cases like rodriguez+full year reach 22)
    if len(local) > 22:
        local = local[:22]
    return local


class MailProvider:
    """Generate catch-all subdomain random email + delegate CF KV provider to get OTP.

    `last_persona` exposes the complete persona produced by the most recent `create_mailbox()`
    (email / first / last / password) for reuse by `browser_register`,
    ensuring "email first-name matches registration display name" — OpenAI anti-fraud system
    penalizes mismatch between the two."""

    def __init__(self, catch_all_domain: str = ""):
        self.catch_all_domain = catch_all_domain
        self._reuse_email: Optional[str] = None  # Compatible with register-only resume
        # Algorithmic persona generator (syllabic synthesis method, see persona.py)
        from persona import PersonaGenerator, Persona
        self._persona_gen = PersonaGenerator(catch_all_domain)
        self.last_persona: Optional[Persona] = None

    @staticmethod
    def _random_name() -> str:
        # Keep old API compatibility; new flow uses persona generator
        return _humanlike_local_part()

    def create_mailbox(self) -> str:
        """Generate random@catch_all email address (can also reuse _reuse_email).

        Simultaneously cache the algorithmically generated complete persona to `self.last_persona`,
        `browser_register` reads name / password with same origin as email from this field."""
        if self._reuse_email:
            addr = self._reuse_email
            self._reuse_email = None
            logger.info(f"复用邮箱: {addr}")
            self.last_persona = None  # Resume path cannot back-derive first/last
            return addr
        if not self.catch_all_domain:
            raise RuntimeError(
                "MailProvider.create_mailbox: catch_all_domain 未配置；"
                "CF Email Worker 路径需要 catch-all 子域（在 zone 内）"
            )
        persona = self._persona_gen.next()
        self.last_persona = persona
        logger.info(
            f"邮箱已创建: {persona.email} | persona={persona.first} {persona.last} "
            f"(路径: CF Email Worker → KV)"
        )
        return persona.email

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """Block-wait for OTP. Go directly to CF KV, no IMAP fallback anymore.

        Throw TimeoutError or RuntimeError on failure. Original IMAP path has been deleted —
        QQ mailbox / auth_code and all such parameters are deprecated."""
        from cf_kv_otp_provider import CloudflareKVOtpProvider

        logger.info(
            f"[mail] 走 CF KV 取 OTP -> {email_addr} (timeout={timeout}s)"
        )
        provider = CloudflareKVOtpProvider.from_env_or_secrets()
        return provider.wait_for_otp(
            email_addr, timeout=timeout, issued_after=issued_after
        )
