#!/usr/bin/env python3
"""One-click setup Cloudflare Email Worker + KV for receiving OTP emails.

Running this script performs these operations (idempotent, can be run repeatedly):
  1. Validate CF API token permissions
  2. Find/create KV namespace (default name OTP_KV)
  3. Upload scripts/otp_email_worker.js as Worker (default name otp-relay),
     bind OTP_KV + optional FALLBACK_TO environment variable
  4. For each zone: enable Email Routing (if not enabled), route catch-all
     to this Worker
  5. Print fields to be backfilled to SQLite runtime_meta[secrets]

Required CF API token permissions:
  - Account → Workers Scripts:Edit
  - Account → Workers KV Storage:Edit
  - Zone → Email Routing Rules:Edit
  - Zone → Zone:Read

Usage:
  # token + account_id via environment variables
  CF_API_TOKEN=xxx CF_ACCOUNT_ID=yyy \\
    python scripts/setup_cf_email_worker.py --zones example.com,foo.com

  # Or read cloudflare.api_token + cloudflare.account_id from SQLite runtime_meta[secrets]
  python scripts/setup_cf_email_worker.py --zones example.com

  # Add fallback: forward OTP copy to QQ (insurance during migration)
  python scripts/setup_cf_email_worker.py --zones example.com \\
      --fallback-to your_qq@qq.com

  # Dry-run only (only validate token + list zones, don't modify anything)
  python scripts/setup_cf_email_worker.py --zones example.com --dry-run"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from webui.backend.db import get_db

CF = "https://api.cloudflare.com/client/v4"
ROOT = Path(__file__).resolve().parent.parent
WORKER_JS = Path(__file__).resolve().parent / "otp_email_worker.js"


class CFError(RuntimeError):
    """CFClient operation failed (webui endpoint will catch this and convert to HTTP error)."""


class CFClient:
    """Minimal Cloudflare API client (stdlib only, no http_proxy hijack)."""

    def __init__(self, token: str):
        self.token = token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _req(self, method: str, path: str, body=None, ctype: str = "application/json"):
        url = CF + path
        data: Optional[bytes] = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
                headers["Content-Type"] = ctype
            else:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get("Content-Type", "").startswith("application/json"):
                    return json.loads(raw.decode())
                return {"raw": raw.decode(errors="replace"), "success": True}
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            try:
                parsed = json.loads(body_text)
                parsed.setdefault("success", False)
                return parsed
            except Exception:
                return {
                    "success": False,
                    "errors": [{"code": e.code, "message": body_text[:400]}],
                }

    # ── token / account ─────────────────────────────────────

    def verify_token(self, account_id: str) -> dict:
        """Hit /accounts/{id} as token availability probe.

        Note: New format 'cfat_' tokens return 1000 Invalid Token on /user/tokens/verify
        (endpoint designed for old v1 tokens), but the token itself is valid. Verify directly
        via /accounts/{id}, which works reliably for all token formats."""
        r = self._req("GET", f"/accounts/{account_id}")
        if not r.get("success"):
            raise CFError(
                f"token 不能访问 account={account_id}: {_short(r)}"
            )
        return r

    def get_zone_id(self, zone_name: str) -> str:
        r = self._req("GET", f"/zones?name={zone_name}")
        if not r.get("success") or not r.get("result"):
            raise CFError(f"找不到 zone={zone_name!r}: {_short(r)}")
        return r["result"][0]["id"]

    # ── KV ──────────────────────────────────────────────────

    def find_or_create_kv(self, account_id: str, name: str) -> str:
        page = 1
        while True:
            r = self._req(
                "GET",
                f"/accounts/{account_id}/storage/kv/namespaces?per_page=100&page={page}",
            )
            if not r.get("success"):
                raise CFError(f"KV 列表失败: {_short(r)}")
            for ns in r.get("result", []):
                if ns.get("title") == name:
                    return ns["id"]
            info = r.get("result_info") or {}
            if info.get("page", 1) >= info.get("total_pages", 1):
                break
            page += 1

        c = self._req(
            "POST",
            f"/accounts/{account_id}/storage/kv/namespaces",
            {"title": name},
        )
        if not c.get("success"):
            raise CFError(f"KV 创建失败: {_short(c)}")
        return c["result"]["id"]

    # ── Worker upload ──────────────────────────────────────

    def upload_worker(
        self,
        account_id: str,
        script_name: str,
        script_body: str,
        kv_namespace_id: str,
        fallback_to: str = "",
        compatibility_date: str = "2024-09-23",
    ) -> dict:
        bindings = [
            {
                "type": "kv_namespace",
                "name": "OTP_KV",
                "namespace_id": kv_namespace_id,
            }
        ]
        if fallback_to:
            bindings.append(
                {"type": "plain_text", "name": "FALLBACK_TO", "text": fallback_to}
            )

        metadata = {
            "main_module": "worker.js",
            "compatibility_date": compatibility_date,
            "bindings": bindings,
        }

        boundary = "----CFOTPRelayBoundary7c3a1f"
        body = _build_multipart(boundary, metadata, script_body)
        ctype = f"multipart/form-data; boundary={boundary}"

        r = self._req(
            "PUT",
            f"/accounts/{account_id}/workers/scripts/{script_name}",
            body=body,
            ctype=ctype,
        )
        if not r.get("success"):
            raise CFError(f"Worker 上传失败: {_short(r)}")
        return r["result"] or {}

    # ── Email Routing ──────────────────────────────────────

    def ensure_email_routing_enabled(self, zone_id: str) -> None:
        """Best effort to confirm Email Routing is enabled.

        Note: `GET /zones/{id}/email/routing` endpoint is not the same permission as
        Email Routing Rules (Email Routing master switch is Account level). If token only
        has Email Routing Rules:Edit (sufficient for modifying catch-all rule), this GET
        returns 10000 Authentication error. When catch-all rule is readable and enabled=True,
        Email Routing must be enabled, skip enable step."""
        r = self._req("GET", f"/zones/{zone_id}/email/routing")
        if r.get("success"):
            if (r.get("result") or {}).get("enabled"):
                return
            # Not enabled → attempt enable
            e = self._req("POST", f"/zones/{zone_id}/email/routing/enable")
            if not e.get("success"):
                errs = e.get("errors") or []
                if any(
                    "already enabled" in (x.get("message") or "").lower()
                    for x in errs
                ):
                    return
                raise CFError(
                    f"enable email routing 失败 zone={zone_id}: {_short(e)}"
                )
            return
        # GET failed: usually token lacks Email Routing master switch read permission. If
        # caller can already read/modify catch-all rule, Email Routing must be enabled,
        # this step can be skipped.
        errs = r.get("errors") or []
        if any(e.get("code") == 10000 for e in errs):
            print(
                f"      [info] zone={zone_id[:12]}... 读 email routing 总状态"
                f" 无权限；假设已启用（catch-all rule 已能读说明启用了）"
            )
            return
        raise CFError(
            f"读 email routing 状态失败 zone={zone_id}: {_short(r)}"
        )

    def set_catch_all_to_worker(self, zone_id: str, worker_script: str) -> None:
        body = {
            "name": "catch-all → otp-relay worker",
            "enabled": True,
            "matchers": [{"type": "all"}],
            "actions": [{"type": "worker", "value": [worker_script]}],
        }
        r = self._req(
            "PUT",
            f"/zones/{zone_id}/email/routing/rules/catch_all",
            body,
        )
        if not r.get("success"):
            raise CFError(f"catch-all 设置失败 zone={zone_id}: {_short(r)}")

    def get_catch_all(self, zone_id: str) -> dict:
        r = self._req("GET", f"/zones/{zone_id}/email/routing/rules/catch_all")
        if not r.get("success"):
            return {}
        return r.get("result") or {}


def _build_multipart(boundary: str, metadata: dict, script_body: str) -> bytes:
    crlf = "\r\n"
    parts: list[str] = []
    parts.append(f"--{boundary}{crlf}")
    parts.append(
        f'Content-Disposition: form-data; name="metadata"; filename="metadata.json"{crlf}'
    )
    parts.append(f"Content-Type: application/json{crlf}{crlf}")
    parts.append(json.dumps(metadata))
    parts.append(crlf)
    parts.append(f"--{boundary}{crlf}")
    parts.append(
        f'Content-Disposition: form-data; name="worker.js"; filename="worker.js"{crlf}'
    )
    parts.append(f"Content-Type: application/javascript+module{crlf}{crlf}")
    parts.append(script_body)
    parts.append(crlf)
    parts.append(f"--{boundary}--{crlf}")
    return "".join(parts).encode("utf-8")


def _short(resp: dict) -> str:
    """Shorten an API error response for logs."""
    try:
        errs = resp.get("errors") or []
        if errs:
            return "; ".join(
                f"[{e.get('code','?')}] {(e.get('message') or '')[:200]}" for e in errs
            )
        return json.dumps(resp, ensure_ascii=False)[:400]
    except Exception:
        return str(resp)[:400]


def _load_secrets() -> dict:
    try:
        data = get_db().get_runtime_json("secrets", {})
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[warn] 读 SQLite runtime_meta[secrets] 失败: {e}", file=sys.stderr)
        return {}


def main() -> None:
    p = argparse.ArgumentParser(
        description="配置 Cloudflare Email Worker + KV 用于接收 OTP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--zones",
        required=True,
        help="逗号分隔的 zone 列表，如 example.com,foo.com",
    )
    p.add_argument("--worker-name", default="otp-relay")
    p.add_argument("--kv-name", default="OTP_KV")
    p.add_argument(
        "--fallback-to",
        default="",
        help="抓到 OTP 后同时 forward 邮件到这里（迁移期保险）",
    )
    p.add_argument("--account-id", default="", help="覆盖环境变量 / SQLite runtime_meta[secrets]")
    p.add_argument("--token", default="", help="覆盖环境变量 / SQLite runtime_meta[secrets]")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验 token + 解析 zone id，不改任何东西",
    )
    args = p.parse_args()

    secrets = _load_secrets()
    cf_secrets = (secrets.get("cloudflare") or {})

    token = args.token or os.getenv("CF_API_TOKEN") or cf_secrets.get("api_token", "")
    account_id = (
        args.account_id
        or os.getenv("CF_ACCOUNT_ID")
        or cf_secrets.get("account_id", "")
    )

    if not token:
        sys.exit("缺 CF_API_TOKEN（或 SQLite runtime_meta[secrets] 的 cloudflare.api_token）")
    if not account_id:
        sys.exit(
            "缺 CF_ACCOUNT_ID。Cloudflare dashboard 右下角能看到 Account ID，"
            "传 --account-id 或 CF_ACCOUNT_ID 或写进 SQLite runtime_meta[secrets]"
        )
    if not WORKER_JS.exists():
        sys.exit(f"找不到 Worker 脚本：{WORKER_JS}")

    client = CFClient(token)

    try:
        _run_setup_cli(client, account_id, args)
    except CFError as e:
        sys.exit(f"[ERROR] {e}")


def _run_setup_cli(client: "CFClient", account_id: str, args) -> None:
    print(f"[1/5] 校验 token 能访问 account={account_id} ...")
    info = client.verify_token(account_id)
    aname = (info.get("result") or {}).get("name", "?")
    print(f"      OK: account name={aname!r}")

    zones = [z.strip() for z in args.zones.split(",") if z.strip()]
    if not zones:
        sys.exit("--zones 解析后为空")

    print(f"[2/5] 解析 zone id（{len(zones)} 个）...")
    zone_ids = {}
    for zname in zones:
        zid = client.get_zone_id(zname)
        zone_ids[zname] = zid
        print(f"      {zname} → {zid}")

    if args.dry_run:
        print("\n[dry-run] 校验通过。要正式执行去掉 --dry-run。")
        return

    print(f"[3/5] 找/建 KV namespace '{args.kv_name}' ...")
    kv_id = client.find_or_create_kv(account_id, args.kv_name)
    print(f"      kv_id={kv_id}")

    print(f"[4/5] 上传 Worker '{args.worker_name}' ...")
    script_body = WORKER_JS.read_text(encoding="utf-8")
    client.upload_worker(
        account_id=account_id,
        script_name=args.worker_name,
        script_body=script_body,
        kv_namespace_id=kv_id,
        fallback_to=args.fallback_to,
    )
    print(
        f"      OK (FALLBACK_TO="
        f"{args.fallback_to or '<none, 无备份转发>'})"
    )

    print(f"[5/5] 给每个 zone 启 Email Routing + 切 catch-all → Worker ...")
    for zname, zid in zone_ids.items():
        # Check current state first, avoid silently overwriting previous forward rules
        cur = client.get_catch_all(zid)
        cur_actions = cur.get("actions") or []
        cur_summary = "; ".join(
            f"{a.get('type')}={','.join(a.get('value') or [])}" for a in cur_actions
        ) or "<none>"
        print(f"      [{zname}] before: enabled={cur.get('enabled')} actions={cur_summary}")
        client.ensure_email_routing_enabled(zid)
        client.set_catch_all_to_worker(zid, args.worker_name)
        print(f"      [{zname}] after:  worker='{args.worker_name}' ✓")

    print("\n=== Done. 把这两个字段加到 SQLite runtime_meta[secrets]: ===")
    suggestion = {
        "cloudflare": {
            "api_token": "(已有, 不变)",
            "account_id": account_id,
            "otp_kv_namespace_id": kv_id,
            "otp_worker_name": args.worker_name,
        }
    }
    print(json.dumps(suggestion, indent=2, ensure_ascii=False))

    print(
        "\n验证：发一封测试邮件给一个 zone 下的随机地址（catch-all 会兜住），"
        "等 3 秒后用 CF API GET KV 确认 OTP 已落库："
    )
    print(
        f"  curl -s -H 'Authorization: Bearer $CF_API_TOKEN' \\\n"
        f"    'https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/storage/kv/namespaces/{kv_id}/values/<recipient@yourzone>'"
    )


if __name__ == "__main__":
    main()
