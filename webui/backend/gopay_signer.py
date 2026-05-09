from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Mapping


DEFAULT_HMAC_KEY = "4&G6DbV&j8QZs~{)(Ila_w_|v@aqJq]E-;*(J9PanZ8sm01kTi{X<iG``]d7P&L"
DEFAULT_X_E2 = "ED9A2B38749FBDE9ACA61D6A685B7"


def lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def bearer_token(authorization: str) -> str:
    prefix = "Bearer "
    return authorization[len(prefix):] if authorization.startswith(prefix) else authorization


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
    key: str = DEFAULT_HMAC_KEY,
    nonce_hex: str | None = None,
    timestamp_ms: int | None = None,
) -> str:
    timestamp_ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    nonce_hex = os.urandom(80).hex() if nonce_hex is None else nonce_hex.lower()
    if len(nonce_hex) != 160 or any(c not in "0123456789abcdef" for c in nonce_hex):
        raise ValueError("nonce must be exactly 160 lowercase hex chars")
    msg = canonical_message(
        headers,
        method=method,
        host=host,
        path=path,
        body=body,
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
    key: str = DEFAULT_HMAC_KEY,
) -> dict[str, str]:
    headers = {str(k): str(v) for k, v in baseline_headers.items()}
    for name in list(headers):
        if name.lower() in ("x-e1", "host", "content-length"):
            headers.pop(name, None)
    headers["host"] = host
    if "x-e2" not in lower_headers(headers):
        headers["x-e2"] = DEFAULT_X_E2
    headers["x-e1"] = sign_x_e1(headers, method=method, host=host, path=path, body=body, key=key)
    if body and "content-type" not in lower_headers(headers):
        headers["content-type"] = "application/json"
    return headers
