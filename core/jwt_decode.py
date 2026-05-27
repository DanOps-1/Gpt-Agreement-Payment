"""ChatGPT / OpenAI 用 RS256 JWT，本模块解 payload + 拆 email / plan / 过期判定。

不做签名校验（用 OpenAI 公钥的场景由具体业务方持有），只 base64 解中段。
"""

from __future__ import annotations

import base64
import json
import time


def _decode_jwt_payload(token: str) -> dict:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        payload = json.loads(decoded)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _extract_email_from_access_token(token: str) -> str:
    payload = _decode_jwt_payload(token)
    profile = payload.get("https://api.openai.com/profile", {})
    if isinstance(profile, dict):
        return profile.get("email", "") or ""
    return ""


def _extract_plan_type_from_access_token(token: str) -> str:
    payload = _decode_jwt_payload(token)
    auth_claim = payload.get("https://api.openai.com/auth", {})
    if isinstance(auth_claim, dict):
        return auth_claim.get("chatgpt_plan_type", "") or ""
    return ""


def _is_access_token_expired(token: str, skew_seconds: int = 120) -> bool:
    payload = _decode_jwt_payload(token)
    exp = payload.get("exp")
    try:
        exp_ts = int(exp)
    except Exception:
        return False
    return (time.time() + max(0, int(skew_seconds))) >= exp_ts
