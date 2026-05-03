#!/usr/bin/env python3
"""
Pipeline 调度器：注册 ChatGPT 账号 → Stripe/PayPal 支付

⚠️ 仅限授权范围内使用（你拥有的系统 / 合法 CTF / 授权 bug bounty in-scope 资产 /
   安全研究）。运行本程序即视为同意 NOTICE 文件全部条款。AS IS 提供，无任何担
   保，一切后果使用者自负。详见仓库根目录 NOTICE 与 README.md 免责声明段。

用法:
  # 全链路 (注册 + 支付)
  python pipeline.py --config CTF-pay/config.paypal.json --paypal

  # 仅注册
  python pipeline.py --register-only --cardw-config CTF-reg/config.paypal-proxy.json

  # 仅支付 (优先复用 SQLite 数据库里最近未支付账号)
  python pipeline.py --pay-only --config CTF-pay/config.paypal.json --paypal

  # 批量
  python pipeline.py --config CTF-pay/config.paypal.json --paypal --batch 5 --delay 30
"""

import argparse
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

ROOT = Path(__file__).resolve().parent
CARDW_DIR = ROOT / "CTF-reg"
CARD_DIR = ROOT / "CTF-pay"
CARD_PY = CARD_DIR / "card.py"
GOPAY_PY = CARD_DIR / "gopay.py"
OUTPUT_DIR = ROOT / "output"
(OUTPUT_DIR / "logs").mkdir(parents=True, exist_ok=True)
DOMAIN_STATE_KEY = "email_domain_state"
DAEMON_STATE_KEY = "daemon_state"
SECRETS_KEY = "secrets"
RUNTIME_DB_FILE = OUTPUT_DIR / "webui.db"


# ──────────────────────────────────────────────
# 0. 邮箱域池 + gpt-team 系统客户端 + Cloudflare 子域按需开通
# ──────────────────────────────────────────────

class CloudflareDomainProvisioner:
    """通过 Cloudflare API 按需开通新子域：加 3 MX + 1 TXT/SPF，挂 zone 的 catch-all routing。
    zone 的 Email Routing 必须已开启（有 `{type:"all"}` forward 规则即可；子域自动继承）。"""

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
        # 不经环境代理（和 TeamSystemClient 一样，避免被 http_proxy 劫持到本地代理）
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
        """列出当前 zone 下所有已有 MX 记录的子域（不含根域自身）。"""
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
        """开通一个新子域（加 3 MX + 1 TXT），返回完整域名（小写）。
        name 不传则随机生成并避开已存在的。"""
        zid = self.zone_id()
        existing = set(self.list_subdomains())
        attempt = 0
        chosen_full = None
        while attempt < max_retries:
            attempt += 1
            label = name if name else self._random_subdomain_name()
            full = f"{label}.{self.zone_name}".lower()
            if full in existing:
                if name:  # 指定了还冲突 -> 直接报错
                    raise RuntimeError(f"CF 子域 {full} 已存在")
                continue
            chosen_full = full
            break
        if not chosen_full:
            raise RuntimeError(f"CF 随机生成子域名失败（{max_retries} 次都冲突）")

        # 3 条 MX + 1 条 TXT
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
        """删除子域下所有 DNS 记录。返回删除数。"""
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
    """多 zone 包装器：开通时优先用 active_zone，按域名后缀路由删除。"""

    def __init__(self, sub_provisioners):
        self.subs = [p for p in (sub_provisioners or []) if p is not None]
        if not self.subs:
            raise ValueError("MultiZoneDomainProvisioner 需要至少一个子 provisioner")
        self.active_zone = None  # None = 全部 zones 随机

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
        """active_zone 优先；quota exceeded / HTTP 400 时自动 fallback 到其他 zone。"""
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
                # quota 类错误 / 400 Bad Request → fallback 到下一个 zone
                if "quota" in msg or "400" in msg or "exceeded" in msg:
                    print(f"[CF] {p.zone_name} 开通失败({str(e)[:60]})，fallback 下一 zone")
                    continue
                # 其他错误直接抛
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
    """持久化邮箱域状态：ok / burned (带 cooldown)。支付后拿 invite 探测结果反馈更新。
    可选 provisioner：当可用域 < min_available 时，通过 Cloudflare API 按需开通新子域。"""

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
        """挑一个可用域，策略：available 里 'last_used_ts' 最小（最久没用）。
        如启用 provisioner，且 available < min_available，先补给新子域。"""
        now_ts = time.time()
        available = [d for d in self.domains if self._is_available(d, now_ts)]

        # 可用不足 → 按需开通新子域（Cloudflare API）
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
            # 全烧了：挑冷却时间最短的那个先用（尽量减少等待）
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
        连续 2 次 no_permission 升级为 permanent_burned 并触发 Cloudflare 子域清理。
        ok 结果重置 no_permission 计数。"""
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
            meta["no_permission_count"] = 0  # ok 重置计数
        self._save()

    def _cleanup_permanent_burned(self, domain):
        """永久 burn 后：Cloudflare 删除子域 DNS 记录，pool.domains 移除。
        根域（zone_name 本身）不删，只清子域。"""
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
        # 从 pool.domains 里移除（保留 state 用于审计）
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
        # 也列出已永久 burn 的（从 pool 移除但 state 里仍有）
        for d, m in self.state.get("domains", {}).items():
            if d in self.domains:
                continue
            if m.get("status") == "permanent_burned":
                rows.append((d, "PERM_BURN", m.get("last_result", "-")))
        return rows


class TeamSystemClient:
    """gpt-team 系统客户端：login → batch-import 单条 RT，SSE 解析 probe 结果。
    使用独立的 no-proxy opener，避免被环境变量里的 http_proxy 劫持到本地代理/监控。"""

    def __init__(self, base_url, username, password, timeout_s=60):
        import urllib.request
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_s = timeout_s
        self.jwt = None
        self.jwt_exp_ts = 0
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),  # 显式清空代理
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
        # JWT exp 24h，提前 1h 续签
        self.jwt_exp_ts = time.time() + 23 * 3600
        print(f"[Team] 登录成功 user={body.get('user',{}).get('username')}")

    def _ensure_jwt(self):
        if not self.jwt or time.time() >= self.jwt_exp_ts:
            self._login()

    def import_probe(self, refresh_token):
        """导入单条 RT + 等 SSE item 事件。返回 {status, account_id, error, raw}"""
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
        """查 gpt-team 里 '可用' 账号数。
        usage='recovery' → 补号池（默认，daemon 维护目标）
        usage='sales'    → 对外开放池
        可用 = account_usage 匹配 & !isBanned & !isDisabled & !noInvitePermission
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
        """PUT /api/admin/proxy-config 更新全局代理。返回 {proxyUrl, ...}"""
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
# 1. 注册模块
# ──────────────────────────────────────────────

class RegistrationError(RuntimeError):
    pass


def register(cardw_config_path, proxy=None, python="python3", timeout=600, browser: bool = True):
    """注册一个新 ChatGPT 账号。

    browser=True 时走 Camoufox 真浏览器注册流程（Turnstile 真实执行，避免账号被风控）。
    browser=False 时走原 HTTP 协议注册（快但容易被标记 bot）。

    返回 dict: {email, session_token, access_token, device_id, ...}
    """
    cardw_config_path = str(Path(cardw_config_path).resolve())
    auth_bundle_dir = str(CARDW_DIR)

    if browser:
        script = r"""
import json, logging, os, sys
auth_bundle_dir = sys.argv[1]
config_path = sys.argv[2]
sys.path.insert(0, auth_bundle_dir)
from config import Config
from mail_provider import MailProvider
from browser_register import browser_register
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
from auth_flow import AuthFlow
from mail_provider import MailProvider
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
    if proxy:
        # 代理通过配置文件传递，不通过环境变量

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
# 2. 支付模块
# ──────────────────────────────────────────────

class PaymentError(RuntimeError):
    pass


class DatadomeSliderError(PaymentError):
    """PayPal 页面被 DataDome 可见滑块拦截，需要换 IP 重试。"""
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
        device_id=None, use_paypal=False, use_gopay=False,
        gopay_otp_file=None, python="python3", timeout=600):
    """执行 Stripe 支付流程。

    use_paypal / use_gopay 互斥：默认 card 路径，paypal 走 PayPal browser，
    gopay 走 GoPay tokenization (CTF-pay/gopay.py)。
    如果提供了 session_token/access_token，会临时覆盖配置文件中的凭证。
    gopay_otp_file: webui 模式 OTP 文件路径（gopay.py file-watch 读取）。
    返回 dict: {status, session_id, chatgpt_email, ...}
    """
    if use_paypal and use_gopay:
        raise PaymentError("use_paypal 与 use_gopay 互斥")

    card_config_path = str(Path(card_config_path).resolve())
    cfg_for_env = {}

    # 如果有外部凭证，创建临时配置
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
        # 禁用 auto_register（凭证已有）
        auto = auth.get("auto_register", {})
        auto["enabled"] = False
        auth["auto_register"] = auto

        tmp_config = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="pipeline_pay_",
            dir=str(CARD_DIR), delete=False,
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

    cmd = [python, str(CARD_PY), "auto",
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
            text=True, env=env,
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
# 3. Pipeline 调度
# ──────────────────────────────────────────────

def pipeline(card_config_path, cardw_config_path=None, use_paypal=False,
             use_gopay=False, gopay_otp_file=None,
             timeout_reg=300, timeout_pay=600,
             pool=None, team_client=None, card_cfg=None, proxy_pool=None):
    """全链路: 注册 → 支付 → (可选) gpt-team 导入探测 → 更新域池
    proxy_pool 非空时从 pool 挑代理，同时覆盖 CTF-reg + CTF-pay 两个 config 的 proxy 字段"""
    card_config_path = str(Path(card_config_path).resolve())

    if card_cfg is None:
        card_cfg = _read_card_cfg(card_config_path)
    cardw_config_path = _load_cardw_path_from_card_cfg(card_cfg, cardw_config_path)

    # 内部 fallback：如果外部没传 pool/team/proxy，也自动构造（单次调用场景）
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

    # 挑代理（独立于域）
    picked_proxy = proxy_pool.pick() if proxy_pool and proxy_pool.proxies else ""
    if picked_proxy:
        print(f"[ProxyPool] 本次代理: {picked_proxy}")

    # 挑域 + 写临时 CTF-reg config（同时覆盖 proxy）
    # pool.domains 为空时，若有 provisioner 仍要让 pick() 触发自动开通
    picked_domain = pool.pick() if pool and (pool.domains or pool.provisioner) else ""
    temp_cardw = None
    effective_cardw = cardw_config_path
    if picked_domain or picked_proxy:
        temp_cardw = _rewrite_cardw_with_domain(cardw_config_path, picked_domain, picked_proxy)
        effective_cardw = temp_cardw
        if picked_domain:
            pool.mark_used(picked_domain)
            print(f"[DomainPool] 本次使用域: {picked_domain}")

    # CTF-pay 也覆盖 proxy（支付流程走同一代理）
    temp_card = None
    effective_card = card_config_path
    if picked_proxy:
        temp_card = _rewrite_card_with_proxy(card_config_path, picked_proxy)
        effective_card = temp_card

    ts = datetime.now(timezone.utc).isoformat()
    record = {"ts": ts, "registration": {}, "payment": {},
              "domain": picked_domain, "proxy": picked_proxy}

    try:
        # Step 1: 注册
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

        # Step 2: 支付
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

        # Step 3: 支付成功 → gpt-team 导入 + invite 探测
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

        # Step 4: 支付成功 → 额外导入到 CPA（CLIProxyAPI）
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
    """单个 pipeline 任务（供并行调度）"""
    idx, card_config_path, kwargs = args_tuple
    try:
        r = pipeline(card_config_path, **kwargs)
        r["batch_index"] = idx
        return r
    except Exception as e:
        return {"batch_index": idx, "status": "error", "error": str(e)[:200]}


def _run_one_pay_only(args_tuple):
    """PayPal 并发模式下：注册已完成，只串行支付用（预留占位，batch 里不再单独使用）"""
    return {"batch_index": args_tuple[0], "status": "error", "error": "deprecated path"}


def _register_one(args_tuple):
    """单个注册任务。args_tuple = (idx, cardw_config_path, pool_or_None)
    pool 非空时为每个 worker 独立 pick 域 + 改写临时 cardw config。"""
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
    """批量运行 N 次。可选 modifier:
       - register_only=True: 每次只 register（不付费），workers 串行
       - pay_only=True:      每次只 pay_only（复用未付账号），workers 串行
       - 都不开:              每次走完整 pipeline（注册+付费）
    """
    use_paypal = kwargs.get("use_paypal", False)
    is_register_only = bool(kwargs.pop("register_only", False))
    is_pay_only = bool(kwargs.pop("pay_only", False))
    use_gopay = bool(kwargs.pop("use_gopay", False))
    gopay_otp_file = kwargs.pop("gopay_otp_file", "")

    # 构造共享 pool + team_client（所有 worker 复用）
    card_cfg = _read_card_cfg(card_config_path)
    cardw_path = _load_cardw_path_from_card_cfg(card_cfg, kwargs.get("cardw_config_path"))

    # ── register-only batch：每次 register，串行（避免并行同 IP 触发风控）
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

    # ── pay-only batch：每次 pay_only（复用未付账号），串行
    if is_pay_only:
        print(f"\n[batch] === pay-only × {count} 串行 ===")
        results = []
        ok_count = 0
        for i in range(count):
            print(f"\n{'#'*60}\n# 批次 {i+1}/{count}  (pay-only)\n{'#'*60}")
            try:
                r = pay_only(
                    card_config_path,
                    use_paypal=use_paypal, use_gopay=use_gopay,
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
        # PayPal 模式：并行注册 → 串行支付（共用 PayPal 账号不能并行 2FA）
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

        # Step 2: 串行支付 + team probe
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
        # 全串行
        results = []
        for i in range(count):
            print(f"\n{'#'*60}")
            print(f"# 批次 {i + 1}/{count}")
            print(f"{'#'*60}")
            results.append(_run_one((i, card_config_path, kwargs)))
            if i < count - 1 and delay > 0:
                time.sleep(delay)
    else:
        # 非 PayPal: 全并行
        from concurrent.futures import ThreadPoolExecutor, as_completed
        print(f"\n[batch] 并行模式: {workers} workers × {count} 任务")
        tasks = [(i, card_config_path, kwargs) for i in range(count)]
        results = [None] * count
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_run_one, t): t[0] for t in tasks}
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()

    # 汇总
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
# 工具函数
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


def pay_only(card_config_path, *, use_paypal=False, use_gopay=False,
             gopay_otp_file=None, timeout_pay=600, prefer_recent=True):
    """Retry payment only.

    Default behavior is now:
      1. use the newest account in SQLite storage that has not
         already succeeded/been consumed;
      2. fall back to credentials embedded in the payment config if no reusable
         account exists.

    This preserves the old config-token path while preventing freshly
    registered-but-unpaid accounts from being wasted.
    """
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
# free_only mode helpers (OAuth 状态管理 + 失败分类)
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
    """True = 跳过；succeeded/dead 永跳，transient_failed 在 6h cooldown 内跳。"""
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
    """跟 browser_register 一致：local + domain（含点）；< 8 字符附加 2026OpenAI。"""
    p = (email or "").replace("@", "")
    if len(p) < 8:
        p = f"{p}2026OpenAI"
    return p


def _classify_oauth_failure(log: str) -> str:
    """从 _exchange_refresh_token_with_session 的 print log 推断失败原因。

    优先级：account_dead > add_phone_blocked > otp_timeout >
    consent_failed > no_callback > unknown
    """
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
    # OpenAI 拒绝 OAuth authorize 参数（最常见：client_id 占位符 / 失效 token）
    if "AuthApiFailure" in log or "auth.openai.com/error?payload=" in log:
        return "auth_api_failure"
    if "consent" in log and "code=" not in log and "[RT] callback 无" not in log:
        return "consent_failed"
    return "unknown"


def _exchange_rt_with_classification(
    email: str, password: str, mail_cfg: dict, proxy_url: str
):
    """包 card._exchange_refresh_token_with_session 加失败分类。

    返回 (rt, fail_reason)：rt 非空时 fail_reason 为空串。
    print 输出 tee 到真 stdout（webui runner 能看到进度）+ 缓冲（用于 grep 分类）。
    """
    import io

    # card.py 内部会 from cf_kv_otp_provider import ...（在 CTF-reg/）
    # 要确保两个目录都在 sys.path，否则 OTP 路径 ImportError 立即返 ""。
    if str(CARDW_DIR) not in sys.path:
        sys.path.insert(0, str(CARDW_DIR))
    if str(CARD_DIR) not in sys.path:
        sys.path.insert(0, str(CARD_DIR))
    import card as card_mod  # noqa: E402

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
            rt = card_mod._exchange_refresh_token_with_session(
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
    """从 SQLite 运行时库读取 Cloudflare/KV 等敏感凭证。"""
    try:
        data = get_db().get_runtime_json(SECRETS_KEY, {})
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[secrets] 读数据库失败: {e}")
        return {}


# ──────────────────────────────────────────────
# Daemon：持续维护 gpt-team 系统可用账号数
# ──────────────────────────────────────────────

def _cleanup_temp_leftovers(max_age_s: int = 1800, verbose: bool = False) -> dict:
    """清理孤儿临时文件以防 /tmp（tmpfs）被 SIGKILL 残留吃爆。
    只清 mtime 超过 max_age_s 的：
    - /tmp/chatgpt_reg_*        （Camoufox 注册临时 profile）
    - /tmp/xvfb-run.*           （xvfb-run auth 目录，需无 Xvfb 引用）
    - CTF-pay/pipeline_pay_*.json    （pay 临时 config）
    - CTF-reg/pipeline_cardw_*.json  （register 临时 config）
    """
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

    # 2. xvfb-run.* 目录（保留活跃 Xvfb 引用的）
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

    # 3. 项目内临时 config（pipeline 产生的 temp json）
    for base, pattern, key in [(CARD_DIR, "pipeline_pay_*.json", "cfg_pay"),
                                 (CARDW_DIR, "pipeline_cardw_*.json", "cfg_cardw")]:
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
    """CF zone record quota 清理：gpt-team DB 里所有 is_banned/is_disabled/expired 的
    账号，它们的邮箱子域如果在 DB 里没有任何活账号，就把 CF 上该子域的 DNS 记录删掉。
    每子域释放约 4 条 records（3 MX + 1 TXT/SPF）。返回 stats dict。"""
    import sqlite3
    from collections import defaultdict
    stats = {"checked": 0, "dead_subs": 0, "cleaned_subs": 0, "records_removed": 0,
             "errors": 0}
    if not provisioner:
        return stats
    if not gpt_team_db_path or not os.path.exists(gpt_team_db_path):
        print(f"[CF-cleanup] 跳过：gpt-team DB 不存在 ({gpt_team_db_path})")
        return stats
    # 当前 provisioner 覆盖的 zone（单 zone 用 zone_name 属性；多 zone 走 zone_names）
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
        # dead 判定：banned / disabled / expired / no_invite_permission
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
            # 必须是子域（严格 endswith "." + zone），apex 根域本身绝不碰
            if not any(sub.endswith("." + z) for z in zones):
                continue
            if sub in {z.lower() for z in zones}:  # 双保险
                continue
            stats["checked"] += 1
            # no_invite_permission 的账号对业务没用，视同 dead
            if banned or disabled or expired or no_perm:
                by_sub[sub]["dead"] += 1
            else:
                by_sub[sub]["alive"] += 1
    except Exception as e:
        print(f"[CF-cleanup] 查 DB 失败: {e}")
        return stats
    finally:
        conn.close()

    # 1. DB 里所有账号对应子域都 dead → 清
    fully_dead_db = set(s for s, v in by_sub.items() if v["alive"] == 0 and v["dead"] > 0)
    # 2. CF 上有 MX 但 DB 里完全没对应账号的孤儿子域 → 也清
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
    """
    状态机：常驻维护 gpt-team 系统里 '可用邀请' 账号数 ≥ target_ok_accounts。
    - 可用定义：isOpen & !isBanned & !isDisabled & !noInvitePermission & seat 未满
    - 限流：每小时 / 每日上限（避免短时间批量触发风控）
    - 保护：连续失败 ≥ max_consecutive_failures 时冷却 consecutive_fail_cooldown_s
    - 状态持久化：SQLite runtime_meta[daemon_state]
    - 优雅退出：SIGINT/SIGTERM 完成当前循环后停止
    """
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
    # gpt-team 的 batch-import 默认把新账号放入 "recovery" 补号池（is_open=0 + account_usage='recovery'），
    # 对外售卖时 admin 手动改 sales 并开启。daemon 默认维护补号池数量。
    usage_pool = str(d_cfg.get("usage_pool", "recovery")).lower()

    ts_cfg = card_cfg.get("team_system") or {}
    cd_h = int(ts_cfg.get("domain_cooldown_hours", 24))
    pool = _build_domain_pool_from_cardw(cardw_path, cd_h)
    team_client = _build_team_client_from_card_cfg(card_cfg)
    _proxy_pool = _build_proxy_pool_from_card_cfg(card_cfg)  # 预留，暂不参与 pipeline 分发

    if not team_client:
        raise RuntimeError("daemon 需要 team_system.enabled=true")

    stop = {"flag": False}
    def _sig(*_a):
        print("\n[daemon] 收到停止信号，跑完当前循环即退出 ...")
        stop["flag"] = True
    for sg in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(sg, _sig)
        except Exception: pass

    # Webshare 自动轮换配置
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

    # 状态
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

    # 初始化 current_zone（若 provisioner 是多 zone）
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

    # 启动时做一次激进清理（上次 SIGKILL 残留）
    _c0 = _cleanup_temp_leftovers(max_age_s=300, verbose=True)
    if sum(v for k, v in _c0.items() if k != "bytes"):
        print(f"[cleanup] 启动清理完毕")
    # CF 死子域清理：启动时 + 循环中按 cf_cleanup_every_n_runs 周期触发
    cf_db_path = (card_cfg.get("daemon") or {}).get(
        "gpt_team_db_path", "/path/to/gpt-team/backend/db/database.sqlite"
    )
    cf_cleanup_every = int((card_cfg.get("daemon") or {}).get(
        "cf_cleanup_every_n_runs", 30
    ))
    _cleanup_dead_cf_subdomains(getattr(pool, "provisioner", None), cf_db_path)
    # 启动时检查 gost（断了自动拉起）
    _ensure_gost_alive(card_cfg, team_client)

    _hour_label = f"{rate_per_hour}/h" if rate_per_hour > 0 else "无限"
    _day_label = f"{rate_per_day}/d" if rate_per_day > 0 else "无限"
    print(f"[daemon] 启动：pool={usage_pool}  target={target}  poll={poll_s}s  rate={_hour_label}, {_day_label}  seat_limit={seat_limit}")
    print(f"[daemon] 历史累计: attempts={state['total_attempts']} ok={state['total_succeeded']} fail={state['total_failed']}")

    kwargs = {"card_cfg": card_cfg, "pool": pool, "team_client": team_client, "use_paypal": use_paypal}

    while not stop["flag"]:
        # 无 Webshare 轮换额度 + 连续 no_perm 触发的冷却闸门
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

        # 限流
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

        # 连续失败保护
        if state["consecutive_failures"] >= max_cfail:
            print(f"[daemon] 连续失败 {state['consecutive_failures']}/{max_cfail}，冷却 {cfail_cool_s/60:.0f} min")
            _save()
            for _ in range(cfail_cool_s):
                if stop["flag"]: break
                time.sleep(1)
            state["consecutive_failures"] = 0
            continue

        # 抖动
        js = random.uniform(float(jitter[0]), float(jitter[1]))
        print(f"[daemon] 抖动 {js:.0f}s 后开跑（可用缺口 {target-usable}）...")
        for _ in range(int(js)):
            if stop["flag"]: break
            time.sleep(1)
        if stop["flag"]: break

        # 跑 1 次 pipeline
        state["total_attempts"] += 1
        run_ts = time.time()
        state["rate_hour_ts"].append(run_ts)
        state["rate_day_ts"].append(run_ts)
        # 每轮前确认 gost 活着（避免 camoufox geoip 失败）
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
            # 可见滑块：card.py 的 drag solver 已尝试且失败。换 IP 也救不了
            # （PayPal 账号风控才是根源），所以不 rotate，只计失败 + 触发自然冷却
            state["total_failed"] += 1
            state["consecutive_failures"] += 1
            state["last_error"] = "datadome_slider"
            perm = "-"
            print(f"[daemon] ⚠ DataDome 滑块 solver 失败  连续失败={state['consecutive_failures']}  "
                  f"（不轮换 IP，换 IP 不解决账号风控）")
        except RegistrationError as e:
            # 注册失败：分两类
            # - OTP 超时 / 域风控 → 计 zone-级 streak，累到阈值切 zone
            # - InvalidIP / geoip / proxy 脱链 等基础设施类 → 不计 zone streak
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

        # 注册失败连续累积到阈值 → 切 zone（OpenAI signup 域风控专用路径）
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
            state["consecutive_failures"] = 0  # 切 zone 给新 zone 重置 fail 计数
            state["total_zone_rotations"] = state.get("total_zone_rotations", 0) + 1
            print(f"[daemon] zone 切换完成，累计 zone 轮换={state['total_zone_rotations']}")

        # IP no_perm streak（始终追踪，与 Webshare 额度状态无关）
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
                # ok 一单也说明 IP 还能用，清掉历史 rotation_disabled 标记，给下次一个机会
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
                        # 非 quota 异常（网络/DNS 抖动等）不锁死 rotation，只打 log
                        print(f"[daemon] ✗ IP 轮换失败（本次跳过，下次 no_perm 再试）: {e}")

                # 若轮换不可用（额度耗尽 / 异常），进 3h 冷却让 IP 自然回血
                if state.get("webshare_rotation_disabled") and \
                   state["ip_no_perm_streak"] >= ws_threshold:
                    cd_end = time.time() + ws_cooldown_s
                    state["no_perm_cooldown_until"] = cd_end
                    state["ip_no_perm_streak"] = 0
                    print(f"[daemon] ⏸ 无轮换能力 + 连续 {ws_threshold} no_perm，冷却 "
                          f"{ws_cooldown_s/3600:.1f}h 到 "
                          f"{datetime.fromtimestamp(cd_end).strftime('%m-%d %H:%M')}")

            # 在当前 zone 内 IP 轮换次数累计到阈值 → 切 zone
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
                # 把池里不属于新 zone 的子域清掉（让下一单强制从新 zone provision）
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
        # 每轮 pipeline 后清一次 30min 前的孤儿，防 /tmp 被 SIGKILL 残留吃爆
        _cleanup_temp_leftovers(max_age_s=1800, verbose=False)
        # 每 cf_cleanup_every_n_runs 单清一次 CF 死子域（0=禁用）
        if cf_cleanup_every > 0 and state["total_attempts"] % cf_cleanup_every == 0:
            try:
                _cleanup_dead_cf_subdomains(getattr(pool, "provisioner", None), cf_db_path)
            except Exception as e:
                print(f"[CF-cleanup] 周期清理异常: {e}")
        # 下一轮前短 sleep
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
        # 支持 zone_names (list) 或单 zone_name，向后兼容
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
# 代理池（预留，目前不参与 pipeline，等填入 proxies.list 后接管）
# ──────────────────────────────────────────────


class WebshareQuotaExhausted(RuntimeError):
    """Webshare 本月替换额度耗尽。"""
    pass


class WebshareClient:
    """Webshare.io API v2 最小客户端：refresh + 读当前代理 + 额度查询。"""

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
        """GET /subscription/plan/ 返回 active plan dict（含替换额度）。"""
        with self._req("/subscription/plan/") as r:
            data = json.loads(r.read().decode())
        for p in (data.get("results") or []):
            if p.get("status") == "active":
                return p
        return (data.get("results") or [{}])[0]

    def get_replacement_quota(self) -> dict:
        """返回 {total, used, available, cycle_end_iso}"""
        p = self.get_plan()
        return {
            "total": int(p.get("proxy_replacements_total", 0) or 0),
            "used": int(p.get("proxy_replacements_used", 0) or 0),
            "available": int(p.get("proxy_replacements_available", 0) or 0),
            "updated_at": p.get("updated_at", ""),
        }

    def refresh_pool(self, country: str = "", count: int = 1) -> None:
        """POST /proxy/list/refresh/ 触发整池 IP 轮换，204 即成功。
        country 非空时带 {"countries":{country.upper(): count}} body 锁定国家。
        耗尽额度时抛 WebshareQuotaExhausted。"""
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
        """GET /proxy/list/ 返回第一个 proxy。未校验 valid。"""
        with self._req("/proxy/list/?mode=direct&page=1&page_size=5") as r:
            data = json.loads(r.read().decode())
        results = data.get("results") or []
        if not results:
            raise RuntimeError("Webshare 代理列表为空")
        return results[0]

    def wait_for_fresh_proxy(self, prev_ip: str = "", max_wait_s: int = 120,
                              poll_interval_s: int = 5) -> dict:
        """轮询直到：valid=True 且 ip != prev_ip。返回 proxy 字典。"""
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


def _swap_gost_relay(new_ip: str, new_port: int, username: str, password: str,
                       listen_port: int = 18898, upstream_scheme: str = "http") -> None:
    """把监听 listen_port 的 gost 换成新上游。安全匹配进程命令行中的 -L=socks5://:<port> 片段。"""
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
    # 等旧端口释放
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
    time.sleep(1.5)
    if p.poll() is not None:
        raise RuntimeError(f"gost 启动即退出，见 {log_path}")
    print(f"[gost] 启动新中继 PID={p.pid}  {upstream}")


def _ensure_gost_alive(card_cfg: dict, team_client=None) -> bool:
    """启动/循环前调。检 listen_port 是否有进程监听；没的话从 Webshare API 拿
    当前 IP 自动拉起 gost + 同步 team 全局代理。成功返回 True，失败 False。
    （不进行 refresh，只是拉起; refresh 留给 no_perm 触发路径）"""
    ws_cfg = (card_cfg or {}).get("webshare") or {}
    if not ws_cfg.get("enabled"):
        return False
    api_key = (ws_cfg.get("api_key") or "").strip()
    listen_port = int(ws_cfg.get("gost_listen_port", 18898))
    if not api_key:
        return False
    # 已监听则跳
    try:
        ck = subprocess.run(["ss", "-ltn", f"sport = :{listen_port}"],
                             capture_output=True, text=True, timeout=3)
        if f":{listen_port}" in ck.stdout:
            return True
    except Exception:
        pass
    print(f"[gost] listen :{listen_port} 无监听，自动拉起")
    try:
        client = WebshareClient(api_key)
        px = client.get_current_proxy()
    except Exception as e:
        print(f"[gost] 查询 Webshare 当前 IP 失败: {e}")
        return False
    upstream_scheme = str(ws_cfg.get("gost_upstream_scheme", "http"))
    try:
        _swap_gost_relay(px["proxy_address"], int(px["port"]),
                          px["username"], px["password"],
                          listen_port=listen_port,
                          upstream_scheme=upstream_scheme)
    except Exception as e:
        print(f"[gost] 拉起失败: {e}")
        return False
    # 同步 team 全局代理
    if team_client and ws_cfg.get("sync_team_proxy", True):
        team_scheme = str(ws_cfg.get("team_proxy_scheme", "socks5"))
        try:
            team_client.update_global_proxy(
                f"{team_scheme}://{px['username']}:{px['password']}@{px['proxy_address']}:{px['port']}"
            )
        except Exception as e:
            print(f"[gost] team 代理同步失败: {e}")
    return True


def _rotate_webshare_ip(card_cfg: dict, team_client=None, prev_ip: str = "") -> dict:
    """整合：refresh → 轮询新 IP → 换 gost → 同步 team 全局代理。返回新 proxy dict。"""
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

    return new_px


class ProxyPool:
    """代理轮换池（stub）。未来扩展：健康检查、失败标记、LRU 轮换。
    当前行为：有 list 则返回第一个（保持稳定），无 list 返回空字符串（走配置默认代理）。"""

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
        # TODO: 实现失败标记 + 冷却
        pass


def _build_proxy_pool_from_card_cfg(card_cfg) -> "ProxyPool":
    pp = (card_cfg or {}).get("proxies") or {}
    if not pp.get("enabled"):
        return ProxyPool()
    return ProxyPool(proxies=pp.get("list", []), rotation=pp.get("rotation", "static"))


def _rewrite_cardw_with_domain(src_path, domain, proxy_url=""):
    """读 CTF-reg config，把 catch_all_domain 覆盖为 domain，可选覆盖 proxy，写到临时文件返回路径"""
    with open(src_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    mail = data.setdefault("mail", {})
    mail["catch_all_domain"] = domain
    if proxy_url:
        data["proxy"] = proxy_url
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="pipeline_cardw_",
        dir=str(CARDW_DIR), delete=False,
    )
    json.dump(data, tmp, ensure_ascii=False, indent=2)
    tmp.close()
    return tmp.name


def _rewrite_card_with_proxy(src_path, proxy_url):
    """读 CTF-pay config，覆盖 proxy 字段，写到临时文件返回路径"""
    with open(src_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["proxy"] = proxy_url
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="pipeline_pay_px_",
        dir=str(CARD_DIR), delete=False,
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
    """倒序找到首条匹配 email(+session_id) 的支付记录并补字段。"""
    try:
        return get_db().augment_card_result_last_match(email, session_id, extra_fields)
    except Exception as e:
        print(f"[results] 写回字段失败: {e}")
        return False


# ──────────────────────────────────────────────
# 自产自销（self-dealer）相关 helper
# 业务形态：1 个 owner 付费开 Team + N 个 member 被邀请上车 + 全部推 CPA
# 复用现成：register / pay / _exchange_refresh_token_with_session / _cpa_import_after_team
# ──────────────────────────────────────────────

_OAI_CODEX_CLIENT_ID = "YOUR_OPENAI_CODEX_CLIENT_ID"


def _oai_exchange_refresh_to_access_token(refresh_token: str,
                                          client_id: str = _OAI_CODEX_CLIENT_ID) -> dict:
    """refresh_token grant → 完整 token dict (access_token/id_token/refresh_token)。
    走环境 HTTPS_PROXY；若没有，走 urllib 默认（可能直连 auth.openai.com 失败）。"""
    import urllib.request as _urlreq, urllib.parse as _urlparse, urllib.error as _urlerr
    data = _urlparse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "scope": "openid email profile offline_access",
    }).encode()
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    opener = _urlreq.build_opener(_urlreq.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {}
    ))
    req = _urlreq.Request(
        "https://auth.openai.com/oauth/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except _urlerr.HTTPError as e:
        body = ""
        try: body = e.read().decode()[:300]
        except Exception: pass
        raise RuntimeError(f"refresh_token 交换失败 http={e.code} body={body}") from e


def _oai_team_id_from_access_token(access_token: str) -> str:
    """解 access_token JWT → chatgpt_account_id（= workspace id）。"""
    import base64 as _b64
    parts = access_token.split(".")
    if len(parts) < 2:
        return ""
    p = parts[1]
    p += "=" * (-len(p) % 4)
    payload = json.loads(_b64.urlsafe_b64decode(p).decode())
    return (payload.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id", "") or ""


def _oai_send_team_invite(owner_at: str, team_id: str, member_email: str,
                          owner_device_id: str = "", proxy_url: str = "") -> dict:
    """Owner 向 member_email 发送团队邀请。
    调 POST https://chatgpt.com/backend-api/accounts/{team_id}/invites，body 沿用 gpt-team 的写法。
    Returns: {"status": int, "body": str, "invite_id": str or ""}"""
    import curl_cffi.requests as cr
    s = cr.Session(impersonate="chrome136")
    if proxy_url:
        pu = proxy_url.replace("socks5://", "socks5h://")
        s.proxies = {"http": pu, "https": pu}
    r = s.post(
        f"https://chatgpt.com/backend-api/accounts/{team_id}/invites",
        headers={
            "authorization": f"Bearer {owner_at}",
            "chatgpt-account-id": team_id,
            "content-type": "application/json",
            "accept": "*/*",
            "origin": "https://chatgpt.com",
            "referer": "https://chatgpt.com/admin",
            "oai-device-id": owner_device_id or "",
        },
        json={
            "email_addresses": [member_email],
            "role": "standard-user",
            "seat_type": "default",
            "resend_emails": True,
        }, timeout=30,
    )
    invite_id = ""
    try:
        j = r.json()
        invs = j.get("account_invites") or []
        if invs and isinstance(invs[0], dict):
            invite_id = invs[0].get("id", "") or ""
    except Exception:
        pass
    return {"status": r.status_code, "body": r.text, "invite_id": invite_id}


def _oai_accept_team_invite(member_at: str, team_id: str,
                            member_device_id: str = "", proxy_url: str = "") -> dict:
    """Member 接受邀请：POST https://chatgpt.com/backend-api/accounts/{team_id}/invites/accept。
    该 endpoint 是从 chatgpt 前端 JS (auth.login/_accept_account_id cookie handler) 逆出来的。"""
    import curl_cffi.requests as cr
    s = cr.Session(impersonate="chrome136")
    if proxy_url:
        pu = proxy_url.replace("socks5://", "socks5h://")
        s.proxies = {"http": pu, "https": pu}
    r = s.post(
        f"https://chatgpt.com/backend-api/accounts/{team_id}/invites/accept",
        headers={
            "authorization": f"Bearer {member_at}",
            "content-type": "application/json",
            "accept": "*/*",
            "origin": "https://chatgpt.com",
            "referer": "https://chatgpt.com/",
            "oai-device-id": member_device_id or "",
        }, data="", timeout=30,
    )
    return {"status": r.status_code, "body": r.text}


def _find_team_id_from_results(email: str) -> str:
    """倒序扫支付记录，找 chatgpt_email=email 的 team_account_id。"""
    try:
        return get_db().find_team_id_from_results(email)
    except Exception as e:
        print(f"[self-dealer] 读 team_id 失败: {e}")
    return ""


def _cpa_import_after_team(
    email: str,
    sid: str,
    cpa_cfg: dict,
    *,
    refresh_token: str = "",
    is_free: bool = False,
) -> str:
    """支付成功后把账号导入 CPA (CLIProxyAPI)。best-effort，异常不抛。

    Args:
        refresh_token: 显式传入的 rt（free_only 路径用）；不传时从
            SQLite 支付记录按 email/sid 查（pay 流程默认行为）。
        is_free: True → CPA 推送 plan_tag 改用 cpa_cfg.free_plan_tag
            （免费账号档位）；False → 用 plan_tag（team / 付费档位）。

    Returns: ok / skipped / no_rt / fail_refresh / fail_upload
    """
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
        # 真实支付后，refresh_token 可能因为 add-phone / rate-limit / 401 等原因
        # 无法及时拿到；此时至少保留已注册账号里的 access_token 做“裸导入”
        #，让 CPA 端先落库，后续再补 RT。
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
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走本地 proxy
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

    # 刷一次 refresh_token → 拿绑了 team 的 access_token + id_token
    client_id = cpa_cfg.get("oauth_client_id") or "app_EMoamEEZ73f0CkXaXp7hrann"
    at, id_tok, account_id = "", "", ""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走本地 proxy
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

    # 构造 codex 文件
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
        # 走 curl_cffi（chrome TLS+UA 指纹）规避 CF WAF；不可用则降级 urllib
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
    """支付成功后：读 RT → 导入 team → 写回 probe 结果 → 更新域状态。
    返回 probe 返回的 status 字符串（ok/no_permission/failed/error/none）。"""
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
    # 更新支付结果记录
    _augment_card_result_last_match(email, sid, {
        "invite_permission": st,
        "team_gpt_account_pk": probe.get("account_id"),
        "email_domain": domain,
    })
    # 更新域池
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
    """自产自销（state machine 2nd form）：
      Step 1 (1 次)：注册 - 支付 - 推送 team+cpa    →  现成 pipeline()
      Step 2 (N 次)：注册 - 邀请上车 - 推送 cpa     →  register() + invite/accept + relogin + cpa_push
    注册 & 推送流程一字不改，只是 member 循环里在 register() 前照 pipeline() 的做法
    重新 pick 代理 + pick 域 + rewrite 临时 cardw，保证每次注册用新代理/域。
    """
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
    # invite/accept/relogin 统一用 card_cfg.proxies.list[0]（本地 gost 中继，
    # 与 pipeline() 给注册 rewrite 的是同一条）。cardw.proxy 原值凭证可能失效。
    api_proxies = (card_cfg or {}).get("proxies", {}).get("list") or []
    proxy_url = (api_proxies[0] if api_proxies else "") or reg_cfg.get("proxy", "") or ""
    print(f"[self-dealer] invite/accept/relogin 代理: {proxy_url}")

    # member 注册前的 pool 准备——完全照搬 pipeline() 的做法，不动任何流程，
    # 只是把 pipeline() 里的 pool/proxy_pool/pick+rewrite 逻辑抽到 member 循环里
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
            # 与 pipeline() 一致：每次挑代理 + 挑域 + rewrite 临时 cardw
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

            # 邀请
            inv = _oai_send_team_invite(owner_at, team_id, mem_email, proxy_url=proxy_url)
            print(f"[self-dealer] invite status={inv['status']}  invite_id={inv['invite_id'][:20] if inv['invite_id'] else '-'}")
            if inv["status"] not in (200, 201):
                entry["status"] = f"invite_failed http={inv['status']} body={inv['body'][:120]}"
                continue

            time.sleep(max(0, invite_accept_gap_s))

            # 接受
            acc = _oai_accept_team_invite(mem_at, team_id, mem_did, proxy_url=proxy_url)
            print(f"[self-dealer] accept status={acc['status']}  body={acc['body'][:100]}")
            if acc["status"] not in (200, 201):
                entry["status"] = f"accept_failed http={acc['status']} body={acc['body'][:120]}"
                continue

            # 重登拿 refresh_token（Camoufox）
            try:
                rt = card_mod._exchange_refresh_token_with_session(
                    email=mem_email, password=mem_pwd, mail_cfg=mail_cfg, proxy_url=proxy_url,
                )
            except Exception as e:
                print(f"[self-dealer] ✗ {mem_email} 重登异常: {e}")
                entry["status"] = f"relogin_error: {str(e)[:120]}"
                continue
            if not rt:
                entry["status"] = "no_rt"
                continue

            # 追加一条支付成功记录，让 _cpa_import_after_team 能读到
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

            # CPA 推送
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

    # 汇总
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


def free_register_loop(card_config_path, cardw_config_path=None, count: int = 0):
    """free_only mode：注册免费 ChatGPT 号 + 单独跑 OAuth 拿 rt + 推 CPA(free)。

    跟 daemon/self_dealer 不同：不进入支付步骤。

    count = 0 表示无限（直到 SIGTERM）；> 0 跑 count 次后退出。
    """
    import hashlib

    card_cfg = _read_card_cfg(card_config_path)
    cardw_path = _load_cardw_path_from_card_cfg(card_cfg, cardw_config_path)
    cpa_cfg = (card_cfg or {}).get("cpa") or {}
    mail_cfg = card_cfg.get("mail") or {}
    proxy_url = card_cfg.get("proxy", "")

    # 启 gost（如果配了 webshare）
    _ensure_gost_alive(card_cfg)

    # OAuth Codex client_id：card.py:_exchange_refresh_token_with_session 读
    # OAUTH_CODEX_CLIENT_ID env var；从 cpa.oauth_client_id 推过去（subprocess
    # 调用 card 模块时该 env 必须已设，否则 OpenAI 返 AuthApiFailure）。
    _client_id = (cpa_cfg.get("oauth_client_id") or "").strip()
    if _client_id and not os.environ.get("OAUTH_CODEX_CLIENT_ID"):
        os.environ["OAUTH_CODEX_CLIENT_ID"] = _client_id
        print(f"[free] 自动设 OAUTH_CODEX_CLIENT_ID = {_client_id}")

    # 域池
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
    """free_only mode：读数据库里的注册账号给老号补 rt + 推 CPA(free)。

    跳过：已有 refresh_token / oauth_status==succeeded / oauth_status==dead /
    transient_failed 在 6h cooldown 内的账号。
    """
    import hashlib

    card_cfg = _read_card_cfg(card_config_path)
    cpa_cfg = (card_cfg or {}).get("cpa") or {}
    mail_cfg = card_cfg.get("mail") or {}
    proxy_url = card_cfg.get("proxy", "")

    _ensure_gost_alive(card_cfg)

    # OAuth Codex client_id（同 free_register_loop，避免 AuthApiFailure）
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
  python pipeline.py --register-only --cardw-config CTF-reg/config.paypal-proxy.json
  python pipeline.py --pay-only --config CTF-pay/config.paypal.json --paypal
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
    parser.add_argument("--gopay-otp-file", default=None,
                        help="webui 模式: gopay.py 从该文件读取 WhatsApp OTP")
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
    parser.add_argument("--free-register", action="store_true",
                        help="free_only 模式：循环注册免费 ChatGPT 号 + OAuth 拿 rt + 推 CPA(free)")
    parser.add_argument("--free-backfill-rt", action="store_true",
                        help="free_only 模式：读数据库老号记录补 rt + 推 CPA(free)，跳过已 succeeded/dead")
    parser.add_argument("--count", type=int, default=0, metavar="N",
                        help="--free-register 模式下注册 N 次后退出（0 = 无限）")
    args = parser.parse_args()

    if args.paypal and args.gopay:
        print("[ERROR] --paypal 与 --gopay 互斥", file=sys.stderr)
        sys.exit(2)
    if args.free_register and args.free_backfill_rt:
        print("[ERROR] --free-register 与 --free-backfill-rt 互斥", file=sys.stderr)
        sys.exit(2)

    try:
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

        # batch 是循环外壳，跟 register-only / pay-only 正交组合
        if args.batch > 0:
            batch(args.config, args.batch, delay=args.delay, workers=args.workers,
                  use_paypal=args.paypal, cardw_config_path=args.cardw_config,
                  register_only=args.register_only, pay_only=args.pay_only,
                  use_gopay=args.gopay, gopay_otp_file=args.gopay_otp_file)

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
                gopay_otp_file=args.gopay_otp_file,
            )
            print(f"\n结果: {result.get('status', '?')}")

        else:
            pipeline(args.config, cardw_config_path=args.cardw_config,
                     use_paypal=args.paypal, use_gopay=args.gopay,
                     gopay_otp_file=args.gopay_otp_file)

    except (RegistrationError, PaymentError) as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[中断]")
        sys.exit(130)


if __name__ == "__main__":
    main()
