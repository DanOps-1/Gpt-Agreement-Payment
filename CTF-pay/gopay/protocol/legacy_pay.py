#!/usr/bin/env python3
"""GoPay 协议支付客户端。

目标：
  1. 把 `gopay_sign.py` 的 X-E1 / X-E2 封装成可重用的 HTTP 客户端；
  2. 同时兼容两条 pin-token 路径：
       - 旧路径 `/api/v1/users/pin/tokens/nb`：challenge_id + client_id + 明文 PIN
       - 新/协议路径 `/api/v1/users/pin/tokens`：RSA 加密 PIN → pin_token
  3. 提供一个可从 deeplink 直接构造 charge 请求的高层 API，方便后续接入
     `qris.py` / `gopay.py`。

说明：
  - 这里故意把“未知但可配置”的字段留成模板，避免在没有 live sample 的情况下
    把 payload 写死成错的。
  - 默认不会打印敏感值；CLI 只输出去敏后的摘要。
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import requests

_HERE = Path(__file__).resolve().parent
_CTF_PAY = _HERE.parent.parent  # CTF-pay/ 加到 sys.path 让 `import gopay` 解析
if str(_CTF_PAY) not in sys.path:
    sys.path.insert(0, str(_CTF_PAY))

from gopay.sign.v1 import SignContext, build_headers  # noqa: E402

# v2 signer (2026-05-14 verified) — used when cfg.sign_version == 'v2'
try:
    from gopay.sign.v2 import (
        load_template as _v2_load_template,
        build_sign_headers as _v2_build_sign_headers,
    )
    _V2_AVAILABLE = True
except ImportError:
    _V2_AVAILABLE = False

try:
    from curl_cffi.requests import Session as _CurlCffiSession  # type: ignore
except ImportError:
    _CurlCffiSession = None  # type: ignore


DEFAULT_TOKEN_ENDPOINT = "/api/v1/users/pin/tokens"
DEFAULT_TOKEN_ENDPOINT_NB = "/api/v1/users/pin/tokens/nb"
DEFAULT_CHARGE_ENDPOINTS = (
    "/v1/payment/charge",
    "/v1/transactions/{tref}/pay",
)
DEFAULT_TIMEOUT = 30.0


class GoPayProtocolError(RuntimeError):
    """协议支付层错误。"""


def _new_session(impersonate: str = "chrome136") -> Any:
    """创建带 Chrome TLS 指纹的 session；没有 curl_cffi 时回退 requests。"""
    if _CurlCffiSession is not None:
        return _CurlCffiSession(impersonate=impersonate)
    return requests.Session()


def _read_text(path: str) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8").strip()


def _safe_join(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def _render_template(value: Any, variables: dict[str, Any]) -> Any:
    """把模板值展开成最终 payload。

    规则：
      - dict/list 递归展开；
      - 单独的 `{var}` 返回原始类型（int/bool/str）；
      - 其它字符串用 str.format_map 展开，缺失变量默认空串。
    """
    if isinstance(value, dict):
        return {k: _render_template(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template(v, variables) for v in value]
    if not isinstance(value, str):
        return value

    text = value.strip()
    m = re.fullmatch(r"\{([A-Za-z0-9_]+)\}", text)
    if m:
        return variables.get(m.group(1), "")

    class _SafeDict(dict):
        def __missing__(self, key):
            return ""

    return value.format_map(_SafeDict({k: "" if v is None else str(v) for k, v in variables.items()}))


def _json_body(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _load_public_key_pem(cfg: dict[str, Any]) -> str:
    pem = str(cfg.get("pin_public_key_pem") or "").strip()
    if pem:
        return pem
    path = str(cfg.get("pin_public_key_path") or "").strip()
    if path:
        return _read_text(path)
    env_pem = os.getenv("GP_PIN_PUBLIC_KEY_PEM", "").strip()
    if env_pem:
        return env_pem
    env_path = os.getenv("GP_PIN_PUBLIC_KEY_PATH", "").strip()
    if env_path:
        return _read_text(env_path)
    return ""


def _encrypt_pin(pin: str, pem: str, *, padding: str = "pkcs1v15", output: str = "base64") -> str:
    """RSA 加密 PIN。

    默认 PKCS#1 v1.5；如果 live 端点要求 OAEP，可通过配置切换。
    """
    if not pem:
        raise GoPayProtocolError("缺少 PIN RSA 公钥 (pin_public_key_pem/pin_public_key_path)")
    try:
        from Crypto.Cipher import PKCS1_OAEP, PKCS1_v1_5
        from Crypto.Hash import SHA1, SHA256
        from Crypto.PublicKey import RSA
    except ImportError as e:  # pragma: no cover - 依赖缺失时的显式失败
        raise GoPayProtocolError(f"需要 pycryptodome 才能做 RSA 加密: {e}") from e

    key = RSA.import_key(pem)
    raw = pin.encode("utf-8")
    mode = str(padding or "pkcs1v15").strip().lower()
    if mode in ("oaep", "oaep-sha1"):
        cipher = PKCS1_OAEP.new(key, hashAlgo=SHA1)
        encrypted = cipher.encrypt(raw)
    elif mode in ("oaep-sha256", "oaep256"):
        cipher = PKCS1_OAEP.new(key, hashAlgo=SHA256)
        encrypted = cipher.encrypt(raw)
    else:
        cipher = PKCS1_v1_5.new(key)
        encrypted = cipher.encrypt(raw)

    out = str(output or "base64").strip().lower()
    if out == "hex":
        return encrypted.hex()
    return base64.b64encode(encrypted).decode("ascii")


def parse_deeplink(deeplink: str) -> dict[str, Any]:
    """解析 `gopay.co.id/app/merchanttransfer?...` deeplink。"""
    parsed = urllib.parse.urlsplit(deeplink)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    def first(name: str, default: str = "") -> str:
        vals = qs.get(name) or []
        if not vals:
            return default
        return vals[0]

    amount_raw = first("amount")
    amount: Optional[int]
    try:
        amount = int(amount_raw) if amount_raw else None
    except ValueError:
        amount = None

    return {
        "scheme": parsed.scheme,
        "host": parsed.netloc,
        "path": parsed.path,
        "query": parsed.query,
        "tref": first("tref"),
        "amount": amount,
        "amount_raw": amount_raw,
        "activity": first("activity"),
        "callback_url": first("callback_url"),
        "app": first("app"),
        "service_id": first("service_id"),
        "service_type": first("service_type"),
        "raw": deeplink,
    }


@dataclass
class GoPayProtocolConfig:
    """协议支付的最小配置。"""

    base_url: str = "https://customer.gopayapi.com"
    api_base_url: str = "https://api.gojekapi.com"
    auth_secret: str = ""
    device_id: str = ""
    sso_access_token: str = ""
    access_token: str = ""
    display_encoder_key: str = ""
    display_aes_key_hex: str = ""
    display_encoder_id: str = ""
    hmac_signing_key_hex: str = ""
    x_e1_mode: str = "sample"
    x_e1_include_iv: bool = True
    x_e1_repeat: int = 3
    app_id: str = "com.gojek.gopay"
    app_version: str = "2.8.0"
    client_version: str = "2.5.0.0"
    platform: str = "Android"
    os_version: str = "12"
    channel: str = "PlayStore"
    sign_type: str = "MD5"
    sign_version: str = "1"
    charset: str = "UTF-8"
    content_type: str = "application/json"
    user_agent: str = ""
    pin_public_key_pem: str = ""
    pin_public_key_path: str = ""
    pin_rsa_padding: str = "pkcs1v15"
    pin_rsa_output: str = "base64"
    pin_token_endpoint: str = DEFAULT_TOKEN_ENDPOINT
    pin_token_endpoint_nb: str = DEFAULT_TOKEN_ENDPOINT_NB
    pin_token_mode: str = "auto"  # auto | direct | nb
    pin_token_body: Any = None
    charge_endpoints: list[str] = field(default_factory=lambda: list(DEFAULT_CHARGE_ENDPOINTS))
    charge_body: Any = None
    profile_endpoint: str = "/v1/users/profile"
    balances_endpoint: str = "/v1/payment-options/balances"
    kyc_endpoint: str = "/v1/users/kyc/status"
    app_links_endpoint: str = "/v1/app-links/{linkKey}"
    timeout_s: float = DEFAULT_TIMEOUT
    fallback_to_nb: bool = True
    fallback_to_unsigned: bool = True

    @classmethod
    def from_mapping(cls, raw: Optional[dict[str, Any]]) -> "GoPayProtocolConfig":
        raw = dict(raw or {})
        # 兼容 `gopay.protocol` 和顶层 `gopay_protocol`
        if "gopay_protocol" in raw and isinstance(raw.get("gopay_protocol"), dict):
            merged = dict(raw.get("gopay_protocol") or {})
        else:
            merged = dict(raw)
            if isinstance(raw.get("protocol"), dict):
                merged.update(raw["protocol"])

        cfg = cls()
        for field_name in cfg.__dataclass_fields__:  # type: ignore[attr-defined]
            if field_name in merged and merged[field_name] is not None:
                setattr(cfg, field_name, merged[field_name])

        # alias
        if not cfg.access_token:
            cfg.access_token = str(merged.get("access_token") or merged.get("id_sso_access_token") or "")
        if not cfg.sso_access_token:
            cfg.sso_access_token = str(
                merged.get("sso_access_token")
                or merged.get("id_sso_access_token")
                or cfg.access_token
            )
        if not cfg.user_agent:
            cfg.user_agent = (
                merged.get("user_agent")
                or f"GoPay/{cfg.app_version} (Android {cfg.os_version})"
            )
        if not cfg.display_aes_key_hex and merged.get("display_aes_key"):
            cfg.display_aes_key_hex = str(merged.get("display_aes_key"))
        if not cfg.display_encoder_key and merged.get("display_encoder_key"):
            cfg.display_encoder_key = str(merged.get("display_encoder_key"))
        if not cfg.display_encoder_id and merged.get("display_encoder_id"):
            cfg.display_encoder_id = str(merged.get("display_encoder_id"))
        if not cfg.hmac_signing_key_hex and merged.get("hmac_signing_key"):
            cfg.hmac_signing_key_hex = str(merged.get("hmac_signing_key"))
        if not cfg.pin_public_key_pem and merged.get("pin_public_key"):
            cfg.pin_public_key_pem = str(merged.get("pin_public_key"))
        if not cfg.pin_public_key_path and merged.get("pin_public_key_file"):
            cfg.pin_public_key_path = str(merged.get("pin_public_key_file"))

        # 别名：`baseUrl`, `authSecret`, `deviceId`, `ID_SSO_Access_Token`
        cfg.base_url = str(merged.get("base_url") or merged.get("baseUrl") or cfg.base_url)
        cfg.api_base_url = str(merged.get("api_base_url") or merged.get("apiBaseUrl") or cfg.api_base_url)
        cfg.auth_secret = str(merged.get("auth_secret") or merged.get("authSecret") or cfg.auth_secret)
        cfg.device_id = str(merged.get("device_id") or merged.get("deviceId") or cfg.device_id)
        if not cfg.sso_access_token:
            cfg.sso_access_token = str(merged.get("ID_SSO_Access_Token") or "")
        return cfg

    @classmethod
    def from_env(cls) -> "GoPayProtocolConfig":
        """从环境变量加载最小配置。"""
        merged: dict[str, Any] = {
            "base_url": os.getenv("GP_BASE_URL", "").strip(),
            "api_base_url": os.getenv("GP_API_BASE_URL", "").strip(),
            "auth_secret": os.getenv("GP_AUTH_SECRET", "").strip(),
            "device_id": os.getenv("GP_DEVICE_ID", "").strip(),
            "sso_access_token": (
                os.getenv("GP_SSO_ACCESS_TOKEN", "")
                or os.getenv("GP_ID_SSO_ACCESS_TOKEN", "")
                or os.getenv("ID_SSO_ACCESS_TOKEN", "")
            ).strip(),
            "access_token": os.getenv("GP_ACCESS_TOKEN", "").strip(),
            "display_encoder_key": os.getenv("GP_DISPLAY_ENCODER_KEY", "").strip(),
            "display_aes_key_hex": os.getenv("GP_DISPLAY_AES_KEY_HEX", "").strip(),
            "display_encoder_id": os.getenv("GP_DISPLAY_ENCODER_ID", "").strip(),
            "hmac_signing_key_hex": os.getenv("GP_HMAC_SIGNING_KEY_HEX", "").strip(),
            "x_e1_mode": os.getenv("GP_X_E1_MODE", "sample").strip() or "sample",
            "x_e1_include_iv": os.getenv("GP_X_E1_INCLUDE_IV", "1").strip().lower() not in ("0", "false", "no"),
            "x_e1_repeat": int(os.getenv("GP_X_E1_REPEAT", "3") or "3"),
            "pin_public_key_pem": os.getenv("GP_PIN_PUBLIC_KEY_PEM", "").strip(),
            "pin_public_key_path": os.getenv("GP_PIN_PUBLIC_KEY_PATH", "").strip(),
        }
        return cls.from_mapping(merged)

    def sign_context(self) -> SignContext:
        if not (self.display_encoder_key and self.display_aes_key_hex and self.display_encoder_id and self.hmac_signing_key_hex):
            raise GoPayProtocolError(
                "协议签名缺少 runtime keys：display_encoder_key / display_aes_key_hex / "
                "display_encoder_id / hmac_signing_key_hex"
            )
        return SignContext(
            display_encoder_key=self.display_encoder_key,
            display_aes_key=bytes.fromhex(self.display_aes_key_hex),
            display_encoder_id=self.display_encoder_id,
            hmac_signing_key=bytes.fromhex(self.hmac_signing_key_hex),
            x_e1_mode=self.x_e1_mode,
            x_e1_include_iv=bool(self.x_e1_include_iv),
            x_e1_repeat=max(1, int(self.x_e1_repeat or 3)),
            app_id=self.app_id,
            app_version=self.app_version,
            client_version=self.client_version,
            platform=self.platform,
            os_version=self.os_version,
            channel=self.channel,
            sign_type=self.sign_type,
            sign_version=self.sign_version,
            charset=self.charset,
            content_type=self.content_type,
        )


class GoPayProtocolClient:
    """GoPay 协议支付客户端。"""

    def __init__(
        self,
        cfg: GoPayProtocolConfig | dict[str, Any],
        *,
        session: Any | None = None,
        log: Callable[[str], None] = print,
    ):
        self.cfg = cfg if isinstance(cfg, GoPayProtocolConfig) else GoPayProtocolConfig.from_mapping(cfg)
        self.log = log
        self.session = session or _new_session()
        try:
            self.session.headers.update({
                "User-Agent": self.cfg.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            })
        except Exception:
            pass
        if self.cfg.auth_secret and self.cfg.device_id:
            try:
                self.session.headers.setdefault("X-DeviceToken", self.cfg.device_id)
            except Exception:
                pass
        self._sign_ctx: Optional[SignContext] = None
        self._pin_public_key_pem: str = _load_public_key_pem({
            "pin_public_key_pem": self.cfg.pin_public_key_pem,
            "pin_public_key_path": self.cfg.pin_public_key_path,
        })

    @classmethod
    def from_mapping(
        cls,
        raw: Optional[dict[str, Any]],
        *,
        session: Any | None = None,
        log: Callable[[str], None] = print,
    ) -> "GoPayProtocolClient":
        return cls(GoPayProtocolConfig.from_mapping(raw or {}), session=session, log=log)

    @classmethod
    def from_env(
        cls,
        *,
        session: Any | None = None,
        log: Callable[[str], None] = print,
    ) -> "GoPayProtocolClient":
        return cls(GoPayProtocolConfig.from_env(), session=session, log=log)

    # ───────────────────────────── helpers ─────────────────────────────

    def _sign_headers(self, body: str, *, url: str | None = None, method: str = 'POST') -> dict[str, str]:
        # v2 path: when sign_version='v2' is configured, use the verified
        # HMAC-SHA256 envelope algorithm reverse-engineered from libbatteryOpt.
        sign_version = getattr(self.cfg, 'sign_version', None) or \
                       (self.cfg.extra or {}).get('sign_version') if hasattr(self.cfg, 'extra') else None
        if sign_version == 'v2':
            if not _V2_AVAILABLE:
                raise GoPayProtocolError("sign_version=v2 requested but gopay_sign_v2 not importable")
            extras = self.cfg.extra or {} if hasattr(self.cfg, 'extra') else {}
            template_path = extras.get('signed_msg_template') or '/tmp/big_msg_1867.bin'
            K_str = extras.get('display_encoder_key') or self.cfg.display_encoder_key
            if not K_str:
                raise GoPayProtocolError("v2 sign requires display_encoder_key (63B K)")
            tmpl = _v2_load_template(template_path)
            return _v2_build_sign_headers(
                tmpl,
                K_str.encode() if isinstance(K_str, str) else K_str,
                url=url or extras.get('default_url') or '',
                method=method,
            )

        # v1 (legacy) path: hash body via IPA-style algorithm
        ctx = self._sign_ctx
        if ctx is None:
            ctx = self.cfg.sign_context()
            self._sign_ctx = ctx
        auth_token = self.cfg.access_token or self.cfg.sso_access_token
        if not auth_token:
            raise GoPayProtocolError("缺少 sso_access_token / access_token，无法构造 X-E1/X-E2")
        if not self.cfg.auth_secret:
            raise GoPayProtocolError("缺少 authSecret / auth_secret")
        if not self.cfg.device_id:
            raise GoPayProtocolError("缺少 deviceId / device_id")
        return build_headers(
            ctx,
            body,
            access_token=auth_token,
            auth_secret=self.cfg.auth_secret,
            device_id=self.cfg.device_id,
            sso_access_token=auth_token,
        )

    def _post_json(
        self,
        url: str,
        payload: Any,
        *,
        signed: bool = True,
        extra_headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> requests.Response:
        body = _json_body(payload)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if signed:
            # Strip scheme/host for signing (algorithm uses host+path form)
            sign_url = url.replace('https://', '').replace('http://', '')
            headers.update(self._sign_headers(body, url=sign_url, method='POST'))
        else:
            if self.cfg.sso_access_token or self.cfg.access_token:
                token = self.cfg.sso_access_token or self.cfg.access_token
                headers["Authorization"] = f"Bearer {token}"
                headers["X-Identity-Token"] = token
            if self.cfg.auth_secret:
                headers["X-Access-Key"] = self.cfg.auth_secret
            if self.cfg.device_id:
                headers["X-DeviceToken"] = self.cfg.device_id
        if extra_headers:
            headers.update(extra_headers)
        return self.session.post(
            url,
            data=body,
            headers=headers,
            timeout=timeout or self.cfg.timeout_s,
        )

    def _get_json(
        self,
        url: str,
        *,
        signed: bool = True,
        extra_headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> requests.Response:
        headers = {
            "Accept": "application/json",
        }
        if signed:
            sign_url = url.replace('https://', '').replace('http://', '')
            headers.update(self._sign_headers("", url=sign_url, method='GET'))
        else:
            if self.cfg.sso_access_token or self.cfg.access_token:
                token = self.cfg.sso_access_token or self.cfg.access_token
                headers["Authorization"] = f"Bearer {token}"
                headers["X-Identity-Token"] = token
            if self.cfg.auth_secret:
                headers["X-Access-Key"] = self.cfg.auth_secret
            if self.cfg.device_id:
                headers["X-DeviceToken"] = self.cfg.device_id
        if extra_headers:
            headers.update(extra_headers)
        return self.session.get(
            url,
            headers=headers,
            params=params,
            timeout=timeout or self.cfg.timeout_s,
        )

    @staticmethod
    def _extract_token(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        candidates = [
            data.get("pin_token"),
            data.get("token"),
            data.get("access_token"),
            (data.get("data") or {}).get("pin_token") if isinstance(data.get("data"), dict) else "",
            (data.get("data") or {}).get("token") if isinstance(data.get("data"), dict) else "",
            (data.get("data") or {}).get("access_token") if isinstance(data.get("data"), dict) else "",
        ]
        for c in candidates:
            if c:
                return str(c)
        return ""

    def _default_pin_payload(
        self,
        *,
        pin: str,
        challenge_id: str = "",
        client_id: str = "",
        encrypted_pin: str = "",
    ) -> Any:
        if self.cfg.pin_token_body is not None:
            vars_ = {
                "pin": pin,
                "challenge_id": challenge_id,
                "client_id": client_id,
                "encrypted_pin": encrypted_pin,
            }
            return _render_template(copy.deepcopy(self.cfg.pin_token_body), vars_)

        mode = str(self.cfg.pin_token_mode or "auto").strip().lower()
        endpoint = str(self.cfg.pin_token_endpoint or "").strip()
        use_nb = mode == "nb" or endpoint.endswith("/nb")
        if mode == "direct":
            use_nb = False
        elif mode == "nb":
            use_nb = True
        if use_nb:
            return {
                "challenge_id": challenge_id,
                "client_id": client_id,
                "pin": pin,
            }
        return {
            "pin": encrypted_pin or pin,
        }

    def _default_charge_payload(
        self,
        deeplink_meta: dict[str, Any],
        *,
        pin_token: str,
    ) -> dict[str, Any]:
        base = {
            "tref": deeplink_meta.get("tref") or "",
            "reference_id": deeplink_meta.get("tref") or "",
            "amount": deeplink_meta.get("amount"),
            "callback_url": deeplink_meta.get("callback_url") or "",
            "activity": deeplink_meta.get("activity") or "",
            "app": deeplink_meta.get("app") or "",
            "service_id": deeplink_meta.get("service_id") or "",
            "service_type": deeplink_meta.get("service_type") or "",
            "pin_token": pin_token,
        }
        if self.cfg.charge_body is not None:
            return _render_template(copy.deepcopy(self.cfg.charge_body), {**deeplink_meta, **base})
        return base

    def _candidate_charge_urls(self, deeplink_meta: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        for item in self.cfg.charge_endpoints:
            if "{tref}" in item:
                urls.append(_safe_join(self.cfg.base_url, item.format(tref=deeplink_meta.get("tref") or "")))
            else:
                urls.append(_safe_join(self.cfg.base_url, item))
        return urls

    def _request_path(self, path: str) -> str:
        return _safe_join(self.cfg.base_url, path)

    # ───────────────────────── pin token ─────────────────────────────

    def tokenize_pin(
        self,
        pin: str,
        *,
        challenge_id: str = "",
        client_id: str = "",
        endpoint: Optional[str] = None,
    ) -> str:
        """拿 pin_token。

        - `pin_token_mode=nb` 或 endpoint 以 `/nb` 结尾时，继续走旧的
          `challenge_id + client_id + plaintext pin` 方式；
        - 其它情况默认走新协议路径：RSA 加密 PIN → `/api/v1/users/pin/tokens`。
        """
        endpoint = endpoint or self.cfg.pin_token_endpoint
        endpoint = str(endpoint or "").strip()
        mode = str(self.cfg.pin_token_mode or "auto").strip().lower()
        use_nb = mode == "nb" or endpoint.endswith("/nb")
        if mode == "direct":
            use_nb = False
        elif mode == "nb":
            use_nb = True
        if use_nb and not endpoint.endswith("/nb"):
            endpoint = self.cfg.pin_token_endpoint_nb or DEFAULT_TOKEN_ENDPOINT_NB
        elif not use_nb and endpoint.endswith("/nb"):
            endpoint = self.cfg.pin_token_endpoint or DEFAULT_TOKEN_ENDPOINT

        encrypted_pin = ""
        if not use_nb:
            encrypted_pin = _encrypt_pin(
                pin,
                self._pin_public_key_pem,
                padding=self.cfg.pin_rsa_padding,
                output=self.cfg.pin_rsa_output,
            )

        payload = self._default_pin_payload(
            pin=pin,
            challenge_id=challenge_id,
            client_id=client_id,
            encrypted_pin=encrypted_pin,
        )

        url = self._request_path(endpoint)
        r = self._post_json(url, payload, signed=True)
        if r.status_code in (400, 401, 403):
            raise GoPayProtocolError(f"pin token 请求失败 [{r.status_code}]: {r.text[:300]}")
        r.raise_for_status()
        try:
            data = r.json()
        except Exception as e:
            raise GoPayProtocolError(f"pin token 返回不是 JSON: {e}") from e
        token = self._extract_token(data)
        if not token:
            raise GoPayProtocolError(f"pin token 响应中未找到 token: {str(data)[:400]}")
        return token

    # ───────────────────────── charge ────────────────────────────────

    def charge_from_deeplink(
        self,
        deeplink: str,
        pin: str,
        *,
        challenge_id: str = "",
        client_id: str = "",
        charge_endpoints: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """从 merchanttransfer deeplink 直接完成“tokenize PIN → charge”。

        返回值会尽可能保留原始响应，便于在 live 环境里继续修 payload。
        """
        meta = parse_deeplink(deeplink)
        pin_token = self.tokenize_pin(pin, challenge_id=challenge_id, client_id=client_id)
        payload = self._default_charge_payload(meta, pin_token=pin_token)
        if charge_endpoints:
            endpoints = [
                _safe_join(self.cfg.base_url, u) if str(u).startswith("/") else str(u)
                for u in charge_endpoints
            ]
        else:
            endpoints = self._candidate_charge_urls(meta)

        last_error = ""
        for url in endpoints:
            r = self._post_json(url, payload, signed=True)
            if r.status_code in (200, 201):
                try:
                    data = r.json()
                except Exception:
                    data = {"_raw": r.text}
                return {
                    "state": "submitted",
                    "endpoint": url,
                    "pin_token": pin_token,
                    "response": data,
                }
            last_error = f"{r.status_code}: {r.text[:400]}"
        raise GoPayProtocolError(f"charge 失败: {last_error}")

    # ───────────────────────── query helpers ─────────────────────────

    def get_profile(self) -> dict[str, Any]:
        r = self._get_json(self._request_path(self.cfg.profile_endpoint), signed=True)
        r.raise_for_status()
        return r.json()

    def get_balances(self) -> dict[str, Any]:
        r = self._get_json(self._request_path(self.cfg.balances_endpoint), signed=True)
        r.raise_for_status()
        return r.json()

    def get_kyc_status(self) -> dict[str, Any]:
        r = self._get_json(self._request_path(self.cfg.kyc_endpoint), signed=True)
        r.raise_for_status()
        return r.json()

    def get_app_link(self, link_key: str) -> dict[str, Any]:
        path = self.cfg.app_links_endpoint.format(linkKey=link_key)
        r = self._get_json(_safe_join(self.cfg.api_base_url, path), signed=False)
        r.raise_for_status()
        return r.json()

    # ───────────────────────── self-test ─────────────────────────────

    def self_test(self) -> dict[str, Any]:
        """返回一份可检查的请求摘要，便于 CLI / 单元测试。"""
        dummy_body = {"operationType": "profile.get", "requestData": {}}
        ctx = self.cfg.sign_context()
        ts = int(time.time() * 1000)
        headers = build_headers(
            ctx,
            _json_body(dummy_body),
            access_token=self.cfg.access_token or self.cfg.sso_access_token,
            auth_secret=self.cfg.auth_secret or "auth",
            device_id=self.cfg.device_id or "device",
            sso_access_token=self.cfg.sso_access_token or self.cfg.access_token or "token",
            timestamp_ms=ts,
        )
        return {
            "sign_ctx": {
                "display_encoder_id": ctx.display_encoder_id,
                "display_aes_key_len": len(ctx.display_aes_key),
                "hmac_key_len": len(ctx.hmac_signing_key),
            },
            "headers": {
                k: (v[:12] + "..." if k.startswith("X-E") else v)
                for k, v in headers.items()
            },
        }


def _load_cfg(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _redact(text: str, head: int = 8, tail: int = 6) -> str:
    if len(text) <= head + tail:
        return "*" * len(text)
    return text[:head] + "…" + text[-tail:]


def main() -> None:
    parser = argparse.ArgumentParser(description="GoPay 协议支付客户端")
    parser.add_argument("--config", default="", help="包含 gopay.protocol 段的 JSON 配置")
    parser.add_argument("--deeplink", default="", help="gopay merchanttransfer deeplink")
    parser.add_argument("--pin", default="", help="6 位 PIN")
    parser.add_argument("--challenge-id", default="", help="旧 `/nb` 路径需要的 challenge_id")
    parser.add_argument("--client-id", default="", help="旧 `/nb` 路径需要的 client_id")
    parser.add_argument("--profile", action="store_true", help="仅拉取 profile")
    parser.add_argument("--balances", action="store_true", help="仅拉取 balances")
    parser.add_argument("--kyc", action="store_true", help="仅拉取 KYC status")
    parser.add_argument("--dry-run", action="store_true", help="只展示签名和 payload，不发请求")
    parser.add_argument("--json-result", action="store_true", help="输出 GOPAY_PROTOCOL_RESULT_JSON=...")
    args = parser.parse_args()

    cfg: dict[str, Any] = {}
    if args.config:
        cfg = _load_cfg(args.config)
    gopay_cfg = (cfg.get("gopay") or {}) if isinstance(cfg, dict) else {}
    protocol_cfg = gopay_cfg.get("protocol") or cfg.get("gopay_protocol") or {}
    client = GoPayProtocolClient.from_mapping(protocol_cfg)

    try:
        if args.profile:
            result = client.get_profile()
        elif args.balances:
            result = client.get_balances()
        elif args.kyc:
            result = client.get_kyc_status()
        elif args.dry_run:
            meta = parse_deeplink(args.deeplink) if args.deeplink else {}
            pin_token = "<not-generated>"
            if args.pin and (client.cfg.pin_token_mode == "nb" or str(client.cfg.pin_token_endpoint).endswith("/nb")):
                pin_token = "<legacy-nb>"
            key_status = {
                "has_auth_secret": bool(client.cfg.auth_secret),
                "has_device_id": bool(client.cfg.device_id),
                "has_sso_access_token": bool(client.cfg.sso_access_token or client.cfg.access_token),
                "has_display_encoder_key": bool(client.cfg.display_encoder_key),
                "has_display_aes_key_hex": bool(client.cfg.display_aes_key_hex),
                "has_display_encoder_id": bool(client.cfg.display_encoder_id),
                "has_hmac_signing_key_hex": bool(client.cfg.hmac_signing_key_hex),
                "has_pin_public_key": bool(client._pin_public_key_pem),
                "x_e1_mode": client.cfg.x_e1_mode,
                "x_e1_include_iv": bool(client.cfg.x_e1_include_iv),
                "x_e1_repeat": int(client.cfg.x_e1_repeat or 3),
            }
            result = {
                "mode": "dry-run",
                "deeplink": meta,
                "pin_preview": _redact(args.pin) if args.pin else "",
                "token_endpoint": client.cfg.pin_token_endpoint,
                "charge_endpoints": client.cfg.charge_endpoints,
                "pin_token_preview": pin_token,
                "key_status": key_status,
            }
        else:
            if not args.deeplink:
                raise GoPayProtocolError("--deeplink 不能为空")
            if not args.pin:
                raise GoPayProtocolError("--pin 不能为空")
            result = client.charge_from_deeplink(
                args.deeplink,
                args.pin,
                challenge_id=args.challenge_id,
                client_id=args.client_id,
            )
    except Exception as e:
        print(f"[gopay_protocol] FAILED: {e}", file=sys.stderr)
        if args.json_result:
            print(f"GOPAY_PROTOCOL_RESULT_JSON={json.dumps({'state': 'failed', 'error': str(e)})}")
        raise SystemExit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json_result:
        print(f"GOPAY_PROTOCOL_RESULT_JSON={json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
