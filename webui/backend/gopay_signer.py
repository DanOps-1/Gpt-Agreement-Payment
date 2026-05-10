from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Mapping


DEFAULT_HMAC_KEY = "4&G6DbV&j8QZs~{)(Ila_w_|v@aqJq]E-;*(J9PanZ8sm01kTi{X<iG``]d7P&L"
DEFAULT_X_E2 = "ED9A2B38749FBDE9ACA61D6A685B7"
_APP_NONCE_ZERO_SEGMENT = "0" * 32


def lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def bearer_token(authorization: str) -> str:
    prefix = "Bearer "
    return authorization[len(prefix):] if authorization.startswith(prefix) else authorization


def _is_hex(value: str) -> bool:
    return all(c in "0123456789abcdef" for c in value)


def nonce_marker_from_x_e1(x_e1: str | None) -> str | None:
    if not x_e1:
        return None
    parts = str(x_e1).split(":")
    if len(parts) < 2:
        return None
    nonce_hex = parts[1].lower()
    if len(nonce_hex) != 160 or not _is_hex(nonce_hex):
        return None
    if nonce_hex[64:96] != _APP_NONCE_ZERO_SEGMENT:
        return None
    marker_hex = nonce_hex[96:128]
    return marker_hex if len(marker_hex) == 32 and _is_hex(marker_hex) else None


def new_nonce_hex(marker_hex: str | None = None) -> str:
    if marker_hex:
        marker_hex = marker_hex.lower()
        if len(marker_hex) != 32 or not _is_hex(marker_hex):
            raise ValueError("nonce marker must be exactly 32 lowercase hex chars")
        return os.urandom(32).hex() + _APP_NONCE_ZERO_SEGMENT + marker_hex + os.urandom(16).hex()
    return os.urandom(80).hex()


def canonical_message(
    headers: Mapping[str, str],
    *,
    method: str,
    host: str,
    path: str,
    body: str,
    nonce_hex: str,
    timestamp_ms: int,
) -> str:
    h = lower_headers(headers)
    required = (
        "x-apptype",
        "x-phonemodel",
        "authorization",
        "x-uniqueid",
        "x-deviceos",
        "x-appversion",
        "x-m1",
        "x-appid",
        "x-phonemake",
        "x-platform",
    )
    missing = [name for name in required if not h.get(name)]
    if missing:
        raise ValueError("GoPay signed headers missing: " + ", ".join(missing))
    body_md5 = hashlib.md5(body.encode("utf-8")).hexdigest()
    return (
        f"{h['x-apptype']};"
        f"{h['x-phonemodel']}:{bearer_token(h['authorization'])};"
        f"{h['x-uniqueid']}:{body};"
        f"{body_md5}:{host}{path};"
        f"{method.upper()}:{timestamp_ms};"
        f"{h['x-deviceos']}:{h['x-appversion']};"
        f"{h['x-m1']}:{h['x-appid']};"
        f"{nonce_hex}:{h['x-phonemake']};"
        f"{h['x-platform']}"
    )


def sign_x_e1(
    headers: Mapping[str, str],
    *,
    method: str,
    host: str,
    path: str,
    body: str = "",
    body_for_signature: str | None = None,
    key: str = DEFAULT_HMAC_KEY,
    nonce_hex: str | None = None,
    nonce_marker_hex: str | None = None,
    timestamp_ms: int | None = None,
) -> str:
    timestamp_ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    nonce_hex = new_nonce_hex(nonce_marker_hex) if nonce_hex is None else nonce_hex.lower()
    if len(nonce_hex) != 160 or not _is_hex(nonce_hex):
        raise ValueError("nonce must be exactly 160 lowercase hex chars")
    msg = canonical_message(
        headers,
        method=method,
        host=host,
        path=path,
        body=body if body_for_signature is None else body_for_signature,
        nonce_hex=nonce_hex,
        timestamp_ms=timestamp_ms,
    )
    digest = hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{digest}:{nonce_hex}:D:{timestamp_ms}"


def signed_headers(
    baseline_headers: Mapping[str, str],
    *,
    method: str,
    host: str,
    path: str,
    body: str,
    body_for_signature: str | None = None,
    key: str = DEFAULT_HMAC_KEY,
    nonce_marker_hex: str | None = None,
) -> dict[str, str]:
    headers = {str(k): str(v) for k, v in baseline_headers.items()}
    captured_x_e1 = ""
    for name in list(headers):
        if name.lower() == "x-e1":
            captured_x_e1 = str(headers.get(name) or "")
        if name.lower() in ("x-e1", "host", "content-length"):
            headers.pop(name, None)
    headers["host"] = host
    if "x-e2" not in lower_headers(headers):
        headers["x-e2"] = DEFAULT_X_E2
    headers["x-e1"] = sign_x_e1(
        headers,
        method=method,
        host=host,
        path=path,
        body=body,
        body_for_signature=body_for_signature,
        key=key,
        nonce_marker_hex=nonce_marker_hex or nonce_marker_from_x_e1(captured_x_e1),
    )
    if body and "content-type" not in lower_headers(headers):
        headers["content-type"] = "application/json"
    return headers
