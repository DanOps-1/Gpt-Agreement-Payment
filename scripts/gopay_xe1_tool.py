from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


DEFAULT_HMAC_KEY = "4&G6DbV&j8QZs~{)(Ila_w_|v@aqJq]E-;*(J9PanZ8sm01kTi{X<iG``]d7P&L"
DEFAULT_X_E2 = "ED9A2B38749FBDE9ACA61D6A685B7"
APP_NONCE_ZERO_SEGMENT = "0" * 32


@dataclass(frozen=True)
class RawRequest:
    method: str
    target: str
    host: str
    path: str
    headers: dict[str, str]
    body: str


def _is_hex(value: str) -> bool:
    return all(c in "0123456789abcdef" for c in value)


def lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def bearer_token(authorization: str) -> str:
    prefix = "Bearer "
    return authorization[len(prefix):] if authorization.startswith(prefix) else authorization


def nonce_marker_from_x_e1(x_e1: str | None) -> str | None:
    if not x_e1:
        return None
    parts = str(x_e1).split(":")
    if len(parts) < 2:
        return None
    nonce_hex = parts[1].lower()
    if len(nonce_hex) != 160 or not _is_hex(nonce_hex):
        return None
    if nonce_hex[64:96] != APP_NONCE_ZERO_SEGMENT:
        return None
    marker_hex = nonce_hex[96:128]
    return marker_hex if len(marker_hex) == 32 and _is_hex(marker_hex) else None


def new_nonce_hex(marker_hex: str | None = None) -> str:
    if marker_hex:
        marker_hex = marker_hex.lower()
        if len(marker_hex) != 32 or not _is_hex(marker_hex):
            raise ValueError("nonce marker must be exactly 32 lowercase hex chars")
        return os.urandom(32).hex() + APP_NONCE_ZERO_SEGMENT + marker_hex + os.urandom(16).hex()
    return os.urandom(80).hex()


def canonical_message(
    headers: Mapping[str, str],
    *,
    method: str,
    host: str,
    path: str,
    body: str,
    body_text: str | None,
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

    body_md5_source = body if body_text is None else body_text
    body_md5 = hashlib.md5(body_md5_source.encode("utf-8")).hexdigest()
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
    body_text: str | None = None,
    key: str = DEFAULT_HMAC_KEY,
    nonce_hex: str | None = None,
    nonce_marker_hex: str | None = None,
    timestamp_ms: int | None = None,
) -> tuple[str, str]:
    timestamp_ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    nonce_hex = new_nonce_hex(nonce_marker_hex) if nonce_hex is None else nonce_hex.lower()
    if len(nonce_hex) != 160 or not _is_hex(nonce_hex):
        raise ValueError("nonce must be exactly 160 lowercase hex chars")

    message = canonical_message(
        headers,
        method=method,
        host=host,
        path=path,
        body=body if body_for_signature is None else body_for_signature,
        body_text=body_text,
        nonce_hex=nonce_hex,
        timestamp_ms=timestamp_ms,
    )
    digest = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{digest}:{nonce_hex}:D:{timestamp_ms}", message


def signed_headers(
    baseline_headers: Mapping[str, str],
    *,
    method: str,
    host: str,
    path: str,
    body: str,
    body_for_signature: str | None = None,
    body_text: str | None = None,
    key: str = DEFAULT_HMAC_KEY,
    nonce_hex: str | None = None,
    nonce_marker_hex: str | None = None,
    timestamp_ms: int | None = None,
) -> tuple[dict[str, str], str]:
    headers = {str(k): str(v) for k, v in baseline_headers.items()}
    captured_x_e1 = ""
    for name in list(headers):
        lname = name.lower()
        if lname == "x-e1":
            captured_x_e1 = str(headers.get(name) or "")
        if lname in ("x-e1", "host", "content-length"):
            headers.pop(name, None)

    headers["host"] = host
    if "x-e2" not in lower_headers(headers):
        headers["x-e2"] = DEFAULT_X_E2

    x_e1, message = sign_x_e1(
        headers,
        method=method,
        host=host,
        path=path,
        body=body,
        body_for_signature=body_for_signature,
        body_text=body_text,
        key=key,
        nonce_hex=nonce_hex,
        nonce_marker_hex=nonce_marker_hex or nonce_marker_from_x_e1(captured_x_e1),
        timestamp_ms=timestamp_ms,
    )
    headers["x-e1"] = x_e1
    if body and "content-type" not in lower_headers(headers):
        headers["content-type"] = "application/json"
    return headers, message


def parse_raw_request(raw: str) -> RawRequest:
    raw = raw.replace("\r\n", "\n")
    head, sep, body = raw.partition("\n\n")
    if not sep:
        head = raw
        body = ""
    lines = [line for line in head.split("\n") if line.strip()]
    if not lines:
        raise ValueError("empty request")

    request_line = lines[0].strip()
    match = re.match(r"^([A-Za-z]+)\s+(\S+)\s+HTTP/\d(?:\.\d)?$", request_line)
    if not match:
        raise ValueError("first line must look like: PATCH /v3/payments/... HTTP/1.1")
    method, target = match.group(1).upper(), match.group(2)

    headers: dict[str, str] = {}
    last_name = ""
    for line in lines[1:]:
        if line[:1] in (" ", "\t") and last_name:
            headers[last_name] += " " + line.strip()
            continue
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        last_name = name.strip()
        headers[last_name] = value.strip()

    parsed_target = urlsplit(target)
    lower = lower_headers(headers)
    host = parsed_target.netloc or lower.get("host", "")
    if not host:
        raise ValueError("missing Host header")
    path = parsed_target.path or "/"
    if parsed_target.query:
        path = f"{path}?{parsed_target.query}"
    return RawRequest(method=method, target=target, host=host, path=path, headers=headers, body=body)


def qris_signature_body(method: str, body: str) -> tuple[str | None, str | None, str]:
    if body and method.upper() in ("POST", "PUT", "PATCH"):
        return "", body, "empty+body-md5"
    return None, None, "body"


def read_raw_request(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    if sys.stdin.isatty():
        raise SystemExit("Pass --request-file or pipe a raw HTTP request into stdin.")
    return sys.stdin.read()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Debug GoPay x-e1 signing from a raw HTTP request.",
    )
    parser.add_argument("--request-file", "-f", help="Text file containing a raw HTTP request.")
    parser.add_argument("--key", default=DEFAULT_HMAC_KEY, help="HMAC key. Defaults to project signer key.")
    parser.add_argument("--nonce", help="Exact 160-char nonce hex for reproducible output.")
    parser.add_argument("--nonce-marker", help="32-char app nonce marker hex. Ignored when --nonce is used.")
    parser.add_argument("--timestamp-ms", type=int, help="Exact timestamp in milliseconds for reproducible output.")
    parser.add_argument(
        "--mode",
        choices=("qris", "plain"),
        default="qris",
        help="qris: POST/PUT/PATCH signs empty body but MD5s raw body; plain: signs raw body.",
    )
    parser.add_argument("--show-canonical", action="store_true", help="Print canonical message used for HMAC.")
    parser.add_argument("--json", action="store_true", help="Print result as JSON.")
    args = parser.parse_args()

    req = parse_raw_request(read_raw_request(args.request_file))
    if args.mode == "qris":
        body_for_signature, body_text, body_source = qris_signature_body(req.method, req.body)
    else:
        body_for_signature, body_text, body_source = None, None, "body"

    headers, canonical = signed_headers(
        req.headers,
        method=req.method,
        host=req.host,
        path=req.path,
        body=req.body,
        body_for_signature=body_for_signature,
        body_text=body_text,
        key=args.key,
        nonce_hex=args.nonce,
        nonce_marker_hex=args.nonce_marker,
        timestamp_ms=args.timestamp_ms,
    )

    if args.json:
        print(json.dumps({
            "method": req.method,
            "host": req.host,
            "path": req.path,
            "body_signature_source": body_source,
            "x_e1": headers["x-e1"],
            "x_e2": headers.get("x-e2"),
            "headers": headers,
            "canonical": canonical if args.show_canonical else None,
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"method: {req.method}")
    print(f"host: {req.host}")
    print(f"path: {req.path}")
    print(f"body_signature_source: {body_source}")
    print(f"x-e1: {headers['x-e1']}")
    print(f"x-e2: {headers.get('x-e2', '')}")
    print("\nsigned headers:")
    for name in sorted(headers, key=str.lower):
        print(f"{name}: {headers[name]}")
    if args.show_canonical:
        print("\ncanonical:")
        print(canonical)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
