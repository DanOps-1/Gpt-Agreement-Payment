"""OTP Extraction: Mining 4-8 digit verification codes from arbitrary text / JSON payload.

WhatsApp / SMS / Email / Meta Webhook multi-format payloads unified under one rule set:
First keyword-based window matching (otp / kode / verification / verification code ...),
then fallback to raw regex. JSON-type payloads traverse recursively,
and timestamped events are further filtered by issued_after to discard expired messages."""

from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any, Optional

DEFAULT_OTP_REGEX = r"(?<!\d)(\d{6})(?!\d)"


def _clean_otp_candidate(value: Any) -> str:
    code = re.sub(r"\D", "", str(value or ""))
    if 4 <= len(code) <= 8:
        return code
    return ""


def _extract_otp_from_text(text: str, code_regex: str = DEFAULT_OTP_REGEX) -> str:
    """Extract the most likely WhatsApp OTP from a text blob.

    Keyword-aware patterns run before the generic regex to avoid confusing
    amounts / phone numbers with OTPs in verbose WhatsApp messages.
    """
    if not text:
        return ""
    patterns = [
        r"(?:otp|one[-\s]*time|verification|verify|code|kode|verifikasi|gopay|whatsapp|验证码|驗證碼)[^\d]{0,80}(\d{4,8})(?!\d)",
        r"(?<!\d)(\d{4,8})(?!\d)[^\n\r]{0,80}(?:otp|one[-\s]*time|verification|verify|code|kode|verifikasi|gopay|验证码|驗證碼)",
        code_regex or DEFAULT_OTP_REGEX,
    ]
    for pattern in patterns:
        try:
            matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL))
        except re.error:
            continue
        for match in reversed(matches):
            groups = match.groups() or (match.group(0),)
            for group in reversed(groups):
                code = _clean_otp_candidate(group)
                if code:
                    return code
    return ""


def _json_path_get(obj: Any, path: str) -> Any:
    cur = obj
    for part in (path or "").split("."):
        part = part.strip()
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            return None
    return cur


def _parse_payload_timestamp(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:  # milliseconds
            ts /= 1000.0
        if 946684800 <= ts <= 4102444800:  # 2000-01-01 .. 2100-01-01
            return ts
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        return _parse_payload_timestamp(float(text))
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _dict_timestamp(obj: dict) -> Optional[float]:
    for key in ("ts", "timestamp", "time", "created_at", "received_at", "date"):
        if key in obj:
            ts = _parse_payload_timestamp(obj.get(key))
            if ts is not None:
                return ts
    return None


def _iter_json_message_candidates(obj: Any) -> Any:
    """Yield (text, timestamp) candidates from generic relay / Meta webhook JSON."""
    if isinstance(obj, dict):
        ts = _dict_timestamp(obj)
        pieces: list[str] = []
        for key in ("otp", "code", "body", "message", "text", "content", "caption", "raw"):
            if key not in obj:
                continue
            value = obj.get(key)
            if isinstance(value, dict):
                body = value.get("body") or value.get("text") or value.get("message")
                if body not in (None, ""):
                    pieces.append(str(body))
            elif isinstance(value, (str, int, float)):
                pieces.append(str(value))
        if pieces:
            yield " ".join(pieces), ts
        for value in obj.values():
            yield from _iter_json_message_candidates(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_json_message_candidates(item)
    elif isinstance(obj, str):
        yield obj, None


def _extract_otp_from_payload(
    payload: Any,
    *,
    code_regex: str = DEFAULT_OTP_REGEX,
    json_path: str = "",
    issued_after: float = 0.0,
) -> str:
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped[:1] in ("{", "["):
            try:
                payload = json.loads(stripped)
            except Exception:
                return _extract_otp_from_text(payload, code_regex=code_regex)
        else:
            return _extract_otp_from_text(payload, code_regex=code_regex)

    if json_path:
        target = _json_path_get(payload, json_path)
        if target is None:
            return ""
        if not isinstance(target, str):
            target = json.dumps(target, ensure_ascii=False)
        return _extract_otp_from_text(target, code_regex=code_regex)

    found = ""
    for text, ts in _iter_json_message_candidates(payload):
        if issued_after and ts is not None and ts < issued_after:
            continue
        code = _extract_otp_from_text(text, code_regex=code_regex)
        if code:
            found = code
    return found
