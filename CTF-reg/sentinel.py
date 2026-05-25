"""OpenAI Sentinel Token generation (pure Python solution).

Integrated from https://github.com/zc-zhangchen/any-auto-register
(platforms/chatgpt/sentinel_token.py, MIT License).

Legacy implementation see sentinel_v1_legacy.py: compute PoW locally via SHA3-512 first then send, but OpenAI server-side silent-drop (200 OK but no OTP issued). Key differences in the new implementation:
  1. First POST /sentinel/req sends `requirements_token` (without real PoW), only declares config schema.
  2. Server returns `{token, proofofwork: {required, seed, difficulty}}`, we run FNV-1a 32-bit PoW using **server-provided seed/difficulty**.
  3. Second PoW solved token is spliced into `{p, t:"", c: server_token, id, flow}` to be valid.
  4. SDK version string upgraded from legacy `prod-f501fe...` to current `https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js`.

Public API `get_sentinel_token(session, device_id, flow, user_agent)` remains unchanged, 4 call sites in auth_flow.py need no modification."""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
SENTINEL_REFERER = "https://sentinel.openai.com/backend-api/sentinel/frame.html"
SENTINEL_SDK_URL = "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_SEC_CH_UA = (
    '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'
)


class SentinelTokenGenerator:
    """Pure Python Sentinel Token generator.

    - Does not depend on Node / JS.
    - `t` field is fixed to empty string; upstream interface (`/sentinel/req`) return will determine acceptability."""

    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str | None = None, user_agent: str | None = None):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or DEFAULT_UA
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now
        nav_prop = random.choice([
            "vendorSub", "productSub", "vendor", "maxTouchPoints",
            "scheduling", "userActivation", "doNotTrack", "geolocation",
            "connection", "plugins", "mimeTypes", "pdfViewerEnabled",
            "webkitTemporaryStorage", "webkitPersistentStorage",
            "hardwareConcurrency", "cookieEnabled", "credentials",
            "mediaDevices", "permissions", "locks", "ink",
        ])
        return [
            "1920x1080",
            date_str,
            4294705152,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            f"{nav_prop}−undefined",
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time_origin,
        ]

    @staticmethod
    def _base64_encode(data) -> str:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _run_check(self, start_time, seed, difficulty, config, nonce):
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        encoded = self._base64_encode(config)
        digest = self._fnv1a_32(seed + encoded)
        if digest[: len(difficulty)] <= difficulty:
            return encoded + "~S"
        return None

    def generate_token(self, seed: str | None = None, difficulty: str | None = None) -> str:
        seed = seed or self.requirements_seed
        difficulty = difficulty or "0"
        start_time = time.time()
        config = self._get_config()
        for nonce in range(self.MAX_ATTEMPTS):
            value = self._run_check(start_time, seed, difficulty, config, nonce)
            if value:
                return "gAAAAAB" + value
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self) -> str:
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._base64_encode(config)


def fetch_sentinel_challenge(
    session,
    device_id: str,
    flow: str = "authorize_continue",
    user_agent: str | None = None,
    sec_ch_ua: str | None = None,
    impersonate: str | None = None,
    request_p: str | None = None,
) -> dict | None:
    """POST `/sentinel/req` and return response JSON. Return None on failure."""
    generator = SentinelTokenGenerator(device_id=device_id, user_agent=user_agent)
    req_body = {
        "p": str(request_p or "").strip() or generator.generate_requirements_token(),
        "id": device_id,
        "flow": flow,
    }
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": SENTINEL_REFERER,
        "Origin": "https://sentinel.openai.com",
        "User-Agent": user_agent or DEFAULT_UA,
        "sec-ch-ua": sec_ch_ua or DEFAULT_SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    kwargs = {"data": json.dumps(req_body), "headers": headers, "timeout": 20}
    if impersonate:
        kwargs["impersonate"] = impersonate
    try:
        response = session.post(SENTINEL_REQ_URL, **kwargs)
        if response.status_code == 200:
            return response.json()
        logger.warning(f"Sentinel /req 非 200: {response.status_code}")
    except Exception as exc:
        logger.warning(f"Sentinel /req 异常: {exc}")
        return None
    return None


def build_sentinel_token(
    session,
    device_id: str,
    flow: str = "authorize_continue",
    user_agent: str | None = None,
    sec_ch_ua: str | None = None,
    impersonate: str | None = None,
) -> str | None:
    """Complete Sentinel token: fetch challenge → solve PoW using server-given seed/difficulty → assemble.

    Return JSON string, None on failure."""
    challenge = fetch_sentinel_challenge(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
    )
    if not challenge:
        return None

    c_value = str(challenge.get("token") or "").strip()
    if not c_value:
        logger.warning("Sentinel 响应缺 token 字段")
        return None

    generator = SentinelTokenGenerator(device_id=device_id, user_agent=user_agent)
    pow_data = challenge.get("proofofwork") or {}
    if pow_data.get("required") and pow_data.get("seed"):
        p_value = generator.generate_token(
            seed=pow_data.get("seed"),
            difficulty=pow_data.get("difficulty", "0"),
        )
    else:
        p_value = generator.generate_requirements_token()

    payload = {
        "p": p_value,
        "t": "",
        "c": c_value,
        "id": device_id,
        "flow": flow,
    }
    return json.dumps(payload, separators=(",", ":"))


def get_sentinel_token(
    session,
    device_id: str,
    flow: str = "authorize_continue",
    user_agent: str = DEFAULT_UA,
) -> str:
    """Entry point reused by auth_flow.py; return JSON string, never throw exceptions.

    Priority:
      1. QuickJS path (`sentinel_quickjs.get_sentinel_token_via_quickjs`)
         runs OpenAI real sdk.js in Node subprocess, returns token with deep server-side validation.
         This is key for OTP email delivery.
      2. Pure Python path (`build_sentinel_token`) — only surface-level PoW, passes 200 OK but OpenAI server-side OTP dispatch will silent-drop. Fallback only when Node unavailable or sdk.js download fails.
      3. No-challenge mode — final fallback, ensures returning parseable string to avoid blocking.

    Disable QuickJS: `export OPENAI_SENTINEL_DISABLE_QUICKJS=1`."""
    if not os.environ.get("OPENAI_SENTINEL_DISABLE_QUICKJS"):
        try:
            from sentinel_quickjs import get_sentinel_token_via_quickjs
            qtoken = get_sentinel_token_via_quickjs(
                session,
                device_id=device_id,
                flow=flow,
                log=lambda m: logger.info(m),
            )
            if qtoken:
                logger.info(f"Sentinel Token 组装完成 (QuickJS 长度: {len(qtoken)})")
                return qtoken
            logger.warning("Sentinel QuickJS 失败，回退到纯 Python")
        except Exception as e:
            logger.warning(f"Sentinel QuickJS 加载/调用异常，回退到纯 Python: {e}")

    token = build_sentinel_token(
        session,
        device_id=device_id,
        flow=flow,
        user_agent=user_agent,
    )
    if token:
        logger.info(f"Sentinel Token 组装完成 (纯 Python 长度: {len(token)})")
        return token

    logger.warning("Sentinel /req 也失败，回退到无 challenge 模式")
    fallback_p = SentinelTokenGenerator(
        device_id=device_id, user_agent=user_agent
    ).generate_requirements_token()
    return json.dumps({
        "p": fallback_p,
        "t": "",
        "c": "",
        "id": device_id,
        "flow": flow,
    }, separators=(",", ":"))
