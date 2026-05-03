"""Preflight check for Cloudflare KV-backed OTP path.

替代原 IMAP preflight。OTP 现在走 CF Email Routing → otp-relay Worker → KV
（见 scripts/setup_cf_email_worker.py 一键部署 + scripts/otp_email_worker.js）。

校验三件事：
  1. token 能访问指定 account（也是 setup 脚本的最低门槛）
  2. KV namespace ID 在该 account 下确实存在 + 可读
  3. (可选) worker 名字下确实有 script 部署着
"""
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
    """GET 不经过 http_proxy，避开本机 mitm 代理。"""
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

    # 1) token + account 访问
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

    # 2) KV namespace ID 可读
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

    # 3) Worker 存在 — 用 list scripts 间接判断（GET script 单条返回 multipart 不便解析）
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
