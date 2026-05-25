"""Preflight check for Cloudflare KV-backed OTP path.

Replaces the original IMAP preflight. OTP now goes through CF Email Routing → otp-relay Worker → KV
(see scripts/setup_cf_email_worker.py for one-click deployment + scripts/otp_email_worker.js).

Validates three things:
  1. token can access the specified account (also the minimum threshold for setup script)
  2. KV namespace ID actually exists under that account + is readable
  3. (optional) worker name actually has a script deployed under it"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Tuple

from pydantic import BaseModel

from ._common import CheckResult, PreflightResult, aggregate

CF = "https://api.cloudflare.com/client/v4"


class CloudflareKVInput(BaseModel):
    api_token: str
    account_id: str
    kv_namespace_id: str
    worker_name: str = "otp-relay"


def _http_get(token: str, path: str) -> Tuple[int, dict]:
    """GET bypasses http_proxy to avoid local MITM proxy."""
    req = urllib.request.Request(
        CF + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
            if ctype.startswith("application/json"):
                return r.status, json.loads(raw.decode())
            return r.status, {"raw": raw.decode(errors="replace"), "success": True}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            parsed = json.loads(body)
            parsed.setdefault("success", False)
            return e.code, parsed
        except Exception:
            return e.code, {"success": False, "errors": [{"message": body[:200]}]}
    except Exception as e:
        return -1, {"success": False, "errors": [{"message": str(e)[:200]}]}


def _err_msg(resp: dict) -> str:
    errs = resp.get("errors") or []
    return "; ".join(f"[{e.get('code','?')}] {(e.get('message') or '')[:160]}" for e in errs) or "未知错误"


def check(body: dict) -> PreflightResult:
    cfg = CloudflareKVInput.model_validate(body)
    checks: list[CheckResult] = []

    # 1) token + account access
    code, data = _http_get(cfg.api_token, f"/accounts/{cfg.account_id}")
    if not data.get("success"):
        checks.append(
            CheckResult(
                name="account",
                status="fail",
                message=f"无法访问 account: {_err_msg(data)}",
            )
        )
        return aggregate(checks)
    aname = (data.get("result") or {}).get("name", "?")
    checks.append(
        CheckResult(name="account", status="ok", message=f"account: {aname}")
    )

    # 2) KV namespace ID is readable
    code, data = _http_get(
        cfg.api_token,
        f"/accounts/{cfg.account_id}/storage/kv/namespaces/{cfg.kv_namespace_id}",
    )
    if not data.get("success"):
        checks.append(
            CheckResult(
                name="kv_namespace",
                status="fail",
                message=f"KV namespace {cfg.kv_namespace_id[:12]}... 不可访问: {_err_msg(data)}",
            )
        )
    else:
        title = (data.get("result") or {}).get("title", "?")
        checks.append(
            CheckResult(
                name="kv_namespace",
                status="ok",
                message=f"namespace title='{title}'",
            )
        )

    # 3) Worker exists — use list scripts to determine indirectly (GET script single returns multipart which is inconvenient to parse)
    code, data = _http_get(
        cfg.api_token,
        f"/accounts/{cfg.account_id}/workers/scripts?per_page=100",
    )
    if not data.get("success"):
        checks.append(
            CheckResult(
                name="worker",
                status="warn",
                message=f"无法列 workers (token 可能缺 Workers Scripts:Read): {_err_msg(data)}",
            )
        )
    else:
        names = {(s or {}).get("id") for s in (data.get("result") or [])}
        if cfg.worker_name in names:
            checks.append(
                CheckResult(
                    name="worker",
                    status="ok",
                    message=f"worker '{cfg.worker_name}' 已部署",
                )
            )
        else:
            checks.append(
                CheckResult(
                    name="worker",
                    status="warn",
                    message=(
                        f"worker '{cfg.worker_name}' 未找到；"
                        f"先跑 scripts/setup_cf_email_worker.py 部署"
                    ),
                )
            )

    return aggregate(checks)
