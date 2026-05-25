#!/usr/bin/env python3
"""Pipeline Scheduler: Register ChatGPT Account → Stripe/PayPal Payment

⚠️ Authorized use only (systems you own / legitimate CTF / authorized bug bounty in-scope assets /
   security research). Running this program constitutes agreement to all terms in the NOTICE file.
   Provided AS IS, without any warranties; all consequences are the user's responsibility.
   See NOTICE and README.md disclaimer section in the repository root.

Usage:
  # Full pipeline (register + payment)
  python pipeline.py --config CTF-pay/config.paypal.json --paypal

  # Register only
  python pipeline.py --register-only --cardw-config CTF-reg/config.paypal-proxy.json

  # Payment only (prioritizes reusing unpaid accounts from SQLite database)
  python pipeline.py --pay-only --config CTF-pay/config.paypal.json --paypal

  # Batch mode
  python pipeline.py --config CTF-pay/config.paypal.json --paypal --batch 5 --delay 30"""

import argparse
import atexit
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from webui.backend.db import get_db

ROOT = Path(__file__).resolve().parent.parent  # pipeline/_monolith.py → Repository root directory
CARDW_DIR = ROOT / "CTF-reg"
CARD_DIR = ROOT / "CTF-pay"
CARD_PY = CARD_DIR / "card.py"
GOPAY_PY = CARD_DIR / "gopay.py"
QRIS_PY = CARD_DIR / "qris.py"
RUNTIME_PAY = CARD_DIR / ".runtime"
RUNTIME_REG = CARDW_DIR / ".runtime"
RUNTIME_PAY.mkdir(parents=True, exist_ok=True)
RUNTIME_REG.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR = ROOT / "output"
(OUTPUT_DIR / "logs").mkdir(parents=True, exist_ok=True)
DOMAIN_STATE_KEY = "email_domain_state"
DAEMON_STATE_KEY = "daemon_state"
SECRETS_KEY = "secrets"
RUNTIME_DB_FILE = OUTPUT_DIR / "webui.db"


# --plan {team,plus} default value written to fresh_checkout.plan when covering; user already
# If the corresponding field (promo_campaign_id / entry_point) is explicitly set in config, do not modify it.
_PLAN_OVERRIDE_DEFAULTS = {
    "plus": {
        "plan_name": "chatgptplusplan",
        "entry_point": "all_plans_pricing_modal",
        "promo_campaign_id": "plus-1-month-free",
    },
    "team": {
        "plan_name": "chatgptteamplan",
        "entry_point": "team_workspace_purchase_modal",
        "promo_campaign_id": "team-1-month-free",
    },
}


def _apply_plan_override(card_config_path: str, plan: str) -> str:
    """When `--plan plus/team` is used, do not modify the user's file — generate a temporary config,
    align the key fields of fresh_checkout.plan to the target plan, and return the temporary path
    to the pipeline for all subsequent branches to use. In Plus mode, also strip the default
    workspace/seat fields from example. Clean up the temporary file using atexit."""
    plan = plan.lower()
    if plan not in _PLAN_OVERRIDE_DEFAULTS:
        return card_config_path

    with open(card_config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    fresh = cfg.setdefault("fresh_checkout", {})
    plan_cfg = fresh.setdefault("plan", {})
    defaults = _PLAN_OVERRIDE_DEFAULTS[plan]
    # plan_name must be overwritten; other fields only fill default values when users haven't set them, to avoid overwriting manually adjusted discount codes
    plan_cfg["plan_name"] = defaults["plan_name"]
    for key in ("entry_point", "promo_campaign_id"):
        if not plan_cfg.get(key):
            plan_cfg[key] = defaults[key]
    if plan == "plus":
        plan_cfg.pop("workspace_name", None)
        plan_cfg.pop("seat_quantity", None)

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=f"pipeline_plan_{plan}_",
        dir=str(RUNTIME_PAY), delete=False, encoding="utf-8",
    )
    json.dump(cfg, tmp, ensure_ascii=False, indent=2)
    tmp.close()
    atexit.register(lambda p=tmp.name: os.path.exists(p) and os.unlink(p))
    print(f"[plan] 应用 --plan={plan}：临时配置 {tmp.name}")
    return tmp.name


# ──────────────────────────────────────────────
# 0. Email domain pool + gpt-team system client + Cloudflare subdomain on-demand provisioning
# ──────────────────────────────────────────────

class CloudflareDomainProvisioner:
    """Provision new subdomains on demand via Cloudflare API: add 3 MX + 1 TXT/SPF, with zone catch-all routing.
    Email Routing must be enabled on the zone (having a `{type:"all"}` forward rule is sufficient; subdomains automatically inherit)."""

    _CF = "https://api.cloudflare.com/client/v4"

    def __init__(self, api_token: str, zone_name: str, forward_to: str = "",
                 min_seg_len: int = 2, max_seg_len: int = 5,
                 min_segs: int = 1, max_segs: int = 4,
                 dns_propagation_s: int = 20):
        import urllib.request, random, string
        self._urllib = urllib.request
        self._random = random
        self._string = string
        self.token = api_token
        self.zone_name = zone_name.lower().strip()
        self.forward_to = forward_to
        self.min_seg_len = min_seg_len
        self.max_seg_len = max_seg_len
        self.min_segs = min_segs
        self.max_segs = max_segs
        self.dns_propagation_s = dns_propagation_s
        self._zone_id_cached = None
        # Not through environment proxy (same as TeamSystemClient, avoid being hijacked by http_proxy to local proxy)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _http(self, method: str, path: str, body=None):
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = self._urllib.Request(self._CF + path, data=data, headers=headers, method=method)
        with self._opener.open(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def zone_id(self) -> str:
        if self._zone_id_cached:
            return self._zone_id_cached
        d = self._http("GET", f"/zones?name={self.zone_name}")
        if not d.get("success") or not d.get("result"):
            raise RuntimeError(f"CF zone 查询失败: {d}")
        self._zone_id_cached = d["result"][0]["id"]
        return self._zone_id_cached

    def list_subdomains(self) -> list:
        """List all subdomains with existing MX records under the current zone (excluding the zone apex itself)."""
        zid = self.zone_id()
        seen = set()
        page = 1
        while True:
            d = self._http("GET", f"/zones/{zid}/dns_records?type=MX&per_page=100&page={page}")
            if not d.get("success"):
                break
            for r in d.get("result", []):
                n = r.get("name", "").lower()
                if n and n != self.zone_name:
                    seen.add(n)
            info = d.get("result_info") or {}
            if info.get("page", 1) >= info.get("total_pages", 1):
                break
            page += 1
        return sorted(seen)

    def _random_subdomain_name(self) -> str:
        n_segs = self._random.randint(self.min_segs, self.max_segs)
        parts = []
        for _ in range(n_segs):
            seg_len = self._random.randint(self.min_seg_len, self.max_seg_len)
            parts.append("".join(self._random.choices(self._string.ascii_lowercase, k=seg_len)))
        return ".".join(parts)

    def provision(self, name: str = None, max_retries: int = 5) -> str:
        """Enable a new subdomain (add 3 MX + 1 TXT), return the complete domain name (lowercase).
        If name is not provided, it will be randomly generated and avoid existing ones."""
        zid = self.zone_id()
        existing = set(self.list_subdomains())
        attempt = 0
        chosen_full = None
        while attempt < max_retries:
            attempt += 1
            label = name if name else self._random_subdomain_name()
            full = f"{label}.{self.zone_name}".lower()
            if full in existing:
                if name:  # Specified but still conflicting -> Error directly
                    raise RuntimeError(f"CF 子域 {full} 已存在")
                continue
            chosen_full = full
            break
        if not chosen_full:
            raise RuntimeError(f"CF 随机生成子域名失败（{max_retries} 次都冲突）")

        # 3 MX records + 1 TXT record
        for route_idx in (1, 2, 3):
            pri = self._random.randint(5, 95)
            r = self._http("POST", f"/zones/{zid}/dns_records", {
                "type": "MX", "name": chosen_full,
                "content": f"route{route_idx}.mx.cloudflare.net",
                "priority": pri, "ttl": 1,
            })
            if not r.get("success"):
                raise RuntimeError(f"CF 加 MX{route_idx} 失败: {r.get('errors')}")
        r = self._http("POST", f"/zones/{zid}/dns_records", {
            "type": "TXT", "name": chosen_full,
            "content": '"v=spf1 include:_spf.mx.cloudflare.net ~all"',
            "ttl": 1,
        })
        if not r.get("success"):
            raise RuntimeError(f"CF 加 TXT 失败: {r.get('errors')}")

        print(f"[CF] 开通子域 {chosen_full}  等 {self.dns_propagation_s}s DNS 生效 ...")
        time.sleep(self.dns_propagation_s)
        return chosen_full

    def delete_subdomain(self, full_name: str) -> int:
        """Delete all DNS records under the subdomain. Return the number of deleted records."""
        zid = self.zone_id()
        d = self._http("GET", f"/zones/{zid}/dns_records?name={full_name}&per_page=100")
        if not d.get("success"):
            return 0
        n = 0
        for r in d.get("result", []):
            rr = self._http("DELETE", f"/zones/{zid}/dns_records/{r['id']}")
            if rr.get("success"):
                n += 1
        return n


class MultiZoneDomainProvisioner:
    """Multi-zone wrapper: Prioritize using active_zone when opening, route deletion by domain suffix."""

    def __init__(self, sub_provisioners):
        self.subs = [p for p in (sub_provisioners or []) if p is not None]
        if not self.subs:
            raise ValueError("MultiZoneDomainProvisioner 需要至少一个子 provisioner")
        self.active_zone = None  # None = all zones random

    @property
    def zone_name(self) -> str:
        return ",".join(p.zone_name for p in self.subs)

    @property
    def zone_names(self) -> list:
        return [p.zone_name for p in self.subs]

    def set_active_zone(self, zone: str) -> None:
        z = (zone or "").strip().lower() or None
        self.active_zone = z

    def list_subdomains(self) -> list:
        out = []
        for p in self.subs:
            try:
                out.extend(p.list_subdomains())
            except Exception as e:
                print(f"[CF] list {p.zone_name} 失败: {e}")
        return sorted(set(out))

    def provision(self, name: str = None, max_retries: int = 5) -> str:
        """active_zone takes priority; automatically fallback to other zones when quota exceeded / HTTP 400."""
        active, others = [], []
        for p in self.subs:
            if self.active_zone and p.zone_name.lower() == self.active_zone:
                active.append(p)
            else:
                others.append(p)
        order = (active or []) + others if active else list(self.subs)
        last_exc = None
        for p in order:
            try:
                return p.provision(name=name, max_retries=max_retries)
            except Exception as e:
                last_exc = e
                msg = str(e).lower()
                # quota class error / 400 Bad Request → fallback to next zone
                if "quota" in msg or "400" in msg or "exceeded" in msg:
                    print(f"[CF] {p.zone_name} 开通失败({str(e)[:60]})，fallback 下一 zone")
                    continue
                # Other errors are thrown directly
                raise
        raise last_exc or RuntimeError("所有 zone 都开通失败")

    def delete_subdomain(self, full_name: str) -> int:
        full_name = full_name.lower()
        for p in self.subs:
            zn = p.zone_name.lower()
            if full_name == zn or full_name.endswith("." + zn):
                return p.delete_subdomain(full_name)
        return 0


class DomainPool:
    """Persist email domain status: ok / burned (with cooldown). Get invite detection results feedback to update after payment.
    Optional provisioner: When available domains < min_available, provision new subdomains on-demand via Cloudflare API."""

    def __init__(self, domains, state_key=DOMAIN_STATE_KEY, cooldown_hours=24,
                 provisioner: "CloudflareDomainProvisioner" = None, min_available: int = 2):
        self.domains = [d.strip() for d in (domains or []) if d and d.strip()]
        self.state_key = str(state_key or DOMAIN_STATE_KEY)
        self.cooldown_s = max(0, int(cooldown_hours)) * 3600
        self.state = self._load()
        self.provisioner = provisioner
        self.min_available = max(1, int(min_available))

    def _load(self):
        try:
            data = get_db().get_runtime_json(self.state_key, {"domains": {}})
            if not isinstance(data, dict) or "domains" not in data:
                return {"domains": {}}
            return data
        except Exception as e:
            print(f"[DomainPool] 读数据库状态失败: {e}，重建")
            return {"domains": {}}

    def _save(self):
        try:
            get_db().set_runtime_json(self.state_key, self.state)
        except Exception as e:
            print(f"[DomainPool] 存数据库状态失败: {e}")

    def _is_available(self, domain, now_ts):
        meta = self.state["domains"].get(domain, {})
        st = meta.get("status")
        if st == "permanent_burned":
            return False
        if st == "burned":
            cd = meta.get("cooldown_until_ts", 0)
            return now_ts >= cd
        return True

    def pick(self):
        """Pick an available domain, strategy: select the one with the smallest 'last_used_ts' in available (least recently used).
        If provisioner is enabled and available < min_available, replenish new subdomains first."""
        now_ts = time.time()
        available = [d for d in self.domains if self._is_available(d, now_ts)]

        # Insufficient availability → Open new subdomains on demand (Cloudflare API)
        if self.provisioner and len(available) < self.min_available:
            need = self.min_available - len(available)
            print(f"[DomainPool] 可用域 {len(available)} < {self.min_available}，通过 Cloudflare 开通 {need} 个 ...")
            for _ in range(need):
                try:
                    new_dom = self.provisioner.provision()
                    if new_dom and new_dom not in self.domains:
                        self.domains.append(new_dom)
                        available.append(new_dom)
                        print(f"[DomainPool] ✅ 新子域加入池: {new_dom}")
                except Exception as e:
                    print(f"[DomainPool] ❌ 开通失败: {e}")
                    break

        if not self.domains:
            return ""
        if not available:
            # All burned: use the one with the shortest cooling time first (minimize waiting)
            def _cd(d):
                return self.state["domains"].get(d, {}).get("cooldown_until_ts", 0)
            available = sorted(self.domains, key=_cd)[:1]
            print(f"[DomainPool] ⚠️ 所有域都在冷却，强制选 {available[0]}")

        def _last_used(d):
            return self.state["domains"].get(d, {}).get("last_used_ts", 0)
        available.sort(key=_last_used)
        return available[0]

    def mark_used(self, domain):
        meta = self.state["domains"].setdefault(domain, {})
        meta["last_used_ts"] = time.time()
        meta["last_used_iso"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def mark_result(self, domain, invite_status):
        """invite_status: 'ok' / 'no_permission' / 'unknown'
        Two consecutive 'no_permission' results escalate to 'permanent_burned' and trigger Cloudflare subdomain cleanup.
        'ok' result resets the 'no_permission' counter."""
        meta = self.state["domains"].setdefault(domain, {})
        meta["last_result"] = invite_status
        meta["last_result_ts"] = time.time()
        if invite_status == "no_permission":
            cnt = int(meta.get("no_permission_count", 0)) + 1
            meta["no_permission_count"] = cnt
            if cnt >= 2:
                meta["status"] = "permanent_burned"
                meta["permanent_burned_iso"] = datetime.now(timezone.utc).isoformat()
                self._save()
                self._cleanup_permanent_burned(domain)
                return
            meta["status"] = "burned"
            meta["burned_at_iso"] = datetime.now(timezone.utc).isoformat()
            meta["cooldown_until_ts"] = time.time() + self.cooldown_s
            meta["cooldown_until_iso"] = datetime.fromtimestamp(
                meta["cooldown_until_ts"], tz=timezone.utc).isoformat()
        elif invite_status == "ok":
            meta["status"] = "ok"
            meta["last_success_iso"] = datetime.now(timezone.utc).isoformat()
            meta["no_permission_count"] = 0  # ok reset count
        self._save()

    def _cleanup_permanent_burned(self, domain):
        """After permanent burn: Cloudflare deletes subdomain DNS records, pool.domains is removed.
        The root domain (zone_name itself) is not deleted, only subdomains are cleared."""
        if not self.provisioner:
            print(f"[DomainPool] ⛔ {domain} 永久标记（无 provisioner，不清理 CF 记录）")
            return
        zone = self.provisioner.zone_name
        if domain == zone or not domain.endswith("." + zone):
            print(f"[DomainPool] ⛔ {domain} 永久标记（非 {zone} 子域，跳过 CF 清理）")
            return
        try:
            n = self.provisioner.delete_subdomain(domain)
            print(f"[DomainPool] 🔥 永久 burn + CF 清理: {domain} (删 {n} 条 DNS 记录)")
        except Exception as e:
            print(f"[DomainPool] CF 清理 {domain} 失败: {e}")
        # Remove from pool.domains (keep state for audit)
        if domain in self.domains:
            self.domains.remove(domain)

    def summary(self):
        now_ts = time.time()
        rows = []
        for d in self.domains:
            m = self.state["domains"].get(d, {})
            st = m.get("status", "fresh")
            if st == "burned" and m.get("cooldown_until_ts", 0) <= now_ts:
                st = "cooled"
            rows.append((d, st, m.get("last_result", "-")))
        # Also list those that have been permanently burned (removed from pool but still in state)
        for d, m in self.state.get("domains", {}).items():
            if d in self.domains:
                continue
            if m.get("status") == "permanent_burned":
                rows.append((d, "PERM_BURN", m.get("last_result", "-")))
        return rows


class TeamSystemClient:
    """gpt-team system client: login → batch-import single RT, parse probe results via SSE.
    Use an independent no-proxy opener to avoid being hijacked by http_proxy in environment variables to local proxy/monitoring."""

    def __init__(self, base_url, username, password, timeout_s=60):
        import urllib.request
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_s = timeout_s
        self.jwt = None
        self.jwt_exp_ts = 0
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),  # Explicitly clear the proxy
        )

    def _login(self):
        import urllib.request
        req = urllib.request.Request(
            f"{self.base_url}/api/auth/login",
            data=json.dumps({"username": self.username, "password": self.password}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._opener.open(req, timeout=self.timeout_s) as r:
            body = json.loads(r.read().decode())
        self.jwt = body["token"]
        # JWT exp 24h, renew 1h in advance
        self.jwt_exp_ts = time.time() + 23 * 3600
        print(f"[Team] 登录成功 user={body.get('user',{}).get('username')}")

    def _ensure_jwt(self):
        if not self.jwt or time.time() >= self.jwt_exp_ts:
            self._login()

    def import_probe(self, refresh_token):
        """Import a single RT + SSE item event. Returns {status, account_id, error, raw}"""
        import urllib.request, base64 as _b64
        self._ensure_jwt()
        payload = {"tokens": [refresh_token]}
        data_b64 = _b64.b64encode(json.dumps(payload).encode()).decode()
        url = f"{self.base_url}/api/gpt-accounts/batch-import/stream?token={self.jwt}&data={data_b64}"
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})

        item_event = None
        done_event = None
        try:
            with self._opener.open(req, timeout=self.timeout_s) as resp:
                event_name = None
                data_buf = []
                for raw in resp:
                    line = raw.decode("utf-8", errors="ignore").rstrip("\n").rstrip("\r")
                    if line.startswith("event:"):
                        event_name = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        data_buf.append(line.split(":", 1)[1].strip())
                    elif line == "":
                        if event_name and data_buf:
                            try:
                                payload = json.loads("\n".join(data_buf))
                            except Exception:
                                payload = {"raw": "\n".join(data_buf)}
                            if event_name == "item":
                                item_event = payload
                            elif event_name == "done":
                                done_event = payload
                                break
                        event_name = None
                        data_buf = []
        except Exception as e:
            return {"status": "error", "error": f"SSE 异常: {e}", "raw": None}

        if not item_event:
            return {"status": "error", "error": "未收到 item 事件", "raw": {"done": done_event}}

        # status: 'success' / 'no_invite_permission' / 'failed'
        raw_status = item_event.get("status", "")
        mapped = {
            "success": "ok",
            "no_invite_permission": "no_permission",
            "failed": "failed",
        }.get(raw_status, raw_status or "unknown")
        return {
            "status": mapped,
            "account_id": item_event.get("accountId"),
            "email": item_event.get("email", ""),
            "error": item_event.get("error", ""),
            "warning": item_event.get("warning", ""),
            "raw": item_event,
        }

    def count_usable_accounts(self, seat_limit: int = 5, usage: str = "recovery") -> dict:
        """Check the number of 'available' accounts in gpt-team.
        usage='recovery' → recovery pool (default, daemon maintenance target)
        usage='sales'    → external pool
        available = account_usage matches & !isBanned & !isDisabled & !noInvitePermission
                    & !expired & userCount + inviteCount < seat_limit"""
        import urllib.request
        from datetime import datetime
        self._ensure_jwt()
        stats = {"total_active": 0, "usable": 0, "full": 0,
                 "no_invite_permission": 0, "banned_or_disabled": 0,
                 "expired": 0, "usage": usage}
        now_sec = time.time()
        page = 1
        while True:
            url = (f"{self.base_url}/api/gpt-accounts?page={page}&pageSize=100"
                   f"&usageStatus={usage}&banStatus=normal")
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.jwt}",
                                                        "Accept": "application/json"})
            with self._opener.open(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode())
            accs = data.get("accounts", [])
            if not accs:
                break
            for a in accs:
                stats["total_active"] += 1
                if a.get("isBanned") or a.get("isDisabled"):
                    stats["banned_or_disabled"] += 1
                    continue
                if a.get("noInvitePermission"):
                    stats["no_invite_permission"] += 1
                    continue
                exp = a.get("expireAt")
                if exp:
                    try:
                        exp_str = str(exp).strip()
                        exp_dt = None
                        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
                                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                            try:
                                exp_dt = datetime.strptime(exp_str[:19], fmt)
                                break
                            except ValueError:
                                continue
                        if exp_dt and exp_dt.timestamp() < now_sec:
                            stats["expired"] += 1
                            continue
                    except Exception:
                        pass
                used = int(a.get("userCount", 0) or 0) + int(a.get("inviteCount", 0) or 0)
                if used >= seat_limit:
                    stats["full"] += 1
                    continue
                stats["usable"] += 1
            info = data.get("pagination", {})
            total = info.get("total", 0)
            if page * info.get("pageSize", 100) >= total:
                break
            page += 1
        return stats

    def update_global_proxy(self, proxy_url: str) -> dict:
        """PUT /api/admin/proxy-config Update global proxy. Returns {proxyUrl, ...}"""
        import urllib.request
        self._ensure_jwt()
        req = urllib.request.Request(
            f"{self.base_url}/api/admin/proxy-config",
            data=json.dumps({"proxyUrl": proxy_url}).encode(),
            headers={"Authorization": f"Bearer {self.jwt}",
                     "Content-Type": "application/json"},
            method="PUT",
        )
        with self._opener.open(req, timeout=self.timeout_s) as r:
            return json.loads(r.read().decode())


# ──────────────────────────────────────────────
# 1. Registration module
# ──────────────────────────────────────────────

class RegistrationError(RuntimeError):
    pass


def register(cardw_config_path, proxy=None, python="python3", timeout=600,
             browser: bool | None = None, _max_outlook_retries: int = 50):
    """Register a new ChatGPT account.

    `browser` priority: explicit parameter > `WEBUI_REG_MODE` environment variable > default False.
    `WEBUI_REG_MODE=protocol` uses `auth_flow.AuthFlow.run_register` (HTTP direct connection,
    sentinel + OTP protocol flow), `=browser` uses Camoufox/Playwright.
    WebUI added a toggle button on the Run page; each pipeline startup passes the selection
    as an environment variable.

    In outlook pool scenarios: protocol layer "account already exists" branch immediately
    fast-fail mark dead, automatically claim next available retry. Single fail-fast < 5s,
    50 retry limit is sufficient to sweep typical pool sizes; stop immediately if avail==0.

    Returns dict: {email, session_token, access_token, device_id, ...}"""
    last_err: Exception | None = None
    for attempt in range(1, max(1, _max_outlook_retries) + 1):
        try:
            return _register_once(
                cardw_config_path, proxy=proxy, python=python,
                timeout=timeout, browser=browser,
            )
        except RegistrationError as e:
            last_err = e
            msg = str(e)
            # Retry automatically only when "outlook silent rejection" type failure occurs AND there are still available connections in the pool.
            if "outlook OTP timeout" not in msg and "OpenAI 静默拒绝发 OTP" not in msg:
                raise
            try:
                import sys as _sys
                from pathlib import Path as _Path
                _root = _Path(__file__).resolve().parent
                if str(_root) not in _sys.path:
                    _sys.path.insert(0, str(_root))
                from webui.backend import outlook_pool as _op
                avail = _op.stats().get("available", 0)
            except Exception:
                avail = 0
            if avail <= 0:
                print(f"[register] outlook 池已无 available（第 {attempt} 次失败），放弃重试")
                raise
            print(
                f"[register] 第 {attempt} 次注册因 outlook 静默拒绝失败；"
                f"池里还有 {avail} 个 available，自动 claim 下一个重试..."
            )
            time.sleep(2)
    if last_err:
        raise last_err
    raise RegistrationError("register 异常终止")


def _register_once(cardw_config_path, proxy=None, python="python3", timeout=600,
                   browser: bool | None = None):
    """Single register implementation (without retry). register() wraps retry logic."""
    if browser is None:
        mode = (os.environ.get("WEBUI_REG_MODE") or "").strip().lower()
        if mode in ("protocol", "http", "api", "auth_flow"):
            browser = False
        elif mode in ("browser", "camoufox", "playwright"):
            browser = True
        else:
            browser = False
    # Note: Previously, this forced the Outlook pool to use browser, which has been removed;
    # auth_flow now handles outlook "account already exists" branch itself (only mark dead after two OTP timeouts),
    # Give real new Outlook a protocol path opportunity, auto-elimination of fake new (OpenAI silent rejection).
    cardw_config_path = str(Path(cardw_config_path).resolve())
    auth_bundle_dir = str(CARDW_DIR)

    # Wave H: After CTF-reg reorganization, mail_provider/auth_flow/browser_register moved into subpackage
    if browser:
        script = r"""
import json, logging, os, sys
auth_bundle_dir = sys.argv[1]
config_path = sys.argv[2]
sys.path.insert(0, auth_bundle_dir)
from config import Config
from mail.provider import MailProvider
from drivers.browser import browser_register
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
cfg = Config.from_file(config_path)
mail = MailProvider(cfg.mail.catch_all_domain)
result = browser_register(cfg, mail)
print("LOCALAUTH_RESULT_JSON=" + json.dumps(result, ensure_ascii=False), flush=True)
"""
    else:
        script = r"""
import json, logging, os, sys
auth_bundle_dir = sys.argv[1]
config_path = sys.argv[2]
sys.path.insert(0, auth_bundle_dir)
from config import Config
from drivers.protocol import AuthFlow
from mail.provider import MailProvider
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
cfg = Config.from_file(config_path)
mail = MailProvider(cfg.mail.catch_all_domain)
flow = AuthFlow(cfg)
result = flow.run_register(mail)
print("LOCALAUTH_RESULT_JSON=" + json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
"""

    env = dict(os.environ)
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)
    if not browser:
        env["WEBUI_REG_MODE"] = "protocol"
        env.setdefault("OPENAI_SENTINEL_REQUIRE_QUICKJS", "1")
    if proxy:
        # Proxy passed via config file, not via environment variables

        pass

    cmd = [python, "-c", script, auth_bundle_dir, cardw_config_path]
    print(f"[register] 注册新账号 (config={os.path.basename(cardw_config_path)}) ...")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env, cwd=str(CARDW_DIR),
    )

    result_json = None
    lines = []
    try:
        deadline = time.time() + timeout
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            print(f"  [reg] {line}")
            if line.startswith("LOCALAUTH_RESULT_JSON="):
                payload = line.split("=", 1)[1]
                result_json = json.loads(payload)
            if time.time() > deadline:
                proc.kill()
                raise RegistrationError("注册超时")
    finally:
        proc.wait()

    if proc.returncode != 0 and result_json is None:
        last_lines = "\n".join(lines[-5:])
        raise RegistrationError(f"注册失败 (exit={proc.returncode}): {last_lines}")

    if result_json is None:
        raise RegistrationError("注册完成但未获取到凭证")

    email = result_json.get("email", "?")
    print(f"[register] 注册成功: {email}")
    try:
        entry = dict(result_json)
        entry["ts"] = datetime.now(timezone.utc).isoformat()
        get_db().add_registered_account(entry)
    except Exception as e:
        print(f"[register] 保存凭证失败: {e}")
    return result_json


# ──────────────────────────────────────────────
# 2. Payment module
# ──────────────────────────────────────────────

class PaymentError(RuntimeError):
    pass


class DatadomeSliderError(PaymentError):
    """PayPal page blocked by DataDome visible slider, need to retry with different IP."""
    pass


def _codex_oauth_client_id_from_card_cfg(cfg: dict) -> str:
    """Resolve Codex OAuth client_id for child card.py processes.

    `card.py` needs a real client_id to finish the post-payment Codex OAuth
    flow and obtain refresh_token.  Prefer the WebUI/CPA field because that is
    where this project already stores the downstream Codex client id.  Literal
    placeholders are ignored.
    """
    if not isinstance(cfg, dict):
        cfg = {}
    cpa_cfg = cfg.get("cpa") or {}
    fresh_cfg = cfg.get("fresh_checkout") or {}
    auth_cfg = fresh_cfg.get("auth") or {}
    for value in (
        (cpa_cfg or {}).get("oauth_client_id", ""),
        cfg.get("oauth_client_id", ""),
        cfg.get("codex_oauth_client_id", ""),
        auth_cfg.get("oauth_client_id", ""),
    ):
        client_id = str(value or "").strip()
        if not client_id:
            continue
        if client_id.startswith("YOUR_") or client_id.endswith("_CLIENT_ID"):
            continue
        return client_id
    return ""


def _cpa_cfg_for_card_payment(card_cfg: dict) -> dict:
    """Return CPA config adjusted to the configured paid product.

    Older WebUI exports used `cpa.plan_tag=team` globally.  GoPay is used here
    for ChatGPT Plus, so importing a Plus account with a `team` suffix is
    misleading downstream.  Unless `auto_plan_tag` is explicitly disabled,
    derive a safer tag from fresh_checkout.plan.
    """
    if not isinstance(card_cfg, dict):
        return {}
    cpa_cfg = dict(card_cfg.get("cpa") or {})
    if not cpa_cfg:
        return cpa_cfg
    if cpa_cfg.get("auto_plan_tag", True) is False:
        return cpa_cfg
    plan_cfg = (card_cfg.get("fresh_checkout") or {}).get("plan") or {}
    plan_name = str(plan_cfg.get("plan_name") or "").lower()
    plan_type = str(plan_cfg.get("plan_type") or "").lower()
    if "plus" in plan_name or plan_type == "plus":
        cpa_cfg["plan_tag"] = (cpa_cfg.get("plus_plan_tag") or "plus").strip() or "plus"
    elif "team" in plan_name or plan_type == "team":
        cpa_cfg["plan_tag"] = (
            cpa_cfg.get("team_plan_tag")
            or cpa_cfg.get("plan_tag")
            or "team"
        ).strip() or "team"
    return cpa_cfg


def pay(card_config_path, session_token=None, access_token=None,
        device_id=None, use_paypal=False, use_gopay=False, use_qris=False,
        gopay_otp_file=None, python="python3", timeout=600):
    """Execute Stripe payment flow.

    use_paypal / use_gopay / use_qris are mutually exclusive: default card, paypal uses PayPal browser,
    gopay uses GoPay tokenization (CTF-pay/gopay.py), qris uses Midtrans QRIS QR code scan
    (CTF-pay/qris.py), QRIS requires no OTP/PIN/binding, user can scan with any Indonesian e-wallet to pay.
    If session_token/access_token provided, will temporarily override credentials in config file.
    gopay_otp_file: webui mode OTP file path (gopay.py file-watch reads it).
    Returns dict: {status, session_id, chatgpt_email, ...}"""
    flags = sum(1 for x in (use_paypal, use_gopay, use_qris) if x)
    if flags > 1:
        raise PaymentError("use_paypal / use_gopay / use_qris 互斥，只能一种")

    card_config_path = str(Path(card_config_path).resolve())
    cfg_for_env = {}

    # webshare/gost keepalive: pay_only / pay_only_targets go directly to pay(), skip pipeline()
    # Also ensure gost is running, otherwise Indonesian payment via socks5://127.0.0.1:18898 direct connect refused
    try:
        _early_card_cfg = _read_card_cfg(card_config_path)
        _ensure_gost_alive(_early_card_cfg)
    except Exception as _e:
        print(f"[pay] gost 保活提前调用失败（不致命，继续）: {_e}")

    # If external credentials exist, create temporary config
    config_to_use = card_config_path
    tmp_config = None
    if session_token or access_token:
        with open(card_config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg_for_env = cfg
        auth = cfg.setdefault("fresh_checkout", {}).setdefault("auth", {})
        auth["mode"] = "access_token"
        if session_token:
            auth["session_token"] = session_token
        if access_token:
            auth["access_token"] = access_token
        if device_id:
            auth["device_id"] = device_id
        auth["prefer_session_refresh"] = True
        # Disable auto_register (credentials already exist)
        auto = auth.get("auto_register", {})
        auto["enabled"] = False
        auth["auto_register"] = auto

        tmp_config = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="pipeline_pay_",
            dir=str(RUNTIME_PAY), delete=False,
        )
        json.dump(cfg, tmp_config, ensure_ascii=False, indent=2)
        tmp_config.close()
        config_to_use = tmp_config.name
    else:
        try:
            with open(card_config_path, "r", encoding="utf-8") as f:
                cfg_for_env = json.load(f)
        except Exception:
            cfg_for_env = {}

    if use_qris:
        # QRIS skips card.py (no hCaptcha / DataDome / cards needed), directly spawn qris.py
        # Wave I: cwd=CARD_DIR + python -m qris, replace [python, str(QRIS_PY)]
        cmd = [python, "-m", "qris",
               "--config", config_to_use, "--json-result"]
        result_marker = "QRIS_RESULT_JSON="
        mode_label = "qris"
    else:
        # Wave I: cwd=CARD_DIR + python -m card auto, replace [python, str(CARD_PY)]
        cmd = [python, "-m", "card", "auto",
               "--config", config_to_use, "--json-result"]
        if use_paypal:
            cmd.append("--paypal")
        elif use_gopay:
            cmd.append("--gopay")
            if gopay_otp_file:
                cmd += ["--gopay-otp-file", str(gopay_otp_file)]
        result_marker = "CARD_RESULT_JSON="
        mode_label = "gopay" if use_gopay else ("paypal" if use_paypal else "card")

    env = dict(os.environ)
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    if not env.get("OAUTH_CODEX_CLIENT_ID"):
        client_id = _codex_oauth_client_id_from_card_cfg(cfg_for_env)
        if client_id:
            env["OAUTH_CODEX_CLIENT_ID"] = client_id

    print(f"[pay] 启动支付 (mode={mode_label}) ...")

    result_json = None
    datadome_slider = False
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, cwd=str(CARD_DIR),  # Wave I: -m qris/card needs cwd=CTF-pay
        )
        deadline = time.time() + timeout
        for line in proc.stdout:
            line = line.rstrip("\n")
            print(f"  [pay] {line}")
            if line.startswith(result_marker):
                payload = line.split("=", 1)[1]
                result_json = json.loads(payload)
            if "CARD_DATADOME_SLIDER=1" in line:
                datadome_slider = True
            if time.time() > deadline:
                proc.kill()
                raise PaymentError("支付超时")
        proc.wait()
    finally:
        if tmp_config and os.path.exists(tmp_config.name):
            os.unlink(tmp_config.name)

    if datadome_slider and (not result_json or result_json.get("state") != "succeeded"):
        raise DatadomeSliderError("PayPal 页面被 DataDome 可见滑块拦截")

    if result_json:
        status = result_json.get("state", "unknown")
        print(f"[pay] 结果: state={status}")
        return {"status": status, "raw": result_json}

    if proc.returncode != 0:
        raise PaymentError(f"支付失败 (exit={proc.returncode})")

    return {"status": "unknown", "raw": None}


# ──────────────────────────────────────────────
# 3. Pipeline scheduling
# ──────────────────────────────────────────────

def pipeline(card_config_path, cardw_config_path=None, use_paypal=False,
             use_gopay=False, use_qris=False, gopay_otp_file=None,
             timeout_reg=300, timeout_pay=600,
             pool=None, team_client=None, card_cfg=None, proxy_pool=None):
    """Full pipeline: register → pay → (optional) gpt-team import probe → update domain pool
    When proxy_pool non-empty, pick proxy from pool, simultaneously override proxy field in both CTF-reg + CTF-pay config"""
    card_config_path = str(Path(card_config_path).resolve())

    if card_cfg is None:
        card_cfg = _read_card_cfg(card_config_path)
    cardw_config_path = _load_cardw_path_from_card_cfg(card_cfg, cardw_config_path)

    # Internal fallback: if external doesn't pass pool/team/proxy, auto-construct anyway (single call scenario)
    owned_pool = False
    if pool is None:
        ts_cfg = card_cfg.get("team_system") or {}
        cd_h = int(ts_cfg.get("domain_cooldown_hours", 24))
        pool = _build_domain_pool_from_cardw(cardw_config_path, cd_h)
        owned_pool = True
    if team_client is None:
        team_client = _build_team_client_from_card_cfg(card_cfg)
    if proxy_pool is None:
        proxy_pool = _build_proxy_pool_from_card_cfg(card_cfg)

    # Pick proxy (independent of domain)
    picked_proxy = proxy_pool.pick() if proxy_pool and proxy_pool.proxies else ""
    if picked_proxy:
        print(f"[ProxyPool] 本次代理: {picked_proxy}")

    # Pick domain + write temporary CTF-reg config (simultaneously override proxy)
    # When pool.domains empty, if provisioner exists still trigger pick() to auto-provision
    picked_domain = pool.pick() if pool and (pool.domains or pool.provisioner) else ""
    temp_cardw = None
    effective_cardw = cardw_config_path
    if picked_domain or picked_proxy:
        temp_cardw = _rewrite_cardw_with_domain(cardw_config_path, picked_domain, picked_proxy)
        effective_cardw = temp_cardw
        if picked_domain:
            pool.mark_used(picked_domain)
            print(f"[DomainPool] 本次使用域: {picked_domain}")

    # CTF-pay also override proxy (payment flow uses same proxy)
    temp_card = None
    effective_card = card_config_path
    if picked_proxy:
        temp_card = _rewrite_card_with_proxy(card_config_path, picked_proxy)
        effective_card = temp_card

    ts = datetime.now(timezone.utc).isoformat()
    record = {"ts": ts, "registration": {}, "payment": {},
              "domain": picked_domain, "proxy": picked_proxy}

    # webshare/gost keepalive: daemon path included, single pipeline() also needs one run,
    # otherwise camoufox startup will hang on public_ip(socks5://127.0.0.1:18898)
    _ensure_gost_alive(card_cfg, team_client)

    try:
        # Step 1: Register
        print(f"\n{'='*60}")
        print(f"[pipeline] Step 1/2: 注册 ChatGPT 账号")
        print(f"{'='*60}")
        try:
            reg = register(effective_cardw, timeout=timeout_reg)
            record["registration"] = {"status": "ok", "email": reg.get("email", "")}
        except RegistrationError as e:
            record["registration"] = {"status": "error", "error": str(e)[:200]}
            record["payment"] = {"status": "skipped"}
            _append_result(record)
            raise

        # Step 2: Pay
        print(f"\n{'='*60}")
        print(f"[pipeline] Step 2/2: Stripe 支付 ({reg.get('email', '?')})")
        print(f"{'='*60}")
        try:
            pay_result = pay(
                effective_card,
                session_token=reg.get("session_token"),
                access_token=reg.get("access_token"),
                device_id=reg.get("device_id", ""),
                use_paypal=use_paypal,
                use_gopay=use_gopay,
                use_qris=use_qris,
                gopay_otp_file=gopay_otp_file,
                timeout=timeout_pay,
            )
            record["payment"] = {
                "status": pay_result.get("status", "unknown"),
                "email": reg.get("email", ""),
            }
        except PaymentError as e:
            record["payment"] = {"status": "error", "email": reg.get("email", ""), "error": str(e)[:200]}
            _append_result(record)
            raise

        # Step 3: Payment success → gpt-team import + invite probe
        pay_status = pay_result.get("status", "unknown")
        if pay_status == "succeeded" and team_client:
            try:
                probe_status = _team_probe_after_payment(
                    {**pay_result, "email": reg.get("email", "")},
                    team_client, pool, picked_domain,
                )
                record["invite_permission"] = probe_status
            except Exception as e:
                print(f"[Team] probe 异常: {e}")
                record["invite_permission"] = "error"

        # Step 4: Payment success → additional import to CPA (CLIProxyAPI)
        cpa_cfg = _cpa_cfg_for_card_payment(card_cfg or {})
        if pay_status == "succeeded" and cpa_cfg.get("enabled"):
            try:
                sid = (pay_result.get("raw") or {}).get("session_id", "") if isinstance(pay_result.get("raw"), dict) else ""
                cpa_status = _cpa_import_after_team(reg.get("email", ""), sid, cpa_cfg)
                record["cpa_import"] = cpa_status
            except Exception as e:
                print(f"[CPA] 导入异常: {e}")
                record["cpa_import"] = "error"

        _append_result(record)
        emoji = "✓" if pay_status == "succeeded" else "✗"
        perm = record.get("invite_permission", "-")
        print(f"\n[pipeline] {emoji} {reg.get('email', '?')} → {pay_status}  invite={perm}")
        return record
    finally:
        if temp_cardw and os.path.exists(temp_cardw):
            try: os.unlink(temp_cardw)
            except Exception: pass
        if temp_card and os.path.exists(temp_card):
            try: os.unlink(temp_card)
            except Exception: pass


def _run_one(args_tuple):
    """Single pipeline task (for parallel scheduling)"""
    idx, card_config_path, kwargs = args_tuple
    try:
        r = pipeline(card_config_path, **kwargs)
        r["batch_index"] = idx
        return r
    except Exception as e:
        return {"batch_index": idx, "status": "error", "error": str(e)[:200]}


def _run_one_pay_only(args_tuple):
    """PayPal concurrent mode: registration already complete, only serial payment (reserved placeholder, not used separately in batch)"""
    return {"batch_index": args_tuple[0], "status": "error", "error": "deprecated path"}


def _register_one(args_tuple):
    """Single register task. args_tuple = (idx, cardw_config_path, pool_or_None)
    When pool non-empty, independently pick domain for each worker + rewrite temporary cardw config."""
    if len(args_tuple) == 3:
        idx, cardw_config_path, pool = args_tuple
    else:
        idx, cardw_config_path = args_tuple
        pool = None
    picked_domain = ""
    temp_cardw = None
    effective = cardw_config_path
    try:
        if pool and pool.domains:
            picked_domain = pool.pick()
            pool.mark_used(picked_domain)
            temp_cardw = _rewrite_cardw_with_domain(cardw_config_path, picked_domain)
            effective = temp_cardw
        r = register(effective)
        return {"index": idx, "status": "ok", "picked_domain": picked_domain, **r}
    except Exception as e:
        return {"index": idx, "status": "error", "picked_domain": picked_domain, "error": str(e)[:200]}
    finally:
        if temp_cardw and os.path.exists(temp_cardw):
            try: os.unlink(temp_cardw)
            except Exception: pass


def batch(card_config_path, count, delay=30, workers=1, **kwargs):
    """Batch run N times. Optional modifier:
       - register_only=True: each time only register (no payment), workers serial
       - pay_only=True:      each time only pay_only (reuse unpaid accounts), workers serial
       - neither enabled:     each time run full pipeline (register+pay)"""
    use_paypal = kwargs.get("use_paypal", False)
    is_register_only = bool(kwargs.pop("register_only", False))
    is_pay_only = bool(kwargs.pop("pay_only", False))
    use_gopay = bool(kwargs.pop("use_gopay", False))
    use_qris = bool(kwargs.pop("use_qris", False))
    gopay_otp_file = kwargs.pop("gopay_otp_file", "")

    # Construct shared pool + team_client (all workers reuse)
    card_cfg = _read_card_cfg(card_config_path)
    cardw_path = _load_cardw_path_from_card_cfg(card_cfg, kwargs.get("cardw_config_path"))

    # ── register-only batch: each register, serial (avoid parallel same IP triggering risk control)
    if is_register_only:
        if not cardw_path:
            print("[batch:register-only] 缺 cardw_config_path", file=sys.stderr)
            sys.exit(2)
        print(f"\n[batch] === register-only × {count} 串行 ===")
        results = []
        ok_count = 0
        for i in range(count):
            print(f"\n{'#'*60}\n# 批次 {i+1}/{count}  (register-only)\n{'#'*60}")
            try:
                r = register(cardw_path)
                r["batch_index"] = i
                if r.get("status") == "ok":
                    ok_count += 1
            except Exception as e:
                r = {"batch_index": i, "status": "error", "error": str(e)[:200]}
                print(f"[batch] ✗ 注册异常: {e}")
            results.append(r)
            print(f"[batch] 进度 {i+1}/{count}  累计 ok={ok_count}")
            if i < count - 1 and delay > 0:
                time.sleep(delay)
        print(f"\n[batch] register-only 完成: {ok_count}/{count} 成功")
        return results

    # ── pay-only batch: each pay_only (reuse unpaid accounts), serial
    if is_pay_only:
        print(f"\n[batch] === pay-only × {count} 串行 ===")
        results = []
        ok_count = 0
        for i in range(count):
            print(f"\n{'#'*60}\n# 批次 {i+1}/{count}  (pay-only)\n{'#'*60}")
            try:
                r = pay_only(
                    card_config_path,
                    use_paypal=use_paypal, use_gopay=use_gopay, use_qris=use_qris,
                    gopay_otp_file=gopay_otp_file,
                )
                r["batch_index"] = i
                if r.get("status") == "succeeded":
                    ok_count += 1
            except Exception as e:
                r = {"batch_index": i, "status": "error", "error": str(e)[:200]}
                print(f"[batch] ✗ 支付异常: {e}")
            results.append(r)
            print(f"[batch] 进度 {i+1}/{count}  累计 ok={ok_count}")
            if i < count - 1 and delay > 0:
                time.sleep(delay)
        print(f"\n[batch] pay-only 完成: {ok_count}/{count} 成功")
        return results
    ts_cfg = card_cfg.get("team_system") or {}
    cd_h = int(ts_cfg.get("domain_cooldown_hours", 24))
    pool = _build_domain_pool_from_cardw(cardw_path, cd_h)
    team_client = _build_team_client_from_card_cfg(card_cfg)
    proxy_pool = _build_proxy_pool_from_card_cfg(card_cfg)
    if pool.domains:
        print(f"[DomainPool] 域池大小={len(pool.domains)}  cooldown={cd_h}h")
        for d, st, lr in pool.summary():
            print(f"   - {d:40s} status={st:8s} last={lr}")
    if team_client:
        print(f"[Team] 端点: {ts_cfg.get('base_url')}  user={ts_cfg.get('username')}")
    if proxy_pool.proxies:
        print(f"[ProxyPool] 代理池大小={len(proxy_pool.proxies)}  rotation={proxy_pool.rotation}")
        for p in proxy_pool.proxies:
            print(f"   - {p}")
    kwargs.setdefault("pool", pool)
    kwargs.setdefault("team_client", team_client)
    kwargs.setdefault("card_cfg", card_cfg)
    kwargs.setdefault("proxy_pool", proxy_pool)

    if workers > 1 and use_paypal:
        # PayPal mode: parallel register → serial pay (shared PayPal account cannot parallel 2FA)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        cardw_cfg = cardw_path

        print(f"\n[batch] === 阶段 1: 并行注册 ({workers} workers × {count} 账号) ===")
        reg_tasks = [(i, cardw_cfg, pool) for i in range(count)]
        accounts = [None] * count
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_register_one, t): t[0] for t in reg_tasks}
            for future in as_completed(futures):
                idx = futures[future]
                accounts[idx] = future.result()
                r = accounts[idx]
                mark = "✓" if r["status"] == "ok" else "✗"
                email = r.get("email", "?")
                dom = r.get("picked_domain", "")
                done = sum(1 for a in accounts if a)
                print(f"  {mark} [{done}/{count}] {email}  domain={dom}")

        reg_ok = [a for a in accounts if a and a["status"] == "ok"]
        print(f"\n[batch] 注册完成: {len(reg_ok)}/{count} 成功")

        # Step 2: Serial pay + team probe
        print(f"\n[batch] === 阶段 2: 串行支付 ({len(reg_ok)} 账号) ===")
        results = []
        for i, acc in enumerate(reg_ok):
            print(f"\n{'─'*40}")
            print(f"[batch] 支付 {i+1}/{len(reg_ok)}: {acc['email']}")
            picked_domain = acc.get("picked_domain", "")
            invite_perm = "-"
            try:
                pay_result = pay(
                    card_config_path,
                    session_token=acc.get("session_token"),
                    access_token=acc.get("access_token"),
                    device_id=acc.get("device_id", ""),
                    use_paypal=True,
                )
                record = {
                    "registration": {"status": "ok", "email": acc["email"]},
                    "payment": {"status": pay_result.get("status", "unknown"), "email": acc["email"]},
                    "domain": picked_domain,
                }
                if pay_result.get("status") == "succeeded" and team_client:
                    try:
                        invite_perm = _team_probe_after_payment(
                            {**pay_result, "email": acc["email"]},
                            team_client, pool, picked_domain,
                        )
                        record["invite_permission"] = invite_perm
                    except Exception as e:
                        print(f"[Team] probe 异常: {e}")
                        record["invite_permission"] = "error"
            except Exception as e:
                record = {
                    "registration": {"status": "ok", "email": acc["email"]},
                    "payment": {"status": "error", "email": acc["email"], "error": str(e)[:200]},
                    "domain": picked_domain,
                }
            record["batch_index"] = i
            _append_result(record)
            results.append(record)
            status = record["payment"]["status"]
            mark = "✓" if status == "succeeded" else "✗"
            ok_so_far = sum(1 for r in results if r["payment"]["status"] == "succeeded")
            perm = record.get("invite_permission", "-")
            print(f"  {mark} {acc['email']} → {status}  invite={perm}  (累计 {ok_so_far}/{len(results)})")
            if i < len(reg_ok) - 1:
                time.sleep(delay)

    elif workers <= 1:
        # Full serial
        results = []
        for i in range(count):
            print(f"\n{'#'*60}")
            print(f"# 批次 {i + 1}/{count}")
            print(f"{'#'*60}")
            results.append(_run_one((i, card_config_path, kwargs)))
            if i < count - 1 and delay > 0:
                time.sleep(delay)
    else:
        # Non-PayPal: full parallel
        from concurrent.futures import ThreadPoolExecutor, as_completed
        print(f"\n[batch] 并行模式: {workers} workers × {count} 任务")
        tasks = [(i, card_config_path, kwargs) for i in range(count)]
        results = [None] * count
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_run_one, t): t[0] for t in tasks}
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()

    # Summary
    print(f"\n{'='*60}")
    print(f"批量结果汇总 ({count} 次, workers={workers})")
    print(f"{'='*60}")
    ok = sum(1 for r in results if r and r.get("payment", {}).get("status") == "succeeded")
    inv_ok = sum(1 for r in results if r and r.get("invite_permission") == "ok")
    inv_no = sum(1 for r in results if r and r.get("invite_permission") == "no_permission")
    fail = len(results) - ok
    print(f"  支付成功: {ok}  失败: {fail}  |  invite=ok: {inv_ok}  invite=no: {inv_no}")
    for r in (results or []):
        if not r:
            continue
        idx = r.get("batch_index", "?")
        email = r.get("registration", {}).get("email") or r.get("payment", {}).get("email", "?")
        status = r.get("payment", {}).get("status", r.get("status", "?"))
        perm = r.get("invite_permission", "-")
        dom = r.get("domain", "-")
        print(f"  [{idx}] {email:40s} → {status:10s} invite={perm:13s} domain={dom}")

    if pool and pool.domains:
        print(f"\n[DomainPool] 最终状态:")
        for d, st, lr in pool.summary():
            print(f"   - {d:40s} status={st:8s} last={lr}")
    return results


# ──────────────────────────────────────────────
# Utility functions
# ──────────────────────────────────────────────

def _append_result(record):
    try:
        get_db().add_pipeline_result(record)
    except Exception:
        pass


def _norm_email(value: str) -> str:
    return str(value or "").strip().lower()


def _paid_or_consumed_emails() -> set[str]:
    """Emails that should not be retried by --pay-only.

    Normal full pipeline writes registration/payment state to SQLite, while
    `card.py` writes terminal payment records to the same DB.  Some
    payment errors happen before card.py can recover the email, so we consult
    both streams.  `User is already paid` is treated as consumed even when the
    state is recorded as error: retrying the same account would only loop.
    """
    consumed: set[str] = set()

    for d in get_db().iter_pipeline_results():
        pay_block = d.get("payment") if isinstance(d.get("payment"), dict) else {}
        reg_block = d.get("registration") if isinstance(d.get("registration"), dict) else {}
        status = str(pay_block.get("status") or d.get("status") or "").lower()
        email = _norm_email(
            pay_block.get("email")
            or reg_block.get("email")
            or d.get("chatgpt_email")
            or d.get("email")
        )
        err = str(pay_block.get("error") or d.get("error") or "")
        if email and (status == "succeeded" or "user is already paid" in err.lower()):
            consumed.add(email)

    for d in get_db().iter_card_results():
        status = str(d.get("status") or "").lower()
        email = _norm_email(d.get("chatgpt_email") or d.get("email"))
        err = str(d.get("error") or "")
        if email and (status == "succeeded" or "user is already paid" in err.lower()):
            consumed.add(email)

    return consumed


def _select_recent_registered_account_for_pay_only() -> dict | None:
    """Pick the newest registered account that has not already succeeded.

    This makes `--pay-only` useful for the common case where registration
    completed but payment was blocked by captcha/OTP/DataDome/etc.  The selected
    account is returned with its original session/access/device credentials.
    """
    accounts = _load_registered_accounts()
    if not accounts:
        return None

    consumed = _paid_or_consumed_emails()
    seen: set[str] = set()
    for acc in reversed(accounts):
        if not isinstance(acc, dict):
            continue
        email = _norm_email(acc.get("email"))
        if not email or email in seen:
            continue
        seen.add(email)
        if email in consumed:
            continue
        if not (acc.get("session_token") or acc.get("access_token")):
            continue
        selected = dict(acc)
        selected["email"] = email
        return selected
    return None


def pay_only(card_config_path, *, use_paypal=False, use_gopay=False, use_qris=False,
             gopay_otp_file=None, timeout_pay=600, prefer_recent=True,
             target_email: str = ""):
    """Retry payment only.

    Default behavior is now:
      1. use the newest account in SQLite storage that has not
         already succeeded/been consumed;
      2. fall back to credentials embedded in the payment config if no reusable
         account exists.

    This preserves the old config-token path while preventing freshly
    registered-but-unpaid accounts from being wasted.
    """
    if target_email:
        # Explicitly specify account — do not go through consumed filter, allow user operations on selected rows (even if previously paid)
        # Also allow retry for convenience in testing. Fall back to normal logic if unable to read.
        target_norm = _norm_email(target_email)
        row = get_db().find_latest_registered_account(target_norm) or None
        if row:
            account = row
            print(f"[pay-only] 使用指定账号: {target_norm}")
        else:
            print(f"[pay-only] ⚠ 指定账号 {target_norm} 在 DB 没找到，回退默认逻辑")
            account = _select_recent_registered_account_for_pay_only() if prefer_recent else None
    else:
        account = _select_recent_registered_account_for_pay_only() if prefer_recent else None
    email = _norm_email(account.get("email")) if account else ""
    try:
        card_cfg = _read_card_cfg(card_config_path)
    except Exception:
        card_cfg = {}
    if account:
        print(
            "[pay-only] 复用最近未支付注册账号: "
            f"{email} "
            f"session_token={'yes' if account.get('session_token') else 'no'} "
            f"access_token={'yes' if account.get('access_token') else 'no'} "
            f"device_id={'yes' if account.get('device_id') else 'no'}"
        )
    else:
        print("[pay-only] 未找到可复用注册账号，回退使用 config 里的 session_token/access_token")

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "pay_only",
        "registration": {"status": "reused" if account else "config", "email": email},
        "payment": {},
        "domain": email.split("@", 1)[1] if "@" in email else "",
        "proxy": "",
    }
    try:
        result = pay(
            card_config_path,
            session_token=account.get("session_token") if account else None,
            access_token=account.get("access_token") if account else None,
            device_id=account.get("device_id", "") if account else None,
            use_paypal=use_paypal,
            use_gopay=use_gopay,
            use_qris=use_qris,
            gopay_otp_file=gopay_otp_file,
            timeout=timeout_pay,
        )
        status = result.get("status", "unknown")
        raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
        pay_email = _norm_email(email or raw.get("chatgpt_email") or raw.get("email"))
        record["payment"] = {"status": status, "email": pay_email}
        cpa_cfg = _cpa_cfg_for_card_payment(card_cfg or {})
        if status == "succeeded" and cpa_cfg.get("enabled"):
            try:
                sid = raw.get("session_id", "") if isinstance(raw, dict) else ""
                cpa_status = _cpa_import_after_team(pay_email, sid, cpa_cfg)
                record["cpa_import"] = cpa_status
            except Exception as e:
                print(f"[CPA] 导入异常: {e}")
                record["cpa_import"] = "error"
        _append_result(record)
        return result
    except PaymentError as e:
        record["payment"] = {"status": "error", "email": email, "error": str(e)[:200]}
        _append_result(record)
        raise


# ──────────────────────────────────────────────
# RT-only: supplement refresh_token capture for already-registered accounts (no payment)
# ──────────────────────────────────────────────


def rt_only_for_email(card_config_path: str, target_email: str) -> dict:
    """Run RT exchange for individual email: use existing password/session from DB via Codex OAuth to get refresh_token, write back to registered_accounts. No payment, no account plan change."""
    target = _norm_email(target_email)
    if not target:
        return {"status": "no_email"}

    account = get_db().find_latest_registered_account(target) or {}
    if not account:
        print(f"[rt-only] ⚠ DB 找不到账号: {target}")
        return {"status": "no_account", "email": target}

    if account.get("refresh_token"):
        print(f"[rt-only] {target} 已有 refresh_token (len={len(account['refresh_token'])}), 跳过")
        return {"status": "already_has_rt", "email": target}

    # Actual RT exchange is routed via _exchange_refresh_token_dispatch by WEBUI_REG_MODE
    # (protocol → AuthFlow.run_protocol_login, otherwise Camoufox), but proxy/oauth_client_id
    # parsing helper is still only in card.py.
    sys.path.insert(0, str(CARD_DIR))
    try:
        from card import (
            _build_proxy_url_from_cfg,
            _codex_oauth_client_id_from_config,
        )
    except Exception as e:
        print(f"[rt-only] import card.py 失败: {e}")
        return {"status": "import_failed", "error": str(e)[:200]}
    finally:
        try: sys.path.remove(str(CARD_DIR))
        except ValueError: pass

    try:
        with open(card_config_path, "r", encoding="utf-8") as f:
            card_cfg = json.load(f)
    except Exception as e:
        return {"status": "read_card_cfg_failed", "error": str(e)[:200]}

    mail_cfg = {}
    reg_cfg_path = ROOT / "CTF-reg" / "config.paypal-proxy.json"
    if reg_cfg_path.exists():
        try:
            with open(reg_cfg_path, "r", encoding="utf-8") as f:
                mail_cfg = (json.load(f).get("mail") or {})
        except Exception:
            mail_cfg = {}

    if not mail_cfg:
        print(f"[rt-only] 缺 mail_cfg（{reg_cfg_path}），无法接收 OTP")
        return {"status": "no_mail_cfg", "email": target}

    print(f"[rt-only] 启动 Codex OAuth → email={target} password={'有' if account.get('password') else '无(passwordless)'}")
    try:
        rt = _exchange_refresh_token_dispatch(
            email=target,
            password=account.get("password", "") or "",
            mail_cfg=mail_cfg,
            proxy_url=_build_proxy_url_from_cfg(card_cfg.get("proxy")),
            oauth_client_id=_codex_oauth_client_id_from_config(card_cfg),
        )
    except Exception as e:
        print(f"[rt-only] 异常: {type(e).__name__}: {str(e)[:200]}")
        return {"status": "exception", "error": str(e)[:200], "email": target}

    if not rt:
        print(f"[rt-only] ❌ {target} 未获得 refresh_token")
        return {"status": "no_rt", "email": target}

    # Write back to DB. Note: find_latest_registered_account()'s SELECT does not return id field,
    # so account['id'] is empty. Here directly query the latest row's id by email then UPDATE.
    try:
        db = get_db()
        with db._conn() as c:
            row = c.execute(
                "SELECT id FROM registered_accounts WHERE email = ? "
                "ORDER BY id DESC LIMIT 1",
                (target,),
            ).fetchone()
            if not row:
                print(f"[rt-only] 拿到 RT 但找不到 {target} 在 DB 的行（被删了？）")
                return {"status": "row_gone", "email": target}
            row_id = int(row["id"])
            cur = c.execute(
                "UPDATE registered_accounts SET refresh_token = ? WHERE id = ?",
                (rt, row_id),
            )
            updated = cur.rowcount
        if updated < 1:
            print(f"[rt-only] UPDATE 0 行（id={row_id}），写库未生效")
            return {"status": "update_zero", "email": target, "id": row_id}
        print(f"[rt-only] ✅ {target} refresh_token 已写库 (len={len(rt)} id={row_id})")
        return {"status": "succeeded", "email": target, "refresh_token_len": len(rt), "id": row_id}
    except Exception as e:
        print(f"[rt-only] 拿到 RT 但写库失败: {e}")
        return {"status": "write_failed", "email": target, "error": str(e)[:200]}


def rt_only_targets(card_config_path: str, target_emails: list[str]) -> dict:
    """Batch RT-only: run each email serially, summarize results."""
    results = []
    ok = 0
    skip = 0
    fail = 0
    for em in target_emails:
        em = (em or "").strip()
        if not em:
            continue
        r = rt_only_for_email(card_config_path, em)
        results.append(r)
        st = r.get("status", "")
        if st == "succeeded":
            ok += 1
        elif st in ("already_has_rt",):
            skip += 1
        else:
            fail += 1
    print(f"\n[rt-only] 完成: ok={ok} skip={skip} fail={fail} 共 {len(results)}")
    return {"results": results, "ok": ok, "skip": skip, "fail": fail}


def pay_only_targets(card_config_path: str, target_emails: list[str], *,
                     use_paypal=False, use_gopay=False, use_qris=False,
                     gopay_otp_file=None) -> dict:
    """Batch pay-only: run payment for specified email list one by one."""
    results = []
    ok = 0
    fail = 0
    for em in target_emails:
        em = (em or "").strip()
        if not em:
            continue
        try:
            r = pay_only(
                card_config_path,
                use_paypal=use_paypal,
                use_gopay=use_gopay,
                use_qris=use_qris,
                gopay_otp_file=gopay_otp_file,
                target_email=em,
            )
            results.append({"email": em, "result": r})
            if (r or {}).get("status") == "succeeded":
                ok += 1
            else:
                fail += 1
        except Exception as e:
            print(f"[pay-only-targets] {em} 异常: {e}")
            results.append({"email": em, "status": "error", "error": str(e)[:200]})
            fail += 1
    print(f"\n[pay-only-targets] 完成: ok={ok} fail={fail} 共 {len(results)}")
    return {"results": results, "ok": ok, "fail": fail}


# ──────────────────────────────────────────────
# free_only mode helpers (OAuth state management + failure classification)
# ──────────────────────────────────────────────

_OAUTH_TRANSIENT_COOLDOWN_S = 6 * 3600  # transient_failed 6h cooldown


def _load_oauth_status_map() -> dict:
    try:
        return get_db().load_oauth_status_map()
    except Exception:
        return {}


def _save_oauth_status_map(m: dict) -> None:
    """Persist an oauth status map to SQLite.

    Kept for internal callers that operate on the whole map; runtime state is
    not exported to JSON.
    """
    try:
        for email, row in (m or {}).items():
            if isinstance(row, dict):
                get_db().set_oauth_status(
                    email,
                    str(row.get("status") or ""),
                    str(row.get("fail_reason") or ""),
                    str(row.get("ts") or ""),
                )
    except Exception as e:
        print(f"[free] 保存 oauth status 失败: {e}")


def _set_account_oauth_status(email: str, status: str, fail_reason: str = "") -> None:
    """status: pending / succeeded / dead / transient_failed"""
    if not email:
        return
    ts = datetime.now(timezone.utc).isoformat()
    try:
        get_db().set_oauth_status(email, status, fail_reason, ts)
    except Exception:
        pass


def _get_account_oauth_status(email: str):
    if not email:
        return None
    try:
        return _load_oauth_status_map().get(email.lower())
    except Exception:
        return None


def _should_skip_oauth_account(email: str) -> bool:
    """True = skip; succeeded/dead always skip, transient_failed skips within 6h cooldown."""
    s = _get_account_oauth_status(email)
    if not s:
        return False
    status = s.get("status", "")
    if status in ("succeeded", "dead"):
        return True
    if status == "transient_failed":
        try:
            t = datetime.fromisoformat(s.get("ts", ""))
            return (datetime.now(timezone.utc) - t).total_seconds() < _OAUTH_TRANSIENT_COOLDOWN_S
        except Exception:
            return False
    return False


def _load_registered_accounts() -> list:
    try:
        return get_db().iter_registered_accounts()
    except Exception:
        return []


def _find_latest_registered_account_for_email(email: str) -> dict:
    """Return the newest registered-account row for `email`, if any."""
    return get_db().find_latest_registered_account(email)


def _password_from_email(email: str) -> str:
    """Consistent with browser_register: local + domain (including dots); < 8 chars append 2026OpenAI."""
    p = (email or "").replace("@", "")
    if len(p) < 8:
        p = f"{p}2026OpenAI"
    return p


def _classify_oauth_failure(log: str) -> str:
    """Infer failure reason from print log of _exchange_refresh_token_with_session.

    Priority: account_dead > add_phone_blocked > otp_timeout >
    consent_failed > no_callback > unknown"""
    if not log:
        return "unknown"
    low = log.lower()
    if "invalid_grant" in log or "no longer exists" in low or "doesn't exist" in low:
        return "account_dead"
    if "/add-phone" in log and "[RT] consent" not in log:
        return "add_phone_blocked"
    if (
        "CF KV 等 OTP 超时" in log
        or "OTP 获取超时" in log
        or ("OTP" in log and "超时" in log)
    ):
        return "otp_timeout"
    if "未捕获到 callback URL" in log or "callback 无 code" in log:
        return "no_callback"
    # OpenAI rejects OAuth authorize parameters (most common: client_id placeholder / expired token)
    if "AuthApiFailure" in log or "auth.openai.com/error?payload=" in log:
        return "auth_api_failure"
    if "consent" in log and "code=" not in log and "[RT] callback 无" not in log:
        return "consent_failed"
    return "unknown"


def _rt_mode_is_protocol() -> bool:
    """When WEBUI selects pure protocol, RT supplementation must also go through AuthFlow, cannot silently fall back to Camoufox."""
    mode = (os.environ.get("WEBUI_REG_MODE") or "").strip().lower()
    return mode in ("protocol", "http", "api", "auth_flow")


def _exchange_refresh_token_dispatch(
    email: str, password: str, mail_cfg: dict,
    proxy_url: str = "", oauth_client_id: str = "",
) -> str:
    """Choose RT exchange path by WEBUI_REG_MODE.

    - protocol mode: exchange_refresh_token_protocol from CTF-reg/drivers/protocol.py
      (sentinel + email OTP + Codex OAuth, all self.session.get/post). Failure raises,
      no fallback to Camoufox.
    - otherwise: card._exchange_refresh_token_with_session (Camoufox real browser).

    Both ends have consistent signatures, pipeline call point unified via this helper."""
    if _rt_mode_is_protocol():
        if str(CARDW_DIR) not in sys.path:
            sys.path.insert(0, str(CARDW_DIR))
        from drivers.protocol import exchange_refresh_token_protocol
        print(f"[rt] WEBUI_REG_MODE=protocol → 纯协议 RT 重登 email={email}")
        return exchange_refresh_token_protocol(
            email=email, password=password, mail_cfg=mail_cfg,
            proxy_url=proxy_url, oauth_client_id=oauth_client_id,
        )
    if str(CARDW_DIR) not in sys.path:
        sys.path.insert(0, str(CARDW_DIR))
    if str(CARD_DIR) not in sys.path:
        sys.path.insert(0, str(CARD_DIR))
    import card as card_mod  # noqa: E402
    return card_mod._exchange_refresh_token_with_session(
        email=email, password=password, mail_cfg=mail_cfg,
        proxy_url=proxy_url, oauth_client_id=oauth_client_id,
    )


def _exchange_rt_with_classification(
    email: str, password: str, mail_cfg: dict, proxy_url: str
):
    """Wrap _exchange_refresh_token_dispatch with failure classification.

    Return (rt, fail_reason): when rt is non-empty, fail_reason is empty string.
    print output tees to real stdout (webui runner can see progress) + buffered (for grep classification).

    Path selection by _exchange_refresh_token_dispatch per WEBUI_REG_MODE."""
    import io

    # card.py internally does from mail.cf_kv import ... (before Wave H: cf_kv_otp_provider)
    # Must ensure both directories are in sys.path, otherwise OTP path ImportError returns "" immediately.
    if str(CARDW_DIR) not in sys.path:
        sys.path.insert(0, str(CARDW_DIR))
    if str(CARD_DIR) not in sys.path:
        sys.path.insert(0, str(CARD_DIR))

    class _Tee(io.TextIOBase):
        def __init__(self, buf, real):
            self.buf = buf
            self.real = real

        def write(self, s):
            try:
                self.buf.write(s)
            except Exception:
                pass
            return self.real.write(s)

        def flush(self):
            try:
                self.real.flush()
            except Exception:
                pass

    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = _Tee(buf, real_stdout)
    rt = ""
    try:
        try:
            rt = _exchange_refresh_token_dispatch(
                email=email,
                password=password,
                mail_cfg=mail_cfg,
                proxy_url=proxy_url,
            )
        except Exception as e:
            print(f"[free] _exchange_rt 异常: {e}")
            return "", "exception"
    finally:
        sys.stdout = real_stdout

    log = buf.getvalue()
    if rt:
        return rt, ""
    return "", _classify_oauth_failure(log)


def _read_card_cfg(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_cardw_path_from_card_cfg(card_cfg, fallback_cardw=None):
    if fallback_cardw:
        return str(Path(fallback_cardw).resolve())
    p = (card_cfg.get("fresh_checkout", {}).get("auth", {})
                 .get("auto_register", {}).get("config_path", ""))
    return str(Path(p).resolve()) if p else str(CARDW_DIR / "config.noproxy.json")


def _load_secrets():
    """Read sensitive credentials like Cloudflare/KV from SQLite runtime library."""
    try:
        data = get_db().get_runtime_json(SECRETS_KEY, {})
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[secrets] 读数据库失败: {e}")
        return {}


# ──────────────────────────────────────────────
# Daemon: continuously maintain available account count for gpt-team system
# ──────────────────────────────────────────────

def _cleanup_temp_leftovers(max_age_s: int = 1800, verbose: bool = False) -> dict:
    """Clean orphan temp files to prevent /tmp (tmpfs) from being blown up by SIGKILL remnants.
    Only clean files with mtime > max_age_s:
    - /tmp/chatgpt_reg_*        (Camoufox registration temp profile)
    - /tmp/xvfb-run.*           (xvfb-run auth directory, must have no Xvfb references)
    - CTF-pay/.runtime/pipeline_*.json   (pay temp config, contains pay/plan/px)
    - CTF-reg/.runtime/pipeline_cardw_*.json (register temp config)"""
    now = time.time()
    stats = {"profiles": 0, "xvfb": 0, "cfg_pay": 0, "cfg_cardw": 0, "bytes": 0}

    # 1. Camoufox profile dirs
    try:
        for p in Path("/tmp").glob("chatgpt_reg_*"):
            if not p.is_dir():
                continue
            try:
                if now - p.stat().st_mtime < max_age_s:
                    continue
                size = 0
                for f in p.rglob("*"):
                    try:
                        if f.is_file():
                            size += f.stat().st_size
                    except Exception:
                        pass
                shutil.rmtree(p, ignore_errors=True)
                stats["profiles"] += 1
                stats["bytes"] += size
            except Exception:
                pass
    except Exception:
        pass

    # 2. xvfb-run.* directories (preserve those with active Xvfb references)
    active_auth_dirs = set()
    try:
        out = subprocess.check_output(["pgrep", "-af", "Xvfb"], text=True)
        for line in out.splitlines():
            if "-auth " in line:
                auth_path = line.split("-auth ", 1)[1].split()[0]
                active_auth_dirs.add(str(Path(auth_path).parent))
    except Exception:
        pass
    try:
        for d in Path("/tmp").glob("xvfb-run.*"):
            if str(d) in active_auth_dirs or not d.is_dir():
                continue
            try:
                if now - d.stat().st_mtime < max_age_s:
                    continue
                shutil.rmtree(d, ignore_errors=True)
                stats["xvfb"] += 1
            except Exception:
                pass
    except Exception:
        pass

    # 3. in-project temp configs (temp json produced by pipeline)
    for base, pattern, key in [(RUNTIME_PAY, "pipeline_*.json", "cfg_pay"),
                                 (RUNTIME_REG, "pipeline_cardw_*.json", "cfg_cardw")]:
        try:
            for p in base.glob(pattern):
                try:
                    if now - p.stat().st_mtime < max_age_s:
                        continue
                    p.unlink()
                    stats[key] += 1
                except Exception:
                    pass
        except Exception:
            pass

    touched = stats["profiles"] + stats["xvfb"] + stats["cfg_pay"] + stats["cfg_cardw"]
    if touched and verbose:
        print(f"[cleanup] profile={stats['profiles']}  xvfb={stats['xvfb']}  "
              f"cfg_pay={stats['cfg_pay']}  cfg_cardw={stats['cfg_cardw']}  "
              f"释放 {stats['bytes']/1024/1024:.0f}MB")
    return stats


def _cleanup_dead_cf_subdomains(provisioner, gpt_team_db_path: str,
                                  dry_run: bool = False) -> dict:
    """CF zone record quota cleanup: for all is_banned/is_disabled/expired accounts in gpt-team DB,
    if their email subdomains have no active accounts in DB, delete those subdomains' DNS records on CF.
    Each subdomain frees ~4 records (3 MX + 1 TXT/SPF). Return stats dict."""
    import sqlite3
    from collections import defaultdict
    stats = {"checked": 0, "dead_subs": 0, "cleaned_subs": 0, "records_removed": 0,
             "errors": 0}
    if not provisioner:
        return stats
    if not gpt_team_db_path or not os.path.exists(gpt_team_db_path):
        print(f"[CF-cleanup] 跳过：gpt-team DB 不存在 ({gpt_team_db_path})")
        return stats
    # Current provisioner's covered zone (single zone uses zone_name attribute; multiple zones use zone_names)
    zones = []
    if hasattr(provisioner, "zone_names"):
        zones = [z.lower() for z in provisioner.zone_names]
    elif hasattr(provisioner, "zone_name"):
        zn = (provisioner.zone_name or "").lower()
        if "," in zn:
            zones = [z.strip() for z in zn.split(",") if z.strip()]
        elif zn:
            zones = [zn]
    if not zones:
        return stats

    try:
        conn = sqlite3.connect(gpt_team_db_path)
    except Exception as e:
        print(f"[CF-cleanup] 连 gpt-team DB 失败: {e}")
        return stats
    by_sub = defaultdict(lambda: {"alive": 0, "dead": 0})
    try:
        # dead determination: banned / disabled / expired / no_invite_permission
        q = """SELECT email, is_banned, is_disabled,
               CASE WHEN expire_at IS NULL OR expire_at=''
                 OR DATETIME(REPLACE(expire_at,'/','-')) < DATETIME('now','localtime')
               THEN 1 ELSE 0 END AS expired,
               COALESCE(no_invite_permission, 0) AS no_perm
               FROM gpt_accounts"""
        for email, banned, disabled, expired, no_perm in conn.execute(q):
            if not email or "@" not in email:
                continue
            sub = email.split("@", 1)[1].lower()
            # Must be subdomain (strictly endswith "." + zone), never touch apex root domain itself
            if not any(sub.endswith("." + z) for z in zones):
                continue
            if sub in {z.lower() for z in zones}:  # double insurance
                continue
            stats["checked"] += 1
            # no_invite_permission accounts are useless for business, treat as dead
            if banned or disabled or expired or no_perm:
                by_sub[sub]["dead"] += 1
            else:
                by_sub[sub]["alive"] += 1
    except Exception as e:
        print(f"[CF-cleanup] 查 DB 失败: {e}")
        return stats
    finally:
        conn.close()

    # 1. All subdomains for all DB accounts are dead → cleanup
    fully_dead_db = set(s for s, v in by_sub.items() if v["alive"] == 0 and v["dead"] > 0)
    # 2. MX exists on CF but no corresponding accounts at all in DB for orphan subdomains → also cleanup
    cf_subs = set()
    try:
        cf_subs = {s.lower() for s in provisioner.list_subdomains()}
    except Exception as e:
        print(f"[CF-cleanup] 列 CF 子域失败: {e}")
    db_subs = set(by_sub.keys())
    orphan_subs = {s for s in cf_subs if s not in db_subs and any(
        s.endswith("." + z) for z in zones)}
    stats["orphan_subs"] = len(orphan_subs)

    to_clean = fully_dead_db | orphan_subs
    stats["dead_subs"] = len(fully_dead_db)
    if not to_clean:
        return stats
    if dry_run:
        print(f"[CF-cleanup] dry-run: DB 死透 {len(fully_dead_db)} + CF 孤儿 {len(orphan_subs)} = "
              f"共 {len(to_clean)} 个子域可清")
        return stats
    for sub in to_clean:
        try:
            n = provisioner.delete_subdomain(sub)
            stats["cleaned_subs"] += 1
            stats["records_removed"] += int(n or 0)
        except Exception:
            stats["errors"] += 1
    print(f"[CF-cleanup] DB 死透 {stats['dead_subs']} + CF 孤儿 {stats['orphan_subs']} "
          f"→ 清 {stats['cleaned_subs']} 个，释放 {stats['records_removed']} 条 records"
          + (f"，失败 {stats['errors']}" if stats["errors"] else ""))
    return stats


def daemon(card_config_path, cardw_config_path=None, use_paypal=False):
    """State machine: continuously maintain available invite account count in gpt-team system ≥ target_ok_accounts.
    - Available defined: isOpen & !isBanned & !isDisabled & !noInvitePermission & seat not full
    - Rate limiting: per hour / per day limit (avoid triggering risk control in short time batch)
    - Protection: consecutive failures ≥ max_consecutive_failures triggers consecutive_fail_cooldown_s cooldown
    - State persistence: SQLite runtime_meta[daemon_state]
    - Graceful shutdown: SIGINT/SIGTERM stops after completing current loop"""
    import signal
    card_cfg = _read_card_cfg(card_config_path)
    cardw_path = _load_cardw_path_from_card_cfg(card_cfg, cardw_config_path)

    d_cfg = card_cfg.get("daemon") or {}
    target = int(d_cfg.get("target_ok_accounts", 20))
    poll_s = int(d_cfg.get("poll_interval_s", 600))
    rate = d_cfg.get("rate_limit") or {}
    rate_per_hour = int(rate.get("per_hour", 3))
    rate_per_day = int(rate.get("per_day", 30))
    max_cfail = int(d_cfg.get("max_consecutive_failures", 5))
    cfail_cool_s = int(d_cfg.get("consecutive_fail_cooldown_s", 1800))
    jitter = d_cfg.get("jitter_before_run_s") or [60, 180]
    seat_limit = int(d_cfg.get("seat_limit", 5))
    # gpt-team's batch-import by default puts new accounts into "recovery" replenishment pool (is_open=0 + account_usage='recovery'),
    # admin manually changes to sales and enables when selling externally. daemon maintains replenishment pool count by default.
    usage_pool = str(d_cfg.get("usage_pool", "recovery")).lower()

    ts_cfg = card_cfg.get("team_system") or {}
    cd_h = int(ts_cfg.get("domain_cooldown_hours", 24))
    pool = _build_domain_pool_from_cardw(cardw_path, cd_h)
    team_client = _build_team_client_from_card_cfg(card_cfg)
    _proxy_pool = _build_proxy_pool_from_card_cfg(card_cfg)  # reserved, currently not participating in pipeline distribution

    if not team_client:
        raise RuntimeError("daemon 需要 team_system.enabled=true")

    stop = {"flag": False}
    def _sig(*_a):
        print("\n[daemon] 收到停止信号，跑完当前循环即退出 ...")
        stop["flag"] = True
    for sg in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(sg, _sig)
        except Exception: pass

    # Webshare auto-rotation config
    ws_cfg = card_cfg.get("webshare") or {}
    ws_enabled = bool(ws_cfg.get("enabled"))
    ws_threshold = int(ws_cfg.get("refresh_threshold", 3))
    ws_cooldown_s = int(ws_cfg.get("no_rotation_cooldown_s", 10800))  # 3h
    zone_rotate_after = int(ws_cfg.get("zone_rotate_after_ip_rotations", 2))
    zone_rotate_on_reg_fails = int(ws_cfg.get("zone_rotate_on_reg_fails", 3))
    if ws_enabled and ws_cfg.get("api_key"):
        try:
            _ws_q = WebshareClient(ws_cfg["api_key"]).get_replacement_quota()
            print(f"[Webshare] 启动时额度：available={_ws_q['available']}/{_ws_q['total']}  "
                  f"threshold={ws_threshold} no_perm 触发轮换；无额度时连续 {ws_threshold} no_perm 冷却 {ws_cooldown_s/3600:.1f}h")
            if _ws_q["available"] <= 0:
                print(f"[Webshare] ⚠ 无剩余替换次数，本次启动将禁用自动轮换（走冷却回退）")
        except Exception as e:
            print(f"[Webshare] 启动额度查询失败（不影响运行）: {e}")

    # State
    state = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total_attempts": 0, "total_succeeded": 0, "total_failed": 0,
        "consecutive_failures": 0,
        "rate_hour_ts": [], "rate_day_ts": [],
        "last_error": "", "last_check_iso": "",
        "last_stats": {},
        "ip_no_perm_streak": 0,
        "current_proxy_ip": "",
        "total_ip_rotations": 0,
        "webshare_rotation_disabled": False,
        "no_perm_cooldown_until": 0,
        "current_zone": "",
        "zone_ip_rotations": 0,
        "total_zone_rotations": 0,
        "zone_reg_fail_streak": 0,
    }
    try:
        old = get_db().get_runtime_json(DAEMON_STATE_KEY, {})
        if isinstance(old, dict):
            for k in ("total_attempts", "total_succeeded", "total_failed",
                      "consecutive_failures", "rate_hour_ts", "rate_day_ts",
                      "last_error", "last_stats",
                      "ip_no_perm_streak", "current_proxy_ip", "total_ip_rotations",
                      "webshare_rotation_disabled", "no_perm_cooldown_until",
                      "current_zone", "zone_ip_rotations", "total_zone_rotations",
                      "zone_reg_fail_streak"):
                if k in old: state[k] = old[k]
    except Exception as e:
        print(f"[daemon] 读历史 state 失败: {e}")

    # Initialize current_zone (if provisioner is multi-zone)
    zone_list = []
    prov = getattr(pool, "provisioner", None)
    if prov and hasattr(prov, "zone_names"):
        zone_list = list(prov.zone_names)
    elif prov and hasattr(prov, "zone_name"):
        zn = prov.zone_name
        if "," not in zn:
            zone_list = [zn]
    if zone_list:
        if not state.get("current_zone") or state["current_zone"] not in zone_list:
            state["current_zone"] = zone_list[0]
        if hasattr(prov, "set_active_zone"):
            prov.set_active_zone(state["current_zone"])
        print(f"[Zone] 可用 zones={zone_list}  当前 active={state['current_zone']}  "
              f"每 {zone_rotate_after} 次 IP 轮换后切 zone")

    def _save():
        try:
            get_db().set_runtime_json(DAEMON_STATE_KEY, state)
        except Exception as e:
            print(f"[daemon] 存 state 失败: {e}")

    # Do aggressive cleanup once at startup (residue from last SIGKILL)
    _c0 = _cleanup_temp_leftovers(max_age_s=300, verbose=True)
    if sum(v for k, v in _c0.items() if k != "bytes"):
        print(f"[cleanup] 启动清理完毕")
    # CF dead subdomain cleanup: at startup + triggered periodically by cf_cleanup_every_n_runs in loop
    cf_db_path = (card_cfg.get("daemon") or {}).get(
        "gpt_team_db_path", "/path/to/gpt-team/backend/db/database.sqlite"
    )
    cf_cleanup_every = int((card_cfg.get("daemon") or {}).get(
        "cf_cleanup_every_n_runs", 30
    ))
    _cleanup_dead_cf_subdomains(getattr(pool, "provisioner", None), cf_db_path)
    # Check gost at startup (auto-restart if broken)
    _ensure_gost_alive(card_cfg, team_client)

    _hour_label = f"{rate_per_hour}/h" if rate_per_hour > 0 else "无限"
    _day_label = f"{rate_per_day}/d" if rate_per_day > 0 else "无限"
    print(f"[daemon] 启动：pool={usage_pool}  target={target}  poll={poll_s}s  rate={_hour_label}, {_day_label}  seat_limit={seat_limit}")
    print(f"[daemon] 历史累计: attempts={state['total_attempts']} ok={state['total_succeeded']} fail={state['total_failed']}")

    kwargs = {"card_cfg": card_cfg, "pool": pool, "team_client": team_client, "use_paypal": use_paypal}

    while not stop["flag"]:
        # No Webshare rotation quota + cooldown gate triggered by consecutive no_perm
        now_ts = time.time()
        cd_until = float(state.get("no_perm_cooldown_until", 0) or 0)
        if cd_until > now_ts:
            wait_s = cd_until - now_ts
            print(f"[daemon] ⏸ no_perm 冷却中，剩 {wait_s/60:.0f} min（到 "
                  f"{datetime.fromtimestamp(cd_until).strftime('%m-%d %H:%M')})")
            _save()
            for _ in range(int(min(wait_s, poll_s))):
                if stop["flag"]: break
                time.sleep(1)
            continue

        state["last_check_iso"] = datetime.now(timezone.utc).isoformat()
        try:
            stats = team_client.count_usable_accounts(seat_limit=seat_limit, usage=usage_pool)
            state["last_stats"] = stats
            usable = stats["usable"]
        except Exception as e:
            print(f"[daemon] 查账号数异常: {e}")
            state["last_error"] = f"count: {e}"
            _save()
            for _ in range(60):
                if stop["flag"]: break
                time.sleep(1)
            continue

        print(f"[daemon] {state['last_check_iso']}  {usage_pool} 池可用 {usable}/{target}  "
              f"(total={stats['total_active']} full={stats['full']} "
              f"no_perm={stats['no_invite_permission']} banned/dis={stats['banned_or_disabled']} "
              f"expired={stats['expired']})")

        if usable >= target:
            _save()
            for _ in range(poll_s):
                if stop["flag"]: break
                time.sleep(1)
            continue

        # Rate limiting
        now = time.time()
        state["rate_hour_ts"] = [t for t in state["rate_hour_ts"] if now - t < 3600]
        state["rate_day_ts"] = [t for t in state["rate_day_ts"] if now - t < 86400]
        if rate_per_hour > 0 and len(state["rate_hour_ts"]) >= rate_per_hour:
            wait_s = 3600 - (now - state["rate_hour_ts"][0]) + 5
            print(f"[daemon] 小时限额已满 ({rate_per_hour}/h)，等 {wait_s/60:.1f} min")
            _save()
            for _ in range(int(min(wait_s, poll_s))):
                if stop["flag"]: break
                time.sleep(1)
            continue
        if rate_per_day > 0 and len(state["rate_day_ts"]) >= rate_per_day:
            wait_s = 86400 - (now - state["rate_day_ts"][0]) + 5
            print(f"[daemon] 日限额已满 ({rate_per_day}/d)，等 {wait_s/3600:.1f} h")
            _save()
            for _ in range(int(min(wait_s, poll_s))):
                if stop["flag"]: break
                time.sleep(1)
            continue

        # Consecutive failure protection
        if state["consecutive_failures"] >= max_cfail:
            print(f"[daemon] 连续失败 {state['consecutive_failures']}/{max_cfail}，冷却 {cfail_cool_s/60:.0f} min")
            _save()
            for _ in range(cfail_cool_s):
                if stop["flag"]: break
                time.sleep(1)
            state["consecutive_failures"] = 0
            continue

        # Jitter
        js = random.uniform(float(jitter[0]), float(jitter[1]))
        print(f"[daemon] 抖动 {js:.0f}s 后开跑（可用缺口 {target-usable}）...")
        for _ in range(int(js)):
            if stop["flag"]: break
            time.sleep(1)
        if stop["flag"]: break

        # Run 1 pipeline
        state["total_attempts"] += 1
        run_ts = time.time()
        state["rate_hour_ts"].append(run_ts)
        state["rate_day_ts"].append(run_ts)
        # Confirm gost is alive before each round (avoid camoufox geoip failure)
        _ensure_gost_alive(card_cfg, team_client)
        try:
            rec = pipeline(card_config_path, **kwargs)
            status = rec.get("payment", {}).get("status", "?")
            perm = rec.get("invite_permission", "-")
            if status == "succeeded":
                state["total_succeeded"] += 1
                state["consecutive_failures"] = 0
                state["zone_reg_fail_streak"] = 0
                state["last_error"] = ""
                print(f"[daemon] ✓ pipeline 成功  invite={perm}  累计 ok={state['total_succeeded']}")
            else:
                state["total_failed"] += 1
                state["consecutive_failures"] += 1
                state["last_error"] = f"pay={status}"
                print(f"[daemon] ✗ pipeline 失败 (pay={status})  连续失败={state['consecutive_failures']}")
        except DatadomeSliderError as e:
            # Visible slider: drag solver in card.py already attempted and failed. IP rotation won't help either
            # (PayPal account risk control is the root cause), so no rotate, just count failure + trigger natural cooldown
            state["total_failed"] += 1
            state["consecutive_failures"] += 1
            state["last_error"] = "datadome_slider"
            perm = "-"
            print(f"[daemon] ⚠ DataDome 滑块 solver 失败  连续失败={state['consecutive_failures']}  "
                  f"（不轮换 IP，换 IP 不解决账号风控）")
        except RegistrationError as e:
            # Registration failure: split into two categories
            # - OTP timeout / zone rate limit → count zone-level streak, switch zone when threshold reached
            # - InvalidIP / geoip / proxy disconnection etc. infrastructure issues → don't count zone streak
            state["total_failed"] += 1
            state["consecutive_failures"] += 1
            state["last_error"] = f"reg: {str(e)[:160]}"
            perm = "-"
            err_low = str(e).lower()
            is_infra = any(k in err_low for k in (
                "invalidip", "failed to get ip", "geoip",
                "cannot open display", "proxy", "connection refused",
                "socks5", "camoufox"
            ))
            if is_infra:
                print(f"[daemon] ✗ 注册失败（基础设施问题，不计 zone streak）"
                      f" 连续失败={state['consecutive_failures']}")
            else:
                state["zone_reg_fail_streak"] = state.get("zone_reg_fail_streak", 0) + 1
                print(f"[daemon] ✗ 注册失败 zone_reg_fail={state['zone_reg_fail_streak']}/{zone_rotate_on_reg_fails}  "
                      f"连续失败={state['consecutive_failures']}")
        except Exception as e:
            state["total_failed"] += 1
            state["consecutive_failures"] += 1
            state["last_error"] = str(e)[:200]
            perm = "-"
            print(f"[daemon] ✗ pipeline 异常: {str(e)[:200]}  连续失败={state['consecutive_failures']}")

        # Registration failure consecutive accumulation to threshold → switch zone (OpenAI signup domain rate limit dedicated path)
        if (zone_list and len(zone_list) > 1
            and state.get("zone_reg_fail_streak", 0) >= zone_rotate_on_reg_fails):
            cur_zone = state.get("current_zone", zone_list[0])
            try:
                idx = zone_list.index(cur_zone)
            except ValueError:
                idx = -1
            next_zone = zone_list[(idx + 1) % len(zone_list)]
            print(f"[daemon] 🔀 zone={cur_zone} 注册连挂 "
                  f"{state['zone_reg_fail_streak']} 次（疑似 OpenAI 域风控），"
                  f"切 zone → {next_zone}")
            kept = [d for d in pool.domains
                     if d.lower().endswith("." + next_zone.lower())
                     or d.lower() == next_zone.lower()]
            pool.domains = kept
            if hasattr(prov, "set_active_zone"):
                prov.set_active_zone(next_zone)
            state["current_zone"] = next_zone
            state["zone_reg_fail_streak"] = 0
            state["zone_ip_rotations"] = 0
            state["consecutive_failures"] = 0  # Switching zone resets fail counter for new zone
            state["total_zone_rotations"] = state.get("total_zone_rotations", 0) + 1
            print(f"[daemon] zone 切换完成，累计 zone 轮换={state['total_zone_rotations']}")

        # IP no_perm streak (always tracked, independent of Webshare quota status)
        if ws_enabled:
            if perm == "no_permission":
                state["ip_no_perm_streak"] += 1
                print(f"[daemon] IP no_perm 连续={state['ip_no_perm_streak']}/{ws_threshold}")
            elif perm == "ok":
                if state["ip_no_perm_streak"] or state.get("zone_ip_rotations", 0):
                    print(f"[daemon] no_perm/zone 计数清零（IP streak={state['ip_no_perm_streak']} "
                          f"zone_rotations={state.get('zone_ip_rotations', 0)}）")
                state["ip_no_perm_streak"] = 0
                state["zone_ip_rotations"] = 0
                # One successful order also proves IP still works, clear historical rotation_disabled flag, give next attempt a chance
                if state.get("webshare_rotation_disabled"):
                    print(f"[daemon] invite=ok → 清 webshare_rotation_disabled")
                    state["webshare_rotation_disabled"] = False

            if state["ip_no_perm_streak"] >= ws_threshold:
                if not state.get("webshare_rotation_disabled"):
                    print(f"[daemon] 达到 {ws_threshold} 次 no_perm，触发 Webshare 轮换 IP ...")
                    try:
                        new_px = _rotate_webshare_ip(card_cfg, team_client=team_client,
                                                       prev_ip=state.get("current_proxy_ip", ""))
                        state["current_proxy_ip"] = new_px.get("proxy_address", "")
                        state["total_ip_rotations"] += 1
                        state["zone_ip_rotations"] = state.get("zone_ip_rotations", 0) + 1
                        state["ip_no_perm_streak"] = 0
                        print(f"[daemon] ✓ IP 已换 → {state['current_proxy_ip']}  "
                              f"累计 IP 轮换={state['total_ip_rotations']}  "
                              f"当前 zone 内={state['zone_ip_rotations']}/{zone_rotate_after}")
                    except WebshareQuotaExhausted as e:
                        state["webshare_rotation_disabled"] = True
                        print(f"[daemon] ⚠ Webshare 替换额度耗尽: {e}")
                    except Exception as e:
                        # Non-quota exceptions (network/DNS jitter etc.) don't lock rotation, just log
                        print(f"[daemon] ✗ IP 轮换失败（本次跳过，下次 no_perm 再试）: {e}")

                # If rotation unavailable (quota exhausted / exception), enter 3h cooldown to let IP naturally recover
                if state.get("webshare_rotation_disabled") and \
                   state["ip_no_perm_streak"] >= ws_threshold:
                    cd_end = time.time() + ws_cooldown_s
                    state["no_perm_cooldown_until"] = cd_end
                    state["ip_no_perm_streak"] = 0
                    print(f"[daemon] ⏸ 无轮换能力 + 连续 {ws_threshold} no_perm，冷却 "
                          f"{ws_cooldown_s/3600:.1f}h 到 "
                          f"{datetime.fromtimestamp(cd_end).strftime('%m-%d %H:%M')}")

            # IP rotation count within current zone accumulates to threshold → switch zone
            if (zone_list and len(zone_list) > 1
                and state.get("zone_ip_rotations", 0) >= zone_rotate_after):
                cur_zone = state.get("current_zone", zone_list[0])
                try:
                    idx = zone_list.index(cur_zone)
                except ValueError:
                    idx = -1
                next_zone = zone_list[(idx + 1) % len(zone_list)]
                print(f"[daemon] 🔀 当前 zone={cur_zone} 内已 IP 轮换 "
                      f"{state['zone_ip_rotations']} 次仍 no_perm，切换 zone → {next_zone}")
                # Remove subdomains in pool that don't belong to new zone (force new zone provision for next order)
                kept = [d for d in pool.domains if d.lower().endswith("." + next_zone.lower())
                         or d.lower() == next_zone.lower()]
                removed = len(pool.domains) - len(kept)
                pool.domains = kept
                if hasattr(prov, "set_active_zone"):
                    prov.set_active_zone(next_zone)
                state["current_zone"] = next_zone
                state["zone_ip_rotations"] = 0
                state["total_zone_rotations"] = state.get("total_zone_rotations", 0) + 1
                print(f"[daemon] zone 切换完成。池中移除 {removed} 个旧 zone 子域；"
                      f"累计 zone 轮换={state['total_zone_rotations']}")

        _save()
        # Clean orphans from 30min ago after each pipeline round, prevent /tmp from being exhausted by SIGKILL residue
        _cleanup_temp_leftovers(max_age_s=1800, verbose=False)
        # Clean CF dead subdomains every cf_cleanup_every_n_runs orders (0=disabled)
        if cf_cleanup_every > 0 and state["total_attempts"] % cf_cleanup_every == 0:
            try:
                _cleanup_dead_cf_subdomains(getattr(pool, "provisioner", None), cf_db_path)
            except Exception as e:
                print(f"[CF-cleanup] 周期清理异常: {e}")
        # Short sleep before next round
        for _ in range(10):
            if stop["flag"]: break
            time.sleep(1)

    _save()
    print(f"[daemon] 已退出。累计 attempts={state['total_attempts']} ok={state['total_succeeded']} fail={state['total_failed']}")


def _build_domain_pool_from_cardw(cardw_path, cooldown_hours=24):
    try:
        with open(cardw_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mail_cfg = data.get("mail", {})
        lst = mail_cfg.get("catch_all_domains", []) or []
        ap_cfg = mail_cfg.get("auto_provision") or {}
    except Exception as e:
        print(f"[DomainPool] 读 {cardw_path} 失败: {e}")
        lst, ap_cfg = [], {}

    provisioner = None
    if ap_cfg.get("enabled"):
        secrets = _load_secrets().get("cloudflare") or {}
        token = secrets.get("api_token", "").strip()
        # Support zone_names (list) or single zone_name, backward compatible
        zones_cfg = ap_cfg.get("zone_names")
        if zones_cfg and isinstance(zones_cfg, list):
            zones = [z for z in zones_cfg if z and isinstance(z, str) and z.strip()]
        else:
            single = ap_cfg.get("zone_name") or secrets.get("zone_name") or ""
            zones = [single] if single else []

        if token and zones:
            subs = []
            for zone in zones:
                try:
                    subs.append(CloudflareDomainProvisioner(
                        api_token=token,
                        zone_name=zone.strip(),
                        forward_to=secrets.get("forward_to", ""),
                        min_seg_len=int(ap_cfg.get("min_seg_len", 2)),
                        max_seg_len=int(ap_cfg.get("max_seg_len", 5)),
                        min_segs=int(ap_cfg.get("min_segs", 1)),
                        max_segs=int(ap_cfg.get("max_segs", 4)),
                        dns_propagation_s=int(ap_cfg.get("dns_propagation_s", 20)),
                    ))
                except Exception as e:
                    print(f"[CF] 构造 provisioner zone={zone} 失败: {e}")
            if len(subs) == 1:
                provisioner = subs[0]
                print(f"[CF] 已启用自动开通：zone={subs[0].zone_name}  "
                      f"min_available={ap_cfg.get('min_available',2)}")
            elif len(subs) > 1:
                provisioner = MultiZoneDomainProvisioner(subs)
                print(f"[CF] 已启用多 zone 自动开通：zones={[p.zone_name for p in subs]}  "
                      f"min_available={ap_cfg.get('min_available',2)}")
        else:
            print(f"[CF] auto_provision 已启用但缺 token 或 zone（api_token={bool(token)}, zones={zones}）")

    # auto-loop / daemon single zone lock: WEBUI_FORCE_ZONE restricts this process to use only specified zone
    # domains (including subdomains), and switch multi-zone provisioner to that zone.
    forced_zone = (os.environ.get("WEBUI_FORCE_ZONE", "") or "").strip().lower()
    if forced_zone:
        kept = [d for d in lst if d.lower() == forced_zone or d.lower().endswith("." + forced_zone)]
        if kept:
            print(f"[DomainPool] WEBUI_FORCE_ZONE={forced_zone} → 池过滤 {len(lst)}→{len(kept)} 个域")
            lst = kept
        else:
            print(f"[DomainPool] WEBUI_FORCE_ZONE={forced_zone} 但池里无匹配域，保留原 {len(lst)} 个")
        if provisioner and hasattr(provisioner, "set_active_zone"):
            try:
                provisioner.set_active_zone(forced_zone)
            except Exception:
                pass

    min_avail = int(ap_cfg.get("min_available", 2))
    return DomainPool(lst, DOMAIN_STATE_KEY, cooldown_hours,
                       provisioner=provisioner, min_available=min_avail)


def _build_team_client_from_card_cfg(card_cfg):
    ts = card_cfg.get("team_system") or {}
    if not ts.get("enabled"):
        return None
    base_url = ts.get("base_url", "").strip()
    if not base_url:
        return None
    return TeamSystemClient(
        base_url=base_url,
        username=ts.get("username", ""),
        password=ts.get("password", ""),
        timeout_s=int(ts.get("timeout_s", 60)),
    )


# ──────────────────────────────────────────────
# Proxy pool (reserved, currently not involved in pipeline, will take over after proxies.list is filled)
# ──────────────────────────────────────────────


class WebshareQuotaExhausted(RuntimeError):
    """Webshare monthly replacement quota exhausted."""
    pass


class WebshareClient:
    """Webshare.io API v2 minimal client: refresh + read current proxy + quota query."""

    BASE = "https://proxy.webshare.io/api/v2"

    def __init__(self, api_key: str, timeout_s: int = 30):
        import urllib.request
        self.api_key = api_key.strip()
        self.timeout_s = timeout_s
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
        )

    def _req(self, path: str, method: str = "GET", body: dict = None):
        import urllib.request
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.BASE}{path}", data=data,
            headers={"Authorization": f"Token {self.api_key}",
                     "Content-Type": "application/json"},
            method=method,
        )
        return self._opener.open(req, timeout=self.timeout_s)

    def get_plan(self) -> dict:
        """GET /subscription/plan/ returns active plan dict (with replacement quota)."""
        with self._req("/subscription/plan/") as r:
            data = json.loads(r.read().decode())
        for p in (data.get("results") or []):
            if p.get("status") == "active":
                return p
        return (data.get("results") or [{}])[0]

    def get_replacement_quota(self) -> dict:
        """Returns {total, used, available, cycle_end_iso}"""
        p = self.get_plan()
        return {
            "total": int(p.get("proxy_replacements_total", 0) or 0),
            "used": int(p.get("proxy_replacements_used", 0) or 0),
            "available": int(p.get("proxy_replacements_available", 0) or 0),
            "updated_at": p.get("updated_at", ""),
        }

    def refresh_pool(self, country: str = "", count: int = 1) -> None:
        """POST /proxy/list/refresh/ triggers pool-wide IP rotation, 204 means success.
        When country is non-empty, pass {"countries":{country.upper(): count}} body to lock country.
        Raises WebshareQuotaExhausted when quota exhausted."""
        import urllib.error
        body = None
        if country:
            body = {"countries": {country.upper(): int(count)}}
        try:
            with self._req("/proxy/list/refresh/", method="POST", body=body) as r:
                if r.status != 204:
                    raise RuntimeError(f"Webshare refresh 非 204: {r.status}")
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="ignore")[:300]
            except Exception:
                pass
            low = (body_text or "").lower()
            if e.code in (402, 429) or "quota" in low or "exhaust" in low or "limit" in low:
                raise WebshareQuotaExhausted(f"http={e.code} body={body_text}") from e
            raise RuntimeError(f"Webshare refresh HTTP {e.code}: {body_text}") from e

    def get_current_proxy(self) -> dict:
        """GET /proxy/list/ returns first proxy. valid not verified."""
        with self._req("/proxy/list/?mode=direct&page=1&page_size=5") as r:
            data = json.loads(r.read().decode())
        results = data.get("results") or []
        if not results:
            raise RuntimeError("Webshare 代理列表为空")
        return results[0]

    def wait_for_fresh_proxy(self, prev_ip: str = "", max_wait_s: int = 120,
                              poll_interval_s: int = 5) -> dict:
        """Poll until: valid=True and ip != prev_ip. Returns proxy dict."""
        deadline = time.time() + max_wait_s
        last = None
        while time.time() < deadline:
            try:
                p = self.get_current_proxy()
                last = p
                if p.get("valid") and (not prev_ip or p.get("proxy_address") != prev_ip):
                    return p
            except Exception as e:
                print(f"[Webshare] 查询代理异常: {e}")
            time.sleep(poll_interval_s)
        if last is not None:
            return last
        raise RuntimeError("Webshare 等待新代理超时")


_GOST_LAST_FILE = "/tmp/gost_last.json"


def _save_gost_last(new_ip: str, new_port: int, username: str, password: str,
                     listen_port: int, upstream_scheme: str) -> None:
    """After successful swap, persist to disk so other subprocesses can fallback and restart when webshare API jitters.
    Sensitive fields (password) stay in container, /tmp only accessible by this container."""
    try:
        import json as _json
        with open(_GOST_LAST_FILE, "w", encoding="utf-8") as f:
            _json.dump({
                "proxy_address": new_ip,
                "port": int(new_port),
                "username": username,
                "password": password,
                "listen_port": int(listen_port),
                "upstream_scheme": upstream_scheme,
                "saved_at": time.time(),
            }, f)
    except Exception:
        pass


def _load_gost_last() -> dict | None:
    try:
        import json as _json
        with open(_GOST_LAST_FILE, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return None


def _swap_gost_relay(new_ip: str, new_port: int, username: str, password: str,
                       listen_port: int = 18898, upstream_scheme: str = "http") -> None:
    """Replace gost listening on listen_port with new upstream. Safely match -L=socks5://:<port> segment in process command line."""
    import signal
    listen_pat = f"-L=socks5://:{listen_port}"
    try:
        out = subprocess.check_output(["pgrep", "-af", "gost"], text=True)
    except subprocess.CalledProcessError:
        out = ""
    victims = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_s, cmd = parts
        if listen_pat in cmd:
            try: victims.append(int(pid_s))
            except ValueError: pass
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[gost] 停止旧 gost PID={pid}")
        except Exception as e:
            print(f"[gost] 杀 PID={pid} 失败: {e}")
    # Wait for old port to be released
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            ck = subprocess.run(["ss", "-ltn", f"sport = :{listen_port}"],
                                 capture_output=True, text=True, timeout=3)
            if f":{listen_port}" not in ck.stdout:
                break
        except Exception:
            break
        time.sleep(0.3)

    upstream = f"{upstream_scheme}://{username}:{password}@{new_ip}:{new_port}"
    cmd = ["gost", f"-L=socks5://:{listen_port}", f"-F={upstream}"]
    log_path = f"/tmp/gost-{listen_port}.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        p = subprocess.Popen(cmd, stdout=fd, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        os.close(fd)
    time.sleep(0.5)
    if p.poll() is not None:
        raise RuntimeError(f"gost 启动即退出，见 {log_path}")
    # Synchronously verify upstream connectivity — avoid "code has rotated IP, gost still cold-started" window in caller invocation
    # The first request steps on SOCKS5 failure and is mistakenly categorized as proxy_dead → which triggers another round of rotation spiral.
    settle_deadline = time.time() + 15
    while time.time() < settle_deadline:
        if _probe_gost_upstream(listen_port, timeout_s=3):
            print(f"[gost] 启动新中继 PID={p.pid}  {upstream}  (探活通过)")
            _save_gost_last(new_ip, new_port, username, password, listen_port, upstream_scheme)
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"gost 启动后 15s 探活仍失败，IP={new_ip}:{new_port}（见 {log_path}）"
    )


def _probe_gost_upstream(listen_port: int, timeout_s: int = 5) -> bool:
    """Probing: curl via socks5://127.0.0.1:<port> to access the public network. HTTP 200 is considered alive; others (407, SOCKS5 97, etc.) are considered dead."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout_s),
             "-x", f"socks5h://127.0.0.1:{listen_port}",
             "https://api.ipify.org"],
            capture_output=True, text=True, timeout=timeout_s + 2,
        )
        return r.stdout.strip() == "200"
    except Exception:
        return False


def _ensure_gost_alive(card_cfg: dict, team_client=None) -> bool:
    """Startup/pre-loop hook. First check listen, then perform upstream health check—if either fails, perform refresh+swap
    self-healing (handles scenarios like 407 errors where Webshare IP is swapped but gost still points to the old IP).

    When concurrent workers call simultaneously, use /tmp/gost_ensure.lock flock to serialize,
    avoiding N workers simultaneously calling swap_gost_relay and killing each other's newly started gost processes (causing chromium
    ERR_CONNECTION_CLOSED). If unable to acquire lock, spin-wait; when the second worker enters the lock,
    listen is already up + health check passed, return True directly."""
    ws_cfg = (card_cfg or {}).get("webshare") or {}
    if not ws_cfg.get("enabled"):
        return False
    api_key = (ws_cfg.get("api_key") or "").strip()
    listen_port = int(ws_cfg.get("gost_listen_port", 18898))
    if not api_key:
        return False

    import fcntl
    lock_path = f"/tmp/gost_ensure_{listen_port}.lock"
    lock_fd = None
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        # Blocking LOCK_EX, wait at most 90s (gost swap takes ~15s at most, plus webshare API failure fallback 30s timeout)
        import signal as _sig
        class _Timeout(Exception): ...
        def _alarm(_s, _f):
            raise _Timeout()
        old_handler = _sig.signal(_sig.SIGALRM, _alarm)
        _sig.alarm(90)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except _Timeout:
            print(f"[gost] ensure lock 等 90s 超时, 放弃")
            return False
        finally:
            _sig.alarm(0)
            _sig.signal(_sig.SIGALRM, old_handler)
    except Exception as e:
        # Environments where fcntl is not supported (mac / windows) fall back to lock-free path
        print(f"[gost] flock 失败 ({e}), 走无锁路径")
        if lock_fd is not None:
            try: os.close(lock_fd)
            except Exception: pass
        lock_fd = None

    try:
        return _ensure_gost_alive_inner(card_cfg, team_client, ws_cfg, listen_port)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass


def _ensure_gost_alive_inner(card_cfg: dict, team_client, ws_cfg: dict, listen_port: int) -> bool:
    api_key = (ws_cfg.get("api_key") or "").strip()
    # Check listen
    listening = False
    try:
        ck = subprocess.run(["ss", "-ltn", f"sport = :{listen_port}"],
                             capture_output=True, text=True, timeout=3)
        listening = f":{listen_port}" in ck.stdout
    except Exception:
        pass
    # listen in → also need to explore upstream to see if it can access the network
    if listening:
        if _probe_gost_upstream(listen_port):
            return True
        print(f"[gost] listen :{listen_port} 在但上游探活失败 → 触发 refresh + swap 自愈")
        try:
            _rotate_webshare_ip(card_cfg, team_client=team_client)
            # rotate again after completion
            if _probe_gost_upstream(listen_port):
                return True
            print(f"[gost] rotate 后探活仍失败")
            return False
        except Exception as e:
            print(f"[gost] 上游死，rotate 自愈也失败: {e}")
            return False
    print(f"[gost] listen :{listen_port} 无监听，自动拉起")
    upstream_scheme = str(ws_cfg.get("gost_upstream_scheme", "http"))
    px: dict | None = None
    try:
        client = WebshareClient(api_key)
        px = client.get_current_proxy()
    except Exception as e:
        print(f"[gost] 查询 Webshare 当前 IP 失败: {e}")
        # webshare API shake (502/timeout common) → fallback to use last successful _swap_gost_relay
        # Keep the cache (/tmp/gost_last.json), at least pull up gost first. Even if the IP has already
        # _probe_gost_upstream will immediately fail probe when replaced by webshare pool, caller will retry
        # will re-enter listen-on-but-upstream-dead branch to trigger rotate.
        cached = _load_gost_last()
        if cached:
            print(f"[gost] fallback 用 cache IP {cached.get('proxy_address')}:{cached.get('port')} 拉起")
            px = {
                "proxy_address": cached["proxy_address"],
                "port": int(cached["port"]),
                "username": cached["username"],
                "password": cached["password"],
            }
        else:
            print("[gost] 也没 /tmp/gost_last.json cache, 放弃")
            return False
    try:
        _swap_gost_relay(px["proxy_address"], int(px["port"]),
                          px["username"], px["password"],
                          listen_port=listen_port,
                          upstream_scheme=upstream_scheme)
    except Exception as e:
        print(f"[gost] 拉起失败: {e}")
        return False
    # Synchronize team global proxy
    if team_client and ws_cfg.get("sync_team_proxy", True):
        team_scheme = str(ws_cfg.get("team_proxy_scheme", "socks5"))
        try:
            team_client.update_global_proxy(
                f"{team_scheme}://{px['username']}:{px['password']}@{px['proxy_address']}:{px['port']}"
            )
        except Exception as e:
            print(f"[gost] team 代理同步失败: {e}")
    return True


# Rotate cooling: module-level timestamp + last result cache. Any caller (auto-loop classification /
# pipeline._ensure_gost_alive / manual /api/proxy/rotate-ip) shared, avoid short-term
# Repeatedly refresh Webshare quota and burn it. Calls within cooldown directly return the previous IP without querying the API.
_LAST_ROTATE_TS: float = 0.0
_LAST_ROTATE_PX: dict | None = None


def _rotate_webshare_ip(card_cfg: dict, team_client=None, prev_ip: str = "",
                          force: bool = False) -> dict:
    """Integration: refresh → poll new IP → switch gost → sync team global proxy. Return new proxy dict.

    Cooldown is controlled by cardw.webshare.rotate_cooldown_s (default 300s);
    Pass force=True to skip cooldown (useful for manual button /api/proxy/rotate-ip)."""
    global _LAST_ROTATE_TS, _LAST_ROTATE_PX
    ws_cfg = (card_cfg or {}).get("webshare") or {}
    if not ws_cfg.get("enabled"):
        raise RuntimeError("webshare 未启用")
    api_key = (ws_cfg.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("webshare.api_key 为空")
    listen_port = int(ws_cfg.get("gost_listen_port", 18898))
    upstream_scheme = str(ws_cfg.get("gost_upstream_scheme", "http"))
    team_scheme = str(ws_cfg.get("team_proxy_scheme", "socks5"))
    sync_team = bool(ws_cfg.get("sync_team_proxy", True))
    poll_wait = int(ws_cfg.get("poll_timeout_s", 120))
    cooldown = int(ws_cfg.get("rotate_cooldown_s", 300))

    if not force and cooldown > 0 and _LAST_ROTATE_PX:
        elapsed = time.time() - _LAST_ROTATE_TS
        if elapsed < cooldown:
            print(
                f"[Webshare] 距上次 rotate {elapsed:.0f}s < 冷却 {cooldown}s，"
                f"跳过新 refresh（沿用 IP={_LAST_ROTATE_PX.get('proxy_address')}）"
            )
            return _LAST_ROTATE_PX

    client = WebshareClient(api_key)
    try:
        quota = client.get_replacement_quota()
        print(f"[Webshare] 替换额度：available={quota['available']}/{quota['total']} used={quota['used']}")
        if quota["available"] <= 0:
            raise WebshareQuotaExhausted(f"quota: {quota}")
    except WebshareQuotaExhausted:
        raise
    except Exception as e:
        print(f"[Webshare] 查询额度异常（继续 refresh）: {e}")

    lock_country = str(ws_cfg.get("lock_country", "")).strip().upper()
    if lock_country:
        print(f"[Webshare] refresh pool，锁国家={lock_country}（prev_ip={prev_ip or '?'}）")
    else:
        print(f"[Webshare] refresh pool（prev_ip={prev_ip or '?'}）")
    client.refresh_pool(country=lock_country)
    new_px = client.wait_for_fresh_proxy(prev_ip=prev_ip, max_wait_s=poll_wait)
    new_ip = new_px["proxy_address"]
    new_port = int(new_px["port"])
    user = new_px["username"]
    pw = new_px["password"]
    print(f"[Webshare] 新 IP: {new_ip}:{new_port}  "
          f"{new_px.get('country_code')}/{new_px.get('asn_name')}  valid={new_px.get('valid')}")

    _swap_gost_relay(new_ip, new_port, user, pw,
                      listen_port=listen_port, upstream_scheme=upstream_scheme)

    if sync_team and team_client:
        team_url = f"{team_scheme}://{user}:{pw}@{new_ip}:{new_port}"
        try:
            r = team_client.update_global_proxy(team_url)
            print(f"[Team] 全局代理已更新 → {r.get('proxyUrl', team_url)}")
        except Exception as e:
            print(f"[Team] 更新全局代理失败: {e}")

    _LAST_ROTATE_TS = time.time()
    _LAST_ROTATE_PX = new_px
    return new_px


class ProxyPool:
    """Proxy rotation pool (stub). Future expansion: health checks, failure marking, LRU rotation.
    Current behavior: return the first one if list exists (maintain stability), return empty string if no list (use default proxy from configuration)."""

    def __init__(self, proxies=None, rotation="static", state_file=None):
        self.proxies = [p for p in (proxies or []) if p and str(p).strip()]
        self.rotation = rotation  # static / random / lru
        self.state_file = state_file

    def pick(self) -> str:
        if not self.proxies:
            return ""
        if self.rotation == "random":
            return random.choice(self.proxies)
        return self.proxies[0]

    def mark_fail(self, proxy):
        # TODO: Implement failure flag + cooldown
        pass


def _build_proxy_pool_from_card_cfg(card_cfg) -> "ProxyPool":
    pp = (card_cfg or {}).get("proxies") or {}
    if not pp.get("enabled"):
        return ProxyPool()
    return ProxyPool(proxies=pp.get("list", []), rotation=pp.get("rotation", "static"))


def _rewrite_cardw_with_domain(src_path, domain, proxy_url=""):
    """Read CTF-reg config, override catch_all_domain with domain, optionally override proxy, write to temp file and return path"""
    with open(src_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    mail = data.setdefault("mail", {})
    mail["catch_all_domain"] = domain
    if proxy_url:
        data["proxy"] = proxy_url
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="pipeline_cardw_",
        dir=str(RUNTIME_REG), delete=False,
    )
    json.dump(data, tmp, ensure_ascii=False, indent=2)
    tmp.close()
    return tmp.name


def _rewrite_card_with_proxy(src_path, proxy_url):
    """Read CTF-pay config, override proxy field, write to temp file and return path"""
    with open(src_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["proxy"] = proxy_url
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="pipeline_pay_px_",
        dir=str(RUNTIME_PAY), delete=False,
    )
    json.dump(data, tmp, ensure_ascii=False, indent=2)
    tmp.close()
    return tmp.name


def _find_latest_refresh_token_for_email(email, session_id=""):
    try:
        return get_db().latest_refresh_token_for_email(email, session_id)
    except Exception as e:
        print(f"[results] 读 refresh_token 失败: {e}")
    return ""


def _augment_card_result_last_match(email, session_id, extra_fields):
    """Find the first matching payment record with email(+session_id) in reverse order and fill in the missing fields."""
    try:
        return get_db().augment_card_result_last_match(email, session_id, extra_fields)
    except Exception as e:
        print(f"[results] 写回字段失败: {e}")
        return False


# ──────────────────────────────────────────────
# Self-dealer related helper
# Business model: 1 owner pays to open a Team + N members are invited to join + all push CPA
# Reuse existing: register / pay / _exchange_refresh_token_with_session / _cpa_import_after_team
# ──────────────────────────────────────────────

# OpenAI team API helpers have been extracted to pipeline.oauth.team_api (Wave C-2)
from pipeline.oauth.team_api import (  # noqa: E402
    _oai_exchange_refresh_to_access_token,
    _oai_team_id_from_access_token,
    _oai_send_team_invite,
    _oai_accept_team_invite,
    _find_team_id_from_results,
)


def _cpa_import_after_team(
    email: str,
    sid: str,
    cpa_cfg: dict,
    *,
    refresh_token: str = "",
    is_free: bool = False,
) -> str:
    """Import the account to CPA (CLIProxyAPI) after successful payment. best-effort, no exception thrown.

    Args:
        refresh_token: Explicitly passed rt (used in free_only path); when not passed,
            query from SQLite payment records by email/sid (default behavior of pay flow).
        is_free: True → CPA pushes plan_tag using cpa_cfg.free_plan_tag
            (free account tier); False → use plan_tag (team / paid tier).

    Returns: ok / skipped / no_rt / fail_refresh / fail_upload"""
    import urllib.request, urllib.parse, urllib.error, base64, hashlib
    if not cpa_cfg or not cpa_cfg.get("enabled"):
        return "skipped"
    base_url = (cpa_cfg.get("base_url") or "").rstrip("/")
    admin_key = (cpa_cfg.get("admin_key") or "").strip()
    if not base_url or not admin_key or not email:
        return "skipped"

    rt = (refresh_token or "").strip() or _find_latest_refresh_token_for_email(email, sid)
    reg_acc = _find_latest_registered_account_for_email(email)
    if not rt:
        # After actual payment, refresh_token may fail due to reasons such as add-phone / rate-limit / 401, etc.
        # Unable to get in time; in this case, at least keep the access_token in the registered account for "bare import"
        #Let the CPA side write to the database first, and supplement RT later.
        at = (reg_acc.get("access_token") or "").strip()
        id_tok = (reg_acc.get("id_token") or "").strip() or at
        if not at:
            print(f"[CPA] {email} 无 refresh_token 且无 access_token，跳过")
            return "no_rt"
        account_id = _oai_team_id_from_access_token(at)
        expired_iso = ""
        try:
            p = at.split(".")[1]
            p += "=" * (4 - len(p) % 4)
            payload = json.loads(base64.urlsafe_b64decode(p).decode())
            if payload.get("exp"):
                expired_iso = datetime.fromtimestamp(payload["exp"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {
            "id_token": id_tok,
            "access_token": at,
            "refresh_token": rt,
            "account_id": account_id,
            "email": email,
            "last_refresh": now_iso,
            "expired": expired_iso,
            "type": "codex",
        }
        print(f"[CPA] {email} 无 refresh_token，回退使用现有 access_token 裸导入")
        tag = hashlib.md5(email.encode()).hexdigest()[:8]
        plan_tag = (cpa_cfg.get("plan_tag") or "team").strip() or "team"
        if is_free:
            plan_tag = (cpa_cfg.get("free_plan_tag") or "free").strip() or "free"
        name = f"codex-{tag}-{email}-{plan_tag}.json"
        try:
            try:
                import curl_cffi.requests as cr
                sess = cr.Session(impersonate="chrome136")
                sess.proxies = {}
                sess.trust_env = False
                r = sess.post(
                    f"{base_url}/v0/management/auth-files",
                    params={"name": name},
                    json=body,
                    headers={"Authorization": f"Bearer {admin_key}",
                             "Content-Type": "application/json"},
                    timeout=int(cpa_cfg.get("timeout_s", 20)),
                )
                if r.status_code >= 400:
                    raise RuntimeError(f"http={r.status_code} body={r.text[:200]}")
                print(f"[CPA] ✓ {email} 已导入 → {base_url}  account_id={account_id[:8] or '?'}")
                return "ok"
            except ImportError:
                pass
            req = urllib.request.Request(
                f"{base_url}/v0/management/auth-files?name={urllib.parse.quote(name)}",
                data=json.dumps(body).encode(),
                headers={"Authorization": f"Bearer {admin_key}",
                         "Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"},
                method="POST",
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Do not use local proxy
            with opener.open(req, timeout=int(cpa_cfg.get("timeout_s", 20))) as r:
                resp = r.read().decode()
            print(f"[CPA] ✓ {email} 已导入 → {base_url}  account_id={account_id[:8] or '?'}")
            return "ok"
        except urllib.error.HTTPError as e:
            try: eb = e.read().decode()[:200]
            except Exception: eb = ""
            print(f"[CPA] ✗ {email} 上传失败 http={e.code} {eb}")
            return "fail_upload"
        except Exception as e:
            print(f"[CPA] ✗ {email} 上传异常: {e}")
            return "fail_upload"

    # Refresh once refresh_token → Get access_token + id_token bound with team
    client_id = cpa_cfg.get("oauth_client_id") or "app_EMoamEEZ73f0CkXaXp7hrann"
    at, id_tok, account_id = "", "", ""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Do not use local proxy
    try:
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": client_id,
            "scope": "openid email profile offline_access",
        }).encode()
        req = urllib.request.Request(
            "https://auth.openai.com/oauth/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            method="POST",
        )
        with opener.open(req, timeout=20) as r:
            tok = json.loads(r.read().decode())
        at = tok.get("access_token", "") or ""
        id_tok = tok.get("id_token", "") or at
        rt = tok.get("refresh_token", rt) or rt
        if at:
            try:
                p = at.split(".")[1]
                p += "=" * (4 - len(p) % 4)
                payload = json.loads(base64.urlsafe_b64decode(p).decode())
                account_id = (payload.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id", "") or ""
            except Exception:
                pass
    except Exception as e:
        print(f"[CPA] {email} refresh_token 交换失败（仍尝试裸导入）: {e}")

    # Construct codex file
    expired_iso = ""
    if at:
        try:
            p = at.split(".")[1]
            p += "=" * (4 - len(p) % 4)
            payload = json.loads(base64.urlsafe_b64decode(p).decode())
            if payload.get("exp"):
                expired_iso = datetime.fromtimestamp(payload["exp"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "id_token": id_tok, "access_token": at, "refresh_token": rt,
        "account_id": account_id, "email": email,
        "last_refresh": now_iso, "expired": expired_iso, "type": "codex",
    }
    tag = hashlib.md5(email.encode()).hexdigest()[:8]
    plan_tag = (cpa_cfg.get("plan_tag") or "team").strip() or "team"
    if is_free:
        plan_tag = (cpa_cfg.get("free_plan_tag") or "free").strip() or "free"
    name = f"codex-{tag}-{email}-{plan_tag}.json"
    try:
        # Use curl_cffi (Chrome TLS + UA fingerprint) to bypass CF WAF; downgrade to urllib if unavailable
        try:
            import curl_cffi.requests as cr
            sess = cr.Session(impersonate="chrome136")
            sess.proxies = {}
            sess.trust_env = False
            r = sess.post(
                f"{base_url}/v0/management/auth-files",
                params={"name": name},
                json=body,
                headers={"Authorization": f"Bearer {admin_key}",
                         "Content-Type": "application/json"},
                timeout=int(cpa_cfg.get("timeout_s", 20)),
            )
            if r.status_code >= 400:
                raise RuntimeError(f"http={r.status_code} body={r.text[:200]}")
            print(f"[CPA] ✓ {email} 已导入 → {base_url}  account_id={account_id[:8] or '?'}")
            return "ok"
        except ImportError:
            pass
        req = urllib.request.Request(
            f"{base_url}/v0/management/auth-files?name={urllib.parse.quote(name)}",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {admin_key}",
                     "Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"},
            method="POST",
        )
        with opener.open(req, timeout=int(cpa_cfg.get("timeout_s", 20))) as r:
            resp = r.read().decode()
        print(f"[CPA] ✓ {email} 已导入 → {base_url}  account_id={account_id[:8] or '?'}")
        return "ok"
    except urllib.error.HTTPError as e:
        try: eb = e.read().decode()[:200]
        except Exception: eb = ""
        print(f"[CPA] ✗ {email} 上传失败 http={e.code} {eb}")
        return "fail_upload"
    except Exception as e:
        print(f"[CPA] ✗ {email} 上传异常: {e}")
        return "fail_upload"


def _team_probe_after_payment(pay_record, team_client, pool, domain):
    """After successful payment: read RT → import team → write back probe results → update domain status.
    Return the status string returned by probe (ok/no_permission/failed/error/none)."""
    if not team_client:
        return "none"
    email = ""
    sid = ""
    raw = pay_record.get("raw") or {}
    if isinstance(raw, dict):
        ru = raw.get("return_url", "") or ""
        sid = raw.get("session_id", "") or ""
        if "chatgpt_email" in raw:
            email = raw.get("chatgpt_email", "")
    email = email or pay_record.get("email", "") or ""
    if not email:
        print("[Team] 无 email，跳过 probe")
        return "none"
    rt = _find_latest_refresh_token_for_email(email, sid)
    if not rt:
        print(f"[Team] {email} 无 refresh_token，跳过 probe")
        return "none"
    print(f"[Team] 导入 {email} 到 gpt-team 并探测邀请权限 ...")
    probe = team_client.import_probe(rt)
    st = probe.get("status", "unknown")
    if st == "ok":
        print(f"[Team] ✅ {email} invite=ok  accountId={probe.get('account_id')}")
    elif st == "no_permission":
        print(f"[Team] ⚠️  {email} invite=NO_PERMISSION  域 {domain} 将冷却")
    elif st == "failed":
        print(f"[Team] ❌ {email} 导入失败: {probe.get('error')}")
    else:
        print(f"[Team] ❓ {email} status={st}  err={probe.get('error','')[:200]}")
    # Update payment result record
    _augment_card_result_last_match(email, sid, {
        "invite_permission": st,
        "team_gpt_account_pk": probe.get("account_id"),
        "email_domain": domain,
    })
    # Update domain pool
    if pool and domain:
        pool.mark_result(domain, st if st in ("ok", "no_permission") else "unknown")
    return st


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def self_dealer(card_config_path, cardw_config_path=None, use_paypal=False,
                members_count=4, timeout_reg=300, timeout_pay=600,
                invite_accept_gap_s=3,
                resume_owner_email: str = ""):
    """Self-production and self-sales (state machine 2nd form):
      Step 1 (1 time): Registration - Payment - Push team+cpa    →  Ready-made pipeline()
      Step 2 (N times): Registration - Invite to join - Push cpa     →  register() + invite/accept + relogin + cpa_push
    Registration & push flow remains unchanged, only in the member loop before register() follow the approach of pipeline()
    Re-pick proxy + pick domain + rewrite temporary cardw, ensuring each registration uses new proxy/domain."""
    card_config_path = str(Path(card_config_path).resolve())
    card_cfg = _read_card_cfg(card_config_path)
    cardw_path = _load_cardw_path_from_card_cfg(card_cfg, cardw_config_path)
    cpa_cfg = (card_cfg or {}).get("cpa") or {}
    if not cpa_cfg.get("enabled"):
        print("[self-dealer] 警告：CPA 未启用，CPA 推送将跳过")

    if resume_owner_email:
        print("=" * 72)
        print(f"[self-dealer] 跳过 Step 1：复用已存在的 owner = {resume_owner_email}")
        print("=" * 72)
        owner_email = resume_owner_email
    else:
        print("=" * 72)
        print(f"[self-dealer] Step 1/2：Owner 注册 + 支付 + 推送 team+CPA（复用 pipeline()）")
        print("=" * 72)
        owner_record = pipeline(
            card_config_path, cardw_config_path=cardw_path,
            use_paypal=use_paypal, timeout_reg=timeout_reg, timeout_pay=timeout_pay,
        )
        owner_email = (owner_record.get("payment") or {}).get("email") \
            or (owner_record.get("registration") or {}).get("email") or ""
        if (owner_record.get("payment") or {}).get("status") != "succeeded":
            raise RuntimeError("[self-dealer] Owner 支付未成功，无法继续邀请")
        if not owner_email:
            raise RuntimeError("[self-dealer] 取不到 owner email")

    team_id = _find_team_id_from_results(owner_email)
    owner_rt = _find_latest_refresh_token_for_email(owner_email)
    if not (team_id and owner_rt):
        raise RuntimeError(f"[self-dealer] 从支付数据库读 owner 数据失败 team_id={bool(team_id)} rt={bool(owner_rt)}")
    print(f"\n[self-dealer] ✓ Owner 就绪：{owner_email}  team_id={team_id}  rt_len={len(owner_rt)}")

    tok = _oai_exchange_refresh_to_access_token(owner_rt)
    owner_at = tok.get("access_token", "") or ""
    if not owner_at:
        raise RuntimeError("[self-dealer] owner access_token 交换失败")
    mint_team_id = _oai_team_id_from_access_token(owner_at)
    if mint_team_id and mint_team_id != team_id:
        print(f"[self-dealer] ⚠ owner access_token 默认 workspace={mint_team_id} 与 pay-team={team_id} 不一致，仍按 pay-team 邀请")

    reg_cfg = {}
    try:
        reg_cfg = json.loads(Path(cardw_path).read_text(encoding="utf-8"))
    except Exception:
        pass
    mail_cfg = reg_cfg.get("mail", {}) or {}
    # invite/accept/relogin use card_cfg.proxies.list[0] (local gost relay) uniformly,
    # same as the one registered and rewritten by pipeline(). cardw.proxy original credential may expire.
    api_proxies = (card_cfg or {}).get("proxies", {}).get("list") or []
    proxy_url = (api_proxies[0] if api_proxies else "") or reg_cfg.get("proxy", "") or ""
    print(f"[self-dealer] invite/accept/relogin 代理: {proxy_url}")

    # member pool preparation before registration -- completely follow pipeline()'s approach, don't change any flow,
    # just extract the pool/proxy_pool/pick+rewrite logic from pipeline() into member loop
    ts_cfg = card_cfg.get("team_system") or {}
    cd_h = int(ts_cfg.get("domain_cooldown_hours", 24))
    try:
        domain_pool = _build_domain_pool_from_cardw(cardw_path, cd_h)
    except Exception as e:
        print(f"[self-dealer] 构建 domain_pool 异常: {e}")
        domain_pool = None
    try:
        proxy_pool = _build_proxy_pool_from_card_cfg(card_cfg)
    except Exception as e:
        print(f"[self-dealer] 构建 proxy_pool 异常: {e}")
        proxy_pool = None

    sys.path.insert(0, str(CARD_DIR))
    import card as card_mod  # noqa: E402

    members_report = []
    print("\n" + "=" * 72)
    print(f"[self-dealer] Step 2/2：循环 {members_count} 次（注册 - 邀请上车 - 推送 CPA）")
    print("=" * 72)

    for i in range(1, members_count + 1):
        print(f"\n--- [self-dealer] Member {i}/{members_count} ---")
        entry = {"index": i, "email": "", "status": "pending"}
        try:
            # consistent with pipeline(): each time pick proxy + pick domain + rewrite temp cardw
            picked_proxy = proxy_pool.pick() if proxy_pool and proxy_pool.proxies else ""
            if picked_proxy:
                print(f"[self-dealer] ProxyPool 本次代理: {picked_proxy}")
            picked_domain = ""
            if domain_pool and (domain_pool.domains or domain_pool.provisioner):
                try:
                    picked_domain = domain_pool.pick() or ""
                    if picked_domain:
                        domain_pool.mark_used(picked_domain)
                        print(f"[self-dealer] DomainPool 本次使用域: {picked_domain}")
                except Exception as e:
                    print(f"[self-dealer] 挑域异常: {e}")

            effective_cardw = cardw_path
            temp_cardw = None
            if picked_domain or picked_proxy:
                try:
                    temp_cardw = _rewrite_cardw_with_domain(cardw_path, picked_domain, picked_proxy)
                    effective_cardw = temp_cardw
                except Exception as e:
                    print(f"[self-dealer] rewrite cardw 异常: {e}; 沿用原 cardw")

            try:
                reg = register(effective_cardw, timeout=timeout_reg)
            except Exception as e:
                print(f"[self-dealer] ✗ member {i} 注册失败: {e}")
                entry["status"] = f"register_error: {str(e)[:120]}"
                continue
            finally:
                if temp_cardw and os.path.exists(temp_cardw):
                    try: os.unlink(temp_cardw)
                    except Exception: pass

            mem_email = reg.get("email") or ""
            mem_at = reg.get("access_token") or ""
            mem_did = reg.get("device_id") or ""
            mem_pwd = reg.get("password") or ""
            entry["email"] = mem_email
            if not (mem_email and mem_at and mem_pwd):
                print(f"[self-dealer] ✗ member {i} 注册结果字段缺失")
                entry["status"] = "register_incomplete"
                continue

            # invite
            inv = _oai_send_team_invite(owner_at, team_id, mem_email, proxy_url=proxy_url)
            print(f"[self-dealer] invite status={inv['status']}  invite_id={inv['invite_id'][:20] if inv['invite_id'] else '-'}")
            if inv["status"] not in (200, 201):
                entry["status"] = f"invite_failed http={inv['status']} body={inv['body'][:120]}"
                continue

            time.sleep(max(0, invite_accept_gap_s))

            # accept
            acc = _oai_accept_team_invite(mem_at, team_id, mem_did, proxy_url=proxy_url)
            print(f"[self-dealer] accept status={acc['status']}  body={acc['body'][:100]}")
            if acc["status"] not in (200, 201):
                entry["status"] = f"accept_failed http={acc['status']} body={acc['body'][:120]}"
                continue

            # relogin to get refresh_token (follow protocol or Camoufox based on WEBUI_REG_MODE)
            try:
                rt = _exchange_refresh_token_dispatch(
                    email=mem_email, password=mem_pwd, mail_cfg=mail_cfg, proxy_url=proxy_url,
                )
            except Exception as e:
                print(f"[self-dealer] ✗ {mem_email} 重登异常: {e}")
                entry["status"] = f"relogin_error: {str(e)[:120]}"
                continue
            if not rt:
                entry["status"] = "no_rt"
                continue

            # append one payment success record so _cpa_import_after_team can read it
            sid = f"self-dealer-m{i}-{mem_email.split('@')[0][:20]}"
            res_entry = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "status": "succeeded",
                "chatgpt_email": mem_email,
                "session_id": sid,
                "channel": "self_dealer_member",
                "refresh_token": rt,
                "team_account_id": team_id,
            }
            try:
                get_db().add_card_result(res_entry)
            except Exception as e:
                print(f"[self-dealer] 写支付记录失败: {e}")

            # CPA push
            try:
                status = _cpa_import_after_team(mem_email, sid, cpa_cfg)
            except Exception as e:
                status = f"cpa_error: {str(e)[:100]}"
            entry["status"] = status
        except Exception as e:
            import traceback
            print(f"[self-dealer] ✗ member {i} 未捕获异常: {e}")
            traceback.print_exc()
            if not entry.get("status") or entry["status"] == "pending":
                entry["status"] = f"unhandled: {str(e)[:120]}"
        members_report.append(entry)

    # summary
    print("\n" + "=" * 72)
    print("[self-dealer] 汇总")
    print("=" * 72)
    print(f"  Owner:   {owner_email}")
    print(f"  Team ID: {team_id}")
    print(f"  Members ({members_count}):")
    ok_cnt = 0
    for e in members_report:
        marker = "✓" if e["status"] == "ok" else "✗"
        if e["status"] == "ok":
            ok_cnt += 1
        print(f"    {marker} [{e['index']}] {e['email']:55s} → {e['status']}")
    print(f"\n  成功推 CPA 的 member: {ok_cnt}/{members_count}  （owner 单独计）")
    return {"owner": owner_email, "team_id": team_id, "members": members_report}


def promo_link_loop(card_config_path, cardw_config_path=None, count: int = 1,
                    plan: str = "plus", country: str = "ID", currency: str = "IDR",
                    promo_campaign_id: str = ""):
    """Register / login outlook mailbox → call ChatGPT checkout API to grab hosted long URL → store in promo_links table.

    Difference from register-only: set WEBUI_ALLOW_LOGIN=1 → "existing account" does not fast-fail, goes through OTP login
    to reuse account. This allows phone numbers marked by OpenAI in the code-receiving pool to still work (login gets access_token, call checkout API to get promo).

    count: grab N items; 0 = unlimited (until SIGTERM / outlook pool empty)."""
    from pipeline.promo_link import fetch_promo_link

    card_cfg = _read_card_cfg(card_config_path)
    cardw_path = _load_cardw_path_from_card_cfg(card_cfg, cardw_config_path)
    proxy_url = card_cfg.get("proxy", "")

    _ensure_gost_alive(card_cfg)
    # existing account also accepted → outlook fast-fail turned off
    os.environ["WEBUI_ALLOW_LOGIN"] = "1"

    print(f"[promo-link] start count={count or '∞'} plan={plan} {country}/{currency} "
          f"campaign={promo_campaign_id or '(默认)'} proxy={proxy_url or '<none>'}")

    ok_count = 0
    fail_count = 0
    i = 0
    while True:
        i += 1
        if count and i > count:
            break
        print(f"\n{'#'*60}\n# [promo-link] 第 {i}/{count or '∞'} 次\n{'#'*60}")
        try:
            r = register(cardw_path)  # return {email, access_token, cookie_header, device_id, ...}
        except RegistrationError as e:
            fail_count += 1
            print(f"[promo-link] 第 {i} 次 register fail: {str(e)[:200]}")
            if "outlook 池已无 available" in str(e):
                print(f"[promo-link] outlook 池空, 终止 loop. ok={ok_count} fail={fail_count}")
                break
            continue

        email = r.get("email", "?")
        access_token = r.get("access_token") or ""
        cookie_header = r.get("cookie_header") or ""
        device_id = r.get("device_id") or r.get("oai_device_id") or ""
        if not access_token:
            fail_count += 1
            print(f"[promo-link] {email}: register 返 dict 缺 access_token, 跳过")
            continue

        info = fetch_promo_link(
            access_token=access_token,
            cookie_header=cookie_header,
            device_id=device_id,
            plan=plan, country=country, currency=currency,
            promo_campaign_id=promo_campaign_id,
            proxy_url=proxy_url,
        )
        if not info.get("ok"):
            fail_count += 1
            print(f"[promo-link] {email}: fetch checkout fail → {info.get('error', '?')[:200]}")
            continue

        # write DB
        try:
            link_id = get_db().add_promo_link({
                "email": email,
                "checkout_url": info["checkout_url"],
                "cs_id": info["cs_id"],
                "processor_entity": info["processor_entity"],
                "plan_name": info["plan_name"],
                "promo_campaign_id": info["promo_campaign_id"],
                "billing_country": info["billing_country"],
                "billing_currency": info["billing_currency"],
                "amount_due_cents": info["amount_due_cents"],
                "raw_response": info.get("raw") or {},
            })
        except Exception as e:
            fail_count += 1
            print(f"[promo-link] {email}: 写 DB 失败: {e}")
            continue

        ok_count += 1
        amount_display = info["amount_due_cents"]
        promo_hit = "✓" if amount_display and amount_display <= 100 else "(全价?)"
        print(f"[promo-link] ✓ {email} amount_due={amount_display} {info['billing_currency']} cents "
              f"{promo_hit} url={info['checkout_url'][:100]}... id={link_id}")
        print(f"[promo-link] 进度 {i}/{count or '∞'} ok={ok_count} fail={fail_count}")

    print(f"\n[promo-link] 结束: ok={ok_count} fail={fail_count} total={i-1}")


def free_register_loop(card_config_path, cardw_config_path=None, count: int = 0):
    """free_only mode: register free ChatGPT account + run OAuth separately to get rt + push CPA(free).

    Different from daemon/self_dealer: do not enter payment step.

    count = 0 means unlimited (until SIGTERM); > 0 exits after running count times."""
    import hashlib

    card_cfg = _read_card_cfg(card_config_path)
    cardw_path = _load_cardw_path_from_card_cfg(card_cfg, cardw_config_path)
    cpa_cfg = (card_cfg or {}).get("cpa") or {}
    mail_cfg = card_cfg.get("mail") or {}
    proxy_url = card_cfg.get("proxy", "")

    # start gost (if webshare configured)
    _ensure_gost_alive(card_cfg)

    # OAuth Codex client_id: read by card.py:_exchange_refresh_token_with_session
    # OAUTH_CODEX_CLIENT_ID env var; push from cpa.oauth_client_id (subprocess
    # call card module, this env must already be set, otherwise OpenAI returns AuthApiFailure).
    _client_id = (cpa_cfg.get("oauth_client_id") or "").strip()
    if _client_id and not os.environ.get("OAUTH_CODEX_CLIENT_ID"):
        os.environ["OAUTH_CODEX_CLIENT_ID"] = _client_id
        print(f"[free] 自动设 OAUTH_CODEX_CLIENT_ID = {_client_id}")

    # domain pool
    pool = _build_domain_pool_from_cardw(cardw_path)

    print(
        f"[free-register] start count={count or '∞'} "
        f"cpa.enabled={cpa_cfg.get('enabled')} "
        f"proxy={proxy_url or '<none>'}"
    )

    succeeded = failed = 0
    iteration = 0
    while True:
        iteration += 1
        if count > 0 and iteration > count:
            break

        print(f"\n=== [free-register] {iteration}/{count or '∞'} ===")

        picked_domain = pool.pick() if pool and (pool.domains or pool.provisioner) else ""
        temp_cardw = None
        effective_cardw = cardw_path
        if picked_domain:
            temp_cardw = _rewrite_cardw_with_domain(cardw_path, picked_domain, "")
            effective_cardw = temp_cardw
            pool.mark_used(picked_domain)
            print(f"[free-register] 用域: {picked_domain}")

        try:
            try:
                reg = register(effective_cardw)
            except RegistrationError as e:
                print(f"[free-register] {iteration} 注册失败: {e}")
                failed += 1
                time.sleep(5)
                continue

            email = reg.get("email", "")
            password = reg.get("password") or _password_from_email(email)
            sid = reg.get("device_id", "") or hashlib.md5(email.encode()).hexdigest()[:16]

            rt, fail = _exchange_rt_with_classification(email, password, mail_cfg, proxy_url)

            if rt:
                print(f"[free] [{iteration}] register {email} → succeeded rt_len={len(rt)}")
                _set_account_oauth_status(email, "succeeded")
                if cpa_cfg.get("enabled"):
                    cpa_st = _cpa_import_after_team(
                        email, sid, cpa_cfg, refresh_token=rt, is_free=True,
                    )
                    print(f"[free] [{iteration}] cpa({email}) → {cpa_st}")
                succeeded += 1
            else:
                if fail == "account_dead":
                    _set_account_oauth_status(email, "dead", fail)
                    print(f"[free] [{iteration}] register {email} → dead ({fail})")
                else:
                    _set_account_oauth_status(email, "transient_failed", fail)
                    print(f"[free] [{iteration}] register {email} → transient_failed ({fail})")
                failed += 1
        finally:
            if temp_cardw and os.path.exists(temp_cardw):
                try:
                    os.unlink(temp_cardw)
                except Exception:
                    pass

        time.sleep(5)

    print(f"\n[free-register] 完成 succeeded={succeeded} failed={failed}")


def free_backfill_rt_loop(card_config_path, cardw_config_path=None):
    """free_only mode: read registered accounts from database to supplement old accounts with rt + push CPA(free).

    Skip: accounts with existing refresh_token / oauth_status==succeeded / oauth_status==dead /
    transient_failed within 6h cooldown."""
    import hashlib

    card_cfg = _read_card_cfg(card_config_path)
    cpa_cfg = (card_cfg or {}).get("cpa") or {}
    mail_cfg = card_cfg.get("mail") or {}
    proxy_url = card_cfg.get("proxy", "")

    _ensure_gost_alive(card_cfg)

    # OAuth Codex client_id (same as free_register_loop, avoid AuthApiFailure)
    _client_id = (cpa_cfg.get("oauth_client_id") or "").strip()
    if _client_id and not os.environ.get("OAUTH_CODEX_CLIENT_ID"):
        os.environ["OAUTH_CODEX_CLIENT_ID"] = _client_id
        print(f"[free] 自动设 OAUTH_CODEX_CLIENT_ID = {_client_id}")

    accounts = _load_registered_accounts()
    if not accounts:
        print("[free-backfill] 数据库注册账号为空，无可处理账号")
        return

    todo = []
    skip_has_rt = skip_succeeded = skip_dead = skip_cooldown = 0
    seen_emails = set()
    for acc in accounts:
        email = (acc.get("email") or "").strip()
        if not email or email.lower() in seen_emails:
            continue
        seen_emails.add(email.lower())
        if acc.get("refresh_token"):
            skip_has_rt += 1
            continue
        s = _get_account_oauth_status(email)
        if s:
            status = s.get("status", "")
            if status == "succeeded":
                skip_succeeded += 1
                continue
            if status == "dead":
                skip_dead += 1
                continue
            if status == "transient_failed" and _should_skip_oauth_account(email):
                skip_cooldown += 1
                continue
        todo.append(acc)

    print(
        f"[free-backfill] 共 {len(accounts)} 账号, todo={len(todo)} "
        f"(skip: has_rt={skip_has_rt} succeeded={skip_succeeded} "
        f"dead={skip_dead} cooldown={skip_cooldown})"
    )

    if not todo:
        return

    succeeded = failed = 0
    for i, acc in enumerate(todo, 1):
        email = acc.get("email", "")
        password = acc.get("password") or _password_from_email(email)
        sid = acc.get("device_id", "") or hashlib.md5(email.encode()).hexdigest()[:16]

        print(f"\n=== [free-backfill] [{i}/{len(todo)}] {email} ===")

        rt, fail = _exchange_rt_with_classification(email, password, mail_cfg, proxy_url)

        if rt:
            print(f"[free] [{i}/{len(todo)}] backfill {email} → succeeded rt_len={len(rt)}")
            _set_account_oauth_status(email, "succeeded")
            if cpa_cfg.get("enabled"):
                cpa_st = _cpa_import_after_team(
                    email, sid, cpa_cfg, refresh_token=rt, is_free=True,
                )
                print(f"[free] [{i}/{len(todo)}] cpa({email}) → {cpa_st}")
            succeeded += 1
        else:
            if fail == "account_dead":
                _set_account_oauth_status(email, "dead", fail)
                print(f"[free] [{i}/{len(todo)}] backfill {email} → dead ({fail})")
            else:
                _set_account_oauth_status(email, "transient_failed", fail)
                print(f"[free] [{i}/{len(todo)}] backfill {email} → transient_failed ({fail})")
            failed += 1

        time.sleep(3)

    print(f"\n[free-backfill] 完成 succeeded={succeeded} failed={failed}")


def main():
    parser = argparse.ArgumentParser(
        description="Pipeline: ChatGPT 注册 → Stripe/PayPal 支付",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python pipeline.py --config CTF-pay/config.paypal.json --paypal
  python pipeline.py --config CTF-pay/config.paypal.json --paypal --plan plus
  python pipeline.py --register-only --cardw-config CTF-reg/config.paypal-proxy.json
  python pipeline.py --pay-only --config CTF-pay/config.paypal.json --paypal --plan plus
  python pipeline.py --config CTF-pay/config.paypal.json --paypal --batch 5
  python pipeline.py --config CTF-pay/config.paypal-ip2.json --paypal --self-dealer 4
        """,
    )
    parser.add_argument("--config", default="CTF-pay/config.paypal.json",
                        help="card.py 支付配置文件")
    parser.add_argument("--cardw-config", default=None,
                        help="CTF-reg 注册配置文件 (默认从 --config 中读取)")
    parser.add_argument("--paypal", action="store_true",
                        help="使用 PayPal 支付")
    parser.add_argument("--gopay", action="store_true",
                        help="使用 GoPay tokenization (印尼 e-wallet, ChatGPT Plus)")
    parser.add_argument("--plan", choices=("team", "plus"), default=None,
                        help="覆盖订阅方案（不指定则用 config.fresh_checkout.plan）；"
                             "plus 自动剥掉 workspace/seat，并把 promo 默认改为 plus-1-month-free")
    parser.add_argument("--gopay-otp-file", default=None,
                        help="webui 模式: gopay.py 从该文件读取 WhatsApp OTP")
    parser.add_argument("--qris", action="store_true",
                        help="使用 QRIS 扫码支付 (印尼央行统一二维码标准, 无需 OTP/PIN/绑定)")
    parser.add_argument("--register-only", action="store_true",
                        help="仅注册，不支付")
    parser.add_argument("--pay-only", action="store_true",
                        help="仅支付（优先复用最近注册但未支付账号；没有则使用配置文件中的 session_token）")
    parser.add_argument("--batch", type=int, default=0,
                        help="批量运行 N 次")
    parser.add_argument("--delay", type=float, default=30,
                        help="批量模式下每次间隔秒数 (默认 30)")
    parser.add_argument("--workers", type=int, default=3,
                        help="并行 worker 数 (默认 3)")
    parser.add_argument("--daemon", action="store_true",
                        help="常驻 daemon：维护 gpt-team 系统可用账号数（读 config.daemon 段）")
    parser.add_argument("--self-dealer", type=int, default=0, metavar="N",
                        help="自产自销：1 个 owner 付费开 Team + N 个 member 邀请上车 + 全部推 CPA")
    parser.add_argument("--self-dealer-resume", default="", metavar="OWNER_EMAIL",
                        help="自产自销 resume 模式：跳过 Step 1，用已注册付费过的 owner（从支付数据库读 team_id + rt）")
    parser.add_argument("--promo-link", action="store_true",
                        help="promo-link 模式: 注册/登录 outlook → 调 ChatGPT checkout 拿命中 promo 的 hosted long URL 存 DB")
    parser.add_argument("--promo-plan", default="plus", choices=["plus", "team"],
                        help="promo-link 拿哪个 plan 的优惠 (默认 plus)")
    parser.add_argument("--promo-country", default="ID",
                        help="billing 国家代码 (默认 ID 拿 IDR 1 月免费)")
    parser.add_argument("--promo-currency", default="IDR",
                        help="billing 币种 (默认 IDR)")
    parser.add_argument("--promo-campaign-id", default="",
                        help="promo_campaign_id 覆盖 (默认 plus-1-month-free / team-1-month-free)")
    parser.add_argument("--free-register", action="store_true",
                        help="free_only 模式：循环注册免费 ChatGPT 号 + OAuth 拿 rt + 推 CPA(free)")
    parser.add_argument("--free-backfill-rt", action="store_true",
                        help="free_only 模式：读数据库老号记录补 rt + 推 CPA(free)，跳过已 succeeded/dead")
    parser.add_argument("--count", type=int, default=0, metavar="N",
                        help="--free-register 模式下注册 N 次后退出（0 = 无限）")
    parser.add_argument("--target-emails", default="", metavar="EMAILS",
                        help="逗号分隔的目标 email 列表。配合 --pay-only 或 --rt-only 用，"
                             "对 webui inventory 选中的具体账号操作")
    parser.add_argument("--rt-only", action="store_true",
                        help="只对 --target-emails 跑 RT 交换：用现有 password/session "
                             "走 Codex OAuth 拿 refresh_token 写回 DB（不付款）")
    args = parser.parse_args()

    pay_flags = sum(1 for x in (args.paypal, args.gopay, args.qris) if x)
    if pay_flags > 1:
        print("[ERROR] --paypal / --gopay / --qris 互斥，只能用一种", file=sys.stderr)
        sys.exit(2)
    if args.free_register and args.free_backfill_rt:
        print("[ERROR] --free-register 与 --free-backfill-rt 互斥", file=sys.stderr)
        sys.exit(2)

    # --plan takes effect before all branches: generate temp config, all subsequent register/pay/daemon/
    # batch/self_dealer/pay_only_targets entry points use this patched path, avoid
    # each branch handling it separately.
    if args.plan:
        args.config = _apply_plan_override(args.config, args.plan)

    # webshare/gost keepalive: all paths share one entry point (previously each sub-pipeline called it separately,
    # register-only / rt-only / target-emails branches would miss it, causing camoufox public_ip
    # through socks5://127.0.0.1:18898 connect refused). Place it after --plan,
    # let gost read the patched config (plan doesn't affect webshare field, but order is more stable).
    try:
        _entry_card_cfg = _read_card_cfg(args.config)
        _ensure_gost_alive(_entry_card_cfg)
    except Exception as _e:
        print(f"[gost] 入口保活失败（不致命，继续）: {_e}")

    try:
        if args.promo_link:
            promo_link_loop(
                args.config, cardw_config_path=args.cardw_config,
                count=args.count, plan=args.promo_plan,
                country=args.promo_country, currency=args.promo_currency,
                promo_campaign_id=args.promo_campaign_id,
            )
            return
        if args.free_register:
            free_register_loop(args.config, cardw_config_path=args.cardw_config,
                                count=args.count)
            return
        if args.free_backfill_rt:
            free_backfill_rt_loop(args.config, cardw_config_path=args.cardw_config)
            return
        if args.daemon:
            daemon(args.config, cardw_config_path=args.cardw_config, use_paypal=args.paypal)
            return
        if args.self_dealer > 0:
            self_dealer(args.config, cardw_config_path=args.cardw_config,
                        use_paypal=args.paypal, members_count=args.self_dealer,
                        resume_owner_email=args.self_dealer_resume)
            return

        target_emails_list: list[str] = []
        if args.target_emails:
            target_emails_list = [e.strip() for e in args.target_emails.split(",") if e.strip()]

        if args.rt_only:
            if not target_emails_list:
                print("[ERROR] --rt-only 必须配合 --target-emails 使用", file=sys.stderr)
                sys.exit(2)
            r = rt_only_targets(args.config, target_emails_list)
            print(f"\n结果: ok={r['ok']} skip={r['skip']} fail={r['fail']}")
            return

        if args.pay_only and target_emails_list:
            r = pay_only_targets(
                args.config, target_emails_list,
                use_paypal=args.paypal, use_gopay=args.gopay, use_qris=args.qris,
                gopay_otp_file=args.gopay_otp_file,
            )
            print(f"\n结果: ok={r['ok']} fail={r['fail']}")
            return

        # batch is loop wrapper, orthogonally combined with register-only / pay-only
        if args.batch > 0:
            batch(args.config, args.batch, delay=args.delay, workers=args.workers,
                  use_paypal=args.paypal, cardw_config_path=args.cardw_config,
                  register_only=args.register_only, pay_only=args.pay_only,
                  use_gopay=args.gopay, use_qris=args.qris,
                  gopay_otp_file=args.gopay_otp_file)

        elif "--batch" in sys.argv:
            print(f"[ERROR] --batch 参数必须 ≥ 1（当前 {args.batch}）", file=sys.stderr)
            sys.exit(2)

        elif args.register_only:
            cardw_cfg = args.cardw_config
            if not cardw_cfg:
                with open(args.config) as f:
                    cfg = json.load(f)
                cardw_cfg = cfg.get("fresh_checkout", {}).get("auth", {}).get(
                    "auto_register", {}).get("config_path", "CTF-reg/config.noproxy.json")
            result = register(cardw_cfg)
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.pay_only:
            result = pay_only(
                args.config,
                use_paypal=args.paypal,
                use_gopay=args.gopay,
                use_qris=args.qris,
                gopay_otp_file=args.gopay_otp_file,
            )
            print(f"\n结果: {result.get('status', '?')}")

        else:
            pipeline(args.config, cardw_config_path=args.cardw_config,
                     use_paypal=args.paypal, use_gopay=args.gopay,
                     use_qris=args.qris,
                     gopay_otp_file=args.gopay_otp_file)

    except (RegistrationError, PaymentError) as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[中断]")
        sys.exit(130)


if __name__ == "__main__":
    main()
