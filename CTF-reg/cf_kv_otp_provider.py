"""Cloudflare KV-backed OTP provider (replaces IMAP polling).

Email lifecycle:
  发件人 → CF MX (catch-all) → Email Worker(otp-relay) → KV write
  pipeline → wait_for_otp() → KV read → 拿到 6 位码

Worker（scripts/otp_email_worker.js）会把 OTP 解析后存到 KV：
  key   = recipient email (lowercased)
  value = JSON {otp, ts (ms), from, subject}
  TTL   = 600s

Configuration（按优先级）：
  1. env vars: CF_API_TOKEN / CF_ACCOUNT_ID / CF_OTP_KV_NAMESPACE_ID
  2. SQLite runtime_meta[secrets]: cloudflare.{api_token, account_id, otp_kv_namespace_id}

启用方式：设环境变量 OTP_BACKEND=cf_kv 即可让
mail_provider.wait_for_otp / card.py:_fetch_openai_login_otp 走这条路。
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
from pathlib import Path
from typing import Optional


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger(__name__)

CF_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareKVOtpProvider:
    """Polls a CF KV namespace for OTPs written by the email Worker.

    This replaces the IMAP→QQ pull path with a direct KV read. Typical
    end-to-end latency:
      - Email arrival → Worker → KV write: 1–3s
      - KV poll interval here: 1s
      → 1–4s wall clock vs 30–90s for the IMAP path.
    """

    def __init__(
        self,
        api_token: str,
        account_id: str,
        kv_namespace_id: str,
        poll_interval_s: float = 1.0,
        delete_after_read: bool = True,
    ):
        self.token = api_token
        self.account_id = account_id
        self.kv_id = kv_namespace_id
        self.poll_interval_s = max(0.2, poll_interval_s)
        self.delete_after_read = delete_after_read
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})  # 跟 pipeline.py 同款，避开 http_proxy
        )

    @classmethod
    def from_env_or_secrets(
        cls,
        secrets_path: Optional[Path] = None,
        **kwargs,
    ) -> "CloudflareKVOtpProvider":
        """Build from CF_* env vars; fall back to SQLite runtime_meta[secrets]."""
        token = os.getenv("CF_API_TOKEN", "").strip()
        account_id = os.getenv("CF_ACCOUNT_ID", "").strip()
        kv_id = os.getenv("CF_OTP_KV_NAMESPACE_ID", "").strip()

        if not (token and account_id and kv_id):
            try:
                from webui.backend.db import get_db

                secrets = get_db().get_runtime_json("secrets", {})
                cf = {}
                if isinstance(secrets, dict):
                    cf = secrets.get("cloudflare") or {}
                # kv_api_token 是 KV/Workers 专用 token（实践中 DNS 用的
                # token 跟 KV 用的常是不同 token，权限不同），优先读它，
                # 落回 api_token 做兼容。
                token = token or (
                    cf.get("kv_api_token")
                    or cf.get("api_token")
                    or ""
                ).strip()
                account_id = account_id or (cf.get("account_id") or "").strip()
                kv_id = kv_id or (cf.get("otp_kv_namespace_id") or "").strip()
            except Exception as e:
                logger.warning(f"读 SQLite secrets 失败: {e}")

        # 显式传入 secrets_path 时仍允许读取文件，便于离线/单文件调试；
        # 常规 webui/pipeline 路径不再依赖 legacy secrets file。
        if secrets_path and not (token and account_id and kv_id) and secrets_path.exists():
            try:
                secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
                cf = secrets.get("cloudflare") or {}
                token = token or (cf.get("kv_api_token") or cf.get("api_token") or "").strip()
                account_id = account_id or (cf.get("account_id") or "").strip()
                kv_id = kv_id or (cf.get("otp_kv_namespace_id") or "").strip()
            except Exception as e:
                logger.warning(f"读 {secrets_path} 失败: {e}")

        missing = [
            name
            for name, val in (
                ("CF_API_TOKEN", token),
                ("CF_ACCOUNT_ID", account_id),
                ("CF_OTP_KV_NAMESPACE_ID", kv_id),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                f"CloudflareKVOtpProvider 缺配置：{','.join(missing)} "
                f"（设环境变量或写入 SQLite runtime_meta[secrets].cloudflare.*）"
            )

        return cls(
            api_token=token,
            account_id=account_id,
            kv_namespace_id=kv_id,
            **kwargs,
        )

    def _req(self, method: str, path: str, *, accept_404: bool = False) -> Optional[dict]:
        url = CF_BASE + path
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with self._opener.open(req, timeout=10) as r:
                raw = r.read()
                ctype = r.headers.get("Content-Type", "")
                if ctype.startswith("application/json"):
                    return json.loads(raw.decode())
                return {"raw": raw.decode(errors="replace"), "success": True}
        except urllib.error.HTTPError as e:
            if e.code == 404 and accept_404:
                return None
            body = e.read().decode(errors="replace")[:200]
            raise RuntimeError(f"CF KV {method} {path} → HTTP {e.code}: {body}")

    def _kv_get(self, key: str) -> Optional[dict]:
        """Read one key from KV. Returns parsed JSON or None if missing."""
        encoded = urllib.parse.quote(key, safe="@.+-")
        path = (
            f"/accounts/{self.account_id}"
            f"/storage/kv/namespaces/{self.kv_id}/values/{encoded}"
        )
        # KV value endpoint returns the raw value (not wrapped). We accept 404.
        url = CF_BASE + path
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        try:
            with self._opener.open(req, timeout=10) as r:
                raw = r.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(raw)
                except Exception:
                    # 兼容 worker 可能直接写 OTP 字符串的情况
                    raw = raw.strip()
                    if raw.isdigit() and len(raw) == 6:
                        return {"otp": raw, "ts": 0}
                    return None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            body = e.read().decode(errors="replace")[:200]
            raise RuntimeError(f"CF KV GET → HTTP {e.code}: {body}")

    def _kv_delete(self, key: str) -> None:
        encoded = urllib.parse.quote(key, safe="@.+-")
        try:
            self._req(
                "DELETE",
                f"/accounts/{self.account_id}"
                f"/storage/kv/namespaces/{self.kv_id}/values/{encoded}",
                accept_404=True,
            )
        except Exception as e:
            logger.debug(f"KV delete failed (non-fatal): {e}")

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 180,
        issued_after: Optional[float] = None,
    ) -> str:
        """Poll KV until an OTP keyed by `email_addr` shows up.

        - issued_after (epoch seconds): only accept entries written at or
          after this timestamp (-3s grace for clock skew). Defaults to now,
          which means "ignore anything written before this call started".
        """
        key = email_addr.strip().lower()
        if issued_after is None:
            issued_after = time.time()
        # 放宽 grace 到 600s —— 等同 Worker 写 KV 的 TTL：
        # 注册场景下每次都是全新随机邮箱（catch-all 域），KV 里能命中的
        # 一定是当次注册触发的邮件；OpenAI 常常在 email 提交瞬间就发 OTP，
        # 而 wait_for_otp 在 OTP 页面才被调用，两者间可能差 30-60s，
        # 用窄 grace 会把真正的 OTP 当成旧值丢弃（实测命中点）。
        accept_threshold_s = issued_after - 600.0

        deadline = time.time() + timeout
        start = time.time()
        polls = 0
        last_log_at = 0.0
        logger.info(
            f"[CF-KV] 等 OTP key={key} timeout={timeout}s "
            f"(issued_after={issued_after:.0f})"
        )

        while time.time() < deadline:
            polls += 1
            try:
                payload = self._kv_get(key)
            except Exception as e:
                logger.warning(f"[CF-KV] 轮询异常 key={key}: {e}")
                payload = None

            if payload and payload.get("otp"):
                ts_ms = payload.get("ts") or 0
                ts_s = ts_ms / 1000.0 if ts_ms > 1e10 else float(ts_ms)
                # ts 太老（fallback、上一次 run 残留）→ 忽略，继续轮
                if ts_s and ts_s < accept_threshold_s:
                    if time.time() - last_log_at > 10:
                        logger.info(
                            f"[CF-KV] key={key} 命中但 ts={ts_s:.0f} < "
                            f"threshold={accept_threshold_s:.0f}，忽略旧值"
                        )
                        last_log_at = time.time()
                else:
                    otp = str(payload["otp"]).strip()
                    elapsed = time.time() - start
                    logger.info(
                        f"[CF-KV] 收到 OTP={otp} key={key} "
                        f"poll#{polls} elapsed={elapsed:.1f}s "
                        f"from={payload.get('from','?')[:60]!r}"
                    )
                    if self.delete_after_read:
                        self._kv_delete(key)
                    return otp

            now = time.time()
            if now - last_log_at >= 30:
                logger.info(
                    f"[CF-KV] 轮询中 key={key} 已等 {int(now - start)}s "
                    f"polls={polls}"
                )
                last_log_at = now
            time.sleep(self.poll_interval_s)

        raise TimeoutError(
            f"CloudflareKVOtpProvider: 等 OTP 超时 {timeout}s key={key}"
        )


def is_cf_kv_backend_active() -> bool:
    """True iff OTP_BACKEND env var requests cf_kv."""
    return os.getenv("OTP_BACKEND", "").strip().lower() in ("cf_kv", "cloudflare_kv", "kv")
