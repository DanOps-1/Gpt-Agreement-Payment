"""LuckMail OpenAPI client for code-receiving orders.

LuckMail Mode A allocates one email per order, then exposes the verification
code through `/api/v1/openapi/order/:order_no/code`.  This module is deliberately
small and side-effect free outside those two operations so it can be reused by
registration, OAuth RT backfill, and WebUI preflight.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://mails.luckyous.com"
DEFAULT_EMAIL_TYPE = "ms_graph"


@dataclass
class LuckMailOrder:
    order_no: str
    email_address: str
    project: str = ""
    timeout_seconds: int = 0
    expired_at: Any = None
    created_at_ts: float = field(default_factory=time.time)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _load_luckmail_secrets() -> dict:
    """Read optional LuckMail credentials from SQLite runtime_meta[secrets]."""
    try:
        from webui.backend.db import get_db

        secrets = get_db().get_runtime_json("secrets", {})
        if isinstance(secrets, dict):
            luckmail = secrets.get("luckmail") or {}
            if isinstance(luckmail, dict):
                return luckmail
    except Exception as e:
        logger.debug("读 LuckMail SQLite secrets 失败: %s", e)
    return {}


def resolve_luckmail_config(raw: Any) -> dict:
    """Normalize LuckMail configuration from MailConfig/dict + env + secrets.

    Sensitive values can come from:
      1. env var: LUCKMAIL_API_KEY
      2. SQLite runtime_meta[secrets].luckmail
      3. mail.luckmail in the exported config (standalone fallback)
    """
    if hasattr(raw, "__dict__"):
        raw = raw.__dict__
    mail = raw if isinstance(raw, dict) else {}
    cfg = mail.get("luckmail") if isinstance(mail.get("luckmail"), dict) else {}
    cfg = dict(cfg or {})
    secrets = _load_luckmail_secrets()

    api_key = (
        _text(os.getenv("LUCKMAIL_API_KEY"))
        or _text(secrets.get("api_key"))
        or _text(cfg.get("api_key"))
    )
    out = {
        "api_key": api_key,
        "base_url": _text(cfg.get("base_url")) or DEFAULT_BASE_URL,
        "project_code": _text(cfg.get("project_code")),
        "email_type": _text(cfg.get("email_type")) or DEFAULT_EMAIL_TYPE,
        "domain": _text(cfg.get("domain")),
        "specified_email": _text(cfg.get("specified_email")),
        "poll_interval_s": cfg.get("poll_interval_s", cfg.get("poll_interval", 3)),
        "timeout_seconds": cfg.get("timeout_seconds", cfg.get("timeout", 180)),
    }
    try:
        out["poll_interval_s"] = max(1.0, float(out["poll_interval_s"]))
    except Exception:
        out["poll_interval_s"] = 3.0
    try:
        out["timeout_seconds"] = max(30, int(out["timeout_seconds"]))
    except Exception:
        out["timeout_seconds"] = 180
    return out


class LuckMailOpenAPIClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
    ):
        self.api_key = _text(api_key)
        self.base_url = (_text(base_url) or DEFAULT_BASE_URL).rstrip("/")
        if not self.api_key:
            raise RuntimeError(
                "LuckMail 缺 api_key（设置环境变量 LUCKMAIL_API_KEY，"
                "或写入 SQLite runtime_meta[secrets].luckmail）"
            )
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    @classmethod
    def from_mail_config(cls, mail_config: Any) -> "LuckMailOpenAPIClient":
        cfg = resolve_luckmail_config(mail_config)
        return cls(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
        )

    @staticmethod
    def _json_body(body: dict | None) -> str:
        if not body:
            return ""
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        method = method.upper()
        body_text = self._json_body(body)
        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
        }
        data = None
        if method in {"POST", "PUT", "PATCH"}:
            data = body_text.encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"LuckMail {method} {path} HTTP {e.code}: {raw}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"LuckMail {method} {path} 网络错误: {e}") from e

        try:
            parsed = json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"LuckMail {method} {path} 返回非 JSON: {raw[:300]}") from e

        code = parsed.get("code")
        if code not in (0, "0", None):
            message = parsed.get("message") or parsed.get("msg") or "unknown"
            raise RuntimeError(f"LuckMail {method} {path} code={code}: {message}")
        return parsed

    def get_balance(self) -> dict:
        return self.request("GET", "/api/v1/openapi/balance").get("data") or {}

    def create_order(
        self,
        *,
        project_code: str,
        email_type: str = DEFAULT_EMAIL_TYPE,
        domain: str = "",
        specified_email: str = "",
    ) -> LuckMailOrder:
        project_code = _text(project_code)
        if not project_code:
            raise RuntimeError("LuckMail 创建订单缺 project_code")
        body = {
            "project_code": project_code,
            "email_type": _text(email_type) or DEFAULT_EMAIL_TYPE,
        }
        if domain:
            body["domain"] = _text(domain)
        if specified_email:
            body["specified_email"] = _text(specified_email)

        data = self.request("POST", "/api/v1/openapi/order/create", body).get("data") or {}
        order_no = _text(data.get("order_no"))
        email_address = _text(data.get("email_address"))
        if not order_no or not email_address:
            raise RuntimeError(f"LuckMail 创建订单返回缺 order_no/email_address: {data}")
        return LuckMailOrder(
            order_no=order_no,
            email_address=email_address,
            project=_text(data.get("project")),
            timeout_seconds=int(data.get("timeout_seconds") or 0),
            expired_at=data.get("expired_at"),
        )

    def poll_code(
        self,
        order_no: str,
        *,
        timeout: int = 180,
        poll_interval_s: float = 3.0,
    ) -> str:
        order_no = urllib.parse.quote(_text(order_no), safe="")
        if not order_no:
            raise RuntimeError("LuckMail poll_code 缺 order_no")
        deadline = time.time() + max(30, int(timeout))
        interval = max(1.0, float(poll_interval_s))
        path = f"/api/v1/openapi/order/{order_no}/code"
        last_log = 0.0
        while time.time() < deadline:
            data = self.request("GET", path).get("data") or {}
            status = _text(data.get("status")).lower()
            code = _text(data.get("verification_code"))
            if not code:
                code = _text(data.get("code"))
            if not code:
                code = _text(data.get("otp"))
            if code and (not status or status in {"success", "received", "done"}):
                return code
            if status in {"timeout", "cancelled", "canceled", "expired"}:
                raise TimeoutError(f"LuckMail 订单 {order_no} 已结束: {status}")
            now = time.time()
            if now - last_log >= 30:
                logger.info("[LuckMail] order=%s status=%s 等待验证码中", order_no, status or "pending")
                last_log = now
            time.sleep(interval)
        raise TimeoutError(f"LuckMail 订单 {order_no} 等验证码超时 {timeout}s")

    def cancel_order(self, order_no: str) -> None:
        order_no = urllib.parse.quote(_text(order_no), safe="")
        if not order_no:
            return
        try:
            self.request("POST", f"/api/v1/openapi/order/{order_no}/cancel", {})
        except Exception as e:
            logger.debug("LuckMail cancel order failed: %s", e)


def write_sample_secret(path: Path, api_key: str) -> None:
    """Small helper for manual standalone experiments."""
    data = {"luckmail": {"api_key": api_key}}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
