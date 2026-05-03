"""
Stripe Checkout 自动化支付脚本

⚠️ 仅限授权范围内使用（你拥有的系统 / 合法 CTF / 授权 bug bounty in-scope 资产 /
   安全研究）。运行本程序即视为同意仓库根目录 NOTICE 文件全部条款。AS IS 提供，
   无任何担保，一切后果使用者自负。

用法:
  python pay.py <session_id> [--card N] [--config path] [--token TOKEN]

示例:
  python pay.py cs_live_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
"""

import argparse
import base64
import glob
import hashlib
import http.server
import json
import os
import random
import re
import shutil
import socketserver
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
_REPO_DIR_BOOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_DIR_BOOT not in sys.path:
    sys.path.insert(0, _REPO_DIR_BOOT)
from webui.backend.db import get_db
try:
    from curl_cffi.requests import Session as CurlCffiSession
    _HAS_CURL_CFFI = True
except Exception:
    CurlCffiSession = None
    _HAS_CURL_CFFI = False


_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(os.path.join(_OUTPUT_DIR, "logs"), exist_ok=True)
LOG_FILE = os.path.join(_OUTPUT_DIR, "logs", "card.log")

# 让 `from cf_kv_otp_provider import ...` 在 card.py 直接执行/被 pipeline 子进程
# 拉起时都能命中 `CTF-reg/` 下的实现。否则 RT / PayPal OTP 会在
# `python CTF-pay/card.py ...` 的默认 sys.path 里找不到该模块。
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CTF_REG_DIR = os.path.join(_REPO_DIR, "CTF-reg")
if os.path.isdir(_CTF_REG_DIR) and _CTF_REG_DIR not in sys.path:
    sys.path.insert(0, _CTF_REG_DIR)

def _init_log():
    """清空并初始化 log.txt"""
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"{'='*80}\n")
        f.write(f"  Stripe 自动化支付 日志  —  {datetime.now().isoformat()}\n")
        f.write(f"{'='*80}\n\n")

def _log(msg: str):
    """追加一行到 log.txt 并同时 print"""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def _log_raw(text: str):
    """追加原始文本到 log.txt（不 print）"""
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")

def _log_request(method: str, url: str, data=None, params=None, tag: str = ""):
    """记录 HTTP 请求详情"""
    _log_raw(f"\n{'─'*70}")
    _log_raw(f">>> REQUEST  {tag}")
    _log_raw(f"    {method} {url}")
    if params:
        _log_raw(f"    PARAMS: {json.dumps(params, ensure_ascii=False, indent=6)}")
    if data:
        # 脱敏卡号
        safe = dict(data) if isinstance(data, dict) else {}
        if "card[number]" in safe:
            safe["card[number]"] = "****" + str(safe["card[number]"])[-4:]
        if "card[cvc]" in safe:
            safe["card[cvc]"] = "***"
        _log_raw(f"    BODY: {json.dumps(safe, ensure_ascii=False, indent=6)}")

def _log_response(resp: requests.Response, tag: str = ""):
    """记录 HTTP 响应详情"""
    _log_raw(f"<<< RESPONSE {tag}  status={resp.status_code}")
    try:
        body = resp.json()
        _log_raw(f"    BODY: {json.dumps(body, ensure_ascii=False, indent=6)}")
    except Exception:
        _log_raw(f"    BODY(raw): {resp.text[:2000]}")
    _log_raw(f"{'─'*70}\n")


def _describe_challenge_artifact(name: str, value: str) -> str:
    value = str(value or "")
    prefix = value[:3] if len(value) >= 3 else value
    return f"{name}: prefix={prefix!r} len={len(value)}"


class ChallengeReconfirmRequired(RuntimeError):
    """当前 challenge 已失效或被拒，需要重新 confirm 获取新的 challenge。"""
    pass


class CheckoutSessionInactive(RuntimeError):
    """当前 Checkout Session 已失活，需要生成新的 session。"""
    pass


class FreshCheckoutAuthError(RuntimeError):
    """无法通过 ChatGPT 侧生成 fresh checkout。"""
    pass


def _build_proxy_url_from_cfg(proxy_cfg) -> str:
    if not proxy_cfg:
        return ""

    if isinstance(proxy_cfg, str):
        return proxy_cfg.strip()

    if not isinstance(proxy_cfg, dict):
        return ""

    host = str(proxy_cfg.get("host") or "").strip()
    if not host:
        return ""
    port = proxy_cfg.get("port")
    user = str(proxy_cfg.get("user") or "").strip()
    pwd = str(proxy_cfg.get("pass") or "").strip()

    if port in (None, ""):
        return f"http://{host}"
    if user and pwd:
        return f"http://{user}:{pwd}@{host}:{port}"
    return f"http://{host}:{port}"


def _apply_proxy_to_http_session(session_obj, proxy_url: str):
    try:
        session_obj.trust_env = False
    except Exception:
        pass

    if not hasattr(session_obj, "proxies"):
        return

    if proxy_url:
        normalized_proxy = proxy_url
        if _HAS_CURL_CFFI and proxy_url.startswith("socks5://"):
            normalized_proxy = "socks5h://" + proxy_url[len("socks5://"):]
        session_obj.proxies = {"http": normalized_proxy, "https": normalized_proxy}
    else:
        session_obj.proxies = {"http": "", "https": ""}


_PROXY_OVERRIDE_SENTINEL = object()


def _resolve_proxy_cfg(cfg: dict, proxy_cfg_override=_PROXY_OVERRIDE_SENTINEL):
    if proxy_cfg_override is _PROXY_OVERRIDE_SENTINEL:
        return cfg.get("proxy")
    return proxy_cfg_override


def _create_chatgpt_http_session(
    cfg: dict,
    user_agent: str = "",
    proxy_cfg_override=_PROXY_OVERRIDE_SENTINEL,
) -> tuple[object, str]:
    proxy_url = _build_proxy_url_from_cfg(_resolve_proxy_cfg(cfg, proxy_cfg_override))

    if _HAS_CURL_CFFI:
        http = CurlCffiSession(impersonate="chrome136")
        _apply_proxy_to_http_session(http, proxy_url)
        if user_agent:
            http.headers.update({"user-agent": user_agent})
        return http, "curl_cffi(chrome136)"

    http = requests.Session()
    _apply_proxy_to_http_session(http, proxy_url)
    if user_agent:
        http.headers.update({"user-agent": user_agent})
    return http, "requests"


def _describe_proxy_cfg(proxy_cfg) -> str:
    proxy_url = _build_proxy_url_from_cfg(proxy_cfg)
    if not proxy_url:
        return "无 (直连)"

    try:
        parsed = urllib.parse.urlsplit(proxy_url)
        host = parsed.hostname or ""
        port = parsed.port
        user = urllib.parse.unquote(parsed.username or "")
        if host:
            desc = f"{host}:{port}" if port else host
            if user:
                desc += f" (user={user})"
            return desc
    except Exception:
        pass
    return proxy_url


def _resolve_stage_proxy_cfg(stage_proxy_cfg: dict | None, stage_name: str):
    if not isinstance(stage_proxy_cfg, dict) or stage_name not in stage_proxy_cfg:
        return _PROXY_OVERRIDE_SENTINEL
    return stage_proxy_cfg.get(stage_name)


@contextmanager
def _http_session_stage_proxy(session_obj, stage_proxy_cfg: dict | None, stage_name: str):
    proxy_cfg = _resolve_stage_proxy_cfg(stage_proxy_cfg, stage_name)
    if proxy_cfg is _PROXY_OVERRIDE_SENTINEL:
        yield
        return

    prev_proxies = dict(getattr(session_obj, "proxies", {}) or {})
    _apply_proxy_to_http_session(session_obj, _build_proxy_url_from_cfg(proxy_cfg))
    _log(f"      [proxy] stage={stage_name} → {_describe_proxy_cfg(proxy_cfg)}")
    try:
        yield
    finally:
        if hasattr(session_obj, "proxies"):
            session_obj.proxies = prev_proxies


def _extract_api_error(resp) -> tuple[str, str]:
    try:
        data = resp.json()
    except Exception:
        return "", ""

    if not isinstance(data, dict):
        return "", ""
    error = data.get("error")
    if not isinstance(error, dict):
        return "", ""
    code = str(error.get("code") or "").strip()
    message = str(error.get("message") or "").strip()
    return code, message


def _resolve_existing_path(path: str, candidates: list[str]) -> str:
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(path or candidates[0] or "")


def _persist_json(path: str, payload: dict):
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _build_cfg_with_fresh_auth(
    cfg: dict,
    provisioned_auth: dict,
    *,
    forced_mode: str = "access_token",
    mark_auto_register_used: bool = True,
) -> dict:
    cloned = json.loads(json.dumps(cfg))
    if cfg.get("_loaded_from"):
        cloned["_loaded_from"] = cfg["_loaded_from"]

    fresh_cfg = cloned.setdefault("fresh_checkout", {})
    auth_cfg = fresh_cfg.setdefault("auth", {})
    device_id = (
        provisioned_auth.get("device_id")
        or provisioned_auth.get("oai_device_id")
        or ""
    )
    cookie_header = provisioned_auth.get("cookie_header", "") or ""
    auth_cfg["access_token"] = provisioned_auth.get("access_token", "")
    auth_cfg["session_token"] = provisioned_auth.get("session_token", "")
    auth_cfg["device_id"] = device_id
    auth_cfg["oai_device_id"] = device_id
    auth_cfg["cookie_header"] = cookie_header
    if provisioned_auth.get("openai_sentinel_token"):
        auth_cfg["openai_sentinel_token"] = provisioned_auth.get("openai_sentinel_token", "")
    auth_cfg["mode"] = forced_mode
    auth_cfg["_auto_register_used"] = bool(mark_auto_register_used)
    if provisioned_auth.get("email"):
        auth_cfg["_last_registered_email"] = provisioned_auth["email"]
    return cloned


def _load_existing_auth_from_local_bundle_config(cfg: dict, fresh_cfg: dict) -> dict:
    """从本地 CTF-reg 配置中读取现成登录态。

    目标：
    - 优先复用已经在本地 bundle 中拿到的 `session_token/access_token/device_id`
    - 仅当 bundle 中完全没有可用认证信息，或后续 fresh checkout 返回 401 时，
      再回退到真正的新注册流程
    """
    auth_cfg = fresh_cfg.get("auth") or {}
    auto_cfg = (auth_cfg.get("auto_register") or fresh_cfg.get("auto_register") or {})
    auth_bundle_dir = (
        auto_cfg.get("project_dir")
        or auto_cfg.get("auth_bundle_dir")
        or auto_cfg.get("abcard_dir")
        or "./CTF-reg"
    )
    loaded_from = os.path.dirname(os.path.abspath(cfg.get("_loaded_from") or __file__))
    config_path_raw = (
        auto_cfg.get("config_path")
        or auto_cfg.get("auth_bundle_config")
        or auto_cfg.get("abcard_config")
        or os.path.join(auth_bundle_dir, "config.noproxy.json")
    )
    config_path = _resolve_existing_path(
        config_path_raw,
        [
            config_path_raw if os.path.isabs(config_path_raw) else os.path.join(loaded_from, config_path_raw),
            config_path_raw if os.path.isabs(config_path_raw) else os.path.join(auth_bundle_dir, config_path_raw),
            os.path.join(auth_bundle_dir, "config.noproxy.json"),
            os.path.join(auth_bundle_dir, "config.json"),
            os.path.join(auth_bundle_dir, "config.example.json"),
        ],
    )
    persist_to = (auto_cfg.get("persist_to") or "").strip()
    if persist_to and not os.path.isabs(persist_to):
        persist_to = os.path.abspath(os.path.join(loaded_from, persist_to))

    candidate_paths: list[str] = []
    for candidate in (persist_to, config_path):
        if candidate and candidate not in candidate_paths:
            candidate_paths.append(candidate)

    for candidate_path in candidate_paths:
        if not os.path.exists(candidate_path):
            continue
        try:
            with open(candidate_path, "r", encoding="utf-8") as f:
                bundle_cfg = json.load(f)
        except Exception as e:
            _log(f"      [fresh] 读取本地登录态失败，跳过 {candidate_path}: {e}")
            continue

        access_token = (bundle_cfg.get("access_token") or "").strip()
        session_token = (bundle_cfg.get("session_token") or "").strip()
        device_id = (bundle_cfg.get("device_id") or "").strip()
        cookie_header = (bundle_cfg.get("cookie_header") or "").strip()
        if not cookie_header:
            cookie_header = _compose_cookie_header(
                "",
                session_token=session_token,
                device_id=device_id,
            )

        if not access_token and not session_token:
            continue
        if access_token and _is_access_token_expired(access_token):
            if session_token:
                _log(
                    "      [fresh] 本地 bundle 的 access_token 已过期，"
                    "但检测到 session_token，改为走 session 刷新 ..."
                )
                access_token = ""
            else:
                _log(
                    "      [fresh] 跳过本地现成登录态: "
                    f"{candidate_path} 的 access_token 已过期，且没有 session_token"
                )
                continue

        email = _extract_email_from_access_token(access_token) if access_token else ""
        _log(
            "      [fresh] 检测到本地 bundle 现成登录态: "
            f"config={candidate_path} "
            f"email={email or '?'} "
            f"access_token_len={len(access_token)} "
            f"session_token_len={len(session_token)} "
            f"device_id={'yes' if device_id else 'no'}"
        )
        return {
            "email": email,
            "access_token": access_token,
            "session_token": session_token,
            "device_id": device_id,
            "oai_device_id": device_id,
            "cookie_header": cookie_header,
        }

    return {}


def _provision_openai_auth_via_local_bundle(cfg: dict, fresh_cfg: dict) -> dict:
    auth_cfg = fresh_cfg.get("auth") or {}
    auto_cfg = (auth_cfg.get("auto_register") or fresh_cfg.get("auto_register") or {})
    login_email = (auto_cfg.get("login_email") or "").strip()
    login_password = auto_cfg.get("login_password")
    prefer_existing_account_login = bool(
        auto_cfg.get("prefer_existing_account_login", False) or login_email
    )

    auth_bundle_dir = (
        auto_cfg.get("project_dir")
        or auto_cfg.get("auth_bundle_dir")
        or auto_cfg.get("abcard_dir")
        or "./CTF-reg"
    )
    loaded_from = os.path.dirname(os.path.abspath(cfg.get("_loaded_from") or __file__))
    config_path_raw = (
        auto_cfg.get("config_path")
        or auto_cfg.get("auth_bundle_config")
        or auto_cfg.get("abcard_config")
        or os.path.join(auth_bundle_dir, "config.noproxy.json")
    )
    config_path = _resolve_existing_path(
        config_path_raw,
        [
            config_path_raw if os.path.isabs(config_path_raw) else os.path.join(loaded_from, config_path_raw),
            config_path_raw if os.path.isabs(config_path_raw) else os.path.join(auth_bundle_dir, config_path_raw),
            os.path.join(auth_bundle_dir, "config.noproxy.json"),
            os.path.join(auth_bundle_dir, "config.json"),
            os.path.join(auth_bundle_dir, "config.example.json"),
        ],
    )

    if not os.path.isdir(auth_bundle_dir):
        raise FreshCheckoutAuthError(f"本地认证目录不存在: {auth_bundle_dir}")
    if not os.path.exists(config_path):
        raise FreshCheckoutAuthError(f"本地认证配置不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        ab_cfg = json.load(f)

    # 强制走“新注册拿 fresh token”链路，避免复用已失效凭证。
    if not auto_cfg.get("reuse_existing_auth", False):
        ab_cfg["session_token"] = ""
        ab_cfg["access_token"] = ""
        ab_cfg["device_id"] = ""

    plan_cfg = fresh_cfg.get("plan") or {}
    team_plan = ab_cfg.setdefault("team_plan", {})
    if plan_cfg.get("plan_name"):
        team_plan["plan_name"] = plan_cfg["plan_name"]
    if plan_cfg.get("workspace_name"):
        team_plan["workspace_name"] = plan_cfg["workspace_name"]
    if plan_cfg.get("price_interval"):
        team_plan["price_interval"] = plan_cfg["price_interval"]
    if plan_cfg.get("seat_quantity") is not None:
        team_plan["seat_quantity"] = int(plan_cfg["seat_quantity"])
    if "promo_campaign_id" in plan_cfg and plan_cfg.get("promo_campaign_id") is not None:
        team_plan["promo_campaign_id"] = plan_cfg.get("promo_campaign_id")

    billing = ab_cfg.setdefault("billing", {})
    if plan_cfg.get("billing_country"):
        billing["country"] = str(plan_cfg["billing_country"]).upper()
    if plan_cfg.get("billing_currency"):
        billing["currency"] = str(plan_cfg["billing_currency"]).upper()

    mail_cfg = ab_cfg.setdefault("mail", {})
    # IMAP/SMTP 字段已废弃（OTP 走 CF Email Worker → KV，见 cf_kv_otp_provider）；
    # 这里只保留 catch_all_domain / catch_all_domains / auto_provision。
    for key in ("catch_all_domain", "catch_all_domains", "auto_provision"):
        if key in auto_cfg and auto_cfg.get(key) not in (None, ""):
            mail_cfg[key] = auto_cfg.get(key)
    if isinstance(auto_cfg.get("mail"), dict):
        for key, value in auto_cfg["mail"].items():
            if value not in (None, ""):
                mail_cfg[key] = value

    fresh_proxy_cfg = fresh_cfg["proxy"] if "proxy" in fresh_cfg else _PROXY_OVERRIDE_SENTINEL
    proxy_url = _build_proxy_url_from_cfg(_resolve_proxy_cfg(cfg, fresh_proxy_cfg))
    if auto_cfg.get("use_ctf_proxy", True):
        ab_cfg["proxy"] = proxy_url or ""
    elif auto_cfg.get("proxy") not in (None, ""):
        ab_cfg["proxy"] = auto_cfg.get("proxy")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    default_env_overrides = {
        "OAUTH_CODEX_RT_BEFORE_CALLBACK": "0",
        "OAUTH_CODEX_RT_EXCHANGE": "0",
        "OAUTH_SECONDARY_AUTHORIZE_EXCHANGE": "0",
        "OAUTH_EXCHANGE_BEFORE_CALLBACK": "0",
    }
    if auto_cfg.get("auth_http_trace") is not None:
        default_env_overrides["AUTH_HTTP_TRACE"] = "1" if auto_cfg.get("auth_http_trace") else "0"
    if auto_cfg.get("otp_timeout") not in (None, ""):
        default_env_overrides["OTP_TIMEOUT"] = str(auto_cfg.get("otp_timeout"))
    custom_env = auto_cfg.get("env") or {}
    for key, value in {**default_env_overrides, **custom_env}.items():
        if value is None:
            continue
        env[str(key)] = str(value)
    env["LOCALAUTH_PREFER_EXISTING_ACCOUNT_LOGIN"] = "1" if prefer_existing_account_login else "0"
    env["LOCALAUTH_LOGIN_EMAIL"] = login_email
    env["LOCALAUTH_LOGIN_PASSWORD"] = "" if login_password is None else str(login_password)

    script = r"""
import json
import logging
import os
import sys

auth_bundle_dir = sys.argv[1]
config_path = sys.argv[2]
sys.path.insert(0, auth_bundle_dir)

from config import Config
from auth_flow import AuthFlow
from mail_provider import MailProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

cfg = Config.from_file(config_path)
mail = MailProvider(cfg.mail.catch_all_domain)
flow = AuthFlow(cfg)
login_email = (os.getenv("LOCALAUTH_LOGIN_EMAIL") or "").strip()
login_password = os.getenv("LOCALAUTH_LOGIN_PASSWORD", "")
prefer_existing = os.getenv("LOCALAUTH_PREFER_EXISTING_ACCOUNT_LOGIN", "0") == "1"
if prefer_existing and login_email:
    result = flow.run_protocol_login(mail, email=login_email, password=login_password)
else:
    result = flow.run_register(mail)
print("LOCALAUTH_RESULT_JSON=" + json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
"""

    persist_to = auto_cfg.get("persist_to") or ""
    if persist_to and not os.path.isabs(persist_to):
        persist_to = os.path.abspath(os.path.join(loaded_from, persist_to))

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="abcard_register_cfg_",
        delete=False,
        dir="/tmp",
    ) as tmp_cfg:
        json.dump(ab_cfg, tmp_cfg, ensure_ascii=False, indent=2)
        tmp_cfg.write("\n")
        tmp_cfg_path = tmp_cfg.name

    _log(f"      [fresh] 本地注册配置: {config_path}")
    _log(f"      [fresh] 本地认证目录: {auth_bundle_dir}")
    if prefer_existing_account_login and login_email:
        _log(f"      [fresh] 本地认证模式: 协议登录已有账号 ({login_email})")
    else:
        _log("      [fresh] 本地认证模式: 新注册")
    if ab_cfg.get("proxy"):
        _log(f"      [fresh] 本地注册代理: {ab_cfg.get('proxy')}")
    else:
        _log("      [fresh] 本地注册代理: 无 (直连)")

    max_register_attempts = int(auto_cfg.get("max_register_attempts", 3) or 3)
    last_tail_lines: list[str] = []
    child_result = None

    try:
        for attempt in range(1, max_register_attempts + 1):
            if max_register_attempts > 1:
                _log(f"      [fresh] 本地认证尝试 {attempt}/{max_register_attempts} ...")

            proc = subprocess.Popen(
                [sys.executable, "-c", script, auth_bundle_dir, tmp_cfg_path],
                cwd=auth_bundle_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            child_result = None
            tail_lines: list[str] = []
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                if line.startswith("LOCALAUTH_RESULT_JSON="):
                    payload = line.split("=", 1)[1]
                    try:
                        child_result = json.loads(payload)
                    except Exception as e:
                        raise FreshCheckoutAuthError(f"解析本地注册结果失败: {e}") from e
                    continue
                tail_lines.append(line)
                if len(tail_lines) > 60:
                    tail_lines = tail_lines[-60:]
                _log(f"      [local-auth] {line}")

            rc = proc.wait()
            last_tail_lines = tail_lines
            if rc == 0:
                break

            tail_text = "\n".join(tail_lines).lower()
            retryable = any(
                marker in tail_text
                for marker in (
                    "等待 otp 超时",
                    "timeouterror",
                    "invalid_state",
                    "failed to create account. please try again.",
                    "passwordless 发码失败",
                )
            )
            if attempt < max_register_attempts and retryable:
                _log(
                    "      [fresh] 本地认证本轮失败，但属于可重试场景，"
                    f"准备重新开号重试 ({attempt}/{max_register_attempts}) ..."
                )
                continue

            fallback_auth = _load_existing_auth_from_local_bundle_config(cfg, fresh_cfg)
            if fallback_auth:
                _log(
                    "      [fresh] 本地注册流程失败，"
                    "回退使用 bundle 中现成登录态继续 ..."
                )
                child_result = fallback_auth
                break

            raise FreshCheckoutAuthError(
                "本地注册流程失败"
                + (f" (exit={rc})" if rc else "")
                + (f": {tail_lines[-1]}" if tail_lines else "")
            )
    finally:
        try:
            os.unlink(tmp_cfg_path)
        except Exception:
            pass

    if not isinstance(child_result, dict):
        raise FreshCheckoutAuthError(
            "本地注册流程未返回有效 JSON 结果"
            + (f": {last_tail_lines[-1]}" if last_tail_lines else "")
        )

    access_token = (child_result.get("access_token") or "").strip()
    session_token = (child_result.get("session_token") or "").strip()
    device_id = (child_result.get("device_id") or "").strip()
    if not access_token or not session_token:
        raise FreshCheckoutAuthError("本地注册完成，但未拿到有效 access_token/session_token")

    masked = {
        "email": child_result.get("email", ""),
        "device_id": device_id,
        "access_token_len": len(access_token),
        "session_token_len": len(session_token),
        "cookie_header_len": len((child_result.get("cookie_header") or "").strip()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if persist_to:
        try:
            _persist_json(
                persist_to,
                {
                    **masked,
                    "access_token": access_token,
                    "session_token": session_token,
                    "cookie_header": child_result.get("cookie_header", ""),
                },
            )
            _log(f"      [fresh] 已保存最新本地注册凭证 → {persist_to}")
        except Exception as e:
            _log(f"      [fresh] 保存最新本地注册凭证失败: {e}")

    _log(
        "      [fresh] 本地注册成功: "
        f"email={masked['email']} device_id={device_id} "
        f"access_token_len={masked['access_token_len']} "
        f"session_token_len={masked['session_token_len']} "
        f"cookie_header_len={masked['cookie_header_len']}"
    )
    return child_result

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
STRIPE_VERSION_BASE = "2025-03-31.basil"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
HCAPTCHA_SITE_KEY_FALLBACK = "c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a"
DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"

# 远端打码平台 API base URL（兼容 createTask / getTaskResult 协议）。
# 由 load_config() 从 captcha.api_url 字段读取并写入。
# 任何兼容此协议的服务商都可以填，比如自建打码网关。
_REMOTE_CAPTCHA_BASE_URL = ""


def _remote_captcha_url(path: str = "") -> str:
    """拼出远端打码平台的完整 URL。path 是 /createTask 或 /getTaskResult 这种。"""
    base = (_REMOTE_CAPTCHA_BASE_URL or os.environ.get("CTF_CAPTCHA_API_URL", "")).rstrip("/")
    if not base:
        base = "https://YOUR_CAPTCHA_PROVIDER"
    if path and not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


DEFAULT_STRIPE_HCAPTCHA_ASSET_VERSION = "v32.5"
DEFAULT_FRONTEND_EXECUTION = base64.b64encode(
    json.dumps({"fingerprintOutcome": "not_supported"}, separators=(",", ":")).encode()
).decode()

KNOWN_PUBLISHABLE_KEYS = {
    "1Pj377KslHRdbaPg": "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs",
    "1HOrSwC6h1nxGoI3": "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
}

# PayPal 仅支持欧盟国家/地区
EU_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
    "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
    "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    # EEA + 常见关联
    "NO", "IS", "LI", "CH", "GB",
}

# ---------------------------------------------------------------------------
# 地域 / 浏览器配置 — 必须和代理 IP 出口一致
# ---------------------------------------------------------------------------
LOCALE_PROFILES = {
    "US": {
        "browser_locale": "en-US",
        "browser_timezone": "America/Chicago",
        "browser_language": "en-US",
        "color_depth": 24,
        "screen_w": 1920, "screen_h": 1080, "dpr": 1,
    },
    "CN": {
        "browser_locale": "zh-CN",
        "browser_timezone": "Asia/Shanghai",
        "browser_language": "zh-CN",
        "color_depth": 32,
        "screen_w": 1272, "screen_h": 716, "dpr": 1,
    },
    "ZH": {
        "browser_locale": "zh-CN",
        "browser_timezone": "Asia/Shanghai",
        "browser_language": "zh-CN",
        "color_depth": 32,
        "screen_w": 1272, "screen_h": 716, "dpr": 1,
    },
    "ES": {
        "browser_locale": "es-ES",
        "browser_timezone": "Europe/Madrid",
        "browser_language": "es-ES",
        "color_depth": 24,
        "screen_w": 1366, "screen_h": 768, "dpr": 1,
    },
    "IE": {
        "browser_locale": "en-IE",
        "browser_timezone": "Europe/Dublin",
        "browser_language": "en-IE",
        "color_depth": 24,
        "screen_w": 1920, "screen_h": 1080, "dpr": 1,
    },
    "DE": {
        "browser_locale": "de-DE",
        "browser_timezone": "Europe/Berlin",
        "browser_language": "de-DE",
        "color_depth": 24,
        "screen_w": 1920, "screen_h": 1080, "dpr": 1,
    },
    "FR": {
        "browser_locale": "fr-FR",
        "browser_timezone": "Europe/Paris",
        "browser_language": "fr-FR",
        "color_depth": 24,
        "screen_w": 1920, "screen_h": 1080, "dpr": 1,
    },
    "NL": {
        "browser_locale": "nl-NL",
        "browser_timezone": "Europe/Amsterdam",
        "browser_language": "nl-NL",
        "color_depth": 24,
        "screen_w": 1920, "screen_h": 1080, "dpr": 1,
    },
}


APATA_RBA_ORG_ID = "8t63q4n4"


def _browser_tz_offset(locale_profile: dict) -> int:
    """返回与浏览器 `Date.getTimezoneOffset()` 一致的分钟偏移。"""
    tz_name = locale_profile["browser_timezone"]
    now = datetime.now(ZoneInfo(tz_name))
    offset = now.utcoffset()
    if offset is None:
        return 0
    return int(-(offset.total_seconds() // 60))


def _locale_short(locale_profile: dict) -> str:
    return locale_profile["browser_locale"].split("-")[0]


def _accept_language_for_locale(locale_value: str | None) -> str:
    """把 `zh` / `en-US` 之类的 locale 转成更像真实浏览器的 Accept-Language。"""
    normalized = (locale_value or "").strip()
    lowered = normalized.lower()
    if lowered.startswith("zh"):
        return "zh-CN,zh;q=0.9"
    if lowered.startswith("en"):
        return "en-US,en;q=0.9"
    if lowered.startswith("es"):
        return "es-ES,es;q=0.9"
    if normalized:
        short = normalized.split("-")[0]
        return f"{normalized},{short};q=0.9"
    return "en-US,en;q=0.9"


def _browser_like_session_headers(locale_value: str | None) -> dict:
    """补一组更接近 flows 的浏览器请求头。"""
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": _accept_language_for_locale(locale_value),
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Priority": "u=1, i",
    }


def _elements_options_client_payload() -> dict:
    return {
        "elements_options_client[stripe_js_locale]": "auto",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
    }


def _build_stripe_hcaptcha_url(
    invisible: bool = True,
    frame_id: str | None = None,
    origin: str = "https://js.stripe.com",
) -> str:
    hcaptcha_frame_id = frame_id or str(uuid.uuid4())
    page_name = "HCaptchaInvisible.html" if invisible else "HCaptcha.html"
    return (
        "https://b.stripecdn.com/stripethirdparty-srv/assets/"
        f"{DEFAULT_STRIPE_HCAPTCHA_ASSET_VERSION}/{page_name}"
        f"?id={hcaptcha_frame_id}&origin={urllib.parse.quote(origin, safe='')}"
    )


def _extract_payment_method_types(payload: dict) -> list[str]:
    payment_method_types = payload.get("payment_method_types")
    if isinstance(payment_method_types, list) and payment_method_types:
        return [pm for pm in payment_method_types if isinstance(pm, str)]

    specs = payload.get("payment_method_specs")
    if isinstance(specs, list):
        out = []
        for spec in specs:
            if isinstance(spec, dict) and spec.get("type"):
                out.append(spec["type"])
        if out:
            return out

    return ["card"]

def _build_browser_fingerprint(locale_profile: dict) -> dict:
    """构建 RecordBrowserInfo 的完整设备指纹 payload"""
    sw = locale_profile["screen_w"]
    sh = locale_profile["screen_h"]
    dpr = locale_profile["dpr"]
    cd = locale_profile["color_depth"]
    lang = locale_profile["browser_language"]
    tz_name = locale_profile["browser_timezone"]
    tz_offset = _browser_tz_offset(locale_profile)

    # 可用高度 = 屏幕高度 - 任务栏 (48-60px)
    avail_h = sh - random.randint(40, 60)

    return {
        "navigator": {
            "mediaDevices": {"audioinput": random.randint(1, 3), "videoinput": random.randint(0, 2),
                             "audiooutput": random.randint(1, 3)},
            "battery": {"charging": True, "chargingTime": 0, "dischargingTime": None,
                        "level": round(random.uniform(0.5, 1.0), 2)},
            "appCodeName": "Mozilla", "appName": "Netscape",
            "appVersion": USER_AGENT.replace("Mozilla/", ""),
            "cookieEnabled": True, "doNotTrack": None,
            "hardwareConcurrency": random.choice([8, 12, 16, 32]),
            "language": lang,
            "languages": [lang, lang.split("-")[0]],
            "maxTouchPoints": 0, "onLine": True,
            "platform": "Win32", "product": "Gecko", "productSub": "20030107",
            "userAgent": USER_AGENT,
            "vendor": "Google Inc.", "vendorSub": "",
            "webdriver": False,
            "deviceMemory": random.choice([4, 8, 16]),
            "pdfViewerEnabled": True, "javaEnabled": False,
            "plugins": "PDF Viewer,Chrome PDF Viewer,Chromium PDF Viewer,Microsoft Edge PDF Viewer,WebKit built-in PDF",
            "connections": {
                "effectiveType": "4g",
                "downlink": round(random.uniform(1.0, 10.0), 2),
                "rtt": random.choice([50, 100, 150, 200, 250, 300, 350, 400]),
                "saveData": False,
            },
        },
        "screen": {
            "availHeight": avail_h, "availWidth": sw,
            "availLeft": 0, "availTop": 0,
            "colorDepth": cd, "height": sh, "width": sw,
            "pixelDepth": cd,
            "orientation": "landscape-primary",
            "devicePixelRatio": dpr,
        },
        "timezone": {"offset": tz_offset, "timezone": tz_name},
        "canvas": hashlib.sha256(os.urandom(32)).hexdigest(),
        "permissions": {
            "geolocation": "denied", "notifications": "denied",
            "midi": "denied", "camera": "denied", "microphone": "denied",
            "background-fetch": "prompt", "background-sync": "granted",
            "persistent-storage": "granted", "accelerometer": "granted",
            "gyroscope": "granted", "magnetometer": "granted",
            "clipboard-read": "denied", "clipboard-write": "denied",
            "screen-wake-lock": "denied", "display-capture": "denied",
            "idle-detection": "denied",
        },
        "audio": {"sum": 124.04347527516074},
        "browserBars": {
            "locationbar": True, "menubar": True, "personalbar": True,
            "statusbar": True, "toolbar": True, "scrollbars": True,
        },
        "sensors": {
            "accelerometer": True, "gyroscope": True, "linearAcceleration": True,
            "absoluteOrientation": True, "relativeOrientation": True,
            "magnetometer": False, "ambientLight": False, "proximity": False,
        },
        "storage": {
            "localStorage": True, "sessionStorage": True,
            "indexedDB": True, "openDatabase": False,
        },
        "webGl": {
            "dataHash": hashlib.sha256(os.urandom(32)).hexdigest(),
            "vendor": "Google Inc. (NVIDIA)",
            "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 (0x00002882) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        "adblock": False,
        "clientRects": {
            "x": round(-10004 + random.uniform(-1, 1), 10),
            "y": round(2.35 + random.uniform(-0.01, 0.01), 10),
            "width": round(111.29 + random.uniform(-0.01, 0.01), 10),
            "height": round(111.29 + random.uniform(-0.01, 0.01), 10),
            "top": round(2.35 + random.uniform(-0.01, 0.01), 10),
            "bottom": round(113.64 + random.uniform(-0.01, 0.01), 10),
            "left": round(-10004 + random.uniform(-1, 1), 10),
            "right": round(-9893 + random.uniform(-1, 1), 10),
        },
        "fonts": {"installed_count": random.randint(40, 60), "not_installed_count": 0},
    }


def _gen_fingerprint():
    def _id():
        return str(uuid.uuid4()).replace("-", "") + uuid.uuid4().hex[:6]
    return _id(), _id(), _id()



_PLUGINS_STR = (
    "PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chrome PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chromium PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Microsoft Edge PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "WebKit built-in PDF,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf"
)
_CANVAS_FPS = [
    "0100100101111111101111101111111001110010110111110111111",
    "0100100101111111101111101111111001110010110111110111110",
    "0100100101111111101111101111111001110010110111110111101",
]
_AUDIO_FPS = [
    "d331ca493eb692cfcd19ae5db713ad4b",
    "a7c5f72e1b3d4e8f9c0d2a6b7e8f1c3d",
    "e4b8d6f2a0c3d5e7f9b1c3d5e7f9a0b2",
]


def _encode_m6(payload: dict) -> str:
    """JSON → urlencode → base64 (m.stripe.com/6 编码格式)"""
    raw = json.dumps(payload, separators=(",", ":"))
    return base64.b64encode(urllib.parse.quote(raw, safe="").encode()).decode()


def _b64url_seg(n: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(n)).rstrip(b"=").decode()


def register_fingerprint(http: "requests.Session") -> tuple[str, str, str]:
    """向 m.stripe.com/6 发送 4 次指纹上报, 返回服务端分配的 (guid, muid, sid)。
    如果请求失败, 返回本地随机生成的值。
    """
    # 本地备用值
    guid, muid, sid = _gen_fingerprint()
    fp_id = uuid.uuid4().hex

    # 屏幕参数 (US 常见配置)
    screens = [(1920, 1080, 1), (1536, 864, 1.25), (2560, 1440, 1), (1440, 900, 1)]
    sw, sh, dpr = random.choice(screens)
    vh = sh - random.randint(40, 70)  # viewport = screen - chrome
    cpu = random.choice([4, 8, 12, 16])
    canvas_fp = random.choice(_CANVAS_FPS)
    audio_fp = random.choice(_AUDIO_FPS)

    def _build_full(v2: int, inc_ids: bool) -> dict:
        s1, s2, s3, s4, s5 = (_b64url_seg() for _ in range(5))
        ts_now = int(time.time() * 1000)
        return {
            "v2": v2, "id": fp_id,
            "t": round(random.uniform(3, 120), 1),
            "tag": "$npm_package_version", "src": "js",
            "a": {
                "a": {"v": "true", "t": 0},
                "b": {"v": "true", "t": 0},
                "c": {"v": "en-US", "t": 0},
                "d": {"v": "Win32", "t": 0},
                "e": {"v": _PLUGINS_STR, "t": round(random.uniform(0, 0.5), 1)},
                "f": {"v": f"{sw}w_{vh}h_24d_{dpr}r", "t": 0},
                "g": {"v": str(cpu), "t": 0},
                "h": {"v": "false", "t": 0},
                "i": {"v": "sessionStorage-enabled, localStorage-enabled", "t": round(random.uniform(0.5, 2), 1)},
                "j": {"v": canvas_fp, "t": round(random.uniform(5, 120), 1)},
                "k": {"v": "", "t": 0},
                "l": {"v": USER_AGENT, "t": 0},
                "m": {"v": "", "t": 0},
                "n": {"v": "false", "t": round(random.uniform(3, 50), 1)},
                "o": {"v": audio_fp, "t": round(random.uniform(20, 30), 1)},
            },
            "b": {
                "a": f"https://{s1}.{s2}.{s3}/",
                "b": f"https://{s1}.{s3}/{s4}/{s5}/{_b64url_seg()}",
                "c": _b64url_seg(),
                "d": muid if inc_ids else "NA",
                "e": sid if inc_ids else "NA",
                "f": False, "g": True, "h": True,
                "i": ["location"], "j": [],
                "n": round(random.uniform(800, 2000), 1),
                "u": "chatgpt.com", "v": "auth.openai.com",
                "w": f"{ts_now}:{hashlib.sha256(os.urandom(32)).hexdigest()}",
            },
            "h": os.urandom(10).hex(),
        }

    def _build_mouse(source: str) -> dict:
        return {
            "muid": muid, "sid": sid,
            "url": f"https://{_b64url_seg()}.{_b64url_seg()}/{_b64url_seg()}/{_b64url_seg()}/{_b64url_seg()}",
            "source": source,
            "data": [random.randint(1, 8) for _ in range(10)],
        }

    m6_headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Origin": "https://m.stripe.network",
        "Referer": "https://m.stripe.network/",
    }
    m6_url = "https://m.stripe.com/6"
    _log("      [指纹] 向 m.stripe.com/6 注册设备指纹 ...")

    # #1 完整指纹 (v2=1, 无 ID)
    try:
        r1 = http.post(m6_url, data=_encode_m6(_build_full(1, False)), headers=m6_headers, timeout=10)
        if r1.status_code == 200:
            j = r1.json()
            muid = j.get("muid", muid)
            guid = j.get("guid", guid)
            sid = j.get("sid", sid)
            _log(f"      [指纹] #1 OK → muid={muid[:20]}...")
    except Exception as e:
        _log(f"      [指纹] #1 失败: {e}")

    # #2 完整指纹 (v2=2, 带 ID)
    try:
        r2 = http.post(m6_url, data=_encode_m6(_build_full(2, True)), headers=m6_headers, timeout=10)
        if r2.status_code == 200:
            j = r2.json()
            guid = j.get("guid", guid)
            _log(f"      [指纹] #2 OK → guid={guid[:20]}...")
    except Exception as e:
        _log(f"      [指纹] #2 失败: {e}")

    # #3 鼠标行为 (mouse-timings-10-v2)
    try:
        http.post(m6_url, data=_encode_m6(_build_mouse("mouse-timings-10-v2")), headers=m6_headers, timeout=10)
        _log("      [指纹] #3 OK (mouse-timings-v2)")
    except Exception:
        pass

    # #4 鼠标行为 (mouse-timings-10)
    try:
        http.post(m6_url, data=_encode_m6(_build_mouse("mouse-timings-10")), headers=m6_headers, timeout=10)
        _log("      [指纹] #4 OK (mouse-timings)")
    except Exception:
        pass

    _log(f"      [指纹] 完成 → guid={guid[:25]}... muid={muid[:25]}... sid={sid[:25]}...")
    return guid, muid, sid


def _gen_elements_session_id():
    """生成类似 elements_session_15hfldlRpSm 的 session id"""
    import random, string
    chars = string.ascii_letters + string.digits
    return "elements_session_" + "".join(random.choices(chars, k=11))


def _stripe_headers():
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }
def parse_checkout_url(raw: str) -> tuple[str, str]:
    """解析输入，返回 (session_id, stripe_checkout_url)

    支持以下格式:
      - 裸 session_id: cs_live_xxx / cs_test_xxx
      - Stripe URL: https://checkout.stripe.com/c/pay/cs_live_xxx
      - ChatGPT URL: https://chatgpt.com/checkout/openai_llc/cs_live_xxx
    """
    raw = raw.strip()
    m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", raw)
    if not m:
        raise ValueError(f"无法从输入中提取 checkout_session_id: {raw[:120]}...")
    session_id = m.group(1)

    # 构建用于 Playwright 等回退方案的 Stripe checkout URL
    # 如果输入是 checkout.stripe.com 的链接则直接使用，否则用标准格式构建
    if "checkout.stripe.com" in raw:
        stripe_url = raw
    else:
        stripe_url = f"https://checkout.stripe.com/c/pay/{session_id}"

    return session_id, stripe_url


def _should_generate_fresh_checkout(checkout_input: str | None, force_fresh: bool = False) -> bool:
    if force_fresh:
        return True
    normalized = (checkout_input or "").strip().lower()
    return normalized in {"", "fresh", "auto", "new", "generate", "checkout:auto"}


def _cookie_header_from_flow_request(request) -> str:
    cookie_lines = request.headers.get_all("cookie") or []
    if cookie_lines:
        return "; ".join(cookie_lines)
    return request.headers.get("cookie", "")


def _extract_cookie_value(cookie_header: str, name: str) -> str:
    if not cookie_header:
        return ""
    m = re.search(rf"(?:^|;\s*){re.escape(name)}=([^;]+)", cookie_header)
    if not m:
        return ""
    return urllib.parse.unquote(m.group(1))


def _decode_jwt_payload(token: str) -> dict:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        payload = json.loads(decoded)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _extract_email_from_access_token(token: str) -> str:
    payload = _decode_jwt_payload(token)
    profile = payload.get("https://api.openai.com/profile", {})
    if isinstance(profile, dict):
        return profile.get("email", "") or ""
    return ""


def _extract_plan_type_from_access_token(token: str) -> str:
    payload = _decode_jwt_payload(token)
    auth_claim = payload.get("https://api.openai.com/auth", {})
    if isinstance(auth_claim, dict):
        return auth_claim.get("chatgpt_plan_type", "") or ""
    return ""


def _is_access_token_expired(token: str, skew_seconds: int = 120) -> bool:
    payload = _decode_jwt_payload(token)
    exp = payload.get("exp")
    try:
        exp_ts = int(exp)
    except Exception:
        return False
    return (time.time() + max(0, int(skew_seconds))) >= exp_ts


def _compose_cookie_header(
    cookie_header: str = "",
    session_token: str = "",
    device_id: str = "",
) -> str:
    cookie_parts = []
    seen = set()

    for raw_part in (cookie_header or "").split(";"):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        name = part.split("=", 1)[0].strip()
        if name in seen:
            continue
        seen.add(name)
        cookie_parts.append(part)

    def _append(name: str, value: str):
        if not value or name in seen:
            return
        seen.add(name)
        cookie_parts.append(f"{name}={value}")

    _append("__Secure-next-auth.session-token", session_token)
    _append("oai-did", device_id)
    return "; ".join(cookie_parts)


def _merge_cookie_headers(*cookie_headers: str) -> str:
    merged_parts = []
    seen = set()
    for cookie_header in cookie_headers:
        for raw_part in (cookie_header or "").split(";"):
            part = raw_part.strip()
            if not part or "=" not in part:
                continue
            name = part.split("=", 1)[0].strip()
            if not name or name in seen:
                continue
            seen.add(name)
            merged_parts.append(part)
    return "; ".join(merged_parts)


def _seed_session_cookies_from_header(session_obj, cookie_header: str, domain: str = ".chatgpt.com"):
    if not cookie_header or not hasattr(session_obj, "cookies"):
        return
    for raw_part in (cookie_header or "").split(";"):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name or not value:
            continue
        try:
            session_obj.cookies.set(name, value, domain=domain)
        except Exception:
            continue


def _cookie_header_from_session(session_obj, domain_keyword: str = "chatgpt.com") -> str:
    if not hasattr(session_obj, "cookies"):
        return ""
    cookie_parts = []
    seen = set()
    try:
        jar_iter = list(session_obj.cookies)
    except Exception:
        jar_iter = []
    for cookie in jar_iter:
        try:
            name = (getattr(cookie, "name", "") or "").strip()
            value = getattr(cookie, "value", "") or ""
            domain = (getattr(cookie, "domain", "") or "").strip().lower()
        except Exception:
            continue
        if not name or not value:
            continue
        if domain_keyword and domain_keyword not in domain:
            continue
        if name in seen:
            continue
        seen.add(name)
        cookie_parts.append(f"{name}={value}")
    return "; ".join(cookie_parts)


def _chatgpt_auth_headers(
    *,
    access_token: str = "",
    cookie_header: str = "",
    user_agent: str = "",
    accept_language: str = "",
    oai_device_id: str = "",
    accept: str = "application/json",
    include_origin: bool = False,
) -> dict:
    headers = {
        "user-agent": user_agent or USER_AGENT,
        "accept": accept,
        "accept-language": accept_language or "en-US,en;q=0.9",
        "referer": "https://chatgpt.com/",
    }
    if include_origin:
        headers["origin"] = "https://chatgpt.com"
    if access_token:
        headers["authorization"] = f"Bearer {access_token}"
    if cookie_header:
        headers["cookie"] = cookie_header
    if oai_device_id:
        headers["oai-device-id"] = oai_device_id
    return headers


def _warm_chatgpt_checkout_context(
    session_obj,
    *,
    access_token: str,
    session_token: str,
    cookie_header: str,
    user_agent: str,
    accept_language: str,
    locale_profile: dict,
    oai_device_id: str,
    billing_country: str,
    include_home_bounce: bool = True,
) -> dict:
    """
    在 fresh checkout 之前补齐 flows 中真实出现的 ChatGPT 侧预热请求：
    - 首页 / auth/session
    - accounts/check
    - domain-density-eligibility
    - checkout_pricing_config
    - （可选）home 页面的一组后台接口

    目的不是“证明本地像浏览器”，而是尽量补齐服务端在生成 checkout 前
    依赖的 cookie / eligibility / pricing 上下文。
    """
    if not access_token:
        return {
            "cookie_header": cookie_header,
            "session_token": session_token,
            "access_token": access_token,
            "device_id": oai_device_id,
        }

    _seed_session_cookies_from_header(session_obj, cookie_header, domain=".chatgpt.com")
    billing_country = str(billing_country or "US").upper()
    tz_offset = _browser_tz_offset(locale_profile)

    def _merged_cookie() -> str:
        return _merge_cookie_headers(cookie_header, _cookie_header_from_session(session_obj, "chatgpt.com"))

    def _session_cookie(name: str) -> str:
        try:
            return session_obj.cookies.get(name, "")
        except Exception:
            return ""

    auth_session_data = {}
    domain_density_data = {}
    warm_steps = [
        (
            "home",
            "GET",
            "https://chatgpt.com/",
            None,
            _chatgpt_auth_headers(
                access_token=access_token,
                cookie_header=_merged_cookie(),
                user_agent=user_agent,
                accept_language=accept_language,
                oai_device_id=oai_device_id,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            ),
        ),
        (
            "auth_session",
            "GET",
            "https://chatgpt.com/api/auth/session",
            None,
            _chatgpt_auth_headers(
                cookie_header=_merged_cookie(),
                user_agent=user_agent,
                accept_language=accept_language,
                oai_device_id=oai_device_id,
                accept="application/json",
            ),
        ),
        (
            "accounts_check",
            "GET",
            f"https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min={tz_offset}",
            None,
            _chatgpt_auth_headers(
                access_token=access_token,
                cookie_header=_merged_cookie(),
                user_agent=user_agent,
                accept_language=accept_language,
                oai_device_id=oai_device_id,
                accept="application/json",
                include_origin=True,
            ),
        ),
        (
            "domain_density",
            "GET",
            "https://chatgpt.com/backend-api/accounts/domain-density-eligibility",
            None,
            _chatgpt_auth_headers(
                access_token=access_token,
                cookie_header=_merged_cookie(),
                user_agent=user_agent,
                accept_language=accept_language,
                oai_device_id=oai_device_id,
                accept="application/json",
                include_origin=True,
            ),
        ),
        (
            "pricing_countries",
            "GET",
            "https://chatgpt.com/backend-api/checkout_pricing_config/countries",
            None,
            _chatgpt_auth_headers(
                access_token=access_token,
                cookie_header=_merged_cookie(),
                user_agent=user_agent,
                accept_language=accept_language,
                oai_device_id=oai_device_id,
                accept="application/json",
                include_origin=True,
            ),
        ),
        (
            "pricing_config",
            "GET",
            f"https://chatgpt.com/backend-api/checkout_pricing_config/configs/{billing_country}",
            None,
            _chatgpt_auth_headers(
                access_token=access_token,
                cookie_header=_merged_cookie(),
                user_agent=user_agent,
                accept_language=accept_language,
                oai_device_id=oai_device_id,
                accept="application/json",
                include_origin=True,
            ),
        ),
    ]

    if include_home_bounce:
        warm_steps.extend(
            [
                (
                    "conversation_init",
                    "POST",
                    "https://chatgpt.com/backend-api/conversation/init",
                    {
                        "gizmo_id": None,
                        "requested_default_model": None,
                        "conversation_id": None,
                        "timezone_offset_min": tz_offset,
                    },
                    _chatgpt_auth_headers(
                        access_token=access_token,
                        cookie_header=_merged_cookie(),
                        user_agent=user_agent,
                        accept_language=accept_language,
                        oai_device_id=oai_device_id,
                        accept="application/json",
                        include_origin=True,
                    ) | {"content-type": "application/json"},
                ),
                (
                    "sources_dropdown",
                    "GET",
                    "https://chatgpt.com/backend-api/apps/sources_dropdown",
                    None,
                    _chatgpt_auth_headers(
                        access_token=access_token,
                        cookie_header=_merged_cookie(),
                        user_agent=user_agent,
                        accept_language=accept_language,
                        oai_device_id=oai_device_id,
                        accept="application/json",
                        include_origin=True,
                    ),
                ),
                (
                    "user_segments",
                    "GET",
                    "https://chatgpt.com/backend-api/user_segments",
                    None,
                    _chatgpt_auth_headers(
                        access_token=access_token,
                        cookie_header=_merged_cookie(),
                        user_agent=user_agent,
                        accept_language=accept_language,
                        oai_device_id=oai_device_id,
                        accept="application/json",
                        include_origin=True,
                    ),
                ),
                (
                    "beacons_home",
                    "GET",
                    "https://chatgpt.com/backend-api/beacons/home",
                    None,
                    _chatgpt_auth_headers(
                        access_token=access_token,
                        cookie_header=_merged_cookie(),
                        user_agent=user_agent,
                        accept_language=accept_language,
                        oai_device_id=oai_device_id,
                        accept="application/json",
                        include_origin=True,
                    ),
                ),
                (
                    "realtime_status",
                    "POST",
                    "https://chatgpt.com/realtime/status",
                    {
                        "conversation_id": None,
                        "requested_voice_mode": "advanced",
                        "gizmo_id": None,
                        "voice": "cove",
                        "requested_default_model": "auto",
                        "timezone_offset_min": tz_offset,
                        "nonce": oai_device_id,
                        "voice_status_request_id": str(uuid.uuid4()).upper(),
                    },
                    _chatgpt_auth_headers(
                        access_token=access_token,
                        cookie_header=_merged_cookie(),
                        user_agent=user_agent,
                        accept_language=accept_language,
                        oai_device_id=oai_device_id,
                        accept="application/json",
                        include_origin=True,
                    ) | {"content-type": "application/json"},
                ),
            ]
        )

    before_names = {
        part.split("=", 1)[0].strip()
        for part in (cookie_header or "").split(";")
        if "=" in part
    }

    _log("      [fresh] 预热 ChatGPT 结账上下文 ...")
    for step_name, method, url, payload, headers in warm_steps:
        try:
            if method == "GET":
                resp = session_obj.get(url, headers=headers, timeout=20)
            else:
                resp = session_obj.post(url, headers=headers, json=payload, timeout=20)
            _log(f"      [fresh:warm] {step_name} → {resp.status_code}")
            if step_name == "auth_session" and resp.status_code == 200:
                try:
                    auth_session_data = resp.json() if resp is not None else {}
                except Exception:
                    auth_session_data = {}
            elif step_name == "domain_density":
                try:
                    domain_density_data = resp.json() if resp is not None else {}
                except Exception:
                    domain_density_data = {}
        except Exception as e:
            _log(f"      [fresh:warm] {step_name} 异常: {e}")

    warmed_cookie = _merge_cookie_headers(cookie_header, _cookie_header_from_session(session_obj, "chatgpt.com"))
    warmed_session_token = (
        _extract_cookie_value(warmed_cookie, "__Secure-next-auth.session-token")
        or _session_cookie("__Secure-next-auth.session-token")
        or session_token
    )
    warmed_device_id = (
        _extract_cookie_value(warmed_cookie, "oai-did")
        or _session_cookie("oai-did")
        or oai_device_id
    )
    warmed_access_token = (
        (auth_session_data.get("accessToken") or "").strip()
        if isinstance(auth_session_data, dict)
        else ""
    ) or access_token

    after_names = {
        part.split("=", 1)[0].strip()
        for part in warmed_cookie.split(";")
        if "=" in part
    }
    added_names = sorted(name for name in after_names - before_names if name)
    if added_names:
        _log(f"      [fresh:warm] 新增 cookies: {', '.join(added_names)}")
    if isinstance(domain_density_data, dict) and domain_density_data:
        _log(
            "      [fresh:warm] domain-density: "
            f"eligible={domain_density_data.get('eligible')} "
            f"domain_user_count={domain_density_data.get('domain_user_count')}"
        )

    return {
        "cookie_header": warmed_cookie,
        "session_token": warmed_session_token,
        "access_token": warmed_access_token,
        "device_id": warmed_device_id,
        "oai_device_id": warmed_device_id,
        "domain_density": domain_density_data,
    }


def _extract_checkout_identifiers(data: dict) -> tuple[str, str, str]:
    cs_id = (data.get("checkout_session_id") or data.get("session_id") or "").strip()
    processor_entity = (data.get("processor_entity") or "").strip()
    checkout_url = (
        data.get("checkout_url")
        or data.get("url")
        or data.get("openai_checkout_url")
        or ""
    ).strip()

    candidate_texts = [
        checkout_url,
        data.get("success_url", ""),
        data.get("cancel_url", ""),
        data.get("return_url", ""),
        data.get("client_secret", ""),
    ]

    if not cs_id:
        for text in candidate_texts:
            m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", text or "")
            if m:
                cs_id = m.group(1)
                break

    if not processor_entity:
        for text in candidate_texts:
            m = re.search(r"/checkout/([^/]+)/cs_(?:live|test)_[A-Za-z0-9]+", text or "")
            if m:
                processor_entity = m.group(1)
                break
        if not processor_entity:
            m = re.search(r"processor_entity=([A-Za-z0-9_]+)", " ".join(candidate_texts))
            if m:
                processor_entity = m.group(1)

    if not checkout_url and cs_id and processor_entity:
        checkout_url = f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"

    return cs_id, processor_entity, checkout_url


def _select_fresh_checkout_url(
    *,
    provider_url: str,
    canonical_url: str,
    fresh_cfg: dict,
    checkout_payload: dict,
) -> str:
    """Choose which checkout URL should be exposed to callers.

    ChatGPT's checkout API may return a provider/hosted URL (for example the
    long hosted checkout URL) while the automation can also reconstruct the
    canonical in-app URL:

        https://chatgpt.com/checkout/{processor_entity}/{cs_live...}

    Historically we always rewrote the API response to the canonical URL. That
    is correct for embedded/custom checkout replay, but it hides the real
    hosted/long link when the request was created with hosted checkout mode.

    Selection is config driven:
      - fresh_checkout.output_url_mode or fresh_checkout.plan.output_url_mode
        can be provider/raw/long/hosted or canonical/chatgpt/short.
      - If omitted, checkout_ui_mode=hosted defaults to provider; everything
        else defaults to canonical.
    """

    provider_url = (provider_url or "").strip()
    canonical_url = (canonical_url or "").strip()
    plan_cfg = fresh_cfg.get("plan") or {}
    explicit_mode = str(
        plan_cfg.get("output_url_mode")
        or fresh_cfg.get("output_url_mode")
        or ""
    ).strip().lower()
    checkout_ui_mode = str(
        plan_cfg.get("checkout_ui_mode")
        or checkout_payload.get("checkout_ui_mode")
        or ""
    ).strip().lower()

    provider_modes = {"provider", "raw", "long", "hosted", "pay_openai", "pay.openai.com"}
    canonical_modes = {"canonical", "chatgpt", "short", "custom", "embedded"}

    if explicit_mode in provider_modes:
        return provider_url or canonical_url
    if explicit_mode in canonical_modes:
        return canonical_url or provider_url

    if checkout_ui_mode in {"hosted", "hosted_checkout", "redirect"}:
        return provider_url or canonical_url
    return canonical_url or provider_url


def _extract_checkout_totals(payload: dict | None) -> dict:
    payload = payload or {}
    total_summary = payload.get("total_summary") or {}
    invoice = payload.get("invoice") or {}

    def _to_int(value):
        if value in (None, ""):
            return None
        try:
            return int(value)
        except Exception:
            return None

    return {
        "due": _to_int(total_summary.get("due", invoice.get("amount_due"))),
        "subtotal": _to_int(total_summary.get("subtotal", invoice.get("subtotal"))),
        "total": _to_int(total_summary.get("total", invoice.get("total"))),
        "currency": (
            payload.get("currency")
            or invoice.get("currency")
            or ""
        ).lower(),
    }


def _resolve_expected_checkout_due(fresh_cfg: dict) -> int | None:
    candidates = [
        fresh_cfg.get("expected_due"),
        ((fresh_cfg.get("pricing_expectation") or {}).get("expected_due")),
    ]
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        try:
            return int(candidate)
        except Exception:
            raise FreshCheckoutAuthError(f"expected_due 配置非法: {candidate!r}")
    return None


def _check_coupon_eligibility(
    session_obj,
    *,
    access_token: str,
    cookie_header: str,
    user_agent: str,
    accept_language: str,
    oai_device_id: str,
    coupon: str,
    is_coupon_from_query_param: bool,
    referer_url: str = "",
) -> dict:
    if not coupon:
        return {}

    url = "https://chatgpt.com/backend-api/promo_campaign/check_coupon"
    params = {
        "coupon": coupon,
        "is_coupon_from_query_param": "true" if is_coupon_from_query_param else "false",
    }
    headers = _chatgpt_auth_headers(
        access_token=access_token,
        cookie_header=cookie_header,
        user_agent=user_agent,
        accept_language=accept_language,
        oai_device_id=oai_device_id,
        accept="application/json",
        include_origin=True,
    )
    headers["referer"] = referer_url or "https://chatgpt.com/"

    _log_request("GET", url, params=params, tag="[fresh] check_coupon")
    try:
        resp = session_obj.get(url, params=params, headers=headers, timeout=20)
    except Exception as e:
        _log(f"      [fresh] check_coupon 异常: {e}")
        return {}

    _log_response(resp, tag="[fresh] check_coupon")
    if resp.status_code != 200:
        _log(f"      [fresh] check_coupon 非 200: {resp.status_code}")
        return {}

    try:
        data = resp.json()
    except Exception as e:
        _log(f"      [fresh] check_coupon JSON 解析失败: {e}")
        return {}

    redemption = data.get("redemption") or {}
    _log(
        "      [fresh] check_coupon: "
        f"state={data.get('state')} "
        f"user_redeemed={redemption.get('redeemed_by_user')} "
        f"workspace_redeemed={redemption.get('redeemed_by_workspace')}"
    )
    return data if isinstance(data, dict) else {}


def _is_checkout_inactive_text(text: str) -> bool:
    lower = (text or "").lower()
    markers = (
        "checkout_not_active_session",
        "this checkout session is no longer active",
        "checkout session is no longer active",
        "session is no longer active",
    )
    return any(marker in lower for marker in markers)


def _raise_if_checkout_inactive_response(resp: requests.Response, context: str):
    if _is_checkout_inactive_text(resp.text):
        try:
            payload = resp.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = error.get("message") or resp.text[:300]
        except Exception:
            message = resp.text[:300]
        raise CheckoutSessionInactive(f"{context}: {message}")


def _load_fresh_checkout_bootstrap(flows_path: str) -> dict:
    try:
        from mitmproxy.io import FlowReader
    except Exception as e:
        raise FreshCheckoutAuthError(f"读取 flows 需要 mitmproxy: {e}") from e

    if not os.path.exists(flows_path):
        raise FreshCheckoutAuthError(f"flows 不存在: {flows_path}")

    latest_auth = None
    latest_checkout = None
    latest_sentinel = None

    with open(flows_path, "rb") as f:
        for idx, flow in enumerate(FlowReader(f).stream()):
            req = getattr(flow, "request", None)
            if not req or req.host != "chatgpt.com":
                continue

            base_url = req.pretty_url.split("?", 1)[0]
            if req.method == "GET" and base_url == "https://chatgpt.com/api/auth/session":
                latest_auth = (idx, flow)
                continue

            if req.method == "POST" and base_url == "https://chatgpt.com/backend-api/payments/checkout":
                latest_checkout = (idx, flow)
                continue

            if req.method == "POST" and base_url == "https://chatgpt.com/backend-api/sentinel/req":
                try:
                    body = json.loads(req.get_text(strict=False) or "{}")
                except Exception:
                    body = {}
                if body.get("flow") == "chatgpt_checkout":
                    latest_sentinel = (idx, flow)

    if not latest_checkout:
        raise FreshCheckoutAuthError("flows 中未找到 /backend-api/payments/checkout 请求")

    checkout_req = latest_checkout[1].request
    checkout_resp = latest_checkout[1].response
    checkout_body = {}
    try:
        checkout_body = json.loads(checkout_req.get_text(strict=False) or "{}")
    except Exception:
        checkout_body = {}

    checkout_resp_json = {}
    try:
        checkout_resp_json = checkout_resp.json() if checkout_resp else {}
    except Exception:
        checkout_resp_json = {}

    auth_req = latest_auth[1].request if latest_auth else checkout_req
    sentinel_req = latest_sentinel[1].request if latest_sentinel else None

    cookie_header = _cookie_header_from_flow_request(checkout_req)
    bootstrap = {
        "flows_path": flows_path,
        "cookie_header": cookie_header,
        "user_agent": checkout_req.headers.get("user-agent")
        or auth_req.headers.get("user-agent")
        or USER_AGENT,
        "accept_language": checkout_req.headers.get("accept-language")
        or auth_req.headers.get("accept-language")
        or "zh-CN,zh;q=0.9",
        "oai_language": checkout_req.headers.get("oai-language", ""),
        "oai_device_id": checkout_req.headers.get("oai-device-id")
        or auth_req.headers.get("oai-device-id")
        or _extract_cookie_value(cookie_header, "oai-did"),
        "oai_client_version": checkout_req.headers.get("oai-client-version", ""),
        "oai_client_build_number": checkout_req.headers.get("oai-client-build-number", ""),
        "openai_sentinel_token": checkout_req.headers.get("openai-sentinel-token", ""),
        "checkout_body": checkout_body,
        "checkout_response": checkout_resp_json,
    }

    if sentinel_req is not None:
        bootstrap["sentinel_url"] = sentinel_req.pretty_url
        bootstrap["sentinel_body"] = sentinel_req.get_text(strict=False) or ""
        bootstrap["sentinel_headers"] = {
            "content-type": sentinel_req.headers.get("content-type", "text/plain;charset=UTF-8"),
            "origin": sentinel_req.headers.get("origin", "https://chatgpt.com"),
            "referer": sentinel_req.headers.get(
                "referer",
                "https://chatgpt.com/backend-api/sentinel/frame.html",
            ),
            "user-agent": sentinel_req.headers.get("user-agent") or bootstrap["user_agent"],
            "accept-language": sentinel_req.headers.get("accept-language", bootstrap["accept_language"]),
            "sec-ch-ua": sentinel_req.headers.get("sec-ch-ua", ""),
            "sec-ch-ua-mobile": sentinel_req.headers.get("sec-ch-ua-mobile", ""),
            "sec-ch-ua-platform": sentinel_req.headers.get("sec-ch-ua-platform", ""),
            "oai-device-id": sentinel_req.headers.get("oai-device-id", bootstrap["oai_device_id"]),
        }

    return bootstrap


def _build_abcard_checkout_payload(fresh_cfg: dict) -> dict:
    plan_cfg = fresh_cfg.get("plan") or {}
    billing_country = str(plan_cfg.get("billing_country") or "US").upper()
    payload = {
        "plan_type": plan_cfg.get("plan_name") or "chatgptteamplan",
        "payment_lower_bound_amount_cents": int(
            plan_cfg.get("payment_lower_bound_amount_cents", 0) or 0
        ),
        "payment_upper_bound_amount_cents": int(
            plan_cfg.get("payment_upper_bound_amount_cents", 100000) or 100000
        ),
        "billing_country_code": billing_country,
        "billing_currency_code": str(plan_cfg.get("billing_currency") or "USD").upper(),
        "workspace_name": str(plan_cfg.get("workspace_name") or "MyWorkspace"),
        "seat_quantity": int(plan_cfg.get("seat_quantity", 5) or 5),
    }
    promo_campaign_id = plan_cfg.get("promo_campaign_id")
    if promo_campaign_id:
        payload["promo_campaign_id"] = promo_campaign_id
    # 非 US 国家强制指定 processor_entity 为 openai_ie（爱尔兰实体）
    processor_entity = plan_cfg.get("processor_entity", "")
    if not processor_entity and billing_country != "US":
        processor_entity = "openai_ie"
    if processor_entity:
        payload["processor_entity"] = processor_entity
    return payload


def _build_fresh_checkout_body(fresh_cfg: dict, bootstrap: dict) -> dict:
    base = json.loads(json.dumps(bootstrap.get("checkout_body") or {}))
    plan_cfg = fresh_cfg.get("plan") or {}

    plan_name = plan_cfg.get("plan_name") or (base.get("plan_name") if base else None) or "chatgptteamplan"
    is_plus = "plus" in str(plan_name).lower()
    default_entry = "all_plans_pricing_modal" if is_plus else "team_workspace_purchase_modal"

    if not base:
        base = {
            "entry_point": default_entry,
            "plan_name": plan_name,
            "billing_details": {
                "country": "US",
                "currency": "USD",
            },
            "cancel_url": "https://chatgpt.com/#pricing",
            "checkout_ui_mode": "custom",
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free" if is_plus else "team-1-month-free",
                "is_coupon_from_query_param": False,
            },
        }
        if not is_plus:
            base["team_plan_data"] = {
                "workspace_name": "MyWorkspace",
                "price_interval": "month",
                "seat_quantity": 5,
            }

    if plan_cfg.get("entry_point"):
        base["entry_point"] = plan_cfg["entry_point"]
    base.setdefault("entry_point", default_entry)

    if plan_cfg.get("plan_name"):
        base["plan_name"] = plan_cfg["plan_name"]
    base.setdefault("plan_name", plan_name)

    if is_plus:
        # Plus 没有 workspace/seat 概念，删掉防止后端拒绝
        base.pop("team_plan_data", None)
    else:
        team_plan_data = dict(base.get("team_plan_data") or {})
        if plan_cfg.get("workspace_name"):
            team_plan_data["workspace_name"] = str(plan_cfg["workspace_name"])
        team_plan_data.setdefault("workspace_name", "MyWorkspace")
        if plan_cfg.get("price_interval"):
            team_plan_data["price_interval"] = plan_cfg["price_interval"]
        team_plan_data.setdefault("price_interval", "month")
        if "seat_quantity" in plan_cfg and plan_cfg["seat_quantity"] is not None:
            team_plan_data["seat_quantity"] = int(plan_cfg["seat_quantity"])
        team_plan_data.setdefault("seat_quantity", 5)
        base["team_plan_data"] = team_plan_data

    billing_details = dict(base.get("billing_details") or {})
    if plan_cfg.get("billing_country"):
        billing_details["country"] = str(plan_cfg["billing_country"]).upper()
    billing_details.setdefault("country", "US")
    if plan_cfg.get("billing_currency"):
        billing_details["currency"] = str(plan_cfg["billing_currency"]).upper()
    billing_details.setdefault("currency", "USD")
    base["billing_details"] = billing_details

    base["cancel_url"] = plan_cfg.get("cancel_url") or base.get("cancel_url") or "https://chatgpt.com/#pricing"
    base["checkout_ui_mode"] = (
        plan_cfg.get("checkout_ui_mode")
        or base.get("checkout_ui_mode")
        or "custom"
    )

    promo_campaign_id = plan_cfg.get("promo_campaign_id")
    if promo_campaign_id == "":
        base.pop("promo_campaign", None)
    else:
        promo_campaign = dict(base.get("promo_campaign") or {})
        effective_promo_id = promo_campaign_id or promo_campaign.get("promo_campaign_id")
        if effective_promo_id:
            promo_campaign["promo_campaign_id"] = effective_promo_id
            promo_campaign["is_coupon_from_query_param"] = bool(
                plan_cfg.get(
                    "is_coupon_from_query_param",
                    promo_campaign.get("is_coupon_from_query_param", False),
                )
            )
            base["promo_campaign"] = promo_campaign

    return base


def _fetch_auth_session_with_cookie(
    session: requests.Session,
    cookie_header: str,
    user_agent: str,
    accept_language: str,
) -> dict:
    if not cookie_header:
        return {}
    auth_headers = {
        "user-agent": user_agent,
        "accept": "application/json",
        "referer": "https://chatgpt.com/",
        "accept-language": accept_language,
        "cookie": cookie_header,
    }
    resp = session.get("https://chatgpt.com/api/auth/session", headers=auth_headers, timeout=30)
    if resp.status_code != 200:
        raise FreshCheckoutAuthError(
            f"/api/auth/session 失败 [{resp.status_code}]: {resp.text[:300]}"
        )
    try:
        data = resp.json()
    except Exception as e:
        raise FreshCheckoutAuthError(f"/api/auth/session JSON 解析失败: {e}") from e
    return data if isinstance(data, dict) else {}


def _refresh_openai_sentinel_token(session: requests.Session, cookie_header: str, bootstrap: dict) -> str:
    sentinel_url = bootstrap.get("sentinel_url")
    sentinel_body = bootstrap.get("sentinel_body")
    if not sentinel_url or not sentinel_body:
        return bootstrap.get("openai_sentinel_token", "")

    headers = {
        k: v
        for k, v in (bootstrap.get("sentinel_headers") or {}).items()
        if v
    }
    headers["cookie"] = cookie_header

    _log("      [fresh] 刷新 openai-sentinel-token ...")
    resp = session.post(sentinel_url, headers=headers, data=sentinel_body, timeout=30)
    if resp.status_code != 200:
        _log(f"      [fresh] sentinel/req 失败 [{resp.status_code}]，回退使用 bootstrap token")
        return bootstrap.get("openai_sentinel_token", "")

    try:
        data = resp.json()
    except Exception as e:
        _log(f"      [fresh] sentinel/req JSON 解析失败: {e}，回退使用 bootstrap token")
        return bootstrap.get("openai_sentinel_token", "")

    token = data.get("token", "")
    if token:
        _log(f"      [fresh] sentinel token 已刷新 ({len(token)} chars)")
        return token

    _log("      [fresh] sentinel/req 未返回 token，回退使用 bootstrap token")
    return bootstrap.get("openai_sentinel_token", "")


def generate_fresh_checkout(
    session: requests.Session,
    cfg: dict,
    locale_profile: dict | None = None,
) -> dict:
    fresh_cfg = cfg.get("fresh_checkout") or {}
    if not fresh_cfg.get("enabled", False):
        raise FreshCheckoutAuthError("fresh_checkout 未启用")

    locale_profile = locale_profile or LOCALE_PROFILES["US"]
    cfg_dir = os.path.dirname(os.path.abspath(cfg.get("_loaded_from") or __file__))
    auth_cfg = fresh_cfg.get("auth") or {}
    fresh_proxy_cfg = fresh_cfg["proxy"] if "proxy" in fresh_cfg else _PROXY_OVERRIDE_SENTINEL
    auto_register_cfg = (auth_cfg.get("auto_register") or fresh_cfg.get("auto_register") or {})
    auth_mode = (auth_cfg.get("mode") or "").strip().lower()
    request_style = (fresh_cfg.get("request_style") or "").strip().lower()
    access_token = (auth_cfg.get("access_token") or "").strip()
    session_token = (auth_cfg.get("session_token") or "").strip()
    bootstrap_cookie_header = (auth_cfg.get("cookie_header") or "").strip()
    oai_device_id = (
        (auth_cfg.get("device_id") or "").strip()
        or (auth_cfg.get("oai_device_id") or "").strip()
    )
    auto_register_enabled = bool(
        auto_register_cfg.get("enabled", False)
        or auth_mode in {"auto_register", "register", "abcard_register", "abcard_auth"}
    )
    auto_register_forced = auth_mode in {"auto_register", "register", "abcard_register", "abcard_auth"}
    auto_register_used = bool(auth_cfg.get("_auto_register_used", False))

    if not any((access_token, session_token, bootstrap_cookie_header)) and not auto_register_used:
        prefer_existing_bundle_auth = auto_register_cfg.get("prefer_existing_bundle_auth")
        if prefer_existing_bundle_auth is None:
            prefer_existing_bundle_auth = True
        if prefer_existing_bundle_auth:
            existing_bundle_auth = _load_existing_auth_from_local_bundle_config(cfg, fresh_cfg)
            if existing_bundle_auth:
                _log("[0/6] 自动复用本地 bundle 现成登录态生成 fresh checkout ...")
                refreshed_cfg = _build_cfg_with_fresh_auth(
                    cfg,
                    existing_bundle_auth,
                    forced_mode="access_token",
                    mark_auto_register_used=False,
                )
                return generate_fresh_checkout(session, refreshed_cfg, locale_profile=locale_profile)

    if auto_register_forced and not auto_register_used:
        prefer_existing_bundle_auth = auto_register_cfg.get("prefer_existing_bundle_auth")
        if prefer_existing_bundle_auth is None:
            prefer_existing_bundle_auth = True
        if prefer_existing_bundle_auth:
            existing_bundle_auth = _load_existing_auth_from_local_bundle_config(cfg, fresh_cfg)
            if existing_bundle_auth:
                _log("[0/6] 优先复用本地 bundle 现成登录态生成 fresh checkout ...")
                refreshed_cfg = _build_cfg_with_fresh_auth(
                    cfg,
                    existing_bundle_auth,
                    forced_mode="access_token",
                    mark_auto_register_used=False,
                )
                return generate_fresh_checkout(session, refreshed_cfg, locale_profile=locale_profile)

        _log("[0/6] 先通过本地注册流程获取 fresh 登录态 ...")
        provisioned_auth = _provision_openai_auth_via_local_bundle(cfg, fresh_cfg)
        refreshed_cfg = _build_cfg_with_fresh_auth(
            cfg,
            provisioned_auth,
            forced_mode="access_token",
            mark_auto_register_used=True,
        )
        return generate_fresh_checkout(session, refreshed_cfg, locale_profile=locale_profile)

    if not auth_mode:
        auth_mode = "access_token" if (access_token or session_token or bootstrap_cookie_header) else "flows"
    if not request_style:
        request_style = "abcard" if auth_mode == "access_token" else "modern"

    bootstrap = {}
    should_load_bootstrap = bool(
        fresh_cfg.get("bootstrap_from_flows", True)
        and (
            auth_mode == "flows"
            or fresh_cfg.get("use_flows_for_templates", False)
            or request_style in {"modern", "flow"}
        )
    )

    flows_path = fresh_cfg.get("flows_path", "../flows")
    if should_load_bootstrap:
        if not os.path.isabs(flows_path):
            candidate_paths = [
                os.path.abspath(os.path.join(cfg_dir, flows_path)),
                os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), flows_path)),
                os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "flows")),
            ]
            flows_path = next((path for path in candidate_paths if os.path.exists(path)), candidate_paths[0])
        _log("[0/6] 生成 fresh checkout（flows 模板 / access_token 模式） ...")
        _log(f"      [fresh] bootstrap flows: {flows_path}")
        bootstrap = _load_fresh_checkout_bootstrap(flows_path)
    else:
        _log("[0/6] 生成 fresh checkout（access_token 模式） ...")

    cookie_header = _compose_cookie_header(
        bootstrap_cookie_header or bootstrap.get("cookie_header", ""),
        session_token=session_token,
        device_id=oai_device_id or bootstrap.get("oai_device_id", ""),
    )
    user_agent = auth_cfg.get("user_agent") or bootstrap.get("user_agent") or USER_AGENT
    accept_language = (
        auth_cfg.get("accept_language")
        or bootstrap.get("accept_language")
        or _accept_language_for_locale(locale_profile["browser_locale"])
    )
    oai_device_id = (
        oai_device_id
        or bootstrap.get("oai_device_id")
        or _extract_cookie_value(cookie_header, "oai-did")
        or str(uuid.uuid4())
    )
    cookie_header = _compose_cookie_header(
        cookie_header,
        session_token=session_token,
        device_id=oai_device_id,
    )
    chatgpt_http, fresh_transport = _create_chatgpt_http_session(
        cfg,
        user_agent=user_agent,
        proxy_cfg_override=fresh_proxy_cfg,
    )
    _log(f"      [fresh] ChatGPT transport: {fresh_transport}")
    resolved_fresh_proxy_url = _build_proxy_url_from_cfg(_resolve_proxy_cfg(cfg, fresh_proxy_cfg))
    if resolved_fresh_proxy_url:
        _log(f"      [fresh] ChatGPT proxy: {resolved_fresh_proxy_url}")
    elif fresh_proxy_cfg is not _PROXY_OVERRIDE_SENTINEL:
        _log("      [fresh] ChatGPT proxy: 无 (fresh_checkout.proxy 显式直连)")

    auth_data = {}
    prefer_session_refresh = bool(auth_cfg.get("prefer_session_refresh", True))
    if session_token and prefer_session_refresh:
        _log("      [fresh] 使用 session_token 刷新 access_token (/api/auth/session) ...")
        try:
            auth_data = _fetch_auth_session_with_cookie(
                chatgpt_http,
                cookie_header=cookie_header,
                user_agent=user_agent,
                accept_language=accept_language,
            )
            refreshed_access_token = (auth_data.get("accessToken") or "").strip()
            if refreshed_access_token:
                access_token = refreshed_access_token
                _log("      [fresh] access_token 已通过 session_token 刷新")
        except Exception as e:
            _log(f"      [fresh] session_token 刷新 access_token 失败: {e}")
    elif (not access_token) and cookie_header:
        _log("      [fresh] 通过 cookie / session 获取 access_token (/api/auth/session) ...")
        auth_data = _fetch_auth_session_with_cookie(
            chatgpt_http,
            cookie_header=cookie_header,
            user_agent=user_agent,
            accept_language=accept_language,
        )
        access_token = (auth_data.get("accessToken") or "").strip()

    if not access_token:
        if auto_register_enabled and not auto_register_used:
            _log("      [fresh] 未拿到可用 access_token，尝试通过本地注册流程新开号 ...")
            provisioned_auth = _provision_openai_auth_via_local_bundle(cfg, fresh_cfg)
            refreshed_cfg = _build_cfg_with_fresh_auth(cfg, provisioned_auth)
            return generate_fresh_checkout(session, refreshed_cfg, locale_profile=locale_profile)
        raise FreshCheckoutAuthError(
            "未提供 fresh_checkout.auth.access_token，且也无法通过 session_token/cookie 刷新"
        )

    user_email = (auth_data.get("user") or {}).get("email", "") or _extract_email_from_access_token(access_token) or "?"
    plan_type = (auth_data.get("account") or {}).get("planType", "") or _extract_plan_type_from_access_token(access_token) or "?"
    _log(
        "      [fresh] 凭证来源: "
        f"access_token={'yes' if access_token else 'no'} "
        f"session_token={'yes' if session_token else 'no'} "
        f"cookie={'yes' if cookie_header else 'no'}"
    )
    _log(f"      [fresh] 当前账号: {user_email}  |  planType={plan_type}")
    # 保存到上下文供后续记录
    fresh_cfg["_chatgpt_email"] = user_email

    attempt_specs = []
    plan_cfg = fresh_cfg.get("plan") or {}
    if fresh_cfg.get("warmup_chatgpt_context", True):
        warm_result = _warm_chatgpt_checkout_context(
            chatgpt_http,
            access_token=access_token,
            session_token=session_token,
            cookie_header=cookie_header,
            user_agent=user_agent,
            accept_language=accept_language,
            locale_profile=locale_profile,
            oai_device_id=oai_device_id,
            billing_country=plan_cfg.get("billing_country") or bootstrap.get("billing_details", {}).get("country") or "US",
            include_home_bounce=bool(fresh_cfg.get("warmup_home_bounce", True)),
        )
        cookie_header = warm_result.get("cookie_header") or cookie_header
        session_token = warm_result.get("session_token") or session_token
        access_token = warm_result.get("access_token") or access_token
        oai_device_id = warm_result.get("device_id") or oai_device_id
        _log(
            "      [fresh] 预热后凭证: "
            f"access_token={'yes' if access_token else 'no'} "
            f"session_token={'yes' if session_token else 'no'} "
            f"cookie_count={len([p for p in cookie_header.split(';') if '=' in p])}"
        )

    if request_style in {"abcard", "legacy", "auto"}:
        legacy_payload = _build_abcard_checkout_payload(fresh_cfg)
        _log(
            "      [fresh] legacy 参数: "
            f"plan_type={legacy_payload.get('plan_type')} "
            f"workspace={legacy_payload.get('workspace_name')} "
            f"seats={legacy_payload.get('seat_quantity')} "
            f"country={legacy_payload.get('billing_country_code')} "
            f"currency={legacy_payload.get('billing_currency_code')} "
            f"promo={legacy_payload.get('promo_campaign_id', '')}"
        )
        legacy_headers = {
            "user-agent": user_agent,
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {access_token}",
            "origin": "https://chatgpt.com",
            "referer": "https://chatgpt.com/",
            "accept-language": accept_language,
        }
        if cookie_header:
            legacy_headers["cookie"] = cookie_header
        if oai_device_id:
            legacy_headers["oai-device-id"] = oai_device_id

        legacy_endpoints = fresh_cfg.get("legacy_endpoints") or [
            "https://chatgpt.com/backend-api/payments/checkout",
            "https://chatgpt.com/backend-api/subscriptions/checkout",
        ]
        for url in legacy_endpoints:
            attempt_specs.append(
                {
                    "label": "abcard",
                    "url": url,
                    "headers": legacy_headers,
                    "payload": legacy_payload,
                    "json_mode": True,
                }
            )

    if request_style in {"modern", "flow"} or (request_style == "auto" and bootstrap):
        sentinel_token = (
            auth_cfg.get("openai_sentinel_token")
            or _refresh_openai_sentinel_token(chatgpt_http, cookie_header, bootstrap)
            or bootstrap.get("openai_sentinel_token")
        )
        modern_payload = _build_fresh_checkout_body(fresh_cfg, bootstrap)
        team_plan_data = modern_payload.get("team_plan_data", {})
        billing_details = modern_payload.get("billing_details", {})
        _log(
            "      [fresh] Modern 参数: "
            f"plan_name={modern_payload.get('plan_name')} "
            f"workspace={team_plan_data.get('workspace_name')} "
            f"interval={team_plan_data.get('price_interval')} "
            f"seats={team_plan_data.get('seat_quantity')} "
            f"country={billing_details.get('country')} "
            f"currency={billing_details.get('currency')} "
            f"promo={((modern_payload.get('promo_campaign') or {}).get('promo_campaign_id') or '')} "
            f"coupon_from_query_param={((modern_payload.get('promo_campaign') or {}).get('is_coupon_from_query_param'))}"
        )

        modern_headers = {
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
            "accept": "*/*",
            "origin": "https://chatgpt.com",
            "referer": "https://chatgpt.com/",
            "user-agent": user_agent,
            "accept-language": accept_language,
            "oai-language": auth_cfg.get("oai_language") or bootstrap.get("oai_language") or locale_profile["browser_locale"],
            "oai-session-id": str(uuid.uuid4()),
            "oai-device-id": oai_device_id,
            "x-openai-target-path": "/backend-api/payments/checkout",
            "x-openai-target-route": "/backend-api/payments/checkout",
        }
        if cookie_header:
            modern_headers["cookie"] = cookie_header
        if bootstrap.get("oai_client_version"):
            modern_headers["oai-client-version"] = bootstrap["oai_client_version"]
        if bootstrap.get("oai_client_build_number"):
            modern_headers["oai-client-build-number"] = str(bootstrap["oai_client_build_number"])
        if bootstrap.get("user_agent") or auth_cfg.get("sec_ch_ua"):
            modern_headers["sec-ch-ua"] = auth_cfg.get("sec_ch_ua") or '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"'
            modern_headers["sec-ch-ua-mobile"] = auth_cfg.get("sec_ch_ua_mobile") or "?0"
            modern_headers["sec-ch-ua-platform"] = auth_cfg.get("sec_ch_ua_platform") or '"Windows"'
        if sentinel_token:
            modern_headers["openai-sentinel-token"] = sentinel_token

        attempt_specs.append(
            {
                "label": "modern",
                "url": "https://chatgpt.com/backend-api/payments/checkout",
                "headers": modern_headers,
                "payload": modern_payload,
                "json_mode": True,
            }
        )

    if not attempt_specs:
        raise FreshCheckoutAuthError("fresh checkout 没有可用的请求模式；请检查 request_style 配置")

    errors = []
    saw_401 = False
    saw_token_invalidated = False
    saw_account_deactivated = False
    last_401_code = ""
    last_401_message = ""
    last_response_data = None

    for spec in attempt_specs:
        label = spec["label"]
        checkout_url = spec["url"]
        payload = spec["payload"]
        headers = spec["headers"]

        _log(f"      [fresh] 尝试 {label} checkout → {checkout_url}")
        _log_request("POST", checkout_url, data=payload, tag=f"[fresh:{label}] checkout")
        resp = chatgpt_http.post(
            checkout_url,
            headers=headers,
            json=payload if spec.get("json_mode", True) else None,
            data=None if spec.get("json_mode", True) else payload,
            timeout=30,
        )
        _log_response(resp, tag=f"[fresh:{label}] checkout")

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception as e:
                errors.append(f"{label} JSON 解析失败: {e}")
                continue

            last_response_data = data
            session_id, processor_entity, fresh_url = _extract_checkout_identifiers(data)
            if session_id:
                if not processor_entity:
                    billing_country = str(
                        billing_details.get("country")
                        if label == "modern"
                        else plan_cfg.get("billing_country", "US")
                    ).upper()
                    processor_entity = "openai_llc" if billing_country == "US" else "openai_ie"
                provider_url = fresh_url
                canonical_chatgpt_url = (
                    f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
                    if processor_entity else ""
                )
                fresh_url = _select_fresh_checkout_url(
                    provider_url=provider_url,
                    canonical_url=canonical_chatgpt_url,
                    fresh_cfg=fresh_cfg,
                    checkout_payload=payload,
                )

                _log(f"      [fresh] session_id: {session_id}")
                if provider_url and provider_url != fresh_url:
                    _log(f"      [fresh] provider_url: {provider_url}")
                if canonical_chatgpt_url and canonical_chatgpt_url != fresh_url:
                    _log(f"      [fresh] canonical_url: {canonical_chatgpt_url}")
                if fresh_url:
                    _log(f"      [fresh] fresh_url: {fresh_url}")

                coupon_check = {}
                if label == "modern":
                    promo_campaign = modern_payload.get("promo_campaign") or {}
                    promo_coupon = (promo_campaign.get("promo_campaign_id") or "").strip()
                    should_check_coupon = fresh_cfg.get("check_coupon_after_checkout")
                    if should_check_coupon is None:
                        should_check_coupon = bool(promo_coupon)
                    if promo_coupon and should_check_coupon:
                        coupon_check = _check_coupon_eligibility(
                            chatgpt_http,
                            access_token=access_token,
                            cookie_header=cookie_header,
                            user_agent=user_agent,
                            accept_language=accept_language,
                            oai_device_id=oai_device_id,
                            coupon=promo_coupon,
                            is_coupon_from_query_param=bool(
                                promo_campaign.get("is_coupon_from_query_param")
                            ),
                            referer_url=canonical_chatgpt_url or provider_url or fresh_url,
                        )

                if fresh_cfg.get("warmup_route_data", True) and canonical_chatgpt_url and cookie_header:
                    route_data_url = (
                        f"https://chatgpt.com/checkout/{processor_entity}/{session_id}.data"
                        "?_routes=routes%2Fcheckout.%24entity.%24checkoutId"
                    )
                    route_headers = {
                        "user-agent": user_agent,
                        "accept-language": accept_language,
                        "referer": "https://chatgpt.com/",
                        "cookie": cookie_header,
                    }
                    try:
                        route_resp = chatgpt_http.get(route_data_url, headers=route_headers, timeout=20)
                        _log(f"      [fresh] route data warmup → {route_resp.status_code}")
                    except Exception as e:
                        _log(f"      [fresh] route data warmup 异常: {e}")

                return {
                    "url": fresh_url,
                    "checkout_session_id": session_id,
                    "processor_entity": processor_entity,
                    "provider_url": provider_url,
                    "canonical_url": canonical_chatgpt_url,
                    "publishable_key": data.get("publishable_key", ""),
                    "client_secret": data.get("client_secret", ""),
                    "coupon_check": coupon_check,
                    "raw": data,
                }

            errors.append(f"{label} 返回 200 但未提取到 checkout_session_id: {json.dumps(data, ensure_ascii=False)[:400]}")
            continue

        if resp.status_code == 401:
            saw_401 = True
            err_code, err_message = _extract_api_error(resp)
            if not err_code and "token_invalidated" in resp.text:
                saw_token_invalidated = True
            if err_code == "token_invalidated":
                saw_token_invalidated = True
            if err_code == "account_deactivated":
                saw_account_deactivated = True
            if err_code:
                last_401_code = err_code
            if err_message:
                last_401_message = err_message
            suffix = f"[{err_code}] " if err_code else ""
            detail = err_message or resp.text[:240]
            errors.append(f"{label} 401{suffix}: {detail}")
            continue

        errors.append(f"{label} [{resp.status_code}]: {resp.text[:300]}")

    if saw_account_deactivated:
        if auto_register_enabled and not auto_register_used and auto_register_cfg.get("retry_on_auth_error", True):
            _log("      [fresh] 当前账号已停用，尝试通过本地注册流程新开号后重试 ...")
            provisioned_auth = _provision_openai_auth_via_local_bundle(cfg, fresh_cfg)
            refreshed_cfg = _build_cfg_with_fresh_auth(
                cfg,
                provisioned_auth,
                forced_mode="access_token",
                mark_auto_register_used=True,
            )
            return generate_fresh_checkout(session, refreshed_cfg, locale_profile=locale_profile)
        msg = last_401_message or "当前 OpenAI 账号已被停用，无法生成优惠 checkout"
        raise FreshCheckoutAuthError(
            f"checkout 401[account_deactivated]：{msg}"
        )
    if saw_token_invalidated:
        if auto_register_enabled and not auto_register_used and auto_register_cfg.get("retry_on_auth_error", True):
            _log("      [fresh] 当前登录态已失效，尝试通过本地注册流程新开号后重试 ...")
            provisioned_auth = _provision_openai_auth_via_local_bundle(cfg, fresh_cfg)
            refreshed_cfg = _build_cfg_with_fresh_auth(
                cfg,
                provisioned_auth,
                forced_mode="access_token",
                mark_auto_register_used=True,
            )
            return generate_fresh_checkout(session, refreshed_cfg, locale_profile=locale_profile)
        raise FreshCheckoutAuthError(
            "checkout 401[token_invalidated]：当前 access_token / session_token 登录态已被撤销或失效"
        )
    if saw_401:
        retryable_codes = {"token_expired", "token_invalidated", "account_deactivated", "session_expired", "invalid_session"}
        if auto_register_enabled and not auto_register_used and auto_register_cfg.get("retry_on_auth_error", True):
            if (not last_401_code) or (last_401_code in retryable_codes):
                _log("      [fresh] 当前凭证不可用，尝试通过本地注册流程新开号后重试 ...")
                provisioned_auth = _provision_openai_auth_via_local_bundle(cfg, fresh_cfg)
                refreshed_cfg = _build_cfg_with_fresh_auth(
                    cfg,
                    provisioned_auth,
                    forced_mode="access_token",
                    mark_auto_register_used=True,
                )
                return generate_fresh_checkout(session, refreshed_cfg, locale_profile=locale_profile)
        if last_401_code or last_401_message:
            suffix = f"[{last_401_code}]" if last_401_code else ""
            sep = "：" if suffix or last_401_message else ""
            raise FreshCheckoutAuthError(
                f"checkout 401{suffix}{sep}{last_401_message or '当前 access_token 无效或已过期，无法生成 fresh checkout'}"
            )
        raise FreshCheckoutAuthError(
            "checkout 401：当前 access_token 无效或已过期，无法生成 fresh checkout"
        )

    if last_response_data is not None:
        raise FreshCheckoutAuthError(
            f"fresh checkout 返回无法解析的 200 响应: {json.dumps(last_response_data, ensure_ascii=False)[:400]}"
        )

    raise FreshCheckoutAuthError(
        "生成 fresh checkout 失败: " + " | ".join(errors[:4])
    )

def fetch_publishable_key(session: requests.Session, session_id: str, stripe_checkout_url: str) -> str:
    checkout_url = stripe_checkout_url

    _log("[2/6] 获取 publishable_key ...")

    for acct_id_part, known_pk in KNOWN_PUBLISHABLE_KEYS.items():
        try:
            url = f"{STRIPE_API}/v1/payment_pages/{session_id}/init"
            post_data = {"key": known_pk, "_stripe_version": STRIPE_VERSION_BASE,
                      "browser_locale": "en-US"}
            _log_request("POST", url, data=post_data, tag="[2/6] pk探测")
            test_resp = session.post(url, data=post_data, headers=_stripe_headers(), timeout=15)
            _log_response(test_resp, tag="[2/6] pk探测")
            _raise_if_checkout_inactive_response(test_resp, "publishable_key 探测")
            if test_resp.status_code == 200:
                _log(f"      publishable_key: {known_pk[:30]}... (已知)")
                return known_pk
        except Exception as e:
            _log(f"      pk探测异常: {e}")

    pk = _fetch_pk_playwright(checkout_url)
    if pk:
        _log(f"      publishable_key: {pk[:30]}... (playwright)")
        return pk

    raise RuntimeError("无法提取 publishable_key")


def _fetch_pk_playwright(checkout_url: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    pk = None

    def on_request(request):
        nonlocal pk
        if pk:
            return
        if "api.stripe.com" in request.url and "init" in request.url:
            post = request.post_data or ""
            m = re.search(r"key=(pk_(?:live|test)_[A-Za-z0-9]+)", post)
            if m:
                pk = m.group(1)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("request", on_request)
            try:
                page.goto(checkout_url, wait_until="domcontentloaded", timeout=20000)
                for _ in range(10):
                    if pk:
                        break
                    page.wait_for_timeout(1000)
            except Exception:
                pass
            browser.close()
    except Exception:
        return None

    return pk


def init_checkout(session: requests.Session, session_id: str, pk: str, locale_profile: dict = None) -> tuple[dict, str, dict]:
    """返回 (init_resp, stripe_ver, ctx) — ctx 包含后续步骤需要的上下文"""
    locale_profile = locale_profile or LOCALE_PROFILES["US"]
    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/init"
    stripe_js_id = str(uuid.uuid4())
    elements_session_id = _gen_elements_session_id()
    elements_options = _elements_options_client_payload()

    for version in [STRIPE_VERSION_BASE, STRIPE_VERSION_FULL]:
        data = {
            "browser_locale": locale_profile["browser_locale"],
            "browser_timezone": locale_profile["browser_timezone"],
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": locale_profile["browser_locale"],
            "elements_session_client[is_aggregation_expected]": "false",
            "key": pk,
            "_stripe_version": version,
        }
        data.update(elements_options)
        if version == STRIPE_VERSION_FULL:
            data["elements_session_client[client_betas][0]"] = "custom_checkout_server_updates_1"
            data["elements_session_client[client_betas][1]"] = "custom_checkout_manual_approval_1"

        _log(f"      初始化结账会话 (init) ... version={version[:30]}")
        _log_request("POST", url, data=data, tag="[2b/6] init")
        resp = session.post(url, data=data, headers=_stripe_headers())
        _log_response(resp, tag="[2b/6] init")
        _raise_if_checkout_inactive_response(resp, "init")
        if resp.status_code == 200:
            init_data = resp.json()
            ctx = {
                "stripe_js_id": stripe_js_id,
                "elements_session_id": elements_session_id,
                "elements_options_client": elements_options,
                "browser_locale": locale_profile["browser_locale"],
                "locale": init_data.get("locale") or _locale_short(locale_profile),
                "currency": (init_data.get("currency") or "usd").lower(),
                "checkout_amount": (
                    (init_data.get("total_summary") or {}).get("due")
                    if (init_data.get("total_summary") or {}).get("due") is not None
                    else (init_data.get("invoice") or {}).get("amount_due")
                ),
                "payment_method_types": _extract_payment_method_types(init_data),
                "config_id": init_data.get("config_id", ""),
                "init_checksum": init_data.get("init_checksum", ""),
                "return_url": init_data.get("return_url") or "",
                "stripe_hosted_url": init_data.get("stripe_hosted_url") or "",
            }
            return init_data, version, ctx
        if resp.status_code == 400 and "beta" in resp.text.lower():
            _log(f"      版本 {version[:20]}... 不支持 beta, 尝试下一个 ...")
            continue
        raise RuntimeError(f"init 失败 [{resp.status_code}]: {resp.text[:500]}")

    raise RuntimeError("init 失败: 所有 Stripe API 版本均不可用")


def extract_hcaptcha_config(init_resp: dict) -> dict:
    raw = json.dumps(init_resp)
    result = {
        "site_key": HCAPTCHA_SITE_KEY_FALLBACK,
        "rqdata": "",
        "is_invisible": True,
        "website_url": _build_stripe_hcaptcha_url(invisible=True),
    }

    if init_resp.get("site_key"):
        result["site_key"] = init_resp["site_key"]
    m = re.search(r'"hcaptcha_site_key"\s*:\s*"([^"]+)"', raw)
    if m and not init_resp.get("site_key"):
        result["site_key"] = m.group(1)

    m = re.search(r'"hcaptcha_rqdata"\s*:\s*"([^"]+)"', raw)
    if m:
        result["rqdata"] = m.group(1)

    return result


def extract_passive_captcha_config(init_resp: dict, elements_resp: dict | None = None) -> dict:
    """优先使用 elements/sessions 返回的 passive_captcha，其次回退到 init 响应。"""
    passive = (elements_resp or {}).get("passive_captcha") or {}
    site_key = passive.get("site_key") or init_resp.get("site_key") or HCAPTCHA_SITE_KEY_FALLBACK
    rqdata = passive.get("rqdata")
    if rqdata is None:
        rqdata = init_resp.get("rqdata", "")
    return {
        "site_key": site_key,
        "rqdata": rqdata or "",
        "is_invisible": True,
        "website_url": _build_stripe_hcaptcha_url(invisible=True),
    }


def fetch_elements_session(
    session: requests.Session,
    pk: str,
    session_id: str,
    ctx: dict,
    stripe_ver: str = STRIPE_VERSION_FULL,
    locale_profile: dict = None,
) -> dict:
    """调用 elements/sessions, 返回响应 dict 并更新 ctx 中的 elements_session_id"""
    locale_profile = locale_profile or LOCALE_PROFILES["US"]
    locale_short = ctx.get("locale") or _locale_short(locale_profile)  # HAR: "zh" 而非 "zh-CN"
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    currency = (ctx.get("currency") or "usd").lower()
    deferred_amount = ctx.get("checkout_amount")
    if deferred_amount is None:
        deferred_amount = 0
    payment_method_types = ctx.get("payment_method_types") or ["card"]
    url = f"{STRIPE_API}/v1/elements/sessions"
    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(int(deferred_amount)),
        "deferred_intent[currency]": currency,
        "deferred_intent[setup_future_usage]": "off_session",
        "currency": currency,
        "key": pk,
        "_stripe_version": stripe_ver,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_js_id,
        "locale": locale_short,
        "type": "deferred_intent",
        "checkout_session_id": session_id,
    }
    for idx, payment_method_type in enumerate(payment_method_types):
        params[f"deferred_intent[payment_method_types][{idx}]"] = payment_method_type
    _log("      [elements] GET /v1/elements/sessions ...")
    _log_request("GET", url, params=params, tag="[2c] elements/sessions")
    resp = session.get(url, params=params, headers=_stripe_headers())
    _log_response(resp, tag="[2c] elements/sessions")

    if resp.status_code == 200:
        data = resp.json()
        # 提取真实的 elements_session_id (如果有)
        real_es_id = data.get("session_id") or data.get("id")
        if real_es_id:
            ctx["elements_session_id"] = real_es_id
            _log(f"      [elements] 真实 session_id: {real_es_id}")
        # 提取 config_id
        config_id = data.get("config_id")
        if config_id:
            ctx["elements_session_config_id"] = config_id
            _log(f"      [elements] config_id: {config_id}")
        passive_captcha = data.get("passive_captcha")
        if isinstance(passive_captcha, dict):
            ctx["passive_captcha"] = passive_captcha
        element_payment_types = []
        for spec in data.get("payment_method_specs", []):
            if isinstance(spec, dict) and spec.get("type"):
                element_payment_types.append(spec["type"])
        if element_payment_types:
            ctx["payment_method_types"] = element_payment_types
        return data
    else:
        _log(f"      [elements] 请求失败 [{resp.status_code}], 继续使用本地生成的 ID")
        return {}



def _stripe_link_cookie_headers() -> dict:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://js.stripe.com",
        "referer": "https://js.stripe.com/",
        "user-agent": USER_AGENT,
    }


def _stripe_get_link_cookie_secret(session: requests.Session) -> str:
    url = "https://merchant-ui-api.stripe.com/link/get-cookie?referrer_host=chatgpt.com"
    try:
        resp = session.get(url, headers=_stripe_link_cookie_headers(), timeout=10)
        _log_response(resp, tag="[2d] link/get-cookie")
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                data = {}
            token = (data.get("auth_session_client_secret") or "").strip() if isinstance(data, dict) else ""
            if token:
                return token
    except Exception as e:
        _log(f"      [link] get-cookie 异常: {e}")
    try:
        return session.cookies.get("__Host-LinkSession", "")
    except Exception:
        return ""


def _stripe_set_link_cookie_secret(session: requests.Session, auth_session_client_secret: str):
    if not auth_session_client_secret:
        return
    url = "https://merchant-ui-api.stripe.com/link/set-cookie"
    payload = {"auth_session_client_secret": auth_session_client_secret}
    try:
        _log_request("POST", url, data=payload, tag="[2d] link/set-cookie")
        resp = session.post(url, json=payload, headers=_stripe_link_cookie_headers(), timeout=10)
        _log_response(resp, tag="[2d] link/set-cookie")
    except Exception as e:
        _log(f"      [link] set-cookie 异常: {e}")


def lookup_consumer(
    session: requests.Session,
    pk: str,
    email: str,
    checkout_session_id: str,
    stripe_ver: str = STRIPE_VERSION_FULL,
    ctx: dict | None = None,
    init_resp: dict | None = None,
):
    """查询 Stripe Link 消费者会话，优先按 flows 的 cookie-based 链路重放。"""
    url = f"{STRIPE_API}/v1/consumers/sessions/lookup"
    ctx = ctx or {}
    init_resp = init_resp or {}
    results = []

    stripe_js_id = ctx.get("stripe_js_id", "") or checkout_session_id
    currency = str((ctx.get("currency") or init_resp.get("currency") or "usd")).lower()
    expected_amount = None
    total_summary = init_resp.get("total_summary") or {}
    invoice = init_resp.get("invoice") or {}
    if total_summary.get("due") is not None:
        expected_amount = int(total_summary["due"])
    elif invoice.get("amount_due") is not None:
        expected_amount = int(invoice["amount_due"])
    elif ctx.get("checkout_amount") is not None:
        expected_amount = int(ctx["checkout_amount"])

    verification_secret = (
        ctx.get("link_auth_session_client_secret")
        or ctx.get("verification_session_client_secret")
        or _stripe_get_link_cookie_secret(session)
    )

    if verification_secret:
        ctx["link_auth_session_client_secret"] = verification_secret
        _log("      [link] 使用 auth_session_client_secret 按 flows 模式 lookup ...")
        surfaces = [
            (
                "web_elements_controller",
                {
                    "request_surface": "web_elements_controller",
                    "cookies[verification_session_client_secrets][0]": verification_secret,
                    "cookies[lifetime]": "persistent",
                    "session_id": stripe_js_id,
                    "key": pk,
                    "_stripe_version": stripe_ver,
                    "do_not_log_consumer_funnel_event": "true",
                },
            ),
            (
                "web_link_authentication_in_payment_element",
                {
                    "request_surface": "web_link_authentication_in_payment_element",
                    "currency": currency,
                    "transaction_context[link_supported_payment_methods][0]": "CARD",
                    "transaction_context[link_supported_payment_methods][1]": "INSTANT_DEBITS",
                    "transaction_context[is_recurring]": "true",
                    "transaction_context[link_mode]": "LINK_CARD_BRAND",
                    "supported_payment_details_types[0]": "CARD",
                    "supported_payment_details_types[1]": "BANK_ACCOUNT",
                    "cookies[verification_session_client_secrets][0]": verification_secret,
                    "cookies[lifetime]": "persistent",
                    "session_id": checkout_session_id,
                    "key": pk,
                    "_stripe_version": stripe_ver,
                },
            ),
        ]
    else:
        surfaces = [
            (
                "web_elements_controller",
                {
                    "request_surface": "web_elements_controller",
                    "email_address": email,
                    "email_source": "default_value",
                    "session_id": stripe_js_id,
                    "key": pk,
                    "_stripe_version": stripe_ver,
                    "do_not_log_consumer_funnel_event": "true",
                },
            ),
            (
                "web_link_authentication_in_payment_element",
                {
                    "request_surface": "web_link_authentication_in_payment_element",
                    "email_address": email,
                    "email_source": "default_value",
                    "currency": currency,
                    "transaction_context[link_supported_payment_methods][0]": "CARD",
                    "transaction_context[link_supported_payment_methods][1]": "INSTANT_DEBITS",
                    "transaction_context[is_recurring]": "true",
                    "transaction_context[link_mode]": "LINK_CARD_BRAND",
                    "supported_payment_details_types[0]": "CARD",
                    "supported_payment_details_types[1]": "BANK_ACCOUNT",
                    "session_id": checkout_session_id,
                    "key": pk,
                    "_stripe_version": stripe_ver,
                },
            ),
        ]

    if expected_amount is not None and int(expected_amount) > 0:
        surfaces[-1][1]["amount"] = str(int(expected_amount))

    for surface, data in surfaces:
        try:
            _log(f"      [link] lookup ({surface[:30]}...) ...")
            _log_request("POST", url, data=data, tag="[2d] consumer/lookup")
            resp = session.post(url, data=data, headers=_stripe_headers(), timeout=10)
            _log_response(resp, tag="[2d] consumer/lookup")
            if resp.status_code == 200:
                payload = resp.json()
                results.append(payload)
                if isinstance(payload, dict):
                    new_secret = (payload.get("auth_session_client_secret") or "").strip()
                    if new_secret:
                        verification_secret = new_secret
                        ctx["link_auth_session_client_secret"] = new_secret
        except Exception as e:
            _log(f"      [link] lookup 异常: {e}")
        if verification_secret:
            _stripe_set_link_cookie_secret(session, verification_secret)
        time.sleep(random.uniform(0.3, 0.8))

    ctx["link_lookup_results"] = results
    return results


def update_payment_page_address(
    session: requests.Session,
    pk: str,
    session_id: str,
    card: dict,
    ctx: dict,
    stripe_ver: str = STRIPE_VERSION_FULL,
):
    """模拟浏览器逐字段提交地址/税区信息, 共 6 次 POST"""
    url = f"{STRIPE_API}/v1/payment_pages/{session_id}"
    addr = card.get("address", {})
    elements_session_id = ctx.get("elements_session_id", _gen_elements_session_id())
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    locale = ctx.get("locale") or _locale_short(LOCALE_PROFILES["US"])

    # 基础字段 — 每次 update 都要带
    base = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": elements_session_id,
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": pk,
        "_stripe_version": stripe_ver,
    }
    base.update(ctx.get("elements_options_client") or _elements_options_client_payload())

    # HAR 中的逐字段提交顺序: country → (重复一次) → line1 → city → state → postal_code
    address_steps = [
        {"tax_region[country]": addr.get("country", "US")},
        {},  # 重复提交 (无新字段, 模拟用户切换焦点)
        {"tax_region[line1]": addr.get("line1", "")},
        {"tax_region[city]": addr.get("city", "")},
        {"tax_region[state]": addr.get("state", "")},
        {"tax_region[postal_code]": addr.get("postal_code", "")},
    ]

    _log("      [address] 逐字段提交税区地址 ...")
    accumulated = {}
    for step_idx, new_fields in enumerate(address_steps):
        accumulated.update(new_fields)
        data = dict(base)
        data.update(accumulated)

        step_name = list(new_fields.keys())[0] if new_fields else "(焦点变更)"
        _log(f"      [address] step {step_idx + 1}/6: {step_name}")
        _log_request("POST", url, data=data, tag=f"[2e] update_address({step_idx + 1}/6)")
        resp = session.post(url, data=data, headers=_stripe_headers())
        _log_response(resp, tag=f"[2e] update_address({step_idx + 1}/6)")

        if resp.status_code != 200:
            _log(f"      [address] step {step_idx + 1} 返回 {resp.status_code}, 继续 ...")

        # 模拟人类输入间隔 (2-5 秒)
        time.sleep(random.uniform(2.0, 4.5))

def send_telemetry(
    session: requests.Session,
    event_type: str,
    session_id: str,
    ctx: dict,
):
    """向 r.stripe.com/b 发送遥测事件, 模拟 stripe.js 行为上报"""
    url = "https://r.stripe.com/b"
    muid = ctx.get("muid", "")
    sid = ctx.get("sid", "")
    guid = ctx.get("guid", "")

    payload = {
        "v2": 1,
        "tag": event_type,
        "src": "js",
        "pid": "checkout_" + session_id[:20],
        "muid": muid,
        "sid": sid,
        "guid": guid,
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }
    try:
        body = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        session.post(url, data=body, headers=headers, timeout=5)
    except Exception:
        pass


def send_telemetry_batch(
    session: requests.Session,
    session_id: str,
    ctx: dict,
    phase: str = "init",
):
    """按阶段批量发送遥测事件"""
    events_map = {
        "init": ["checkout.init", "elements.create", "payment_element.mount"],
        "address": ["address.update", "address.focus", "address.blur"],
        "card_input": ["card.focus", "card.input", "card.blur", "cvc.input"],
        "confirm": ["checkout.confirm.start", "payment_method.create", "checkout.confirm.intent"],
        "3ds": ["three_ds2.start", "three_ds2.fingerprint", "three_ds2.authenticate"],
        "poll": ["checkout.poll", "checkout.complete"],
    }
    events = events_map.get(phase, [])
    for evt in events:
        send_telemetry(session, evt, session_id, ctx)
        time.sleep(random.uniform(0.05, 0.2))


def submit_apata_fingerprint(
    session: requests.Session,
    three_ds_server_trans_id: str,
    three_ds_method_url: str,
    notification_url: str,
    locale_profile: dict,
    ctx: dict,
):


    # 1) POST acs-method.apata.io/v1/houston/method — 提交 threeDSMethodData
    _log("      [apata] POST houston/method ...")
    method_data = base64.b64encode(json.dumps({
        "threeDSServerTransID": three_ds_server_trans_id,
        "threeDSMethodNotificationURL": notification_url,
    }, separators=(",", ":")).encode()).decode()

    try:
        method_url = three_ds_method_url or "https://acs-method.apata.io/v1/houston/method"
        resp = session.post(
            method_url,
            data={"threeDSMethodData": method_data},
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://js.stripe.com",
                "Referer": "https://js.stripe.com/",
            },
            timeout=15,
        )
        _log(f"      [apata] houston/method → {resp.status_code}")
    except Exception as e:
        _log(f"      [apata] houston/method 异常: {e}")

    time.sleep(random.uniform(0.5, 1.0))

    # 2) POST acs-method.apata.io/v1/RecordBrowserInfo — 设备指纹上报
    _log("      [apata] POST RecordBrowserInfo ...")
    # 生成 possessionDeviceId (localStorage acsRbaDeviceId 模拟)
    possession_device_id = ctx.get("apata_device_id") or str(uuid.uuid4())
    ctx["apata_device_id"] = possession_device_id

    fp_data = _build_browser_fingerprint(locale_profile)
    record_payload = {
        "threeDSServerTransID": three_ds_server_trans_id,
        "computedValue": hashlib.sha256(os.urandom(32)).hexdigest()[:20],
        "possessionDeviceId": possession_device_id,
    }
    record_payload.update(fp_data)

    try:
        record_url = "https://acs-method.apata.io/v1/RecordBrowserInfo"
        resp = session.post(
            record_url,
            json=record_payload,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Origin": "https://acs-method.apata.io",
                "Referer": "https://acs-method.apata.io/",
            },
            timeout=15,
        )
        _log(f"      [apata] RecordBrowserInfo → {resp.status_code}")
    except Exception as e:
        _log(f"      [apata] RecordBrowserInfo 异常: {e}")

    time.sleep(random.uniform(0.5, 1.0))

    # 3) GET rba.apata.io/xxx.js — 模拟 RBA profile 脚本加载
    _log("      [apata] GET rba profile script ...")
    rba_session_id = ctx.get("rba_session_id") or str(uuid.uuid4())
    ctx["rba_session_id"] = rba_session_id
    try:
        # HAR 中的 URL 格式: rba.apata.io/<random>.js?<random_param>=<org_id>&<random_param>=<session_id>
        rba_script_name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16)) + ".js"
        rba_param1 = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
        rba_param2 = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
        rba_url = f"https://rba.apata.io/{rba_script_name}?{rba_param1}={APATA_RBA_ORG_ID}&{rba_param2}={rba_session_id}"
        resp = session.get(rba_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        _log(f"      [apata] rba profile → {resp.status_code}")
    except Exception as e:
        _log(f"      [apata] rba profile 异常: {e}")

    # 4) 模拟 aa.online-metrix.net CONNECT (WebRTC beacon 不可模拟, 仅日志标记)
    _log("      [apata] online-metrix beacon (WebRTC, 已跳过 — 无法在 requests 中模拟)")

    # 总等待: 让 Apata 有时间处理指纹结果 (HAR 中这个窗口约 8-12 秒)
    wait = random.uniform(5.0, 8.0)
    _log(f"      [apata] 等待指纹处理完成 ({wait:.1f}s) ...")
    time.sleep(wait)

def solve_hcaptcha(
    captcha_cfg: dict,
    hcaptcha_config: dict,
    max_retries: int = 3,
    session: requests.Session | None = None,
) -> tuple[str, str]:
    """返回 (token, ekey) 元组"""
    api_url = (captcha_cfg.get("api_url") or "").rstrip("/") or "https://YOUR_CAPTCHA_PROVIDER"
    client_key = captcha_cfg["api_key"]
    site_key = hcaptcha_config["site_key"]
    rqdata = hcaptcha_config.get("rqdata", "")
    is_invisible = hcaptcha_config.get("is_invisible", True)
    website_url = hcaptcha_config.get("website_url") or _build_stripe_hcaptcha_url(invisible=is_invisible)

    for retry in range(max_retries):
        if retry > 0:
            _log(f"      --- 重试第 {retry + 1}/{max_retries} 次 ---")

        _log(f"      解 hCaptcha (siteKey: {site_key[:20]}...)")

        # 创建 1 个任务
        task_body = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": website_url,
            "websiteKey": site_key,
            "isEnterprise": True,
            "userAgent": USER_AGENT,
        }
        if is_invisible:
            task_body["isInvisible"] = True
        if rqdata:
            task_body["rqdata"] = rqdata

        create_payload = {"clientKey": client_key, "task": task_body}
        try:
            create_url = f"{api_url}/createTask"
            _log_request("POST", create_url, data=create_payload, tag="[captcha] createTask")
            create_resp = requests.post(create_url, json=create_payload, timeout=15)
            _log_response(create_resp, tag="[captcha] createTask")
            data = create_resp.json()
            if data.get("errorId", 1) != 0:
                _log(f"      任务创建失败: {data.get('errorDescription', '?')}")
                time.sleep(3)
                continue
            task_id = data["taskId"]
        except Exception as e:
            _log(f"      任务创建异常: {e}")
            time.sleep(3)
            continue

        _log(f"      任务: {task_id}  等待解题 ...")

     
        for attempt in range(60):
            time.sleep(3)
            try:
                result_url = f"{api_url}/getTaskResult"
                result_payload = {"clientKey": client_key, "taskId": task_id}
                result_resp = requests.post(result_url, json=result_payload, timeout=10)
                result_data = result_resp.json()
            except Exception:
                continue

            if result_data.get("errorId", 0) != 0:
                error_code = result_data.get("errorCode", "")
                if error_code == "ERROR_TASK_TIMEOUT":
                    _log("      任务超时, 重新发起 ...")
                    break
                continue

            if result_data.get("status") == "ready":
                solution = result_data["solution"]
                _log_raw(f"      solution keys: {list(solution.keys())}")
                _log_raw(f"      solution full: {json.dumps(solution, ensure_ascii=False)[:500]}")
                token = solution["gRecaptchaResponse"]
                # eKey 可能在不同字段名下
                ekey = solution.get("eKey", "") or solution.get("respKey", "") or solution.get("ekey", "")
                solved_user_agent = solution.get("userAgent", "")
                if solved_user_agent and solved_user_agent != USER_AGENT:
                    _log(f"      打码平台返回不同 UA，后续请求改用该 UA")
                    if session is not None:
                        session.headers["User-Agent"] = solved_user_agent
                _log(f"      已解决 (token: {len(token)} chars, ekey: {len(ekey)} chars)")
                _log_raw(f"      captcha_token(前100): {token[:100]}...")
                if ekey:
                    _log_raw(f"      captcha_ekey(前100): {ekey[:100]}...")
                return token, ekey

            if attempt % 5 == 4:
                _log(f"      等待中 ... ({attempt + 1}/60)")

    raise RuntimeError(f"打码平台解题失败 (已重试 {max_retries} 轮)")


def _build_stripe_hcaptcha_parent_html(
    frame_id: str,
    wrapper_url: str,
    site_key: str,
    rqdata: str,
    merchant_id: str,
    locale: str,
    invisible: bool = False,
) -> str:
    visible_payload = {
        "sitekey": site_key,
        "rqdata": rqdata,
        "merchantId": merchant_id,
        "locale": locale,
        "headerText": "Verification required",
        "instructionText": "Complete captcha to continue",
        "showCloseButton": False,
    }
    invisible_init_payload = {
        "tag": "INITIALIZE_HCAPTCHA_INVISIBLE",
        "message": {
            "sitekey": site_key,
        },
    }
    invisible_execute_payload = {
        "tag": "EXECUTE_HCAPTCHA_INVISIBLE",
        "message": {
            "sitekey": site_key,
            "rqdata": rqdata,
            "data": {
                "merchant_id": merchant_id or "",
                "locale": locale or "",
                "flow": "passive_captcha",
                "captcha_vendor": "hcaptcha",
            },
        },
    }
    invisible_signal_payloads = [
        {
            "tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE",
            "message": {
                "type": "mouse",
                "eventName": "mousemove",
                "coordinates": {"x": 168, "y": 132},
            },
        },
        {
            "tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE",
            "message": {
                "type": "pointer",
                "eventName": "pointermove",
                "coordinates": {"x": 214, "y": 176},
            },
        },
        {
            "tag": "SEND_FRAUD_SIGNALS_HCAPTCHA_INVISIBLE",
            "message": {
                "type": "keyboard",
                "eventName": "keydown",
            },
        },
    ]
    payload_js = json.dumps(visible_payload, ensure_ascii=False)
    invisible_init_payload_js = json.dumps(invisible_init_payload, ensure_ascii=False)
    invisible_execute_payload_js = json.dumps(invisible_execute_payload, ensure_ascii=False)
    invisible_signal_payloads_js = json.dumps(invisible_signal_payloads, ensure_ascii=False)
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Stripe hCaptcha Bridge</title>
    <style>
      body {{
        margin: 0;
        font-family: Arial, sans-serif;
        background: #f7f7f7;
      }}
      .shell {{
        padding: 16px;
      }}
      #status {{
        font-size: 14px;
        margin-bottom: 12px;
        color: #333;
      }}
      iframe {{
        width: 420px;
        height: 720px;
        border: 0;
        background: white;
      }}
      pre {{
        white-space: pre-wrap;
        word-break: break-word;
        font-size: 12px;
      }}
    </style>
  </head>
  <body>
    <div class="shell">
      <div id="status">Waiting for Stripe captcha frame…</div>
      <iframe id="stripeCaptchaFrame" src="{wrapper_url}"></iframe>
      <pre id="result"></pre>
    </div>
    <script>
      window.__stripeChallengeEvents = [];
      window.__stripeChallengeResult = null;
      window.__stripeChallengeCancelled = false;
      window.__stripeChallengeError = null;
      const frameID = {json.dumps(frame_id)};
      const invisibleMode = {json.dumps(bool(invisible))};
      const childPayload = {payload_js};
      const invisibleInitPayload = {invisible_init_payload_js};
      const invisibleExecutePayload = {invisible_execute_payload_js};
      const invisibleSignalPayloads = {invisible_signal_payloads_js};
      let invisibleInitialized = false;
      let invisibleExecuted = false;

      function setStatus(text) {{
        document.getElementById("status").textContent = text;
      }}

      function postToBridge(path, payload) {{
        fetch(path, {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json",
          }},
          body: JSON.stringify(payload || {{}}),
          keepalive: true,
        }}).catch(() => {{}});
      }}

      function postToChild(source, origin, payload) {{
        source.postMessage({{
          type: "stripe-third-party-parent-to-child",
          frameID,
          payload,
        }}, origin);
      }}

      function postInvisibleInitialize(source, origin) {{
        if (invisibleInitialized) {{
          return;
        }}
        invisibleInitialized = true;
        setStatus("Initializing invisible hCaptcha…");
        postToBridge("/event", {{
          type: "invisible_initialize",
          payload: invisibleInitPayload,
        }});
        postToChild(source, origin, invisibleInitPayload);
      }}

      function postInvisibleExecute(source, origin) {{
        if (invisibleExecuted) {{
          return;
        }}
        invisibleExecuted = true;
        setStatus("Executing invisible hCaptcha…");
        invisibleSignalPayloads.forEach((signalPayload, idx) => {{
          setTimeout(() => postToChild(source, origin, signalPayload), 50 * idx);
        }});
        setTimeout(() => {{
          postToBridge("/event", {{
            type: "invisible_execute",
            payload: invisibleExecutePayload,
          }});
          postToChild(source, origin, invisibleExecutePayload);
        }}, 180);
      }}

      window.addEventListener("message", (event) => {{
        window.__stripeChallengeEvents.push({{
          origin: event.origin,
          data: event.data,
        }});

        const data = event.data || {{}};
        if (data.type === "stripe-third-party-frame-ready" && data.frameID === frameID) {{
          setStatus("Stripe captcha frame ready. Loading challenge…");
          postToBridge("/event", {{
            type: "frame_ready",
            origin: event.origin,
            frameID,
          }});
          if (invisibleMode) {{
            postInvisibleInitialize(event.source, event.origin);
          }} else {{
            postToChild(event.source, event.origin, childPayload);
          }}
          return;
        }}

        if (data.type !== "stripe-third-party-child-to-parent" || data.frameID !== frameID) {{
          return;
        }}

        const payload = data.payload || {{}};
        document.getElementById("result").textContent = JSON.stringify(payload, null, 2);

        postToBridge("/event", {{
          type: "child_payload",
          origin: event.origin,
          requestID: data.requestID || null,
          payload,
        }});

        if (invisibleMode) {{
          const tag = payload.tag || "";
          const value = payload.value || {{}};

          if (tag === "LOAD_HCAPTCHA_INVISIBLE") {{
            setStatus("Invisible hCaptcha loaded. Executing…");
            postInvisibleExecute(event.source, event.origin);
            return;
          }}

          if (tag === "SEND_COMPLETE_HCAPTCHA_INVISIBLE") {{
            setStatus("Invisible hCaptcha fraud signals delivered.");
            return;
          }}

          if (tag === "RESPONSE_HCAPTCHA_INVISIBLE") {{
            const solved = {{
              response: value.response || "",
              ekey: value.key || "",
              duration: value.duration || 0,
              raw: payload,
            }};
            setStatus("Invisible hCaptcha solved. You can close this window.");
            window.__stripeChallengeResult = solved;
            postToBridge("/result", solved);
            return;
          }}

          if (tag === "ERROR_HCAPTCHA_INVISIBLE") {{
            const err = {{
              error: value.error || "unknown_error",
              raw: payload,
            }};
            setStatus("Invisible hCaptcha failed: " + err.error);
            window.__stripeChallengeError = err;
            postToBridge("/error", err);
            return;
          }}

          return;
        }}

        if (payload.type === "event") {{
          setStatus("Stripe captcha event: " + payload.name);
          postToBridge("/event", payload);
        }} else if (payload.type === "response") {{
          setStatus("Challenge solved. You can close this window.");
          window.__stripeChallengeResult = payload;
          postToBridge("/result", payload);
        }} else if (payload.type === "cancel") {{
          setStatus("Challenge cancelled.");
          window.__stripeChallengeCancelled = true;
          postToBridge("/cancel", payload);
        }}
      }});
    </script>
  </body>
</html>
"""


def solve_stripe_hcaptcha_in_browser(
    hcaptcha_config: dict,
    merchant_id: str,
    locale: str,
    browser_cfg: dict | None = None,
    verify_url: str = "",
    verify_form_base: dict | None = None,
) -> tuple[str, str, dict | None]:
    """通过 Stripe 自带 HCaptcha 包装页获取 token。

    - `is_invisible=true` 时优先用于 passive captcha，通常希望浏览器自动执行后直接回传 token
    - `is_invisible=false` 时用于 challenge captcha，必要时可人工处理
    """
    browser_cfg = browser_cfg or {}
    timeout_ms = int(browser_cfg.get("timeout_ms", 5 * 60 * 1000))
    headless = bool(browser_cfg.get("headless", False))
    auto_click_checkbox = bool(browser_cfg.get("auto_click_checkbox", True))
    invisible = bool(hcaptcha_config.get("is_invisible", False))
    external_solver_cfg = dict(browser_cfg.get("external_solver") or {})
    if not invisible and verify_url and not bool(external_solver_cfg.get("enabled")):
        bundled_solver = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "hcaptcha_auto_solver.py",
        )
        python_candidates = [
            str(os.environ.get("CTFML_PYTHON") or "").strip(),
            "~/.venvs/ctfml/bin/python",
            sys.executable,
        ]
        solver_python = next((p for p in python_candidates if p and os.path.exists(p)), sys.executable)
        auto_vlm_cfg = dict(external_solver_cfg.get("vlm") or {})
        auto_vlm_cfg.setdefault("enabled", True)
        auto_vlm_cfg.setdefault("model", "gpt-5.4")
        auto_vlm_cfg.setdefault("base_url", "https://YOUR_VLM_ENDPOINT/api")
        auto_vlm_cfg.setdefault("api_key", "")
        auto_vlm_cfg.setdefault("timeout_s", 45)
        external_solver_cfg = {
            **external_solver_cfg,
            "enabled": True,
            "python": external_solver_cfg.get("python") or solver_python,
            "script": external_solver_cfg.get("script") or bundled_solver,
            "out_dir": external_solver_cfg.get("out_dir") or "/tmp/hcaptcha_auto_solver_live",
            "timeout_s": int(
                external_solver_cfg.get("timeout_s")
                or max(180, int(timeout_ms / 1000))
            ),
            "headed": bool(external_solver_cfg.get("headed", False)),
            "vlm": auto_vlm_cfg,
        }
        browser_cfg["external_solver"] = external_solver_cfg
        _log("      challenge 分支未携带 external_solver，已在 solve_stripe_hcaptcha_in_browser 内补齐内置 solver")
    external_solver_enabled = bool(external_solver_cfg.get("enabled")) and not invisible
    if external_solver_enabled:
        solver_timeout_s = int(external_solver_cfg.get("timeout_s") or max(180, int(timeout_ms / 1000)))
        min_timeout_ms = max(timeout_ms, solver_timeout_s * 1000 + 15_000)
        if min_timeout_ms != timeout_ms:
            _log(
                "      browser_challenge.timeout_ms 对 external_solver 过短，"
                f"自动从 {timeout_ms}ms 提升到 {min_timeout_ms}ms"
            )
            timeout_ms = min_timeout_ms
    auto_launch_browser_requested = bool(browser_cfg.get("auto_launch_browser", True))
    auto_launch_browser = auto_launch_browser_requested and not external_solver_enabled
    viewport = browser_cfg.get("viewport") or {"width": 1280, "height": 960}
    site_key = hcaptcha_config["site_key"]
    rqdata = hcaptcha_config.get("rqdata", "")
    proxy_url = str(browser_cfg.get("proxy_url") or "").strip()
    verify_url = (verify_url or "").strip()
    verify_form_base = dict(verify_form_base or {})
    browser_timezone = str(
        browser_cfg.get("browser_timezone")
        or browser_cfg.get("timezone")
        or DEFAULT_TIMEZONE
    )
    browser_accept_language = str(
        browser_cfg.get("accept_language")
        or _accept_language_for_locale(locale)
    )
    playwright_proxy = None
    _log(
        "      浏览器 hCaptcha 运行参数: "
        f"invisible={invisible} auto_launch={auto_launch_browser_requested} "
        f"external_solver={external_solver_enabled} headless={headless}"
    )
    if proxy_url:
        try:
            parsed_proxy = urllib.parse.urlsplit(proxy_url)
            proxy_host = parsed_proxy.hostname or ""
            proxy_scheme = parsed_proxy.scheme or "http"
            proxy_port = parsed_proxy.port
            if proxy_host:
                server = f"{proxy_scheme}://{proxy_host}"
                if proxy_port:
                    server += f":{proxy_port}"
                playwright_proxy = {
                    "server": server,
                    "bypass": "127.0.0.1,localhost",
                }
                proxy_user = urllib.parse.unquote(parsed_proxy.username or "")
                proxy_pass = urllib.parse.unquote(parsed_proxy.password or "")
                if proxy_user:
                    playwright_proxy["username"] = proxy_user
                if proxy_pass:
                    playwright_proxy["password"] = proxy_pass
        except Exception:
            playwright_proxy = None

    bridge_meta_path = browser_cfg.get("bridge_meta_path") or "/tmp/stripe_hcaptcha_bridge_latest.json"

    def _display_looks_usable(env: dict) -> bool:
        display = str(env.get("DISPLAY") or "").strip()
        if not display:
            return False
        probe_env = dict(env)
        for probe_cmd in (["xdpyinfo"], ["xset", "q"]):
            try:
                proc = subprocess.run(
                    probe_cmd,
                    env=probe_env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
            except FileNotFoundError:
                continue
            except Exception:
                return False
            return proc.returncode == 0
        display_suffix = display.split(":", 1)[-1].split(".", 1)[0]
        if display_suffix.isdigit() and os.path.exists(f"/tmp/.X11-unix/X{display_suffix}"):
            return True
        return False

    with tempfile.TemporaryDirectory(prefix="stripe-hcaptcha-bridge-") as tmpdir:
        frame_id = str(uuid.uuid4())
        bridge_state = {
            "events": [],
            "result": None,
            "cancelled": False,
            "error": None,
        }
        result_event = threading.Event()
        cancel_event = threading.Event()
        error_event = threading.Event()

        def _persist_bridge_meta():
            try:
                bridge_meta = {
                    "bridge_url": f"{origin}/index.html",
                    "site_key": site_key,
                    "rqdata": rqdata,
                    "merchant_id": merchant_id,
                    "locale": locale,
                    "frame_id": frame_id,
                    "wrapper_url": wrapper_url,
                    "invisible": invisible,
                    "created_at": bridge_state.get("created_at") or int(time.time()),
                    "updated_at": int(time.time()),
                    "state": bridge_state,
                }
                with open(bridge_meta_path, "w", encoding="utf-8") as f:
                    json.dump(bridge_meta, f, ensure_ascii=False, indent=2)
                bridge_state["created_at"] = bridge_meta["created_at"]
            except Exception:
                pass

        class _QuietHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=tmpdir, **kwargs)

            def log_message(self, fmt, *args):
                return

            def _write_json(self, status: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                raw_body = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                except Exception:
                    payload = {}

                if self.path == "/event":
                    bridge_state["events"].append(payload)
                    _persist_bridge_meta()
                    self._write_json(200, {"ok": True})
                    return

                if self.path == "/result":
                    bridge_state["result"] = payload
                    _persist_bridge_meta()
                    result_event.set()
                    self._write_json(200, {"ok": True})
                    return

                if self.path == "/cancel":
                    bridge_state["cancelled"] = True
                    bridge_state["cancel_payload"] = payload
                    _persist_bridge_meta()
                    cancel_event.set()
                    self._write_json(200, {"ok": True})
                    return

                if self.path == "/error":
                    bridge_state["error"] = payload
                    _persist_bridge_meta()
                    error_event.set()
                    self._write_json(200, {"ok": True})
                    return

                self._write_json(404, {"error": "not found"})

        class _BridgeTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        httpd = _BridgeTCPServer(("127.0.0.1", 0), _QuietHandler)
        port = httpd.server_address[1]
        origin = f"http://127.0.0.1:{port}"
        wrapper_url = _build_stripe_hcaptcha_url(
            invisible=invisible,
            frame_id=frame_id,
            origin=origin,
        )
        html = _build_stripe_hcaptcha_parent_html(
            frame_id=frame_id,
            wrapper_url=wrapper_url,
            site_key=site_key,
            rqdata=rqdata,
            merchant_id=merchant_id,
            locale=locale,
            invisible=invisible,
        )
        with open(os.path.join(tmpdir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)

        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        try:
            _log(
                "      启用浏览器 "
                + ("passive captcha" if invisible else "challenge")
                + " 方案 ..."
            )
            _log(f"      本地桥接页: {origin}/index.html")
            _log("      如未自动拉起浏览器，请手动复制上面的本地桥接页地址到可用浏览器中打开。")
            try:
                _persist_bridge_meta()
                _log(f"      bridge 元数据已写入: {bridge_meta_path}")
            except Exception as e:
                _log(f"      bridge 元数据写入失败，忽略: {e}")

            browser = None
            page = None
            external_solver_proc = None
            external_solver_reader = None
            external_solver_exit_logged = False
            if external_solver_enabled:
                solver_headed = bool(external_solver_cfg.get("headed", False))
                solver_python = str(external_solver_cfg.get("python") or sys.executable)
                solver_script = str(
                    external_solver_cfg.get("script")
                    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "hcaptcha_auto_solver.py")
                )
                if not os.path.isabs(solver_script):
                    solver_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), solver_script)
                solver_timeout_s = int(
                    external_solver_cfg.get("timeout_s")
                    or max(90, int(timeout_ms / 1000))
                )
                solver_out_dir = str(
                    external_solver_cfg.get("out_dir")
                    or "/tmp/hcaptcha_auto_solver_live"
                )
                os.makedirs(solver_out_dir, exist_ok=True)
                solver_log_path = os.path.join(
                    solver_out_dir,
                    f"solver_stdout_{int(time.time() * 1000)}.log",
                )
                solver_vlm_cfg = dict(external_solver_cfg.get("vlm") or {})
                solver_cmd = [
                    solver_python,
                    "-u",
                    solver_script,
                    f"{origin}/index.html",
                    "--timeout",
                    str(solver_timeout_s),
                    "--out-dir",
                    solver_out_dir,
                ]
                if proxy_url:
                    solver_cmd.extend(["--proxy-url", proxy_url])
                if bool(solver_vlm_cfg.get("enabled", True)):
                    solver_cmd.extend(
                        [
                            "--vlm-base-url",
                            str(solver_vlm_cfg.get("base_url") or "https://YOUR_VLM_ENDPOINT/api"),
                            "--vlm-model",
                            str(solver_vlm_cfg.get("model") or "gpt-5.4"),
                            "--vlm-timeout",
                            str(int(solver_vlm_cfg.get("timeout_s") or 45)),
                        ]
                    )
                else:
                    solver_cmd.append("--no-vlm")
                extra_args = external_solver_cfg.get("extra_args") or []
                if isinstance(extra_args, (list, tuple)):
                    solver_cmd.extend(str(x) for x in extra_args if x not in (None, ""))

                solver_env = os.environ.copy()
                solver_tmpdir = str(external_solver_cfg.get("tmpdir") or "").strip()
                if solver_tmpdir:
                    solver_env["TMPDIR"] = solver_tmpdir
                solver_cmd_prefix = []
                if solver_headed and not _display_looks_usable(solver_env):
                    xvfb_run = shutil.which("xvfb-run")
                    if xvfb_run:
                        solver_cmd_prefix = [
                            xvfb_run,
                            "-a",
                            "--server-args=-screen 0 1280x960x24",
                        ]
                        _log("      external_solver headed 模式无可用 DISPLAY，自动改用 xvfb-run 启动虚拟显示。")
                    else:
                        solver_headed = False
                        _log("      external_solver headed 模式不可用，且系统无 xvfb-run，自动回退为 headless。")
                solver_vlm_api_key = str(solver_vlm_cfg.get("api_key") or "").strip()
                if solver_vlm_api_key:
                    solver_env["CTF_VLM_API_KEY"] = solver_vlm_api_key
                if solver_headed:
                    solver_cmd.append("--headed")
                if verify_url and verify_form_base:
                    solver_cmd.extend(["--verify-url", verify_url])
                    if verify_form_base.get("client_secret"):
                        solver_cmd.extend(["--verify-client-secret", str(verify_form_base["client_secret"])])
                    if verify_form_base.get("key"):
                        solver_cmd.extend(["--verify-key", str(verify_form_base["key"])])
                    if verify_form_base.get("_stripe_version"):
                        solver_cmd.extend(["--verify-stripe-version", str(verify_form_base["_stripe_version"])])
                    if verify_form_base.get("captcha_vendor_name"):
                        solver_cmd.extend(["--verify-captcha-vendor", str(verify_form_base["captcha_vendor_name"])])
                solver_cmd.extend(["--browser-locale", str(locale or "en-US")])
                solver_cmd.extend(["--browser-timezone", browser_timezone])
                solver_cmd.extend(["--accept-language", browser_accept_language])

                final_solver_cmd = [*solver_cmd_prefix, *solver_cmd]
                _log(
                    "      启动 external_solver: "
                    + " ".join(final_solver_cmd)
                )
                _log(f"      external_solver stdout 日志: {solver_log_path}")
                try:
                    external_solver_proc = subprocess.Popen(
                        final_solver_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        cwd=os.path.dirname(solver_script) or None,
                        env=solver_env,
                    )

                    def _forward_external_solver_output():
                        try:
                            if external_solver_proc is None or external_solver_proc.stdout is None:
                                return
                            with open(solver_log_path, "a", encoding="utf-8") as solver_log_f:
                                for line in external_solver_proc.stdout:
                                    solver_log_f.write(line)
                                    solver_log_f.flush()
                                    line = line.rstrip()
                                    if line:
                                        _log(f"      [solver] {line}")
                        except Exception as e:
                            _log(f"      [solver] 输出转发失败，忽略: {e}")

                    external_solver_reader = threading.Thread(
                        target=_forward_external_solver_output,
                        daemon=True,
                    )
                    external_solver_reader.start()
                except Exception as e:
                    raise RuntimeError(f"启动 external_solver 失败: {e}") from e

            if external_solver_enabled and auto_launch_browser_requested:
                _log("      challenge 已启用 external_solver，内置 Playwright 自动拉起将跳过。")
            if auto_launch_browser:
                try:
                    from playwright.sync_api import sync_playwright

                    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
                    effective_headless = headless or not has_display
                    if not has_display and not headless:
                        _log("      当前环境没有可用 DISPLAY，自动回退为 headless Playwright。")
                    if effective_headless or has_display:
                        playwright_ctx = sync_playwright().start()
                        launch_kwargs = {"headless": effective_headless}
                        if playwright_proxy:
                            launch_kwargs["proxy"] = playwright_proxy
                            _log(f"      浏览器桥接代理: {_describe_proxy_cfg(proxy_url)}")
                        browser = playwright_ctx.chromium.launch(**launch_kwargs)
                        browser_context = browser.new_context(
                            viewport=viewport,
                            user_agent=USER_AGENT,
                            locale=str(locale or "en-US"),
                            timezone_id=browser_timezone,
                            extra_http_headers={
                                "Accept-Language": browser_accept_language,
                                "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
                                "Sec-CH-UA-Mobile": "?0",
                                "Sec-CH-UA-Platform": '"Windows"',
                                "Priority": "u=1, i",
                            },
                        )
                        page = browser_context.new_page()
                        page.goto(f"{origin}/index.html", wait_until="domcontentloaded", timeout=60_000)
                        _log(
                            "      已自动拉起本地浏览器桥接页。"
                            f" (headless={'true' if effective_headless else 'false'})"
                        )
                    else:
                        _log("      当前环境没有可用 DISPLAY，跳过 Playwright 拉起；请手动在有图形界面的浏览器中打开桥接页。")
                        playwright_ctx = None
                except Exception as e:
                    playwright_ctx = None
                    _log(f"      自动拉起浏览器失败，改为手动打开桥接页: {e}")
            else:
                playwright_ctx = None

            try:
                if page is not None and auto_click_checkbox and not invisible:
                    try:
                        checkbox_deadline = time.time() + 20
                        checkbox_frame = None
                        while time.time() < checkbox_deadline:
                            checkbox_frame = next(
                                (frame for frame in page.frames if "frame=checkbox" in frame.url),
                                None,
                            )
                            if checkbox_frame:
                                break
                            page.wait_for_timeout(500)
                        if checkbox_frame:
                            for selector in ["#checkbox", '[role="checkbox"]', 'div[aria-checked]']:
                                try:
                                    checkbox_frame.locator(selector).first.click(timeout=2_000)
                                    _log("      已自动点击 hCaptcha checkbox，等待后续 challenge ...")
                                    break
                                except Exception:
                                    continue
                    except Exception as e:
                        _log(f"      自动点击 checkbox 失败，继续等待人工处理: {e}")

                deadline = time.time() + (timeout_ms / 1000)
                logged_event_count = 0
                while time.time() < deadline:
                    events = bridge_state["events"]
                    while logged_event_count < len(events):
                        event_payload = events[logged_event_count]
                        logged_event_count += 1
                        event_type = event_payload.get("type", "event")
                        event_name = event_payload.get("name", "")
                        if event_type == "child_payload" and invisible:
                            tag = (
                                (event_payload.get("payload") or {}).get("tag")
                                or "unknown"
                            )
                            _log(f"      Stripe invisible payload: {tag}")
                            continue
                        if event_type == "frame_ready":
                            _log("      Stripe captcha bridge 已就绪，等待 challenge 加载 ...")
                        elif event_type == "invisible_initialize":
                            _log("      Stripe invisible captcha 初始化 ...")
                        elif event_type == "invisible_execute":
                            _log("      Stripe invisible captcha 开始执行 ...")
                        elif event_name:
                            _log(f"      Stripe captcha 事件: {event_name}")

                    if result_event.wait(timeout=1):
                        result = bridge_state.get("result") or {}
                        raw = result.get("raw") or {}
                        source = raw.get("source") or "bridge_postmessage"
                        if source != "network_checkcaptcha":
                            wait_deadline = time.time() + 1.5
                            while time.time() < wait_deadline:
                                time.sleep(0.1)
                                newer_result = bridge_state.get("result") or {}
                                newer_raw = newer_result.get("raw") or {}
                                newer_source = newer_raw.get("source") or "bridge_postmessage"
                                if newer_source == "network_checkcaptcha":
                                    _log("      bridge result 已出现，但随后拿到真实 checkcaptcha(pass=true) 结果，优先使用网络侧结果")
                                    result = newer_result
                                    raw = newer_raw
                                    source = newer_source
                                    break
                        browser_verify = raw.get("browser_verify") if isinstance(raw, dict) else None
                        if source == "network_checkcaptcha" and not browser_verify and verify_url:
                            wait_deadline = time.time() + 4.0
                            while time.time() < wait_deadline:
                                time.sleep(0.1)
                                newer_result = bridge_state.get("result") or {}
                                newer_raw = newer_result.get("raw") or {}
                                newer_browser_verify = newer_raw.get("browser_verify") if isinstance(newer_raw, dict) else None
                                if newer_browser_verify:
                                    result = newer_result
                                    raw = newer_raw
                                    browser_verify = newer_browser_verify
                                    _log("      已拿到同浏览器上下文内的 verify_challenge 响应")
                                    break
                        token = result.get("response", "")
                        ekey = result.get("ekey", "")
                        _log(
                            "      浏览器 challenge 已完成 "
                            f"(source={source}, token: {len(token)} chars, ekey: {len(ekey)} chars)"
                        )
                        _log(f"      {_describe_challenge_artifact('challenge_token', token)}")
                        _log(f"      {_describe_challenge_artifact('challenge_ekey', ekey)}")
                        if source == "network_checkcaptcha":
                            _log("      challenge 凭证来源: 真实 checkcaptcha(pass=true) 网络结果")
                        if browser_verify:
                            bv_status = browser_verify.get("status")
                            bv_text = str(browser_verify.get("text") or "")
                            _log(
                                "      浏览器内 verify_challenge: "
                                f"status={bv_status} body_len={len(bv_text)}"
                            )
                        return token, ekey, browser_verify

                    if external_solver_proc is not None:
                        rc = external_solver_proc.poll()
                        if rc is not None and not external_solver_exit_logged:
                            external_solver_exit_logged = True
                            _log(f"      external_solver 已退出 rc={rc}")
                            if rc != 0 and not bridge_state.get("result"):
                                raise RuntimeError(f"external_solver 失败 (rc={rc})")

                    if error_event.is_set() or bridge_state.get("error"):
                        err = bridge_state.get("error") or {}
                        raise RuntimeError(
                            "浏览器 challenge 返回错误: "
                            + json.dumps(err, ensure_ascii=False)[:500]
                        )

                    if cancel_event.is_set() or bridge_state.get("cancelled"):
                        raise RuntimeError("浏览器 challenge 被取消")

                raise TimeoutError(f"浏览器 challenge 超时 ({timeout_ms / 1000:.0f}s)")
            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
                if playwright_ctx is not None:
                    try:
                        playwright_ctx.stop()
                    except Exception:
                        pass
                if external_solver_proc is not None:
                    try:
                        if external_solver_proc.poll() is None:
                            external_solver_proc.terminate()
                            try:
                                external_solver_proc.wait(timeout=5)
                            except Exception:
                                external_solver_proc.kill()
                    except Exception:
                        pass
        finally:
            httpd.shutdown()
            httpd.server_close()


def _build_inline_payment_method_fields(
    card: dict,
    session_id: str,
    ctx: dict,
    runtime_version: str,
) -> dict:
    addr = card.get("address", {})
    payment_method_checkout_config_id = (
        ctx.get("payment_method_checkout_config_id")
        or ctx.get("config_id")
        or ""
    )
    elements_session_config_id = (
        ctx.get("elements_session_config_id")
        or str(uuid.uuid4())
    )
    return {
        "payment_method_data[type]": "card",
        "payment_method_data[allow_redisplay]": "unspecified",
        "payment_method_data[billing_details][name]": card["name"],
        "payment_method_data[billing_details][email]": card["email"],
        "payment_method_data[billing_details][address][country]": addr.get("country", "US"),
        "payment_method_data[billing_details][address][line1]": addr.get("line1", ""),
        "payment_method_data[billing_details][address][city]": addr.get("city", ""),
        "payment_method_data[billing_details][address][postal_code]": addr.get("postal_code", ""),
        "payment_method_data[billing_details][address][state]": addr.get("state", ""),
        "payment_method_data[card][number]": card["number"],
        "payment_method_data[card][cvc]": card["cvc"],
        "payment_method_data[card][exp_month]": str(card["exp_month"]).zfill(2),
        "payment_method_data[card][exp_year]": str(card["exp_year"])[-2:],
        "payment_method_data[pasted_fields]": ctx.get("pasted_fields", "number"),
        "payment_method_data[payment_user_agent]": (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
            "payment-element; deferred-intent"
        ),
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(
            ctx.get("time_on_page", random.randint(25000, 55000))
        ),
        "payment_method_data[client_attribution_metadata][client_session_id]": ctx.get("stripe_js_id", str(uuid.uuid4())),
        "payment_method_data[client_attribution_metadata][checkout_session_id]": session_id,
        "payment_method_data[client_attribution_metadata][checkout_config_id]": payment_method_checkout_config_id,
        "payment_method_data[client_attribution_metadata][elements_session_id]": ctx.get("elements_session_id", _gen_elements_session_id()),
        "payment_method_data[client_attribution_metadata][elements_session_config_id]": elements_session_config_id,
        "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
        "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
        "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
        "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
        "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
        "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]": "payment",
        "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]": "address",
    }


def create_payment_method(
    session: requests.Session,
    pk: str,
    card: dict,
    captcha_token: str,
    session_id: str,
    stripe_ver: str = STRIPE_VERSION_BASE,
    ctx: dict = None,
) -> str:
    ctx = ctx or {}
    guid = ctx.get("guid") or _gen_fingerprint()[0]
    muid = ctx.get("muid") or _gen_fingerprint()[0]
    sid  = ctx.get("sid")  or _gen_fingerprint()[0]
    addr = card.get("address", {})

    data = {
        "billing_details[name]": card["name"],
        "billing_details[email]": card["email"],
        "billing_details[address][country]": addr.get("country", "US"),
        "billing_details[address][line1]": addr.get("line1", ""),
        "billing_details[address][city]": addr.get("city", ""),
        "billing_details[address][postal_code]": addr.get("postal_code", ""),
        "billing_details[address][state]": addr.get("state", ""),
        "type": "card",
        "card[number]": card["number"],
        "card[cvc]": card["cvc"],
        "card[exp_year]": card["exp_year"],
        "card[exp_month]": card["exp_month"],
        "allow_redisplay": "unspecified",

        "payment_user_agent": "stripe.js/5412f474d5; stripe-js-v3/5412f474d5; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        # time_on_page: 模拟从页面加载到提交的真实耗时 (HAR: 31368ms / 249421ms)
        "time_on_page": str(ctx.get("time_on_page", random.randint(25000, 55000))),
        "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
        "client_attribution_metadata[checkout_session_id]": session_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": pk,
        "_stripe_version": stripe_ver,
    }
    if captcha_token:
        data["radar_options[hcaptcha_token]"] = captcha_token

    url = f"{STRIPE_API}/v1/payment_methods"
    _log("[4/6] 创建支付方式 (payment_method) ...")
    _log_request("POST", url, data=data, tag="[4/6] create_payment_method")
    resp = session.post(url, data=data, headers=_stripe_headers())
    _log_response(resp, tag="[4/6] create_payment_method")
    if resp.status_code != 200:
        raise RuntimeError(f"创建 payment_method 失败 [{resp.status_code}]: {resp.text[:500]}")

    pm = resp.json()
    pm_id = pm["id"]
    brand = pm.get("card", {}).get("display_brand", "unknown")
    last4 = pm.get("card", {}).get("last4", "????")
    _log(f"      成功: {pm_id}  ({brand} ****{last4})")
    return pm_id


def create_paypal_payment_method(
    session: requests.Session,
    pk: str,
    card: dict,
    session_id: str,
    stripe_ver: str = STRIPE_VERSION_BASE,
    ctx: dict = None,
) -> str:
    """创建 type=paypal 的 payment_method（不含卡号信息）"""
    ctx = ctx or {}
    guid = ctx.get("guid") or _gen_fingerprint()[0]
    muid = ctx.get("muid") or _gen_fingerprint()[0]
    sid  = ctx.get("sid")  or _gen_fingerprint()[0]
    addr = card.get("address", {})
    runtime_version = ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    elements_session_id = ctx.get("elements_session_id", _gen_elements_session_id())
    elements_session_config_id = (
        ctx.get("elements_session_config_id")
        or str(uuid.uuid4())
    )
    payment_method_checkout_config_id = (
        ctx.get("payment_method_checkout_config_id")
        or ctx.get("config_id")
        or ""
    )

    data = {
        "type": "paypal",
        "billing_details[name]": card["name"],
        "billing_details[email]": card["email"],
        "billing_details[address][country]": addr.get("country", "US"),
        "billing_details[address][line1]": addr.get("line1", ""),
        "billing_details[address][city]": addr.get("city", ""),
        "billing_details[address][postal_code]": addr.get("postal_code", ""),
        "billing_details[address][state]": addr.get("state", ""),
        "payment_user_agent": (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
            "payment-element; deferred-intent"
        ),
        "referrer": "https://chatgpt.com",
        "time_on_page": str(ctx.get("time_on_page", random.randint(25000, 55000))),
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": session_id,
        "client_attribution_metadata[checkout_config_id]": payment_method_checkout_config_id,
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": elements_session_config_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": pk,
        "_stripe_version": stripe_ver,
    }

    url = f"{STRIPE_API}/v1/payment_methods"
    _log("[4/6] 创建 PayPal 支付方式 (payment_method type=paypal) ...")
    _log_request("POST", url, data=data, tag="[4/6] create_paypal_payment_method")
    resp = session.post(url, data=data, headers=_stripe_headers())
    _log_response(resp, tag="[4/6] create_paypal_payment_method")
    if resp.status_code != 200:
        raise RuntimeError(f"创建 PayPal payment_method 失败 [{resp.status_code}]: {resp.text[:500]}")

    pm = resp.json()
    pm_id = pm["id"]
    _log(f"      成功: {pm_id}  (paypal)")
    return pm_id


def _drive_gopay_from_redirect(
    redirect_url: str,
    cfg: dict,
    otp_file: str = "",
    session_id: str = "",
) -> None:
    """从 pm-redirects.stripe.com URL 接管 → Midtrans linking → GoPay PIN/OTP → 扣款。

    复用 gopay 模块的 GoPayCharger.run_from_redirect。OTP 从 stdin（CLI）或
    file-watch（webui runner）取。
    """
    import sys as _sys
    from pathlib import Path as _Path
    here = _Path(__file__).resolve().parent
    if str(here) not in _sys.path:
        _sys.path.insert(0, str(here))
    import gopay as _gopay

    auth_cfg = (cfg.get("fresh_checkout") or {}).get("auth") or {}
    cs_session = _gopay._build_chatgpt_session(auth_cfg)
    proxy = (cfg.get("proxy") or "").strip() or None
    gopay_cfg = cfg.get("gopay") or {}

    if otp_file:
        provider = _gopay.file_watch_otp_provider(_Path(otp_file), timeout=300.0)
    else:
        provider = _gopay.build_configured_otp_provider(
            gopay_cfg,
            fallback_provider=_gopay.cli_otp_provider,
            log=_log,
        )

    charger = _gopay.GoPayCharger(
        cs_session, gopay_cfg,
        otp_provider=provider, proxy=proxy,
        runtime_cfg=cfg.get("runtime"),
    )
    _log(f"      [gopay] 从 redirect 接管 → {redirect_url[:80]}...")
    result = charger.run_from_redirect(redirect_url, cs_id=session_id)
    _log(f"      [gopay] 完成: {result}")


def create_gopay_payment_method(
    session: requests.Session,
    pk: str,
    card: dict,
    session_id: str,
    stripe_ver: str = STRIPE_VERSION_BASE,
    ctx: dict = None,
) -> str:
    """创建 type=gopay 的 payment_method（印尼 e-wallet, ChatGPT Plus 用）"""
    ctx = ctx or {}
    guid = ctx.get("guid") or _gen_fingerprint()[0]
    muid = ctx.get("muid") or _gen_fingerprint()[0]
    sid  = ctx.get("sid")  or _gen_fingerprint()[0]
    addr = card.get("address", {}) if card else {}
    runtime_version = ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    elements_session_id = ctx.get("elements_session_id", _gen_elements_session_id())
    elements_session_config_id = (
        ctx.get("elements_session_config_id") or str(uuid.uuid4())
    )
    payment_method_checkout_config_id = (
        ctx.get("payment_method_checkout_config_id")
        or ctx.get("config_id")
        or ""
    )

    data = {
        "type": "gopay",
        "billing_details[name]": (card or {}).get("name") or "John Doe",
        "billing_details[email]": (card or {}).get("email") or "buyer@example.com",
        "billing_details[address][country]": addr.get("country") or "US",
        "billing_details[address][line1]": addr.get("line1") or "3110 Sunset Boulevard",
        "billing_details[address][city]": addr.get("city") or "Los Angeles",
        "billing_details[address][postal_code]": addr.get("postal_code") or "90026",
        "billing_details[address][state]": addr.get("state") or "CA",
        "payment_user_agent": (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
            "payment-element; deferred-intent"
        ),
        "referrer": "https://chatgpt.com",
        "time_on_page": str(ctx.get("time_on_page", random.randint(25000, 55000))),
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": session_id,
        "client_attribution_metadata[checkout_config_id]": payment_method_checkout_config_id,
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": elements_session_config_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": pk,
        "_stripe_version": stripe_ver,
    }

    url = f"{STRIPE_API}/v1/payment_methods"
    _log("[4/6] 创建 GoPay 支付方式 (payment_method type=gopay) ...")
    _log_request("POST", url, data=data, tag="[4/6] create_gopay_payment_method")
    resp = session.post(url, data=data, headers=_stripe_headers())
    _log_response(resp, tag="[4/6] create_gopay_payment_method")
    if resp.status_code != 200:
        raise RuntimeError(f"创建 GoPay payment_method 失败 [{resp.status_code}]: {resp.text[:500]}")

    pm = resp.json()
    pm_id = pm["id"]
    _log(f"      成功: {pm_id}  (gopay)")
    return pm_id


def _solve_arkose_funcaptcha(api_key: str, public_key: str, page_url: str, timeout: int = 120) -> str:
    """调用远端打码平台解 Arkose FunCaptcha"""
    if not api_key:
        _log("      未配置打码平台 API key，无法解 Arkose")
        return ""
    _log(f"      提交 FunCaptcha 到打码平台 (pk={public_key[:20]}...)")
    import requests as _req
    # 创建任务
    create_resp = _req.post(_remote_captcha_url("/createTask"), json={
        "clientKey": api_key,
        "task": {
            "type": "FunCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websitePublicKey": public_key,
        }
    }, timeout=30)
    result = create_resp.json()
    if result.get("errorId"):
        _log(f"      打码平台创建任务失败: {result.get('errorDescription', '')}")
        return ""
    task_id = result.get("taskId")
    _log(f"      打码平台 taskId: {task_id}")

    # 轮询结果
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        poll_resp = _req.post(_remote_captcha_url("/getTaskResult"), json={
            "clientKey": api_key,
            "taskId": task_id,
        }, timeout=15)
        poll_result = poll_resp.json()
        status = poll_result.get("status", "")
        if status == "ready":
            token = poll_result.get("solution", {}).get("token", "")
            _log(f"      打码平台 FunCaptcha 解题成功")
            return token
        elif poll_result.get("errorId"):
            _log(f"      打码平台解题失败: {poll_result.get('errorDescription', '')}")
            return ""
        _log(f"      打码平台轮询中... status={status}")
    _log("      打码平台 FunCaptcha 超时")
    return ""


def _generate_fn_sync_data(email_text: str = "", password_text: str = "") -> str:
    """生成 PayPal fn_sync_data 设备指纹（键盘时序 + 屏幕信息）"""
    def _keystroke_timing(text: str) -> str:
        if not text:
            return ""
        parts = []
        for _ in text:
            di = random.randint(45, 170)
            ui = random.randint(25, 85)
            dk = random.randint(35, 110)
            uk = random.randint(15, 65)
            parts.append(f"Di{di}Ui{ui}Dk{dk}Uk{uk}")
        return ",".join(parts)

    payload = {
        "ts1": _keystroke_timing(email_text),
        "ts2": _keystroke_timing(password_text),
        "rDT": str(random.randint(30, 200)),
        "bP": "24",
        "wI": "1920",
        "wO": "1080",
    }
    inner = json.dumps(payload, separators=(",", ":"))
    return urllib.parse.quote(inner)


def _solve_remote_hcaptcha_paypal(
    api_key: str,
    site_key: str,
    page_url: str,
    timeout: int = 120,
) -> str:
    """通过远端打码平台解 PayPal 页面上的 hCaptcha（多策略尝试）"""
    if not api_key:
        _log("      [hCaptcha] 未配置 captcha API key")
        return ""

    # 多策略尝试：Enterprise → 普通 → 不同 URL
    strategies = [
        {"type": "HCaptchaTaskProxyless", "websiteURL": page_url,
         "websiteKey": site_key, "isEnterprise": True, "userAgent": USER_AGENT},
        {"type": "HCaptchaTaskProxyless", "websiteURL": page_url,
         "websiteKey": site_key, "userAgent": USER_AGENT},
        {"type": "HCaptchaTaskProxyless", "websiteURL": "https://www.paypal.com",
         "websiteKey": site_key, "isEnterprise": True, "userAgent": USER_AGENT},
    ]
    for idx, task_spec in enumerate(strategies):
        ent = task_spec.get("isEnterprise", False)
        _log(f"      [hCaptcha] 策略 {idx + 1}/{len(strategies)} (enterprise={ent}, url={task_spec['websiteURL'][:40]}...)")
        try:
            create_resp = requests.post(_remote_captcha_url("/createTask"), json={
                "clientKey": api_key, "task": task_spec,
            }, timeout=30)
            result = create_resp.json()
        except Exception as e:
            _log(f"      [hCaptcha] 请求异常: {e}")
            continue
        if result.get("errorId"):
            _log(f"      [hCaptcha] 创建失败: {result.get('errorDescription', '')}")
            continue
        task_id = result.get("taskId")
        _log(f"      [hCaptcha] taskId: {task_id}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            try:
                poll_resp = requests.post(_remote_captcha_url("/getTaskResult"), json={
                    "clientKey": api_key, "taskId": task_id,
                }, timeout=15)
                poll_result = poll_resp.json()
            except Exception:
                continue
            status = poll_result.get("status", "")
            if status == "ready":
                token = poll_result.get("solution", {}).get("gRecaptchaResponse", "")
                _log(f"      [hCaptcha] 解题成功 (策略 {idx + 1}, token len={len(token)})")
                return token
            elif poll_result.get("errorId"):
                _log(f"      [hCaptcha] 失败: {poll_result.get('errorDescription', '')}")
                break
            _log(f"      [hCaptcha] 轮询中... status={status}")
        else:
            _log(f"      [hCaptcha] 策略 {idx + 1} 超时")
    _log("      [hCaptcha] 所有策略均失败")
    return ""


def _solve_remote_recaptcha_v3(
    api_key: str,
    site_key: str,
    page_url: str,
    action: str = "LOGIN",
    timeout: int = 60,
) -> str:
    """通过远端打码平台解 Google reCAPTCHA Enterprise v3"""
    if not api_key:
        return ""
    _log(f"      [reCAPTCHA v3] 提交到打码平台 ...")
    create_resp = requests.post(_remote_captcha_url("/createTask"), json={
        "clientKey": api_key,
        "task": {
            "type": "RecaptchaV3EnterpriseTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "pageAction": action,
        }
    }, timeout=30)
    result = create_resp.json()
    if result.get("errorId"):
        _log(f"      [reCAPTCHA v3] 创建失败: {result.get('errorDescription', '')}")
        return ""
    task_id = result.get("taskId")
    _log(f"      [reCAPTCHA v3] taskId: {task_id}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        poll_resp = requests.post(_remote_captcha_url("/getTaskResult"), json={
            "clientKey": api_key, "taskId": task_id,
        }, timeout=15)
        poll_result = poll_resp.json()
        status = poll_result.get("status", "")
        if status == "ready":
            token = poll_result.get("solution", {}).get("gRecaptchaResponse", "")
            _log(f"      [reCAPTCHA v3] 解题成功")
            return token
        elif poll_result.get("errorId"):
            _log(f"      [reCAPTCHA v3] 失败: {poll_result.get('errorDescription', '')}")
            return ""
    _log("      [reCAPTCHA v3] 超时")
    return ""


def _paypal_full_login(
    http: requests.Session,
    approve_html: str,
    approve_url: str,
    paypal_cfg: dict,
    captcha_api_key: str,
    csrf: str,
    sid: str,
    flow_id: str,
    ctx_id: str,
    recaptcha_key: str,
) -> None:
    """完整 PayPal 登录（邮箱→密码→验证码→2FA）。成功后 http session 带有效 auth cookies。"""
    paypal_email = paypal_cfg["email"]
    paypal_password = paypal_cfg["password"]
    _log("      ═══════ PayPal 完整登录 ═══════")

    # [L1] POST /signin/load-resource → 建立 x-pp-s session cookie
    _log("      [L1] load-resource ...")
    lr_data = {
        "_csrf": csrf, "flowId": flow_id,
        "intent": "checkout", "_sessionID": sid,
    }
    resp_lr = http.post(
        "https://www.paypal.com/signin/load-resource", data=lr_data,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.paypal.com",
            "Referer": approve_url,
        }, timeout=30,
    )
    _log(f"      [L1] load-resource status={resp_lr.status_code}")
    try:
        lr_json = resp_lr.json()
        if lr_json.get("nonce"):
            csrf = lr_json["nonce"]
    except Exception:
        pass

    # [L2] POST /signin (email)
    _log(f"      [L2] 提交邮箱: {paypal_email}")
    fn_data_email = _generate_fn_sync_data(paypal_email)
    email_form = {
        "splitLoginContext": "inputEmail",
        "login_email": paypal_email,
        "_csrf": csrf,
        "_sessionID": sid,
        "intent": "checkout",
        "flowId": flow_id,
        "ctxId": ctx_id or f"xo_ctx_{flow_id}",
        "fn_sync_data": fn_data_email,
        "locale.x": "zh_XC",
    }
    resp_email = http.post(
        "https://www.paypal.com/signin", data=email_form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.paypal.com",
            "Referer": approve_url,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/html, */*",
        }, timeout=30,
    )
    _log(f"      [L2] email status={resp_email.status_code}")
    # 更新 csrf
    try:
        ej = resp_email.json()
        if ej.get("nonce"):
            csrf = ej["nonce"]
        _log(f"      [L2] next: {ej.get('splitLoginContext', '?')}")
    except Exception:
        m = re.search(r'name="_csrf"\s+value="([^"]+)"', resp_email.text)
        if m:
            csrf = m.group(1)

    # [L3] (可选) reCAPTCHA Enterprise v3 — 提升信任评分
    if recaptcha_key and captcha_api_key:
        _log("      [L3] 解 reCAPTCHA Enterprise v3 ...")
        grc_token = _solve_remote_recaptcha_v3(
            captcha_api_key, recaptcha_key,
            "https://www.paypal.com/signin", action="LOGIN", timeout=60,
        )
        if grc_token:
            resp_grc = http.post(
                "https://www.paypal.com/auth/verifygrcadenterprise",
                data={
                    "grcV3EntToken": grc_token,
                    "_sessionID": sid,
                    "_csrf": csrf,
                    "action": "LOGIN",
                },
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://www.paypal.com",
                }, timeout=30,
            )
            _log(f"      [L3] reCAPTCHA verify status={resp_grc.status_code}")
    else:
        _log("      [L3] 跳过 reCAPTCHA v3")

    # [L4] POST /signin (password)
    _log("      [L4] 提交密码 ...")
    fn_data_pwd = _generate_fn_sync_data(paypal_email, paypal_password)
    pwd_form = {
        "splitLoginContext": "inputPassword",
        "login_email": paypal_email,
        "login_password": paypal_password,
        "_csrf": csrf,
        "_sessionID": sid,
        "intent": "checkout",
        "flowId": flow_id,
        "ctxId": ctx_id or f"xo_ctx_{flow_id}",
        "fn_sync_data": fn_data_pwd,
        "locale.x": "zh_XC",
    }
    resp_pwd = http.post(
        "https://www.paypal.com/signin", data=pwd_form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.paypal.com",
            "Referer": approve_url,
        },
        allow_redirects=False, timeout=30,
    )
    _log(f"      [L4] password status={resp_pwd.status_code}")

    # ── 处理密码提交后的响应 ──
    request_id = ""
    _hash = ""
    current_resp = resp_pwd

    if resp_pwd.status_code == 200:
        pwd_html = resp_pwd.text
        # debug: 页面标题 + 关键标记
        _title = re.search(r'<title>(.*?)</title>', pwd_html)
        _log(f"      [L4-debug] title={_title.group(1) if _title else 'N/A'}")
        # 调试: dump 完整 HTML
        try:
            with open("/tmp/paypal_pwd_resp.html", "w", encoding="utf-8") as _df:
                _df.write(pwd_html)
            _log("      [L4-debug] 已保存 /tmp/paypal_pwd_resp.html")
        except Exception:
            pass
        has_error = bool(re.search(r'(?:incorrectPassword|loginError|captcha)', pwd_html, re.I))
        has_hcaptcha_tag = "hcaptcha" in pwd_html.lower()
        _log(f"      [L4-debug] hasError={has_error} hasHCaptcha={has_hcaptcha_tag} len={len(pwd_html)}")
        m = re.search(r'name="_requestId"\s+value="([^"]+)"', pwd_html)
        if m:
            request_id = m.group(1)
        m = re.search(r'name="_hash"\s+value="([^"]+)"', pwd_html)
        if m:
            _hash = m.group(1)
        m = re.search(r'name="_csrf"\s+value="([^"]+)"', pwd_html)
        if m:
            csrf = m.group(1)
        _log(f"      [L4-debug] requestId={bool(request_id)} hash={bool(_hash)}")

        # 检查是否需要 hCaptcha
        needs_hcaptcha = has_hcaptcha_tag or bool(request_id)
        if needs_hcaptcha:
            if not captcha_api_key:
                raise RuntimeError("PayPal 需要 hCaptcha 但未配置验证码 API key")
            # 提取 sitekey（可能在 HTML 中）
            hcaptcha_sitekey = ""
            m = re.search(r'data-sitekey="([^"]+)"', pwd_html)
            if m:
                hcaptcha_sitekey = m.group(1)
            if not hcaptcha_sitekey:
                hcaptcha_sitekey = "bf07db68-5c2e-42e8-8779-ea8384890eea"

            _log(f"      [L5] 需要 hCaptcha (sitekey={hcaptcha_sitekey[:20]}...)")
            hcaptcha_token = _solve_remote_hcaptcha_paypal(
                captcha_api_key, hcaptcha_sitekey,
                "https://www.paypal.com/signin", timeout=120,
            )
            if not hcaptcha_token:
                raise RuntimeError("PayPal hCaptcha 解题失败")

            hcaptcha_form = {
                "_csrf": csrf,
                "_requestId": request_id,
                "_hash": _hash,
                "_sessionID": sid,
                "hcaptcha": hcaptcha_token,
                "_adsChallengeType": "visual-challenge",
                "hcaptcha_eval": str(random.randint(200, 600)),
                "hcaptcha_render": str(random.randint(100, 300)),
                "hcaptcha_verification": str(random.randint(5000, 15000)),
            }
            current_resp = http.post(
                "https://www.paypal.com/signin", data=hcaptcha_form,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://www.paypal.com",
                    "Referer": approve_url,
                },
                allow_redirects=False, timeout=30,
            )
            _log(f"      [L5] hCaptcha submit status={current_resp.status_code}")

    # ── 跟随重定向链 ──
    for _ in range(10):
        if current_resp.status_code not in (301, 302, 303, 307, 308):
            break
        loc = current_resp.headers.get("Location", "")
        if not loc:
            break
        if loc.startswith("/"):
            loc = f"https://www.paypal.com{loc}"
        _log(f"      → redirect: {loc[:100]}")
        current_resp = http.get(loc, allow_redirects=False, timeout=30)

    current_url = getattr(current_resp, "url", "") or ""
    current_html = current_resp.text

    # ── 处理 2FA (authflow) ──
    if "/authflow" in current_url or "authflow" in current_html[:5000]:
        _log("      [L6] 进入 2FA 流程 ...")
        af_csrf = ""
        af_sid = ""
        af_doc_id = ""
        for pat in [r'"_csrf"\s*:\s*"([^"]+)"', r'name="_csrf"\s+value="([^"]+)"',
                    r'"csrfToken"\s*:\s*"([^"]+)"']:
            m = re.search(pat, current_html)
            if m:
                af_csrf = m.group(1)
                break
        m = re.search(r'"anw_sid"\s*:\s*"([^"]+)"', current_html)
        if m:
            af_sid = m.group(1)
        for pat in [r'"authflowDocumentId"\s*:\s*"([^"]+)"',
                    r'"documentId"\s*:\s*"([^"]+)"']:
            m = re.search(pat, current_html)
            if m:
                af_doc_id = m.group(1)
                break
        _log(f"      [L6] csrf={af_csrf[:15]}... anw_sid={af_sid[:15]}... docId={af_doc_id[:15]}...")

        # SELECT email challenge
        _log("      [L6-1] 选择邮箱验证 ...")
        select_body = {
            "_csrf": af_csrf,
            "anw_sid": af_sid,
            "authflowDocumentId": af_doc_id,
            "action": "SELECT_CHALLENGE",
            "selectedChallengeType": "email",
            "isCheckoutFlow": True,
            "fn_sync_data": _generate_fn_sync_data(),
        }
        resp_select = http.put(
            "https://www.paypal.com/authflow/challenges/email",
            json=select_body,
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://www.paypal.com",
                "Referer": current_url,
            }, timeout=30,
        )
        _log(f"      [L6-1] select status={resp_select.status_code}")
        try:
            sel_json = resp_select.json()
            af_doc_id = sel_json.get("authflowDocumentId", af_doc_id)
            af_csrf = sel_json.get("_csrf", af_csrf)
        except Exception:
            pass

        # 获取 OTP
        _log("      [L6-2] 等待 PayPal OTP ...")
        otp = _fetch_paypal_otp(paypal_cfg, timeout=90)
        if not otp:
            raise RuntimeError("PayPal 2FA OTP 获取失败")
        _log(f"      [L6-2] OTP: {otp}")

        # 提交 OTP
        answer_body = {
            "_csrf": af_csrf,
            "anw_sid": af_sid,
            "authflowDocumentId": af_doc_id,
            "action": "ANSWER",
            "answer": otp,
            "selectedChallengeType": "email",
            "isCheckoutFlow": True,
            "challengeStartTime": str(int(time.time() * 1000)),
        }
        resp_answer = http.put(
            "https://www.paypal.com/authflow/challenges/email",
            json=answer_body,
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://www.paypal.com",
                "Referer": current_url,
            }, timeout=30,
        )
        _log(f"      [L6-3] submit OTP status={resp_answer.status_code}")
        try:
            ans_json = resp_answer.json()
            for ch in ans_json.get("challenges", []):
                if ch.get("challengeType") == "email":
                    ch_status = ch.get("status", "")
                    _log(f"      [L6-3] challenge status: {ch_status}")
                    if ch_status != "PASSED":
                        raise RuntimeError(f"PayPal 2FA 验证失败: {ch_status}")
        except RuntimeError:
            raise
        except Exception:
            _log("      [L6-3] 无法解析 OTP 响应，继续")

        # 回到 signin/return
        _log("      [L6-4] signin/return ...")
        resp_return = http.get(
            f"https://www.paypal.com/signin/return?flowFrom=anw-stepup&ctxId={ctx_id}",
            allow_redirects=True, timeout=30,
        )
        _log(f"      [L6-4] 最终 URL: {resp_return.url[:100]}")

    _log("      ═══════ PayPal 登录完成 ═══════")


def _safe_screenshot(page, path: str):
    """取截图，失败不影响主流程"""
    try:
        page.screenshot(path=path, timeout=5000)
    except Exception:
        pass


def _fetch_openai_login_otp(target_email: str, timeout: int = 180) -> str:
    """从 CF KV 取 OpenAI 登录 OTP（worker 已替代 IMAP→QQ 转发链路）。

    返回空串表示超时或 KV 路径配置缺失，调用方按需 fallback。
    """
    try:
        from cf_kv_otp_provider import CloudflareKVOtpProvider
    except ImportError as e:
        _log(f"      [RT-OTP] cf_kv_otp_provider 不可用: {e}")
        return ""
    try:
        provider = CloudflareKVOtpProvider.from_env_or_secrets()
        return provider.wait_for_otp(target_email, timeout=timeout)
    except TimeoutError:
        _log(f"      [RT-OTP] CF KV 等 OTP 超时 {timeout}s")
        return ""
    except Exception as e:
        _log(f"      [RT-OTP] CF KV 取 OTP 异常: {e}")
        return ""


_OPENAI_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _resolve_codex_oauth_client_id(*values: str) -> str:
    """Return the first non-placeholder Codex OAuth client id, or the
    OpenAI Codex CLI's well-known constant as a final fallback. Treat
    ``YOUR_*`` placeholders as absent — they would 400 at authorize."""
    candidates = [os.getenv("OAUTH_CODEX_CLIENT_ID", ""), *values]
    for value in candidates:
        client_id = (value or "").strip()
        if not client_id:
            continue
        if client_id.startswith("YOUR_") or client_id.endswith("_CLIENT_ID"):
            continue
        return client_id
    return _OPENAI_CODEX_CLIENT_ID


def _codex_oauth_client_id_from_config(cfg: dict) -> str:
    """Resolve the Codex OAuth client_id from payment config/env."""
    if not isinstance(cfg, dict):
        cfg = {}
    cpa_cfg = cfg.get("cpa") or {}
    fresh_cfg = cfg.get("fresh_checkout") or {}
    auth_cfg = fresh_cfg.get("auth") or {}
    return _resolve_codex_oauth_client_id(
        (cpa_cfg or {}).get("oauth_client_id", ""),
        cfg.get("oauth_client_id", ""),
        cfg.get("codex_oauth_client_id", ""),
        auth_cfg.get("oauth_client_id", ""),
    )


def _exchange_refresh_token_with_session(email: str, password: str, mail_cfg: dict,
                                          proxy_url: str = "",
                                          oauth_client_id: str = "") -> str:
    """
    支付成功后重新登录换 refresh_token。
    流程：
      1. Camoufox 打开 Codex authorize URL
      2. 重定向到 auth.openai.com/log-in
      3. 填邮箱 → 继续 → 填密码 → 继续
      4. 可能触发 Turnstile (Camoufox 自动过) / OTP (IMAP 取)
      5. workspace/select (选择默认 workspace)
      6. 自动 authorize Codex client → localhost callback
      7. POST /oauth/token 换 refresh_token
    """
    import base64 as _b64
    import hashlib as _hashlib
    import secrets as _secrets
    import tempfile as _tmp
    import shutil as _sh
    from urllib.parse import urlparse as _urlparse, urlencode as _urlencode, parse_qs as _parse_qs
    from camoufox.sync_api import Camoufox
    from browserforge.fingerprints import Screen

    def _b64url_nopad(raw: bytes) -> str:
        return _b64.urlsafe_b64encode(raw).decode().rstrip("=")

    codex_client_id = _resolve_codex_oauth_client_id(oauth_client_id)
    codex_redirect = "http://localhost:1455/auth/callback"
    codex_state = _b64url_nopad(_secrets.token_bytes(24))
    verifier = _b64url_nopad(_secrets.token_bytes(64))
    challenge = _b64url_nopad(_hashlib.sha256(verifier.encode()).digest())
    auth_url = "https://auth.openai.com/oauth/authorize?" + _urlencode({
        "client_id": codex_client_id,
        "response_type": "code",
        "redirect_uri": codex_redirect,
        "scope": "openid email profile offline_access",
        "state": codex_state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    })

    # Camoufox proxy
    cf_proxy = None
    if proxy_url:
        pp = _urlparse(proxy_url)
        if pp.scheme in ("socks5", "socks5h") and pp.username:
            import socket as _sock
            relay_port = 18899
            try:
                with _sock.create_connection(("127.0.0.1", relay_port), timeout=2):
                    pass
                cf_proxy = {"server": f"socks5://127.0.0.1:{relay_port}"}
            except Exception:
                pass
        else:
            cf_proxy = {"server": f"{pp.scheme}://{pp.hostname}:{pp.port}",
                        "username": pp.username or "", "password": pp.password or ""}

    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    tmp_profile = _tmp.mkdtemp(prefix="rt_login_")
    code_captured = {"url": ""}

    try:
        with Camoufox(
            headless=not has_display,
            humanize=False,
            persistent_context=True,
            user_data_dir=tmp_profile,
            os="windows",
            screen=Screen(max_width=1920, max_height=1080),
            proxy=cf_proxy,
            geoip=True,
            locale="en-US",
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # localhost 拦截
            def _intercept(route):
                url = route.request.url
                if "localhost:1455" in url and "code=" in url:
                    code_captured["url"] = url
                    _log("      [RT] 拦截 callback: code=<redacted>")
                try:
                    route.fulfill(status=200, content_type="text/html", body="<html>OK</html>")
                except Exception:
                    try: route.abort()
                    except Exception: pass
            page.route("http://localhost:1455/**", _intercept)

            # [1] goto Codex authorize → 触发登录
            _log("      [RT] 打开 Codex authorize URL ...")
            try:
                page.goto(auth_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e_nav:
                _log(f"      [RT] goto 异常: {str(e_nav)[:120]}")
            time.sleep(3)
            _log(f"      [RT] 当前 URL: {page.url[:120]}")

            # [2] 填邮箱
            try:
                page.wait_for_selector('input[type="email"], input[name="email"]',
                                       state="visible", timeout=20000)
                email_input = page.query_selector('input[type="email"]:visible') or \
                              page.query_selector('input[name="email"]:visible')
                email_input.click(); time.sleep(0.3)
                email_input.fill(email)
                time.sleep(random.uniform(0.5, 1.2))
                for sel in ['button[type="submit"]', 'button:has-text("Continue")', '#btnNext']:
                    b = page.query_selector(sel)
                    if b and b.is_visible():
                        b.click()
                        _log("      [RT] 邮箱提交")
                        break
                time.sleep(3)
            except Exception as e:
                _log(f"      [RT] 邮箱填写失败: {e}")
                return ""

            # [3] 填密码（OpenAI 现在很多场景走 passwordless，没密码框就跳过到 OTP）
            try:
                page.wait_for_selector('input[type="password"]', state="visible", timeout=20000)
                pwd_input = page.query_selector('input[type="password"]:visible')
                pwd_input.click(); time.sleep(0.3)
                pwd_input.fill(password)
                time.sleep(random.uniform(0.5, 1.2))
                for sel in ['button[type="submit"]', 'button:has-text("Continue")']:
                    b = page.query_selector(sel)
                    if b and b.is_visible():
                        b.click()
                        _log("      [RT] 密码提交")
                        break
                time.sleep(5)
            except Exception as e:
                _log(f"      [RT] 密码框超时（passwordless 路径），跳过到 OTP 等待: {str(e)[:80]}")
                _safe_screenshot(page, "/tmp/rt_pwd_skip.png")

            # [4] 处理 OTP / Turnstile / 各种中间页
            _log(f"      [RT] 密码后 URL: {page.url[:120]}")
            _safe_screenshot(page, "/tmp/rt_after_pwd.png")
            # 最长等 4 分钟看能不能到 localhost callback
            end = time.time() + 240
            otp_sent_ts = time.time()
            otp_fetched = False
            last_url = ""
            last_log_ts = 0.0
            while time.time() < end:
                if code_captured["url"]:
                    break
                if "localhost:1455" in page.url and "code=" in page.url:
                    code_captured["url"] = page.url
                    break
                cur = page.url
                # URL 变化或每 15s 打印一次
                now = time.time()
                if cur != last_url or (now - last_log_ts) > 15:
                    _log(f"      [RT] URL: {cur[:140]}")
                    last_url = cur
                    last_log_ts = now
                # OTP 页
                if ("/email-otp" in cur or "passwordless" in cur or
                    page.query_selector('input[autocomplete="one-time-code"]') or
                    page.query_selector('input[inputmode="numeric"]')):
                    if not otp_fetched:
                        _log("      [RT] 检测到 OTP 页面，从 IMAP 取验证码 ...")
                        otp_code = _fetch_openai_login_otp(target_email=email, timeout=180)
                        if not otp_code:
                            _log("      [RT] OTP 获取超时")
                            return ""
                        _log(f"      [RT] OTP 已获取 (len={len(otp_code)})")
                        # 填入 OTP
                        filled = False
                        single = page.query_selector('input[autocomplete="one-time-code"]:visible') or \
                                 page.query_selector('input[inputmode="numeric"]:not([maxlength="1"]):visible')
                        if single:
                            single.click(); time.sleep(0.3); single.fill(otp_code); filled = True
                        else:
                            digits = page.query_selector_all('input[maxlength="1"][inputmode="numeric"]') or \
                                     page.query_selector_all('input[maxlength="1"]')
                            if len(digits) >= 6:
                                for i, ch in enumerate(otp_code[:6]):
                                    digits[i].click(); time.sleep(0.1); digits[i].fill(ch)
                                filled = True
                        if filled:
                            time.sleep(0.5)
                            for sel in ['button[type="submit"]', 'button:has-text("Continue")',
                                        'button:has-text("Verify")']:
                                b = page.query_selector(sel)
                                if b and b.is_visible():
                                    b.click()
                                    _log("      [RT] OTP 提交")
                                    break
                            otp_fetched = True
                            time.sleep(3)
                # /about-you 页（偶尔会出现）— 跳过
                if "/about-you" in cur:
                    for sel in ['button:has-text("Finish")', 'button:has-text("Continue")',
                                'button[type="submit"]']:
                        b = page.query_selector(sel)
                        if b and b.is_visible():
                            try:
                                b.click()
                            except Exception:
                                pass
                            break
                # /add-phone 页（OpenAI 风控强制塞这一步）— 找 Skip 按钮跳过
                if "/add-phone" in cur or "phone-number" in cur:
                    if not getattr(page, "_addphone_dumped", False):
                        try:
                            _safe_screenshot(page, "/tmp/rt_addphone.png")
                            btns = page.evaluate("""
                                () => Array.from(document.querySelectorAll('button,a[role=button],a,[role=button]')).map(b => ({
                                    text: (b.innerText||'').trim().slice(0,40),
                                    href: b.href||'',
                                    testid: b.getAttribute('data-testid')||'',
                                    type: b.getAttribute('type')||'',
                                    tag: b.tagName
                                })).filter(b => b.text || b.testid)
                            """)
                            _log(f"      [RT] add-phone 按钮列表: {btns}")
                            page._addphone_dumped = True
                        except Exception:
                            pass
                    skipped = False
                    for sel in [
                        'a:has-text("Skip")', 'button:has-text("Skip")',
                        'a:has-text("Not now")', 'button:has-text("Not now")',
                        'a:has-text("Maybe later")', 'button:has-text("Maybe later")',
                        'a:has-text("Skip for now")', 'button:has-text("Skip for now")',
                        '[data-testid*="skip"]',
                        'a[href*="skip"]',
                    ]:
                        try:
                            b = page.query_selector(sel)
                            if b and b.is_visible():
                                b.click()
                                _log(f"      [RT] add-phone 跳过: {sel}")
                                skipped = True
                                time.sleep(2)
                                break
                        except Exception:
                            pass
                    # 找不到 Skip：OpenAI 强制要求 phone 验证，本次 OAuth 必失败。
                    # 提前 break 避免 240s 空等（caller 通过 callback 没拿到 code 判失败）。
                    if not skipped and not getattr(page, "_addphone_gaveup", False):
                        page._addphone_gaveup = True
                        _log("      [RT] add-phone 找不到 Skip 按钮，提前放弃避免 240s 空等")
                        break
                # Codex consent 授权页 — 自动点 Authorize
                if "/consent" in cur or "/authorize" in cur:
                    if not getattr(page, "_consent_dumped", False):
                        try:
                            _safe_screenshot(page, "/tmp/rt_consent.png")
                            btns = page.evaluate("""
                                () => Array.from(document.querySelectorAll('button,a[role=button],[role=button]')).map(b => ({
                                    text: (b.innerText||'').trim().slice(0,40),
                                    type: b.getAttribute('type')||'',
                                    testid: b.getAttribute('data-testid')||'',
                                    name: b.getAttribute('name')||'',
                                    id: b.id||'',
                                    tag: b.tagName
                                }))
                            """)
                            _log(f"      [RT] consent 页按钮列表: {btns}")
                            page._consent_dumped = True
                        except Exception as e_d:
                            _log(f"      [RT] consent dump 异常: {e_d}")
                    clicked = False
                    for sel in ['button:has-text("Authorize")',
                                'button:has-text("Allow")',
                                'button:has-text("Continue")',
                                'button:has-text("Accept")',
                                'button:has-text("Confirm")',
                                'button[type="submit"]',
                                'button[data-testid*="consent"]',
                                'button[data-testid*="authorize"]',
                                'button[data-testid*="allow"]',
                                'button[name="action"][value="accept"]',
                                'form button']:
                        b = page.query_selector(sel)
                        if b and b.is_visible():
                            try:
                                b.click()
                                _log(f"      [RT] consent 点击: {sel}")
                                clicked = True
                                time.sleep(2)
                            except Exception as e_c:
                                _log(f"      [RT] consent 点击异常 {sel}: {e_c}")
                            break
                    if not clicked:
                        # 兜底：表单 submit
                        try:
                            ok = page.evaluate("""
                                () => {
                                    const f = document.querySelector('form');
                                    if (f) { f.submit(); return true; }
                                    return false;
                                }
                            """)
                            if ok:
                                _log("      [RT] consent 走表单 submit")
                                time.sleep(2)
                        except Exception:
                            pass
                time.sleep(1)

            try:
                page.unroute("http://localhost:1455/**")
            except Exception:
                pass
    finally:
        try:
            _sh.rmtree(tmp_profile, ignore_errors=True)
        except Exception:
            pass

    cb_url = code_captured["url"]
    if not cb_url:
        _log("      [RT] 未捕获到 callback URL")
        return ""
    code = _parse_qs(_urlparse(cb_url).query).get("code", [""])[0]
    if not code:
        _log(f"      [RT] callback 无 code: {cb_url[:150]}")
        return ""
    _log(f"      [RT] 获得 code，POST /oauth/token 换 refresh_token ...")
    try:
        from curl_cffi.requests import Session as CffiSession
        http_rt = CffiSession(impersonate="chrome136")
        if proxy_url:
            _apply_proxy_to_http_session(http_rt, proxy_url)
        r = http_rt.post(
            "https://auth.openai.com/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": codex_client_id,
                "code": code,
                "redirect_uri": codex_redirect,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            timeout=30,
        )
        if r.status_code != 200:
            _log(f"      [RT] /oauth/token: {r.status_code} {r.text[:200]}")
            return ""
        tj = r.json()
        return tj.get("refresh_token", "") or ""
    except Exception as e_tok:
        _log(f"      [RT] /oauth/token 异常: {e_tok}")
        return ""


def _paypal_browser_authorize(
    redirect_url: str,
    paypal_cfg: dict,
    captcha_api_key: str = "",
    proxy_url: str = "",
) -> bool:
    """Playwright 浏览器完成 PayPal 授权全流程（登录+hCaptcha+2FA+授权）。
    当纯 HTTP 因 hCaptcha 失败时的回退路径。
    """
    from playwright.sync_api import sync_playwright
    import subprocess, shutil

    paypal_email = paypal_cfg.get("email", "")
    paypal_password = paypal_cfg.get("password", "")
    if not paypal_email or not paypal_password:
        raise RuntimeError("PayPal 浏览器模式需要 email + password")

    # VLM 配置（用于 hCaptcha 视觉识别）
    vlm_base_url = os.environ.get("CTF_VLM_BASE_URL", "https://YOUR_VLM_ENDPOINT/api")
    vlm_api_key = os.environ.get("CTF_VLM_API_KEY", "")
    vlm_model = os.environ.get("CTF_VLM_MODEL", "gpt-5.4")

    _log("      [Browser] 启动 Camoufox 反检测浏览器 ...")
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    _log(f"      [Browser] display={'yes' if has_display else 'no (virtual)'}")

    # 代理配置 (Camoufox 格式 — socks5 auth 需要 gost 中继)
    cf_proxy = None
    if proxy_url:
        from urllib.parse import urlparse as _urlparse
        pp = _urlparse(proxy_url)
        if pp.scheme in ("socks5", "socks5h") and pp.username:
            import socket as _sock
            relay_port = 18899
            try:
                with _sock.create_connection(("127.0.0.1", relay_port), timeout=2):
                    pass
                cf_proxy = {"server": f"socks5://127.0.0.1:{relay_port}"}
                _log(f"      [Browser] proxy: gost relay 127.0.0.1:{relay_port}")
            except Exception:
                _log(f"      [Browser] 需要 gost 中继: gost -L=socks5://:{relay_port} -F={proxy_url}")
        else:
            cf_proxy = {
                "server": f"{pp.scheme}://{pp.hostname}:{pp.port}",
                "username": pp.username or "",
                "password": pp.password or "",
            }
            _log(f"      [Browser] proxy: {pp.hostname}:{pp.port}")

    success = False
    from camoufox.sync_api import Camoufox
    from browserforge.fingerprints import Screen
    # 持久化 profile: 首次成功登录后 PayPal "Remember this computer" 生效
    # 后续跑批量时跳过 email+password+2FA，直接到 /agreements/approve
    # 保存在项目目录（/tmp 会重启丢失 + tmpfs 空间有限）
    # 若遇到损坏/DDC 失败状态，删除 CTF-pay/paypal_cf_persist 即可重置
    _persist_profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paypal_cf_persist")
    os.makedirs(_persist_profile, exist_ok=True)
    profile_existed = any(os.scandir(_persist_profile))
    _log(f"      [Browser] 持久化 profile: {_persist_profile} (existed={profile_existed})")
    with Camoufox(
        headless=not has_display,
        humanize=False,
        persistent_context=True,
        user_data_dir=_persist_profile,
        os="windows",
        screen=Screen(max_width=1920, max_height=1080),
        proxy=cf_proxy,
        geoip=True,
        locale="zh-CN",
    ) as ctx:
        # persistent_context 返回的是 BrowserContext 而不是 Browser
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # 不注入旧 cookies — 过期 cookies 会让 DDC 更严格

        try:
            # [B1] 跟随 Stripe redirect → PayPal
            _log("      [B1] 打开 PayPal 授权页 ...")
            page.goto(redirect_url, wait_until="domcontentloaded", timeout=60000)
            _log(f"      [B1] URL: {page.url[:100]}")

            # [B-DDC] 等待 DDC 自然通过（Camoufox 反检测指纹 + 全新 profile）
            time.sleep(3)
            _SLIDER_KWS = ("将滑块", "确认您是人类", "Slide the puzzle",
                            "move the slider", "Move the slider", "滑动到最右")
            def _slider_visible() -> bool:
                """主文档 + 所有 iframe（尤其 geo.ddc.paypal.com/captcha）里搜滑块关键字。"""
                try:
                    pt = page.inner_text("body")[:1500]
                    if any(kw in pt for kw in _SLIDER_KWS):
                        return True
                except Exception:
                    pass
                for fr in page.frames:
                    u = fr.url or ""
                    if fr.url == page.url:
                        continue
                    # 只对 DataDome 相关 iframe 取文本（其它 iframe 如 PayPal 内部 about:srcdoc 无意义）
                    if not ("ddc" in u or "captcha" in u or "datadome" in u):
                        continue
                    try:
                        txt = (fr.inner_text("body") or "")[:1500]
                    except Exception:
                        continue
                    if any(kw in txt for kw in _SLIDER_KWS):
                        return True
                return False
            ddc_frame = any("ddc" in f.url or "captcha" in f.url
                             for f in page.frames if f.url != page.url)

            def _find_ddc_iframe():
                for fr in page.frames:
                    u = fr.url or ""
                    if "ddc" in u or "captcha" in u or "datadome" in u:
                        return fr
                return None

            def _try_solve_ddc_slider(attempts: int = 2) -> bool:
                """尝试拖拽 DataDome 可见滑块。成功返回 True。"""
                for attempt in range(attempts):
                    fr = _find_ddc_iframe()
                    if not fr:
                        return False
                    # iframe 元素在主文档里的位置
                    iframe_el = None
                    for sel in ['iframe[src*="ddc"]', 'iframe[src*="captcha"]',
                                'iframe[src*="datadome"]']:
                        iframe_el = page.query_selector(sel)
                        if iframe_el: break
                    if not iframe_el:
                        return False
                    try:
                        iframe_box = iframe_el.bounding_box()
                    except Exception:
                        iframe_box = None
                    if not iframe_box:
                        return False
                    # 滑块 handle：常见 selector
                    handle = None
                    for sel in ['.slider', '[role="slider"]',
                                '.slider-handle', '.sliderIcon',
                                'div[class*="slider"]', 'button[class*="slider"]',
                                '#ddv1-captcha-container .slider']:
                        try:
                            el = fr.query_selector(sel)
                        except Exception:
                            el = None
                        if el:
                            try:
                                if el.is_visible():
                                    handle = el
                                    break
                            except Exception:
                                pass
                    if not handle:
                        return False
                    try:
                        hb = handle.bounding_box()
                    except Exception:
                        hb = None
                    if not hb:
                        return False
                    # 绝对坐标 = iframe 左上 + handle 在 iframe 内的位置
                    start_x = iframe_box["x"] + hb["x"] + hb["width"] / 2
                    start_y = iframe_box["y"] + hb["y"] + hb["height"] / 2
                    # 滑块 track 通常 iframe 宽度减去两侧边距；保守拖到距离 iframe 右边 10px
                    end_x = iframe_box["x"] + iframe_box["width"] - 10
                    end_y = start_y
                    _log(f"      [B-DDC] 拖拽 solver attempt={attempt+1} "
                          f"start=({start_x:.0f},{start_y:.0f}) → end=({end_x:.0f},{end_y:.0f})")
                    # 人类化拖动：移近 → 按下 → smoothstep 多段 → 抬起
                    try:
                        page.mouse.move(start_x - random.uniform(20, 40),
                                         start_y + random.uniform(-5, 5))
                        time.sleep(random.uniform(0.15, 0.35))
                        page.mouse.move(start_x, start_y)
                        time.sleep(random.uniform(0.08, 0.18))
                        page.mouse.down()
                        time.sleep(random.uniform(0.1, 0.22))
                        steps = random.randint(28, 42)
                        for i in range(1, steps + 1):
                            t = i / steps
                            eased = t * t * (3 - 2 * t)
                            x = start_x + (end_x - start_x) * eased
                            y = start_y + random.uniform(-1.8, 1.8)
                            page.mouse.move(x, y)
                            time.sleep(random.uniform(0.012, 0.028))
                        time.sleep(random.uniform(0.08, 0.18))
                        page.mouse.up()
                    except Exception as e:
                        _log(f"      [B-DDC] 拖拽异常: {e}")
                        continue
                    for _wt in range(8):
                        time.sleep(0.8)
                        if not _slider_visible():
                            _log(f"      [B-DDC] ✓ 滑块通过 (attempt {attempt+1})")
                            return True
                        cur = page.url
                        if any(kw in cur for kw in ("/webapps/hermes", "checkoutweb",
                                                      "/signin", "chatgpt.com")):
                            _log(f"      [B-DDC] ✓ 滑块通过 → {cur[:80]}")
                            return True
                    _log(f"      [B-DDC] attempt {attempt+1} 未通过，重试")
                    time.sleep(random.uniform(1.0, 2.0))
                return False

            slider_visible = _slider_visible()
            if slider_visible:
                _safe_screenshot(page, "/tmp/paypal_ddc_slider.png")
                _log("      [B-DDC] 检测到可见滑块，尝试 drag solver ...")
                if _try_solve_ddc_slider(attempts=2):
                    _log("      [B-DDC] drag solver 成功，继续流程")
                else:
                    _log("      [B-DDC] drag solver 失败，发 marker 交给外层")
                    _log("CARD_DATADOME_SLIDER=1")
                    raise RuntimeError("DataDome 滑块 solver 失败")
            if ddc_frame:
                _log("      [B-DDC] 检测到 DDC 隐形挑战，等待自然通过 ...")
                _safe_screenshot(page, "/tmp/paypal_ddc_detected.png")
                for _dw in range(25):
                    time.sleep(2)
                    cur = page.url
                    if any(kw in cur for kw in ["/signin", "/authflow", "/webapps/hermes",
                                                 "/pay", "chatgpt.com"]):
                        _log(f"      [B-DDC] DDC 通过! → {cur[:80]}")
                        break
                    if page.query_selector('input[name="login_email"]') or \
                       page.query_selector('#consentButton'):
                        _log("      [B-DDC] DDC 通过 (检测到页面元素)")
                        break
                    # 中途升级到可见滑块的情况
                    if _slider_visible():
                        _safe_screenshot(page, "/tmp/paypal_ddc_slider.png")
                        _log("      [B-DDC] 等待中升级为可见滑块，中止以便外层换 IP")
                        _log("CARD_DATADOME_SLIDER=1")
                        raise RuntimeError("DataDome 可见滑块，放弃当前 IP")
                    # 如果出现"重试"按钮，点击它刷新
                    retry_btn = page.query_selector('button:has-text("重试")') or \
                                page.query_selector('button:has-text("Retry")')
                    if retry_btn and retry_btn.is_visible():
                        _log("      [B-DDC] 点击重试刷新 DDC ...")
                        retry_btn.click()
                        time.sleep(3)
                    if _dw == 10:
                        _safe_screenshot(page, "/tmp/paypal_ddc_wait.png")
                        _log(f"      [B-DDC] 20s: {cur[:60]}")
                else:
                    _safe_screenshot(page, "/tmp/paypal_ddc_timeout.png")
                    _log(f"      [B-DDC] DDC 50s 超时: {page.url[:80]}")

            # [B2-onetouch] 持久化 profile 识别账号时 PayPal 会显示 "Continue as XXX" /
            # WebAuthn 等一键登录入口，此时 login_email input 仍在 DOM 但被隐藏。
            # 先尝试顺着登录态走一键登录，避免落到 B2 死等 email input 可见。
            onetouch_clicked = False
            try:
                onetouch_selectors = [
                    'button[data-testid*="one-touch"]',
                    'button[data-testid*="continue"]:not([disabled])',
                    'button[data-testid*="login-button"]',
                    'button.oneTouchLoginButton',
                    '#loginButton',
                    'button:has-text("Continue as")',
                    'a:has-text("Continue as")',
                    'button:has-text("Stay signed in")',
                    'button:has-text("以")',
                    'button:has-text("继续")',
                    'button:has-text("Log In as")',
                ]
                for sel in onetouch_selectors:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        txt = (btn.inner_text() or "")[:40]
                        _log(f"      [B2-onetouch] 一键登录按钮: {sel}  text={txt!r}")
                        try:
                            btn.click()
                            onetouch_clicked = True
                            _log("      [B2-onetouch] 已点击一键登录，等待跳转 ...")
                            time.sleep(3)
                        except Exception as e_o:
                            _log(f"      [B2-onetouch] 点击异常: {e_o}")
                        break
            except Exception as e_det:
                _log(f"      [B2-onetouch] 检测异常: {e_det}")

            # 一键登录后若仍在 /signin，说明需要进一步认证，继续走 B2
            email_visible = False
            try:
                ei = page.query_selector('input[name="login_email"]')
                email_visible = bool(ei and ei.is_visible())
            except Exception:
                pass

            # [B2] 需要填写邮箱登录的条件：没走一键登录 + (仍在 signin 页 or email input 可见)
            if (not onetouch_clicked) and ("/signin" in page.url or email_visible):
                _log("      [B2] 需要登录，填写邮箱 ...")
                page.wait_for_selector('input[name="login_email"]', state="visible", timeout=15000)
                # 先关闭 cookie 弹窗（如果有）
                for cookie_sel in ['button:has-text("接受")', 'button:has-text("Accept")', '#acceptAllButton']:
                    try:
                        cb = page.query_selector(cookie_sel)
                        if cb and cb.is_visible():
                            cb.click()
                            time.sleep(0.5)
                            break
                    except Exception:
                        pass
                # PayPal 记住账号时 email input 会被 disabled + 预填（value 已有），此时 fill 会卡住
                # 判断：input.disabled 且 value 非空 → 跳过 fill，直接走 Next/Log In
                skip_fill = False
                try:
                    ei_now = page.query_selector('input[name="login_email"]')
                    if ei_now:
                        is_disabled = ei_now.get_attribute("disabled") is not None
                        cur_val = (ei_now.get_attribute("value") or "").strip()
                        if is_disabled and cur_val:
                            skip_fill = True
                            _log(f"      [B2] email 已预填+disabled ({cur_val}),跳过 fill 直接 Next")
                except Exception:
                    pass
                if not skip_fill:
                    page.fill('input[name="login_email"]', paypal_email)
                    time.sleep(random.uniform(0.8, 1.5))

                # 点击 Next (下一步)
                _log("      [B2] 点击 Next ...")
                for btn_sel in ['#btnNext', 'button[name="signin-submit"]',
                                'button:has-text("下一步")', 'button:has-text("Next")',
                                'button[type="submit"]']:
                    btn = page.query_selector(btn_sel)
                    if btn and btn.is_visible():
                        btn.click()
                        _log(f"      [B2] 点击了: {btn_sel}")
                        break

                # [B3] 等待密码输入框变为可见
                _log("      [B3] 等待密码输入框 ...")
                try:
                    page.wait_for_selector(
                        'input[name="login_password"]',
                        state="visible", timeout=30000,
                    )
                except Exception:
                    # 可能是单页登录或需要更长等待
                    _log("      [B3] 标准等待超时，尝试等待 URL 变化 ...")
                    time.sleep(5)
                pwd_input = page.query_selector('input[name="login_password"]:visible') or \
                            page.query_selector('input[type="password"]:visible')
                if pwd_input:
                    _log("      [B3] 密码框可见，填写密码 ...")
                    pwd_input.fill(paypal_password)
                    time.sleep(random.uniform(0.5, 1))
                    for btn_sel in ['#btnLogin', 'button[name="signin-submit"]',
                                    'button:has-text("登录")', 'button:has-text("Log In")',
                                    'button[type="submit"]']:
                        btn = page.query_selector(btn_sel)
                        if btn and btn.is_visible():
                            btn.click()
                            _log(f"      [B3] 登录按钮: {btn_sel}")
                            break
                    time.sleep(4)
                else:
                    _log("      [B3] 密码框仍不可见")
                    _safe_screenshot(page, "/tmp/paypal_no_pwd.png")
                    _log("      [B3] 截图: /tmp/paypal_no_pwd.png")

            # 登录后截图 + 状态
            time.sleep(2)
            _safe_screenshot(page, "/tmp/paypal_after_login.png")
            _log(f"      [B-diag] 登录后 URL: {page.url[:100]}")
            _log(f"      [B-diag] frames: {[f.url[:60] for f in page.frames[:5]]}")
            _log(f"      [B-diag] 截图: /tmp/paypal_after_login.png")

            # [B4] 处理 hCaptcha（如果出现）
            hcaptcha_frame = None
            for _ in range(8):
                for frame in page.frames:
                    if "hcaptcha" in frame.url:
                        hcaptcha_frame = frame
                        break
                if hcaptcha_frame:
                    break
                time.sleep(1)

            if hcaptcha_frame:
                _log("      [B4] 检测到 hCaptcha，使用人类模拟点击 ...")
                # 用真实鼠标移动 + 点击（避免被检测为自动化）
                clicked = False
                try:
                    hc_iframes = page.locator('iframe[src*="hcaptcha"]')
                    for i in range(hc_iframes.count()):
                        iframe_el = hc_iframes.nth(i)
                        box = iframe_el.bounding_box()
                        if box and box["height"] < 200:  # checkbox iframe 较小
                            # 模拟人类鼠标移动: 先随机位置 → 目标附近 → checkbox
                            cx = box["x"] + box["width"] * 0.3  # checkbox 在左侧
                            cy = box["y"] + box["height"] * 0.5
                            # 先移动到随机位置
                            page.mouse.move(
                                random.uniform(100, 800),
                                random.uniform(200, 500),
                            )
                            time.sleep(random.uniform(0.3, 0.7))
                            # 分多步移动到目标
                            for step in range(5):
                                frac = (step + 1) / 5
                                mx = 400 + (cx - 400) * frac + random.uniform(-3, 3)
                                my = 350 + (cy - 350) * frac + random.uniform(-3, 3)
                                page.mouse.move(mx, my)
                                time.sleep(random.uniform(0.02, 0.06))
                            time.sleep(random.uniform(0.1, 0.3))
                            page.mouse.click(cx, cy)
                            clicked = True
                            _log(f"      [B4] 鼠标点击 hCaptcha checkbox ({cx:.0f},{cy:.0f})")
                            break
                except Exception as e:
                    _log(f"      [B4] 鼠标点击失败: {e}")
                if not clicked:
                    _log("      [B4] 回退到 JS 点击")
                    for frame in page.frames:
                        if "hcaptcha" not in frame.url:
                            continue
                        try:
                            frame.evaluate("document.querySelector('#checkbox')?.click()")
                            clicked = True
                            break
                        except Exception:
                            pass
                time.sleep(5)

                # 等待安全检查完成（最长 60 秒）
                _log("      [B4] 等待安全检查完成 ...")
                captcha_passed = False
                for wait_sec in range(25):
                    cur = page.url
                    # 检查是否跳转到 hermes/consent/2FA/pay
                    if any(kw in cur for kw in ["/webapps/hermes", "/pay/", "/pay?",
                                                 "/authflow", "checkoutweb",
                                                 "chatgpt.com", "pm-redirects"]):
                        captcha_passed = True
                        _log(f"      [B4] 安全检查通过! URL: {cur[:80]}")
                        break
                    # 检查是否还有 hcaptcha iframe（可能已消失）
                    if wait_sec > 10:
                        has_hc = any("hcaptcha" in f.url for f in page.frames)
                        if not has_hc and "signin" not in cur:
                            captcha_passed = True
                            _log(f"      [B4] hCaptcha iframe 已消失，检查通过")
                            break
                    if wait_sec == 15:
                        _safe_screenshot(page, "/tmp/paypal_b4_wait15.png")
                        _log(f"      [B4-diag] 15s: {cur[:80]}")
                    if wait_sec == 30:
                        _safe_screenshot(page, "/tmp/paypal_b4_wait30.png")
                        _log(f"      [B4-diag] 30s: {cur[:80]}")
                    time.sleep(1)

                if not captcha_passed:
                    # 可能触发了视觉挑战或仍在加载
                    _safe_screenshot(page, "/tmp/paypal_hcaptcha_timeout.png")
                    _log(f"      [B4] 60s 超时，URL: {page.url[:80]}")
                    _log("      [B4] 截图: /tmp/paypal_hcaptcha_timeout.png")
                    # 检查是否有视觉挑战
                    has_visual = page.query_selector('.task-image') or \
                                 page.query_selector('[class*="challenge"]')
                    if has_visual:
                        _log("      [B4] 检测到视觉挑战，尝试 VLM ...")
                        challenge_frame = None
                        for frame in page.frames:
                            if "hcaptcha" in frame.url:
                                challenge_frame = frame
                                break
                        if challenge_frame:
                            solved = _solve_hcaptcha_via_vlm(
                                page, challenge_frame,
                                vlm_base_url, vlm_api_key, vlm_model,
                            )
                            if solved:
                                captcha_passed = True
                    if not captcha_passed:
                        raise RuntimeError("PayPal hCaptcha 安全检查超时")

            # [B5] 处理 2FA（如果出现）
            if "/authflow" in page.url:
                _log(f"      [B5] 进入 2FA 流程: {page.url[:80]}")
                _safe_screenshot(page, "/tmp/paypal_2fa.png")
                time.sleep(3)

                # 确保 Remember this device 勾选（让 trusted device 保存到 profile）
                for sel in ['input[type="checkbox"]']:
                    cbs = page.query_selector_all(sel)
                    for cb in cbs:
                        try:
                            if cb.is_visible() and not cb.is_checked():
                                cb.check(force=True)
                                _log(f"      [B5] 勾选复选框 (Remember this device)")
                        except Exception:
                            pass

                # 点击 Next 触发 2FA（email 或 push）
                _log("      [B5] 点击 Next 触发 2FA ...")
                for sel in ['button:has-text("Next")', 'button:has-text("下一步")',
                            'button[type="submit"]', 'button[class*="primary"]']:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        _log(f"      [B5] 点击 Next: {sel}")
                        time.sleep(4)
                        break
                _safe_screenshot(page, "/tmp/paypal_2fa_after_next.png")

                # 根据 URL 判断走 email 还是 push
                cur_url = page.url
                is_email_flow = "/challenges/email" in cur_url
                is_push_flow = "/challenges/pn" in cur_url or "/challenges/push" in cur_url
                # 兼容：URL 还没变时，看页面是否有 OTP 输入框
                if not is_email_flow and not is_push_flow:
                    if page.query_selector('input[autocomplete="one-time-code"]') or \
                       page.query_selector('input[inputmode="numeric"]'):
                        is_email_flow = True
                _log(f"      [B5] 2FA 路径: email={is_email_flow} push={is_push_flow}")

                if is_push_flow:
                    _log("*" * 60)
                    _log("      [B5] ⚠️  请在手机 PayPal app 里点击确认")
                    _log("*" * 60)
                    confirmed = False
                    for _pm_wait in range(150):
                        time.sleep(2)
                        if "/authflow" not in page.url:
                            confirmed = True
                            _log(f"      [B5] ✅ 手机确认完成 → {page.url[:80]}")
                            break
                    if not confirmed:
                        _safe_screenshot(page, "/tmp/paypal_push_timeout.png")
                        raise RuntimeError("PayPal 手机推送 5 分钟未确认")
                else:
                    # 邮件 OTP 模式：等 IMAP 收码（3 分钟，PayPal 有时要 2 分钟才发）
                    _log("      [B5] 等待 PayPal 邮件 OTP (最长 180s) ...")
                    otp = _fetch_paypal_otp(paypal_cfg, timeout=180)
                    if not otp:
                        _log("      [B5] OTP 首次超时，重发 ...")
                        for sel in ['button:has-text("Resend")', 'button:has-text("重新发送")',
                                    'a:has-text("Resend")', 'button:has-text("Send again")']:
                            btn = page.query_selector(sel)
                            if btn and btn.is_visible():
                                btn.click()
                                _log(f"      [B5] 重发 OTP: {sel}")
                                break
                        time.sleep(3)
                        otp = _fetch_paypal_otp(paypal_cfg, timeout=120)
                    if not otp:
                        _safe_screenshot(page, "/tmp/paypal_2fa_timeout.png")
                        raise RuntimeError("PayPal 2FA 邮件 OTP 获取超时")
                    _log(f"      [B5] OTP: {otp}")
                    otp_filled = False
                    for sel in ['input[name="otpCode"]', 'input[autocomplete="one-time-code"]',
                                'input[inputmode="numeric"]', 'input[name="answer"]',
                                'input[type="tel"]',
                                'input[maxlength="6"]', 'input[class*="otp"]']:
                        otp_input = page.query_selector(sel)
                        if otp_input and otp_input.is_visible():
                            otp_input.fill(otp)
                            otp_filled = True
                            _log(f"      [B5] OTP 已填入: {sel}")
                            break
                    if not otp_filled:
                        digit_inputs = page.query_selector_all('input[maxlength="1"]')
                        if len(digit_inputs) >= 6:
                            for i, ch in enumerate(otp[:6]):
                                digit_inputs[i].fill(ch)
                            otp_filled = True
                            _log("      [B5] OTP 已逐位填入")
                    time.sleep(1)
                    for sel in ['button[type="submit"]', 'button:has-text("确认")',
                                'button:has-text("Confirm")', 'button:has-text("Continue")',
                                'button:has-text("Next")']:
                        btn = page.query_selector(sel)
                        if btn and btn.is_visible():
                            btn.click()
                            _log(f"      [B5] 点击确认 OTP: {sel}")
                            break
                    time.sleep(5)
                _log(f"      [B5] 2FA 完成，当前 URL: {page.url[:80]}")

            # [B6] 等待到达 consent 页面 / hermes
            _log("      [B6] 等待授权页面 ...")
            for wait_i in range(30):
                cur = page.url
                if "/webapps/hermes" in cur or "checkoutweb" in cur:
                    _log(f"      [B6] 到达授权页: {cur[:80]}")
                    break
                if "chatgpt.com" in cur or "pm-redirects" in cur:
                    _log(f"      [B6] 已完成: {cur[:80]}")
                    success = True
                    break
                # B6 原地转：check 是否卡在 DataDome 可见滑块
                if wait_i >= 5 and "/agreements/approve" in cur:
                    ddc_frame_now = any(("ddc" in (f.url or "") or
                                           "captcha" in (f.url or "") or
                                           "datadome" in (f.url or ""))
                                          for f in page.frames if f.url != cur)
                    if _slider_visible() or (wait_i >= 15 and ddc_frame_now):
                        _safe_screenshot(page, "/tmp/paypal_ddc_slider.png")
                        reason = "关键字匹配" if _slider_visible() else "agreements 原地转+DDC iframe"
                        _log(f"      [B6] 检到可见滑块 ({reason})，尝试 drag solver ...")
                        if _try_solve_ddc_slider(attempts=2):
                            _log("      [B6] drag solver 成功，继续等 hermes")
                            continue
                        _log("      [B6] drag solver 失败，发 marker 交给外层")
                        _log("CARD_DATADOME_SLIDER=1")
                        raise RuntimeError("DataDome 滑块 solver 失败")
                if wait_i == 15:
                    _safe_screenshot(page, "/tmp/paypal_b6_wait.png")
                    _log(f"      [B6-diag] 15s URL: {cur[:100]}")
                    _log(f"      [B6-diag] 截图: /tmp/paypal_b6_wait.png")
                time.sleep(1)

            if not success:
                # [B7] 到达 hermes 页面 — 提取参数，用纯 HTTP 完成 authorize + return
                _log("      [B7] 到达 hermes，提取授权参数 ...")
                hermes_html = page.content()
                hermes_url = page.url
                # 提取 cookies 供 HTTP 使用
                browser_cookies = ctx.cookies()
                http_finish = requests.Session()
                http_finish.headers.update({
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                })
                try:
                    http_finish.trust_env = False
                except Exception:
                    pass
                if proxy_url:
                    _apply_proxy_to_http_session(http_finish, proxy_url)
                for c in browser_cookies:
                    if "paypal.com" in c.get("domain", ""):
                        http_finish.cookies.set(
                            c["name"], c["value"],
                            domain=c.get("domain", ".paypal.com"),
                            path=c.get("path", "/"),
                        )
                _log(f"      [B7] 提取了 {len(browser_cookies)} 个 cookies")

                # 提取 fundingOptionId + EC token
                funding_m = re.search(r'"fundingOptionId"\s*:\s*"([^"]+)"', hermes_html)
                funding_id = funding_m.group(1) if funding_m else ""
                ec_token = ""
                m = re.search(r'(EC-[A-Z0-9]{17,})', hermes_html)
                if m:
                    ec_token = m.group(1)
                if not ec_token:
                    ec_token = urllib.parse.parse_qs(
                        urllib.parse.urlparse(hermes_url).query
                    ).get("token", [""])[0]
                ba_token_h = urllib.parse.parse_qs(
                    urllib.parse.urlparse(hermes_url).query
                ).get("ba_token", [""])[0]
                _log(f"      [B7] funding={funding_id} ec={ec_token} ba={ba_token_h}")

                if funding_id and ec_token:
                    # [B8] GraphQL authorize
                    _log("      [B8] GraphQL authorize ...")
                    gql = [{
                        "operationName": "authorize",
                        "variables": {
                            "billingAgreementId": ec_token,
                            "fundingPreference": {
                                "fundingOptionId": funding_id,
                                "balancePreference": "OPT_OUT",
                            },
                            "legalAgreements": {},
                        },
                        "query": (
                            "mutation authorize("
                            "$billingAgreementId: String!, $addressId: String, "
                            "$fundingPreference: billingFundingPreferenceInput, "
                            "$legalAgreements: billingLegalAgreementsInput"
                            ") { billing { authorize( "
                            "billingAgreementId: $billingAgreementId "
                            "addressId: $addressId "
                            "fundingPreference: $fundingPreference "
                            "legalAgreements: $legalAgreements "
                            ") { billingAgreementToken paymentAction "
                            "returnURL { href __typename } "
                            "buyer { userId __typename } __typename } __typename } }"
                        ),
                    }]
                    resp_gql = http_finish.post(
                        "https://www.paypal.com/graphql/", json=gql,
                        headers={
                            "Content-Type": "application/json",
                            "X-Requested-With": "fetch",
                            "X-App-Name": "checkoutuinodeweb",
                            "Origin": "https://www.paypal.com",
                            "Referer": hermes_url,
                        }, timeout=30,
                    )
                    _log(f"      [B8] GraphQL status={resp_gql.status_code}")
                    if resp_gql.status_code == 200:
                        try:
                            ret_url = resp_gql.json()[0]["data"]["billing"]["authorize"]["returnURL"]["href"]
                            _log(f"      [B8] returnURL: {ret_url[:200]}")
                            # 确保 returnURL 有完整参数
                            if "status=" not in ret_url:
                                sep = "&" if "?" in ret_url else "?"
                                ret_url += f"{sep}status=success"
                            if "ba_token=" not in ret_url and ba_token_h:
                                ret_url += f"&ba_token={ba_token_h}"
                            _log(f"      [B8] 完整 returnURL: {ret_url[:200]}")
                            # [B9] 用浏览器导航到 returnURL（保留完整 session 上下文）
                            _log("      [B9] 浏览器导航到 returnURL ...")
                            page.goto(ret_url, wait_until="domcontentloaded", timeout=30000)
                            _log(f"      [B9] 最终 URL: {page.url[:120]}")
                            for _ in range(15):
                                if "chatgpt.com" in page.url or "redirect_status=succeeded" in page.url:
                                    break
                                time.sleep(1)
                            _log(f"      [B9] Stripe 回调完成: {page.url[:120]}")
                            success = True
                        except Exception as e:
                            _log(f"      [B8] GraphQL 解析失败: {e}")
                            _log(f"      [B8] 响应: {resp_gql.text[:300]}")
                    else:
                        _log(f"      [B8] GraphQL 失败: {resp_gql.text[:300]}")
                else:
                    # 监听网络请求（捕获 pm-redirects return URL）
                    captured_return_url = []
                    def _on_request(request):
                        if "pm-redirects" in request.url and "/return/" in request.url:
                            captured_return_url.append(request.url)
                            _log(f"      [B-NET] 捕获 pm-redirects return: {request.url[:150]}")
                    page.on("request", _on_request)

                    _log("      [B7] 通过浏览器点击 consent 按钮 ...")
                    for sel in ['button#consentButton', 'button:has-text("Agree")',
                                'button:has-text("同意并继续")', 'button[type="submit"]']:
                        btn = page.query_selector(sel)
                        if btn and btn.is_visible():
                            btn.click()
                            _log(f"      [B7] 已点击: {sel}")
                            break
                    # 等待完整重定向链
                    _log("      [B8] 等待 PayPal → Stripe 重定向链 ...")
                    for wait_b8 in range(90):
                        cur = page.url
                        if "chatgpt.com" in cur or ("stripe.com" in cur and "redirect_status" in cur):
                            _log(f"      [B8] 完成: {cur[:120]}")
                            success = True
                            break
                        if wait_b8 == 30:
                            _log(f"      [B8-diag] 30s: {cur[:80]}")
                        time.sleep(1)
                    page.remove_listener("request", _on_request)
                    if captured_return_url:
                        _log(f"      [B8] 捕获到 {len(captured_return_url)} 个 pm-redirects 请求")
                    else:
                        _log("      [B8] 警告: 未捕获到 pm-redirects 请求")

        except Exception as e:
            _log(f"      [Browser] 异常: {e}")
            # 截图保存
            try:
                _safe_screenshot(page, "/tmp/paypal_browser_error.png")
                _log("      [Browser] 错误截图: /tmp/paypal_browser_error.png")
            except Exception:
                pass
            raise

        # 保存 PayPal cookies 供后续纯 HTTP 模式复用
        if success:
            try:
                all_cookies = ctx.cookies()
                pp_cookies = [c for c in all_cookies if "paypal.com" in (c.get("domain", ""))]
                if pp_cookies:
                    cookies_str = "; ".join(f"{c['name']}={c['value']}" for c in pp_cookies)
                    import json as _json_save, datetime as _dt_save
                    with open("/tmp/paypal_browser_cookies.json", "w") as _cf:
                        _json_save.dump({
                            "cookies_str": cookies_str,
                            "ts": _dt_save.datetime.now().isoformat(),
                            "email": paypal_email,
                        }, _cf)
                    _log(f"      [Browser] PayPal cookies 已保存 ({len(pp_cookies)} 个)")
            except Exception as e_save:
                _log(f"      [Browser] cookies 保存失败: {e_save}")

    # 持久化 profile 保留，下次运行复用（trusted device 生效）
    if success:
        _log("      [Browser] PayPal 浏览器授权成功!")
    return success


def _solve_hcaptcha_via_vlm(page, hcaptcha_frame, vlm_base_url, vlm_api_key, vlm_model, max_rounds=5):
    """在 Playwright 中使用 VLM 求解 hCaptcha 视觉挑战"""
    import base64
    for round_idx in range(max_rounds):
        _log(f"      [VLM-hCaptcha] 第 {round_idx + 1}/{max_rounds} 轮 ...")

        # 截图 hCaptcha 区域
        try:
            screenshot_bytes = hcaptcha_frame.locator("body").screenshot(timeout=10000)
        except Exception:
            screenshot_bytes = page.screenshot()
        b64_img = base64.b64encode(screenshot_bytes).decode()

        # 提取 prompt（挑战文字说明）
        prompt_text = ""
        try:
            prompt_el = hcaptcha_frame.query_selector(".prompt-text") or \
                        hcaptcha_frame.query_selector("[class*='prompt']")
            if prompt_el:
                prompt_text = prompt_el.inner_text()
        except Exception:
            pass
        _log(f"      [VLM-hCaptcha] prompt: {prompt_text[:60]}...")

        # 调用 VLM
        vlm_prompt = (
            f"This is a hCaptcha visual challenge screenshot. "
            f"The challenge says: '{prompt_text}'. "
            f"The image shows a 3x3 grid of tiles numbered 1-9 (left to right, top to bottom). "
            f"Which tiles match the challenge? Return ONLY a JSON: {{\"tiles\": [1, 3, 5]}} "
            f"where the numbers are the matching tile positions."
        )
        try:
            vlm_resp = requests.post(
                f"{vlm_base_url}/v1/chat/completions",
                json={
                    "model": vlm_model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": vlm_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}},
                    ]}],
                    "max_tokens": 200,
                },
                headers={"Authorization": f"Bearer {vlm_api_key}"},
                timeout=45,
            )
            vlm_json = vlm_resp.json()
            if "error" in vlm_json:
                _log(f"      [VLM-hCaptcha] VLM 错误: {str(vlm_json['error'])[:200]}")
                continue
            if "choices" not in vlm_json:
                _log(f"      [VLM-hCaptcha] VLM 异常响应: {str(vlm_json)[:200]}")
                continue
            vlm_text = vlm_json["choices"][0]["message"]["content"]
            _log(f"      [VLM-hCaptcha] VLM 回复: {vlm_text[:100]}")

            # 解析 tiles
            m = re.search(r'\{[^}]*"tiles"\s*:\s*\[([0-9, ]+)\]', vlm_text)
            if m:
                tiles = [int(x.strip()) for x in m.group(1).split(",") if x.strip().isdigit()]
            else:
                # 兜底: 提取所有数字
                tiles = [int(x) for x in re.findall(r'\b([1-9])\b', vlm_text)]
            _log(f"      [VLM-hCaptcha] 选择 tiles: {tiles}")

            if not tiles:
                _log("      [VLM-hCaptcha] 无有效 tiles")
                continue

        except Exception as e:
            _log(f"      [VLM-hCaptcha] VLM 调用失败: {e}")
            continue

        # 点击对应的 tiles
        task_images = hcaptcha_frame.query_selector_all(".task-image") or \
                      hcaptcha_frame.query_selector_all("[class*='image']") or \
                      hcaptcha_frame.query_selector_all(".border-focus")
        _log(f"      [VLM-hCaptcha] 找到 {len(task_images)} 个 tile 元素")

        for tile_num in tiles:
            idx = tile_num - 1  # 1-based to 0-based
            if 0 <= idx < len(task_images):
                task_images[idx].click()
                time.sleep(random.uniform(0.3, 0.8))

        # 点击 verify/submit
        time.sleep(0.5)
        verify_btn = hcaptcha_frame.query_selector('button.verify-button') or \
                     hcaptcha_frame.query_selector('div.button-submit') or \
                     hcaptcha_frame.query_selector('[class*="submit"]')
        if verify_btn:
            verify_btn.click()
            _log("      [VLM-hCaptcha] 已点击验证按钮")

        time.sleep(3)

        # 检查是否通过
        still_has_captcha = False
        for frame in page.frames:
            if "hcaptcha" in frame.url:
                # 检查是否还有 challenge
                challenge = frame.query_selector(".challenge-container") or \
                            frame.query_selector("[class*='challenge']")
                if challenge and challenge.is_visible():
                    still_has_captcha = True
                break
        if not still_has_captcha:
            _log("      [VLM-hCaptcha] hCaptcha 已通过!")
            return True
        _log("      [VLM-hCaptcha] 未通过，重试 ...")

    return False


def _handle_paypal_redirect(
    redirect_url: str,
    paypal_cfg: dict,
    locale_profile: dict = None,
    ctx: dict = None,
) -> bool:
    """纯 HTTP 完成 PayPal 授权。
    支持两种路径：
      1. Cookied Login (ud-token) — 需要 paypal.cookies
      2. Full Login (邮箱→密码→hCaptcha→2FA) — 需要 email/password/imap
    """
    ctx = ctx or {}
    proxy_url = str(ctx.get("proxy_url") or "").strip()
    captcha_api_key = ctx.get("captcha_api_key", "")
    paypal_cookies_str = paypal_cfg.get("cookies", "")
    paypal_email = paypal_cfg.get("email", "")
    paypal_password = paypal_cfg.get("password", "")
    ud_return_url = ""

    # 尝试加载浏览器保存的 PayPal cookies
    if not paypal_cookies_str:
        try:
            import json as _json
            with open("/tmp/paypal_browser_cookies.json", "r") as _cf:
                saved = _json.load(_cf)
            saved_cookies = saved.get("cookies_str", "")
            if saved_cookies:
                paypal_cookies_str = saved_cookies
                _log(f"      [PayPal] 复用浏览器保存的 cookies ({saved.get('email', '?')})")
        except Exception:
            pass

    # ── 创建 HTTP session (curl_cffi Chrome 指纹) ──
    try:
        from curl_cffi.requests import Session as CffiSession
        http = CffiSession(impersonate="chrome136")
        _log("      [PayPal] 使用 curl_cffi (chrome136 TLS 指纹)")
    except ImportError:
        http = requests.Session()
        _log("      [PayPal] curl_cffi 不可用，使用 requests (TLS 指纹暴露风险)")
    http.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "sec-ch-ua": '"Chromium";v="146", "Google Chrome";v="146", "Not=A?Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
    })
    try:
        http.trust_env = False
    except Exception:
        pass
    if proxy_url:
        _apply_proxy_to_http_session(http, proxy_url)
    if paypal_cookies_str:
        for pair in paypal_cookies_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                http.cookies.set(k.strip(), v.strip(), domain=".paypal.com", path="/")

    # [1] 跟随 Stripe redirect → PayPal /agreements/approve
    # 默认跳过 hermes 纯 HTTP 路径——PayPal 对非浏览器 session 返回 genericError(DEFAULT)，
    # 实测 2026-04 近期所有 daemon 日志 hermes 100% 失败（55+ 次全 fallback），每次浪费 5-10s。
    # 如需保留旧路径作为逆向参考，设 SKIP_HERMES_FAST_PATH=0。
    if str(os.environ.get("SKIP_HERMES_FAST_PATH", "1")).lower() in ("1", "true", "yes", "on"):
        if paypal_email and paypal_password:
            _log("      [1] SKIP_HERMES_FAST_PATH=1，直接走浏览器模式")
            return _paypal_browser_authorize(
                redirect_url, paypal_cfg,
                captcha_api_key=captcha_api_key, proxy_url=proxy_url,
            )
    # 如果有有效 cookies，尝试纯 HTTP 路径；否则直接走浏览器（跳过必失败的 HTTP）
    if not paypal_cookies_str and paypal_email and paypal_password:
        _log("      [1] 无 PayPal cookies，直接走浏览器模式（跳过 HTTP）")
        return _paypal_browser_authorize(
            redirect_url, paypal_cfg,
            captcha_api_key=captcha_api_key, proxy_url=proxy_url,
        )
    _log("      [1] 跟随 Stripe redirect → PayPal ...")
    resp1 = http.get(redirect_url, allow_redirects=True, timeout=30)
    _log(f"      [1] 到达: {resp1.url[:120]}  status={resp1.status_code}")
    if resp1.status_code == 403:
        _log("      [1] 403 被拦截，走浏览器模式")
        return _paypal_browser_authorize(
            redirect_url, paypal_cfg,
            captcha_api_key=captcha_api_key, proxy_url=proxy_url,
        )
    html = resp1.text
    ba_token = urllib.parse.parse_qs(
        urllib.parse.urlparse(resp1.url).query
    ).get("ba_token", [""])[0]

    # 提取页面参数
    csrf = ""
    for pat in [r'name="_csrf"\s+value="([^"]+)"',
                r'"csrfNonce"\s*:\s*"([^"]+)"',
                r'"token"\s*:\s*"([^"]{20,})"']:
        m = re.search(pat, html)
        if m:
            csrf = m.group(1)
            break
    sid = ""
    for pat in [r'_sessionID.*?value="([^"]+)"', r'"_sessionID"\s*:\s*"([^"]+)"']:
        m = re.search(pat, html)
        if m:
            sid = m.group(1)
            break
    ctx_id = ""
    m = re.search(r'"ctxId"\s*:\s*"([^"]+)"', html)
    if m:
        ctx_id = m.group(1)
    flow_id = ctx_id
    m = re.search(r'"flowId"\s*:\s*"([^"]+)"', html)
    if m:
        flow_id = m.group(1)
    recaptcha_key = ""
    for rk_pat in [r'"fppAPIKey"\s*:\s*"([^"]+)"',
                   r'recaptcha[^"]*?key[^"]*?["\']\s*:\s*["\']([^"\']{20,})',
                   r'enterpriseKey["\']?\s*:\s*["\']([^"\']+)',
                   r'render/([A-Za-z0-9_-]{30,})\?']:
        m = re.search(rk_pat, html, re.I)
        if m:
            recaptcha_key = m.group(1)
            break
    _log(f"      [1] csrf={csrf[:20]}... ba_token={ba_token} reCAPTCHA_key={'yes' if recaptcha_key else 'no'}")

    # ── 判断登录状态 ──
    at_hermes = "/webapps/hermes" in resp1.url
    logged_in = at_hermes

    if not at_hermes and paypal_cookies_str:
        # 尝试 ud-token 快速登录
        _log("      [2-UD] 尝试 cookied login ...")
        ud_data = {
            "_csrf": csrf, "_sessionID": sid, "intent": "checkout",
            "ctxId": ctx_id, "flowId": flow_id,
            "returnUri": "/webapps/hermes", "locale.x": "zh_XC",
            "state": urllib.parse.urlparse(resp1.url).query,
            "fn_sync_data": "",
        }
        resp_ud = http.post(
            "https://www.paypal.com/signin/ud-token", data=ud_data,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://www.paypal.com",
                "Referer": resp1.url,
            }, timeout=30,
        )
        _log(f"      [2-UD] ud-token status={resp_ud.status_code}")
        if resp_ud.status_code == 200:
            try:
                ud_json = resp_ud.json()
                if ud_json.get("returnUrl") or ud_json.get("email"):
                    logged_in = True
                    ud_return_url = ud_json.get("returnUrl", "")
                    _log(f"      [2-UD] cookied login 成功 (returnUrl={'yes' if ud_return_url else 'no'})")
            except Exception:
                pass
        if not logged_in:
            _log("      [2-UD] cookied login 失败，回退到完整登录")

    if not logged_in:
        if not paypal_email or not paypal_password:
            raise RuntimeError(
                "PayPal 授权需要: (1) 有效 cookies 或 (2) email + password"
            )
        try:
            _paypal_full_login(
                http, html, resp1.url, paypal_cfg, captcha_api_key,
                csrf, sid, flow_id, ctx_id, recaptcha_key,
            )
        except Exception as e:
            _log(f"      纯 HTTP 登录失败: {e}")
            _log("      回退到浏览器模式 ...")
            return _paypal_browser_authorize(
                redirect_url, paypal_cfg,
                captcha_api_key=captcha_api_key,
                proxy_url=proxy_url,
            )

    # ── [H] GET hermes ──
    # 优先使用 ud-token 返回的 URL（包含正确的 EC token）
    # 构造 hermes URL（必须包含 ba_token + EC token）
    hermes_url = (
        f"https://www.paypal.com/webapps/hermes"
        f"?flow=1-P&ulReturn=true&ba_token={ba_token}"
    )
    if flow_id:
        hermes_url += f"&token={flow_id}"
    # ud-token returnUrl 可能包含额外参数（如 ssrt/rcache）
    if ud_return_url:
        _log(f"      [H] ud-token returnUrl: {ud_return_url[:100]}")
        if "ba_token=" in ud_return_url and "token=" in ud_return_url:
            hermes_url = f"https://www.paypal.com{ud_return_url}" if ud_return_url.startswith("/") else ud_return_url
    if at_hermes:
        hermes_html = html
        hermes_final_url = resp1.url
        _log("      [H] 已在 hermes 页面")
    else:
        _log("      [H] GET hermes ...")
        resp_h = http.get(hermes_url, timeout=30)
        hermes_html = resp_h.text
        hermes_final_url = resp_h.url
        _log(f"      [H] hermes status={resp_h.status_code} url={resp_h.url[:80]}")

    # 提取 fundingOptionId + EC token
    funding_m = re.search(r'"fundingOptionId"\s*:\s*"([^"]+)"', hermes_html)
    funding_id = funding_m.group(1) if funding_m else ""
    ec_token = urllib.parse.parse_qs(
        urllib.parse.urlparse(hermes_final_url).query
    ).get("token", [""])[0]
    if not ec_token:
        m = re.search(r'(EC-[A-Z0-9]{17,})', hermes_html)
        ec_token = m.group(1) if m else ""
    _log(f"      [H] fundingOptionId={funding_id}  ec={ec_token}")

    if not funding_id or not ec_token:
        _title = re.search(r'<title>(.*?)</title>', hermes_html)
        title_text = _title.group(1) if _title else "N/A"
        _log(f"      [H] hermes title: {title_text}")
        # hermes 失败（genericError / 需要重新登录）→ 回退到浏览器
        if paypal_email and paypal_password:
            _log("      [H] hermes 失败，回退到浏览器模式 ...")
            return _paypal_browser_authorize(
                redirect_url, paypal_cfg,
                captcha_api_key=captcha_api_key, proxy_url=proxy_url,
            )
        raise RuntimeError(
            f"hermes 参数缺失 (可能需要登录): funding={funding_id} ec={ec_token}"
        )

    # ── [G] GraphQL authorize ──
    _log("      [G] graphql authorize ...")
    gql = [{
        "operationName": "authorize",
        "variables": {
            "billingAgreementId": ec_token,
            "fundingPreference": {
                "fundingOptionId": funding_id,
                "balancePreference": "OPT_OUT",
            },
            "legalAgreements": {},
        },
        "query": (
            "mutation authorize("
            "$billingAgreementId: String!, $addressId: String, "
            "$fundingPreference: billingFundingPreferenceInput, "
            "$legalAgreements: billingLegalAgreementsInput"
            ") { billing { authorize( "
            "billingAgreementId: $billingAgreementId "
            "addressId: $addressId "
            "fundingPreference: $fundingPreference "
            "legalAgreements: $legalAgreements "
            ") { billingAgreementToken paymentAction "
            "returnURL { href __typename } "
            "buyer { userId __typename } __typename } __typename } }"
        ),
    }]
    resp_gql = http.post(
        "https://www.paypal.com/graphql/", json=gql,
        headers={
            "Content-Type": "application/json",
            "X-Requested-With": "fetch",
            "X-App-Name": "checkoutuinodeweb",
            "Origin": "https://www.paypal.com",
            "Referer": hermes_final_url,
        }, timeout=30,
    )
    _log(f"      [G] graphql status={resp_gql.status_code}")
    if resp_gql.status_code != 200:
        raise RuntimeError(
            f"graphql 失败: {resp_gql.status_code} {resp_gql.text[:300]}"
        )
    try:
        ret_url = resp_gql.json()[0]["data"]["billing"]["authorize"]["returnURL"]["href"]
    except Exception:
        raise RuntimeError(f"graphql 响应异常: {resp_gql.text[:500]}")
    _log(f"      [G] return URL: {ret_url[:100]}")

    # ── [R] GET return URL → Stripe 完成 ──
    _log("      [R] 回调 Stripe ...")
    resp_ret = http.get(ret_url, allow_redirects=True, timeout=30)
    _log(f"      [R] 最终: {resp_ret.url[:100]}  status={resp_ret.status_code}")
    _log("      PayPal 授权成功!")
    return True



def _fetch_paypal_otp(paypal_cfg: dict, timeout: int = 90) -> str:
    """从 CF KV 取 PayPal 发送的 2FA OTP。

    前提：PayPal 账户绑定的邮箱（`paypal_cfg["email"]`）已迁移到 catch-all
    域名，PayPal 发的 OTP 邮件就会落进 otp-relay Worker 写到 KV。
    若仍是 IMAP 邮箱（QQ 等），KV 里取不到，会超时返回空串。
    """
    target = (paypal_cfg.get("email") or "").strip()
    if not target:
        _log("      [PayPal OTP] 缺 paypal.email 配置")
        return ""
    try:
        from cf_kv_otp_provider import CloudflareKVOtpProvider
    except ImportError as e:
        _log(f"      [PayPal OTP] cf_kv_otp_provider 不可用: {e}")
        return ""
    try:
        provider = CloudflareKVOtpProvider.from_env_or_secrets()
        otp = provider.wait_for_otp(target, timeout=timeout)
        _log(f"      [PayPal OTP] 收到 {otp} (key={target})")
        return otp
    except TimeoutError:
        _log(f"      [PayPal OTP] CF KV 等 OTP 超时 {timeout}s key={target}")
        return ""
    except Exception as e:
        _log(f"      [PayPal OTP] CF KV 取异常: {e}")
        return ""


def confirm_payment(
    session: requests.Session,
    pk: str,
    session_id: str,
    pm_id: str,
    card: dict | None,
    captcha_token: str,
    init_resp: dict,
    stripe_ver: str = STRIPE_VERSION_BASE,
    captcha_cfg: dict = None,
    captcha_ekey: str = "",
    ctx: dict = None,
    locale_profile: dict = None,
) -> dict:
    ctx = ctx or {}
    locale_profile = locale_profile or LOCALE_PROFILES["US"]
    guid = ctx.get("guid") or _gen_fingerprint()[0]
    muid = ctx.get("muid") or _gen_fingerprint()[0]
    sid  = ctx.get("sid")  or _gen_fingerprint()[0]
    runtime_version = ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION
    locale_short = ctx.get("locale") or _locale_short(locale_profile)
    top_checkout_config_id = ctx.get("top_checkout_config_id") or ctx.get("config_id", "")
    elements_session_config_id = (
        ctx.get("elements_session_config_id")
        or str(uuid.uuid4())
    )
    confirm_mode = ctx.get("confirm_mode", "inline_payment_method_data")

    # 优先从 total_summary.due 获取金额（最准确）
    expected_amount = "0"
    total_summary = init_resp.get("total_summary", {})
    if total_summary.get("due") is not None:
        expected_amount = str(total_summary["due"])
    elif init_resp.get("invoice", {}).get("amount_due") is not None:
        expected_amount = str(init_resp["invoice"]["amount_due"])
    else:
        line_items = init_resp.get("line_items", [])
        if line_items:
            total = sum(item.get("amount", 0) for item in line_items)
            expected_amount = str(total)


    init_checksum = init_resp.get("init_checksum", "")
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    elements_session_id = ctx.get("elements_session_id", _gen_elements_session_id())
    stripe_hosted_url = (
        ctx.get("stripe_hosted_url")
        or init_resp.get("stripe_hosted_url")
        or ""
    )
    success_return_url = (
        ctx.get("return_url")
        or init_resp.get("return_url")
        or init_resp.get("url")
        or ""
    )
    checkout_url = stripe_hosted_url or success_return_url
    if stripe_hosted_url and success_return_url:
        parsed_hosted = urllib.parse.urlsplit(stripe_hosted_url)
        hosted_query = urllib.parse.urlencode(
            [
                ("returned_from_redirect", "true"),
                ("ui_mode", "custom"),
                ("return_url", success_return_url),
            ]
        )
        checkout_url = urllib.parse.urlunsplit(
            (
                parsed_hosted.scheme,
                parsed_hosted.netloc,
                parsed_hosted.path,
                hosted_query,
                parsed_hosted.fragment,
            )
        )


    ver = STRIPE_VERSION_FULL

    data = {
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "expected_amount": expected_amount,
        "expected_payment_method_type": ctx.get("payment_method_type", "card"),
        "key": pk,
        "_stripe_version": ver,
  
        "init_checksum": init_checksum,
     
        "version": runtime_version,
      
        "return_url": checkout_url,
    
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": locale_short,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[session_id]": elements_session_id,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
  
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": session_id,
        "client_attribution_metadata[checkout_config_id]": top_checkout_config_id,
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": elements_session_config_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
    }
    consent_behavior = ctx.get("include_terms_of_service_consent")
    if consent_behavior is None:
        consent_collection = init_resp.get("consent_collection", {}) or {}
        consent_behavior = consent_collection.get("terms_of_service") not in (None, "", "none")
    if consent_behavior:
        data["consent[terms_of_service]"] = "accepted"

    data.update(ctx.get("elements_options_client") or _elements_options_client_payload())

    if ctx.get("js_checksum"):
        data["js_checksum"] = ctx["js_checksum"]
    if ctx.get("rv_timestamp"):
        data["rv_timestamp"] = ctx["rv_timestamp"]

  
    if captcha_token:
        data["passive_captcha_token"] = captcha_token
    if captcha_ekey:
        data["passive_captcha_ekey"] = captcha_ekey

    if confirm_mode == "inline_payment_method_data":
        if not card:
            raise RuntimeError("inline confirm 模式缺少 card 数据")
        if not data.get("js_checksum") or not data.get("rv_timestamp"):
            raise RuntimeError("inline confirm 需要 runtime.js_checksum 与 runtime.rv_timestamp")
        data.update(_build_inline_payment_method_fields(card, session_id, ctx, runtime_version))
    else:
        if not pm_id:
            raise RuntimeError("shared_payment_method 模式缺少 payment_method")
        data["payment_method"] = pm_id
  

    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/confirm"
    _log("[5/6] 确认支付 (confirm) ...")
    _log_request("POST", url, data=data, tag="[5/6] confirm")
    resp = session.post(url, data=data, headers=_stripe_headers())
    _log_response(resp, tag="[5/6] confirm")
    if (
        resp.status_code == 400
        and "consent[terms_of_service]" not in data
        and "terms of service" in (resp.text or "").lower()
    ):
        _log("      confirm 提示需要接受 merchant terms of service，自动补 consent 后重试一次 ...")
        data["consent[terms_of_service]"] = "accepted"
        ctx["include_terms_of_service_consent"] = True
        _log_request("POST", url, data=data, tag="[5/6] confirm(retry_tos)")
        resp = session.post(url, data=data, headers=_stripe_headers())
        _log_response(resp, tag="[5/6] confirm(retry_tos)")
    if resp.status_code != 200:
        raise RuntimeError(f"confirm 失败 [{resp.status_code}]: {resp.text[:500]}")

    confirm_data = resp.json()

    # 从顶层、payment_intent 或 setup_intent 提取 next_action
    next_action = confirm_data.get("next_action")
    if not next_action:
        pi_obj = confirm_data.get("payment_intent")
        if pi_obj and isinstance(pi_obj, dict):
            next_action = pi_obj.get("next_action")
    if not next_action:
        seti = _find_setup_intent(confirm_data)
        if seti and isinstance(seti, dict):
            next_action = seti.get("next_action")

    if next_action and next_action.get("type") == "use_stripe_sdk":
        _log("      触发 3DS/challenge 验证，正在处理 ...")
        _handle_3ds(session, pk, confirm_data, captcha_token, stripe_ver, captcha_cfg,
                    locale_profile=locale_profile, ctx=ctx)

    return confirm_data


def _extract_terminal_payment_failure(intent_obj: dict, source_kind: str = "setup_intent") -> dict | None:
    """把 Stripe 已经明确给出的终态失败对象标准化，避免继续误判成“要重试/继续轮询”."""
    if not isinstance(intent_obj, dict):
        return None

    status = intent_obj.get("status", "")
    error = intent_obj.get("last_setup_error") or intent_obj.get("last_payment_error") or {}
    if status != "requires_payment_method" or not error:
        return None

    err_code = (error.get("code") or "").lower()
    err_msg = (error.get("message") or "").lower()
    if "captcha" in err_msg or "authentication_failure" in err_code:
        return None

    return {
        "state": "failed",
        "payment_object_status": status,
        "source_kind": source_kind,
        "error": error,
        source_kind: intent_obj,
    }


def _find_setup_intent(data: dict) -> dict | None:
    si = data.get("setup_intent")
    if si:
        return si
    pm_obj = data.get("payment_method_object")
    if pm_obj and isinstance(pm_obj, dict):
        return pm_obj.get("setup_intent")
    raw = json.dumps(data)
    m = re.search(r"seti_[A-Za-z0-9]+", raw)
    if m:
        return {"id": m.group(0)}
    return None


def _build_3ds_browser_payload(locale_profile: dict, ctx: dict) -> dict:
    return {
        "fingerprintAttempted": False,
        "fingerprintData": None,
        "challengeWindowSize": None,
        "threeDSCompInd": "Y",
        "browserJavaEnabled": False,
        "browserJavascriptEnabled": True,
        "browserLanguage": locale_profile.get("browser_language", "en-US"),
        "browserColorDepth": str(locale_profile.get("color_depth", 24)),
        "browserScreenHeight": str(locale_profile.get("screen_h", 1080)),
        "browserScreenWidth": str(locale_profile.get("screen_w", 1920)),
        "browserTZ": str(_browser_tz_offset(locale_profile)),
        "browserUserAgent": USER_AGENT,
    }


def _handle_3ds(
    session: requests.Session,
    pk: str,
    confirm_data: dict,
    captcha_token: str,
    stripe_ver: str = STRIPE_VERSION_BASE,
    captcha_cfg: dict = None,
    locale_profile: dict = None,
    ctx: dict = None,
):
    """处理 3DS2 认证流程 (模拟浏览器: captcha → verify_challenge → Apata指纹 → 3ds2/authenticate)"""
    locale_profile = locale_profile or LOCALE_PROFILES["US"]
    ctx = ctx or {}
    browser_challenge_cfg = ctx.get("browser_challenge") or {}
    stage_proxy_cfg = ctx.get("stage_proxies") or {}
    raw = json.dumps(confirm_data)

    # 查找 setatt_ (直接在 confirm 响应中)
    source_match = re.search(r"(setatt_[A-Za-z0-9]+)", raw)
    source = source_match.group(1) if source_match else None
    _log(f"      3DS: setatt_ = {source}")

    # 查找 seti_ 和 client_secret
    seti_match = re.search(r"(seti_[A-Za-z0-9]+)", raw)
    seti_id = seti_match.group(1) if seti_match else None
    _log(f"      3DS: seti_id = {seti_id}")

    client_secret = None
    if seti_id:
        cs_match = re.search(rf"({re.escape(seti_id)}_secret_[A-Za-z0-9]+)", raw)
        if cs_match:
            client_secret = cs_match.group(1)
    _log(f"      3DS: client_secret = {client_secret[:40] + '...' if client_secret else None}")


    challenge_site_key = None
    challenge_rqdata = ""
    challenge_verify_url = None
    intent_id = None
    intent_client_secret = None

    # 优先从 payment_intent 提取 challenge 信息
    pi_obj = confirm_data.get("payment_intent")
    if pi_obj and isinstance(pi_obj, dict):
        intent_id = pi_obj.get("id")
        intent_client_secret = pi_obj.get("client_secret")
        na = pi_obj.get("next_action", {})
        sdk_info = na.get("use_stripe_sdk", {})
        stripe_js = sdk_info.get("stripe_js", {})
        if stripe_js.get("site_key"):
            challenge_site_key = stripe_js["site_key"]
            challenge_rqdata = stripe_js.get("rqdata", "")
            challenge_verify_url = stripe_js.get("verification_url", "")
            _log(f"      检测到 payment_intent confirmation challenge (site_key: {challenge_site_key[:20]}...)")

    # 如果 payment_intent 没有，再从 setup_intent 提取
    if not challenge_site_key:
        seti_obj = _find_setup_intent(confirm_data)
        if seti_obj and isinstance(seti_obj, dict):
            na = seti_obj.get("next_action", {})
            sdk_info = na.get("use_stripe_sdk", {})
            stripe_js = sdk_info.get("stripe_js", {})
            if stripe_js.get("site_key"):
                challenge_site_key = stripe_js["site_key"]
                challenge_rqdata = stripe_js.get("rqdata", "")
                challenge_verify_url = stripe_js.get("verification_url", "")
                _log(f"      检测到 setup_intent confirmation challenge (site_key: {challenge_site_key[:20]}...)")

    # 用于 verify_challenge 的 intent 标识（兼容 pi_ 和 seti_）
    if not intent_id:
        intent_id = seti_id
    if not intent_client_secret:
        intent_client_secret = client_secret

    merchant_id = (
        confirm_data.get("account_settings", {}).get("account_id")
        or ctx.get("merchant_account_id")
        or ""
    )
    effective_browser_challenge_cfg = dict(browser_challenge_cfg or {})
    verify_browser_proxy = _resolve_stage_proxy_cfg(stage_proxy_cfg, "verify_challenge_browser")
    if verify_browser_proxy is not _PROXY_OVERRIDE_SENTINEL:
        effective_browser_challenge_cfg["proxy_url"] = _build_proxy_url_from_cfg(verify_browser_proxy)

    if challenge_site_key and intent_id and intent_client_secret and (captcha_cfg or browser_challenge_cfg.get("enabled")):
        # 构建 verify_challenge URL
        if challenge_verify_url and challenge_verify_url.startswith("/"):
            actual_verify_url = f"{STRIPE_API}{challenge_verify_url}"
        elif intent_id.startswith("pi_"):
            actual_verify_url = f"{STRIPE_API}/v1/payment_intents/{intent_id}/verify_challenge"
        else:
            actual_verify_url = f"{STRIPE_API}/v1/setup_intents/{intent_id}/verify_challenge"

        challenge_hcaptcha_cfg = {
            "site_key": challenge_site_key,
            "rqdata": challenge_rqdata,
            "is_invisible": False,
            "website_url": _build_stripe_hcaptcha_url(invisible=False),
        }

        _log("      解 challenge captcha ...")
        browser_verify_result = None
        verify_form_base = {
            "client_secret": intent_client_secret,
            "captcha_vendor_name": "hcaptcha",
            "key": pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        }
        if browser_challenge_cfg.get("enabled"):
            effective_browser_challenge_cfg = dict(effective_browser_challenge_cfg or {})
            if not effective_browser_challenge_cfg.get("external_solver"):
                bundled_solver = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "hcaptcha_auto_solver.py",
                )
                python_candidates = [
                    str(os.environ.get("CTFML_PYTHON") or "").strip(),
                    "~/.venvs/ctfml/bin/python",
                    sys.executable,
                ]
                solver_python = next((p for p in python_candidates if p and os.path.exists(p)), sys.executable)
                auto_vlm_cfg = {
                    "enabled": True,
                    "model": "gpt-5.4",
                    "base_url": "https://YOUR_VLM_ENDPOINT/api",
                    "api_key": "",
                    "timeout_s": 45,
                }
                effective_browser_challenge_cfg["external_solver"] = {
                    "enabled": True,
                    "python": solver_python,
                    "script": bundled_solver,
                    "out_dir": "/tmp/hcaptcha_auto_solver_live",
                    "timeout_s": max(180, int(effective_browser_challenge_cfg.get("timeout_ms", 300000) / 1000)),
                    "headed": not bool(effective_browser_challenge_cfg.get("headless", False)),
                    "vlm": auto_vlm_cfg,
                }
            challenge_token, challenge_ekey, browser_verify_result = solve_stripe_hcaptcha_in_browser(
                challenge_hcaptcha_cfg,
                merchant_id=merchant_id,
                locale=locale_profile.get("browser_locale", "en-US"),
                browser_cfg=effective_browser_challenge_cfg,
                verify_url=actual_verify_url,
                verify_form_base=verify_form_base,
            )
        else:
            challenge_token, challenge_ekey = solve_hcaptcha(
                captcha_cfg,
                challenge_hcaptcha_cfg,
                max_retries=3,
                session=session,
            )

        _log(f"      {_describe_challenge_artifact('challenge_response_token', challenge_token)}")
        if challenge_ekey:
            _log(f"      {_describe_challenge_artifact('challenge_response_ekey', challenge_ekey)}")
        else:
            _log("      challenge_response_ekey: <empty>")
        _log(f"      verify_challenge ({intent_id[:30]}...) ...")
        verify_data = {
            **verify_form_base,
            "challenge_response_token": challenge_token,
        }
        if challenge_ekey:
            verify_data["challenge_response_ekey"] = challenge_ekey

        verify_status_code = 0
        verify_text = ""
        if browser_verify_result and int(browser_verify_result.get("status") or 0):
            verify_status_code = int(browser_verify_result.get("status") or 0)
            verify_text = str(browser_verify_result.get("text") or "")
            _log_request("POST", actual_verify_url, data=verify_data, tag="[5/6] verify_challenge(browser)")
            _log(f"      使用浏览器内 verify_challenge 响应，跳过 Python requests verify")
        else:
            _log_request("POST", actual_verify_url, data=verify_data, tag="[5/6] verify_challenge")
            with _http_session_stage_proxy(session, stage_proxy_cfg, "verify_challenge"):
                resp = session.post(actual_verify_url, data=verify_data, headers=_stripe_headers())
            _log_response(resp, tag="[5/6] verify_challenge")
            verify_status_code = resp.status_code
            verify_text = resp.text

        if verify_status_code != 200:
            err_text = verify_text[:300]
            _log(f"      verify_challenge 返回 {verify_status_code}: {err_text}")
            if "no valid challenge" in err_text.lower():
                raise ChallengeReconfirmRequired(
                    f"challenge 已失效 (Stripe 返回 {verify_status_code}), 需要重新 confirm 获取新的 challenge"
                )
            raise RuntimeError(f"verify_challenge 失败 [{verify_status_code}]: {err_text}")

        verify_result = (
            browser_verify_result.get("json")
            if browser_verify_result and isinstance(browser_verify_result.get("json"), dict)
            else json.loads(verify_text)
        )
        verify_status = verify_result.get("status", "unknown")
        _log(f"      verify_challenge 状态: {verify_status}")

        # 检测 captcha challenge 失败（payment_intent 用 last_payment_error，setup_intent 用 last_setup_error）
        payment_error = verify_result.get("last_payment_error", {})
        setup_error = verify_result.get("last_setup_error", {})
        error_to_check = payment_error if payment_error else setup_error
        if error_to_check:
            err_code = error_to_check.get("code", "")
            err_msg = error_to_check.get("message", "")
            err_decline = error_to_check.get("decline_code", "")
            _log(
                "      verify_challenge error: "
                f"code={err_code or '-'} decline_code={err_decline or '-'} msg={err_msg or '-'}"
            )
            if "captcha" in err_msg.lower() or "authentication_failure" in err_code:
                raise ChallengeReconfirmRequired(
                    f"challenge captcha 被 Stripe 拒绝: [{err_code}] {err_msg}"
                )

        source_kind = "payment_intent" if intent_id.startswith("pi_") else "setup_intent"
        terminal_failure = _extract_terminal_payment_failure(verify_result, source_kind=source_kind)
        if terminal_failure:
            ctx["terminal_result"] = terminal_failure
            err = terminal_failure.get("error", {})
            _log(
                "      verify_challenge 已落到终态失败: "
                f"[{err.get('code', '?')}] {err.get('decline_code', '')} {err.get('message', '')}".strip()
            )
            return

        if verify_status == "requires_payment_method":
            raise ChallengeReconfirmRequired(
                "verify_challenge 后 setup_intent 进入 requires_payment_method，需要重新 confirm 获取新的 challenge"
            )

        # verify 成功, 从响应中提取 setatt_
        verify_raw = json.dumps(verify_result)
        new_source = re.search(r"(setatt_[A-Za-z0-9]+)", verify_raw)
        if new_source:
            source = new_source.group(1)
            _log(f"      从 verify 响应中获取 setatt_: {source[:30]}...")

    elif seti_id and client_secret and not source:
        # 没有 challenge 但也没有 setatt_, 尝试原始 verify_challenge
        verify_url = f"{STRIPE_API}/v1/setup_intents/{seti_id}/verify_challenge"
        _log(f"      verify_challenge (seti: {seti_id[:30]}...) ...")
        verify_data = {
            "client_secret": client_secret,
            "challenge_response_token": captcha_token,
            "captcha_vendor_name": "hcaptcha",
            "key": pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        }
        if browser_challenge_cfg.get("enabled"):
            fallback_hcaptcha_cfg = {
                "site_key": challenge_site_key or HCAPTCHA_SITE_KEY_FALLBACK,
                "rqdata": challenge_rqdata,
                "is_invisible": False,
                "website_url": _build_stripe_hcaptcha_url(invisible=False),
            }
            challenge_token, challenge_ekey, _ = solve_stripe_hcaptcha_in_browser(
                fallback_hcaptcha_cfg,
                merchant_id=merchant_id,
                locale=locale_profile.get("browser_locale", "en-US"),
                browser_cfg=browser_challenge_cfg,
            )
            verify_data["challenge_response_token"] = challenge_token
            if challenge_ekey:
                verify_data["challenge_response_ekey"] = challenge_ekey
        elif captcha_cfg:
            fallback_hcaptcha_cfg = {
                "site_key": challenge_site_key or HCAPTCHA_SITE_KEY_FALLBACK,
                "rqdata": challenge_rqdata,
                "is_invisible": False,
                "website_url": _build_stripe_hcaptcha_url(invisible=False),
            }
            challenge_token, challenge_ekey = solve_hcaptcha(
                captcha_cfg,
                fallback_hcaptcha_cfg,
                max_retries=3,
                session=session,
            )
            verify_data["challenge_response_token"] = challenge_token
            if challenge_ekey:
                verify_data["challenge_response_ekey"] = challenge_ekey
        _log(f"      {_describe_challenge_artifact('challenge_response_token', verify_data.get('challenge_response_token', ''))}")
        if verify_data.get("challenge_response_ekey"):
            _log(f"      {_describe_challenge_artifact('challenge_response_ekey', verify_data['challenge_response_ekey'])}")
        else:
            _log("      challenge_response_ekey: <empty>")
        _log_request("POST", verify_url, data=verify_data, tag="[5/6] verify_challenge(fallback)")
        with _http_session_stage_proxy(session, stage_proxy_cfg, "verify_challenge"):
            resp = session.post(verify_url, data=verify_data, headers=_stripe_headers())
        _log_response(resp, tag="[5/6] verify_challenge(fallback)")
        if resp.status_code == 200:
            si_result = resp.json()
            _log(f"      verify_challenge 状态: {si_result.get('status', 'unknown')}")
            # 检测 captcha challenge 失败
            setup_error = si_result.get("last_setup_error", {})
            if setup_error:
                err_code = setup_error.get("code", "")
                err_msg = setup_error.get("message", "")
                err_decline = setup_error.get("decline_code", "")
                _log(
                    "      verify_challenge error: "
                    f"code={err_code or '-'} decline_code={err_decline or '-'} msg={err_msg or '-'}"
                )
                if "captcha" in err_msg.lower() or "authentication_failure" in err_code:
                    raise ChallengeReconfirmRequired(f"challenge captcha 被 Stripe 拒绝: [{err_code}] {err_msg}")
            verify_raw = json.dumps(si_result)
            new_source = re.search(r"(setatt_[A-Za-z0-9]+)", verify_raw)
            if new_source:
                source = new_source.group(1)
        else:
            _log(f"      verify_challenge 返回 {resp.status_code}: {resp.text[:300]}")
            if "no valid challenge" in resp.text.lower():
                raise ChallengeReconfirmRequired(
                    f"challenge 已失效 (Stripe 返回 {resp.status_code}), 需要重新 confirm 获取新的 challenge"
                )

    if source:
        auth_url = f"{STRIPE_API}/v1/3ds2/authenticate"
        _log(f"      3DS2 authenticate (source: {source[:30]}...) ...")
        auth_data = {
            "source": source,
            "browser": json.dumps(_build_3ds_browser_payload(locale_profile, ctx)),
            "one_click_authn_device_support[hosted]": "false",
            "one_click_authn_device_support[same_origin_frame]": "false",
            "one_click_authn_device_support[spc_eligible]": "true",
            "one_click_authn_device_support[webauthn_eligible]": "true",
            "one_click_authn_device_support[publickey_credentials_get_allowed]": "true",
            "frontend_execution": ctx.get("frontend_execution", DEFAULT_FRONTEND_EXECUTION),
            "key": pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        }
        _log_request("POST", auth_url, data=auth_data, tag="[5/6] 3ds2/authenticate")
        with _http_session_stage_proxy(session, stage_proxy_cfg, "three_ds_authenticate"):
            resp = session.post(auth_url, data=auth_data, headers=_stripe_headers())
        _log_response(resp, tag="[5/6] 3ds2/authenticate")
        if resp.status_code == 200:
            result = resp.json()
            state = result.get("state", "unknown")
            trans_status = result.get("ares", {}).get("transStatus", "?")
            _log(f"      3DS2 结果: state={state}, transStatus={trans_status}")
            ctx["three_ds_result"] = {
                "state": state,
                "trans_status": trans_status,
                "source": result.get("source") or source,
                "acs_url": result.get("ares", {}).get("acsURL"),
                "creq": result.get("creq"),
                "three_ds_server_trans_id": result.get("ares", {}).get("threeDSServerTransID"),
            }
            if state == "challenge_required":
                _log("      3DS2 进入 challenge_required；这不是废卡，后续需要浏览器侧完成 challenge。")
                return
        else:
            _log(f"      3DS2 authenticate 返回 {resp.status_code}: {resp.text[:200]}")
    else:
        _log("      ⚠ 没有 setatt_ source, 跳过 3DS2 authenticate")
        raise RuntimeError("3DS 验证失败: 未获取到 setatt_ source, 无法完成认证")

  
    if seti_id and client_secret:
        time.sleep(3)
        poll_url = f"{STRIPE_API}/v1/setup_intents/{seti_id}"
        poll_params = {
            "client_secret": client_secret,
            "is_stripe_sdk": "false",
            "key": pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        }
        _log(f"      查询 setup_intent 最终状态 ...")
        _log_request("GET", poll_url, params=poll_params, tag="[5/6] setup_intent状态")
        with _http_session_stage_proxy(session, stage_proxy_cfg, "setup_intent_poll"):
            poll_resp = session.get(poll_url, params=poll_params, headers=_stripe_headers())
        _log_response(poll_resp, tag="[5/6] setup_intent状态")
        if poll_resp.status_code == 200:
            si_data = poll_resp.json()
            si_status = si_data.get("status", "unknown")
            _log(f"      setup_intent 状态: {si_status}")
            terminal_failure = _extract_terminal_payment_failure(si_data, source_kind="setup_intent")
            if terminal_failure:
                ctx["terminal_result"] = terminal_failure
                err = terminal_failure.get("error", {})
                _log(
                    "      setup_intent 已落到终态失败: "
                    f"[{err.get('code', '?')}] {err.get('decline_code', '')} {err.get('message', '')}".strip()
                )
        else:
            _log("      ⚠ 无 seti_id / client_secret, 跳过 setup_intent 查询")


def poll_result(session: requests.Session, pk: str, session_id: str, stripe_ver: str = STRIPE_VERSION_BASE) -> dict:
    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/poll"
    params = {
        "key": pk,
        "_stripe_version": stripe_ver,
    }

    _log("[6/6] 轮询支付结果 ...")
    for attempt in range(30):
        time.sleep(2)
        _log_request("GET", url, params=params, tag=f"[6/6] poll({attempt+1}/30)")
        resp = session.get(url, params=params, headers=_stripe_headers())
        _log_response(resp, tag=f"[6/6] poll({attempt+1}/30)")
        if resp.status_code != 200:
            _log(f"      poll 返回 {resp.status_code}, 重试 ...")
            continue

        data = resp.json()
        state = data.get("state", "unknown")
        payment_status = data.get("payment_object_status", "unknown")

        if state == "succeeded":
            return_url = data.get("return_url", "")
            _log(f"\n{'='*60}")
            _log(f"  支付成功!")
            _log(f"  state:          {state}")
            _log(f"  payment_status: {payment_status}")
            _log(f"  mode:           {data.get('mode', '?')}")
            _log(f"  return_url:     {return_url}")
            _log(f"{'='*60}\n")
            return data

        if state in ("failed", "expired", "canceled"):
            _log(f"\n  支付失败: state={state}")
            _log_raw(f"  完整 poll 响应: {json.dumps(data, ensure_ascii=False, indent=4)}")
            return data

        _log(f"      state={state}, payment_status={payment_status} ({attempt + 1}/30)")

    raise TimeoutError("轮询超时 (60s)")



def _record_result(
    status: str,
    chatgpt_email: str = "",
    session_id: str = "",
    payment_channel: str = "card",
    processor_entity: str = "",
    config_path: str = "",
    error_msg: str = "",
    extra: dict = None,
):
    """把支付结果写入 SQLite 运行时数据库。"""
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "chatgpt_email": chatgpt_email,
        "session_id": session_id,
        "channel": payment_channel,
        "entity": processor_entity,
        "config": os.path.basename(config_path) if config_path else "",
    }
    if error_msg:
        record["error"] = error_msg[:200]
    if extra:
        # 只保留 refresh_token 和 team_account_id，不落盘 access_token/session_token
        allowed = {"refresh_token", "team_account_id"}
        for k, v in extra.items():
            if k in allowed and v:
                record[k] = v
    try:
        get_db().add_card_result(record)
    except Exception:
        pass


def load_config(path: str) -> dict:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = []

    def _add_candidate(candidate: str):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    _add_candidate(path)
    if not os.path.isabs(path):
        _add_candidate(os.path.join(script_dir, path))
    _add_candidate(os.path.join(script_dir, "config.auto.json"))

    for candidate in candidates:
        if os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["_loaded_from"] = candidate
            # 同步打码平台 base URL 到 module-level，让 helper 函数能拿到
            global _REMOTE_CAPTCHA_BASE_URL
            _REMOTE_CAPTCHA_BASE_URL = (
                (cfg.get("captcha", {}) or {}).get("api_url") or ""
            ).rstrip("/")
            return cfg

    raise FileNotFoundError(
        f"未找到配置文件。已尝试: {', '.join(candidates)}"
    )


def _resolve_config_relative_path(cfg: dict, path_value: str, default_value: str = "") -> str:
    candidate = str(path_value or default_value or "").strip()
    if not candidate:
        return ""
    if os.path.isabs(candidate):
        return candidate

    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_dirs = []
    loaded_from = str(cfg.get("_loaded_from") or "").strip()
    if loaded_from:
        base_dirs.append(os.path.dirname(os.path.abspath(loaded_from)))
    base_dirs.append(script_dir)
    base_dirs.append(os.path.dirname(script_dir))

    seen = set()
    for base_dir in base_dirs:
        if not base_dir or base_dir in seen:
            continue
        seen.add(base_dir)
        resolved = os.path.abspath(os.path.join(base_dir, candidate))
        if os.path.exists(resolved):
            return resolved

    first_base = base_dirs[0] if base_dirs else script_dir
    return os.path.abspath(os.path.join(first_base, candidate))


def _normalize_terminal_result(payload: dict | None) -> dict:
    data = json.loads(json.dumps(payload or {}))
    data.setdefault("source_kind", "setup_intent")
    data.setdefault("payment_object_status", "requires_payment_method")
    err = data.setdefault("error", {})
    err.setdefault("code", "card_declined")
    err.setdefault("decline_code", "generic_decline")
    err.setdefault("message", "Your card was declined.")
    return data


def _build_offline_terminal_result(offline_cfg: dict) -> dict:
    explicit = offline_cfg.get("terminal_result")
    if explicit:
        return _normalize_terminal_result(explicit)

    scenario = str(offline_cfg.get("scenario") or "3ds_succeeded_card_declined").strip().lower()
    if scenario == "challenge_failed":
        return _normalize_terminal_result(
            {
                "source_kind": "setup_intent",
                "payment_object_status": "requires_payment_method",
                "error": {
                    "code": "setup_intent_authentication_failure",
                    "decline_code": "",
                    "message": "Captcha challenge failed. Try again with a different payment method.",
                },
            }
        )
    if scenario in {"no_3ds_card_declined", "direct_decline"}:
        return _normalize_terminal_result(
            {
                "source_kind": "payment_intent",
                "payment_object_status": "requires_payment_method",
                "error": {
                    "code": "card_declined",
                    "decline_code": "generic_decline",
                    "message": "Your card was declined.",
                },
            }
        )

    return _normalize_terminal_result(
        {
            "source_kind": "setup_intent",
            "payment_object_status": "requires_payment_method",
            "error": {
                "code": "card_declined",
                "decline_code": "generic_decline",
                "message": "Your card was declined.",
            },
        }
    )


def _build_offline_fresh_checkout_info(cfg: dict) -> dict:
    fresh_cfg = cfg.get("fresh_checkout") or {}
    offline_cfg = cfg.get("offline_replay") or {}
    flows_path = _resolve_config_relative_path(
        cfg,
        offline_cfg.get("flows_path") or fresh_cfg.get("flows_path"),
        "../flows",
    )
    bootstrap = _load_fresh_checkout_bootstrap(flows_path)
    body = _build_fresh_checkout_body(fresh_cfg, bootstrap)
    checkout_resp = bootstrap.get("checkout_response") or {}
    checkout_session_id, processor_entity, checkout_url = _extract_checkout_identifiers(checkout_resp)
    if not checkout_url and checkout_session_id and processor_entity:
        checkout_url = f"https://chatgpt.com/checkout/{processor_entity}/{checkout_session_id}"
    if not checkout_url:
        raise FreshCheckoutAuthError("离线回放未能从 flows 还原 checkout_url")

    return {
        "url": checkout_url,
        "session_id": checkout_session_id,
        "processor_entity": processor_entity,
        "body": body,
        "bootstrap": bootstrap,
    }


def _run_offline_replay(
    checkout_input: str,
    *,
    cfg: dict,
    card: dict,
    locale_profile: dict,
    force_fresh: bool = False,
    fresh_only: bool = False,
):
    offline_cfg = cfg.get("offline_replay") or {}
    scenario = str(offline_cfg.get("scenario") or "3ds_succeeded_card_declined").strip().lower()
    _log("      [offline] 已启用离线回放模式：仅使用本地 flows / fixture，不发起外部网络请求")

    effective_checkout_input = checkout_input
    fresh_info = None
    if _should_generate_fresh_checkout(checkout_input, force_fresh):
        fresh_info = _build_offline_fresh_checkout_info(cfg)
        effective_checkout_input = fresh_info["url"]

        body = fresh_info.get("body") or {}
        team_plan_data = body.get("team_plan_data") or {}
        billing_details = body.get("billing_details") or {}
        promo_campaign = body.get("promo_campaign") or {}
        _log("[fresh/offline] 从本地 flows 重建 checkout 创建参数 ...")
        _log(
            "      request: "
            f"plan_name={body.get('plan_name') or '?'} "
            f"workspace_name={team_plan_data.get('workspace_name') or '?'} "
            f"seat_quantity={team_plan_data.get('seat_quantity') or '?'} "
            f"country={billing_details.get('country') or '?'} "
            f"currency={billing_details.get('currency') or '?'} "
            f"promo={promo_campaign.get('promo_campaign_id') or ''} "
            f"checkout_ui_mode={body.get('checkout_ui_mode') or '?'}"
        )
        _log(f"      fresh checkout: {fresh_info['url']}")
        if fresh_only:
            _log(f"\n日志已保存到: {LOG_FILE}")
            print(fresh_info["url"])
            return fresh_info

    _log("[1/6] 解析 checkout session ID ...")
    session_id, stripe_checkout_url = parse_checkout_url(effective_checkout_input)
    _log(f"      session_id: {session_id}")
    if "chatgpt.com" in effective_checkout_input:
        _log("      输入格式: ChatGPT 嵌入式链接 → 转换为 Stripe URL")
    elif _should_generate_fresh_checkout(checkout_input, force_fresh):
        _log("      输入格式: fresh/auto → 已从本地 flows 重建 checkout")
    _log(f"      stripe_url: {stripe_checkout_url}")

    _log("[2/6] 初始化结账会话 (offline replay) ...")
    _log("      [offline] 跳过指纹、elements、Link、地址上报与遥测请求")
    _log(
        "      [offline] 使用卡: "
        f"****{card['number'][-4:]}  ({card['name']})  "
        f"locale={locale_profile.get('browser_locale', 'en-US')}"
    )

    trace_steps: list[dict] = []

    def _trace(step: str, **extra):
        trace_steps.append(
            {
                "step": step,
                "ts": int(time.time()),
                **extra,
            }
        )

    if scenario in {"challenge_failed", "challenge_pass_then_decline", "3ds_succeeded_card_declined"}:
        _log("[3/6] 进入 challenge/3DS 离线回放链路 ...")
        _trace("confirm", phase="challenge_entry")
        _log("[5/6] 确认支付 (offline replay) ...")
        _log("      触发 3DS/challenge 验证，正在处理 ...")
        _trace("challenge_detected", source_kind="setup_intent")
        if scenario == "challenge_failed":
            _log("      [offline] 模拟浏览器 challenge 结果: network_checkcaptcha(pass=true)=false")
            _log("      verify_challenge 状态: requires_payment_method")
            _log(
                "      challenge captcha 被 Stripe 拒绝: "
                "[setup_intent_authentication_failure] "
                "Captcha challenge failed. Try again with a different payment method."
            )
            _trace(
                "verify_challenge",
                status="requires_payment_method",
                error_code="setup_intent_authentication_failure",
            )
        else:
            _log("      [offline] 模拟浏览器 challenge 结果: network_checkcaptcha(pass=true)")
            _log("      verify_challenge 状态: requires_action")
            _log("      3DS2 authenticate (offline replay) ...")
            _log("      3DS2 结果: state=succeeded, transStatus=Y")
            _log("      查询 setup_intent 最终状态 ...")
            _log("      setup_intent 状态: requires_payment_method")
            _trace("network_checkcaptcha", pass_result=True)
            _trace("verify_challenge", status="requires_action")
            _trace("3ds2_authenticate", state="succeeded", trans_status="Y")
            _trace("setup_intent_terminal", status="requires_payment_method")
    elif scenario in {"no_3ds_card_declined", "direct_decline"}:
        _log("[3/6] 进入非 3DS 离线回放链路 ...")
        _trace("confirm", phase="direct_decline")
        _log("[5/6] 确认支付 (offline replay) ...")
        _log("      [offline] 未触发 3DS/challenge，直接进入支付终态")
        _trace("terminal_without_3ds", status="requires_payment_method")
    else:
        _log(f"[3/6] 未知 offline scenario={scenario!r}，回退到 challenge_pass_then_decline")
        _trace("scenario_fallback", scenario=scenario, fallback="challenge_pass_then_decline")
        _log("[5/6] 确认支付 (offline replay) ...")
        _log("      触发 3DS/challenge 验证，正在处理 ...")
        _log("      [offline] 模拟浏览器 challenge 结果: network_checkcaptcha(pass=true)")
        _log("      verify_challenge 状态: requires_action")
        _log("      3DS2 authenticate (offline replay) ...")
        _log("      3DS2 结果: state=succeeded, transStatus=Y")
        _log("      查询 setup_intent 最终状态 ...")
        _log("      setup_intent 状态: requires_payment_method")
        _trace("network_checkcaptcha", pass_result=True)
        _trace("verify_challenge", status="requires_action")
        _trace("3ds2_authenticate", state="succeeded", trans_status="Y")
        _trace("setup_intent_terminal", status="requires_payment_method")

    terminal_result = _build_offline_terminal_result(offline_cfg)
    err = terminal_result.get("error", {})
    if scenario not in {"challenge_failed"}:
        _log(
            "      setup_intent 已落到终态失败: "
            f"[{err.get('code', '?')}] {err.get('decline_code', '')} {err.get('message', '')}".rstrip()
        )
    artifact_path = (
        _resolve_config_relative_path(
            cfg,
            offline_cfg.get("artifact_path"),
            "/tmp/ctf_offline_replay_latest.json",
        )
        if offline_cfg.get("artifact_path", "/tmp/ctf_offline_replay_latest.json")
        else ""
    )
    if artifact_path:
        try:
            artifact = {
                "scenario": scenario,
                "checkout_input": effective_checkout_input,
                "trace_steps": trace_steps,
                "terminal_result": terminal_result,
            }
            with open(artifact_path, "w", encoding="utf-8") as f:
                json.dump(artifact, f, ensure_ascii=False, indent=2)
            _log(f"      [offline] 回放工件已写入: {artifact_path}")
        except Exception as e:
            _log(f"      [offline] 回放工件写入失败，忽略: {e}")

    _log(f"\n{'='*60}")
    _log("  支付已落到终态失败")
    _log(f"  source_kind:     {terminal_result.get('source_kind', '?')}")
    _log(f"  payment_status:  {terminal_result.get('payment_object_status', '?')}")
    _log(f"  code:            {err.get('code', '?')}")
    _log(f"  decline_code:    {err.get('decline_code', '?')}")
    _log(f"  message:         {err.get('message', '')}")
    _log(f"{'='*60}\n")
    _log(f"\n日志已保存到: {LOG_FILE}")
    return terminal_result


def _run_local_mock_gateway(
    checkout_input: str,
    *,
    cfg: dict,
    card: dict,
    locale_profile: dict,
    force_fresh: bool = False,
    fresh_only: bool = False,
):
    from local_mock_gateway import LocalMockGateway

    mock_cfg = cfg.get("local_mock") or {}
    scenario = str(
        mock_cfg.get("scenario")
        or (cfg.get("offline_replay") or {}).get("scenario")
        or "challenge_pass_then_decline"
    ).strip().lower()
    terminal_result = _build_offline_terminal_result(
        {
            "scenario": scenario,
            "terminal_result": mock_cfg.get("terminal_result"),
        }
    )
    amount_due = int(
        mock_cfg.get("due")
        if mock_cfg.get("due") is not None
        else ((cfg.get("fresh_checkout") or {}).get("expected_due") or 0)
    )

    effective_checkout_input = checkout_input
    fresh_info = None
    if _should_generate_fresh_checkout(checkout_input, force_fresh):
        fresh_info = _build_offline_fresh_checkout_info(cfg)
        effective_checkout_input = fresh_info["url"]

    session_id = ""
    processor_entity = "openai_llc"
    if effective_checkout_input and not _should_generate_fresh_checkout(checkout_input, force_fresh):
        session_id, _ = parse_checkout_url(effective_checkout_input)
    elif fresh_info:
        session_id = fresh_info.get("session_id") or ""
        processor_entity = fresh_info.get("processor_entity") or "openai_llc"

    artifact_path = _resolve_config_relative_path(
        cfg,
        mock_cfg.get("artifact_path"),
        "/tmp/ctf_local_mock_latest.json",
    )

    gateway = LocalMockGateway(
        scenario=scenario,
        terminal_result=terminal_result,
        checkout_url=effective_checkout_input if effective_checkout_input and "chatgpt.com/checkout/" in effective_checkout_input else "",
        checkout_session_id=session_id,
        processor_entity=processor_entity,
        due=amount_due,
    )
    base_url = gateway.start()

    def _request_json(method: str, path: str, payload: dict | None = None, tag: str = "") -> dict:
        url = urllib.parse.urljoin(base_url + "/", path.lstrip("/"))
        _log_request(method, url, data=payload, tag=tag)
        if method.upper() == "GET":
            resp = requests.get(url, timeout=10)
        else:
            resp = requests.post(url, json=payload or {}, timeout=10)
        _log_response(resp, tag=tag)
        resp.raise_for_status()
        return resp.json()

    try:
        _log("      [local-mock] 已启用本地 HTTP mock gateway：所有请求仅发往 127.0.0.1")
        _log(f"      [local-mock] gateway: {base_url}  scenario={scenario}")

        if fresh_info is None:
            _log("[0/6] 向本地 mock 生成 fresh checkout ...")
            body = _build_fresh_checkout_body(cfg.get("fresh_checkout") or {}, {"checkout_response": {}})
            fresh_info = {
                "body": body,
            }

        checkout_body = fresh_info.get("body") or {}
        checkout_resp = _request_json(
            "POST",
            "/backend-api/payments/checkout",
            payload=checkout_body,
            tag="local-mock fresh_checkout",
        )
        effective_checkout_input = (checkout_resp.get("checkout_url") or "").strip() or effective_checkout_input
        if not effective_checkout_input:
            raise RuntimeError("local mock 未返回 checkout_url")

        if fresh_only:
            _log(f"      [local-mock] fresh checkout: {effective_checkout_input}")
            print(effective_checkout_input)
            return checkout_resp

        _log("[1/6] 解析 checkout session ID ...")
        session_id, stripe_checkout_url = parse_checkout_url(effective_checkout_input)
        _log(f"      session_id: {session_id}")
        _log("      输入格式: local mock fresh checkout")
        _log(f"      stripe_url: {stripe_checkout_url}")

        _log("[2/6] 初始化结账会话 (local mock) ...")
        init_data = _request_json(
            "GET",
            f"/v1/checkout/sessions/{session_id}/init",
            tag="local-mock init",
        )
        total_summary = init_data.get("total_summary") or {}
        _log(
            "      商户: "
            f"{init_data.get('merchant', '?')}  |  模式: {init_data.get('mode', '?')}  |  due={total_summary.get('due', '?')}"
        )
        _log(
            "      [local-mock] 使用卡: "
            f"****{card['number'][-4:]}  ({card['name']})  locale={locale_profile.get('browser_locale', 'en-US')}"
        )

        _log("[3/6] 提交 confirm 到本地 mock ...")
        confirm_payload = {
            "payment_method_data": {
                "type": "card",
                "billing_details": {
                    "name": card.get("name") or "",
                    "email": card.get("email") or "",
                    "address": card.get("address") or {},
                },
                "card": {
                    "last4": str(card.get("number") or "")[-4:],
                    "exp_month": card.get("exp_month"),
                    "exp_year": card.get("exp_year"),
                },
            }
        }
        seti_id = gateway.seti_id
        confirm_resp = _request_json(
            "POST",
            f"/v1/setup_intents/{seti_id}/confirm",
            payload=confirm_payload,
            tag="local-mock confirm",
        )

        if scenario in {"no_3ds_card_declined", "direct_decline"}:
            _log("[5/6] 未触发 3DS/challenge，直接进入终态 ...")
        else:
            _log("[5/6] 触发 challenge，提交 verify_challenge 到本地 mock ...")
            next_action = confirm_resp.get("next_action") or {}
            captcha_action = next_action.get("captcha_challenge") or {}
            _log(
                "      challenge: "
                f"site_key={captcha_action.get('site_key', '?')} "
                f"ekey={captcha_action.get('ekey', '?')}"
            )
            verify_resp = _request_json(
                "POST",
                f"/v1/setup_intents/{seti_id}/verify_challenge",
                payload={
                    "client_secret": gateway.client_secret,
                    "challenge_response_token": "mock-solved-token",
                    "challenge_response_ekey": captcha_action.get("ekey") or "",
                },
                tag="local-mock verify_challenge",
            )
            verify_status = verify_resp.get("status") or "?"
            _log(f"      verify_challenge 状态: {verify_status}")
            if verify_status == "requires_action":
                auth_resp = _request_json(
                    "POST",
                    "/v1/3ds2/authenticate",
                    payload={
                        "source": ((verify_resp.get("next_action") or {}).get("use_stripe_sdk") or {}).get("source") or gateway.source_id,
                        "browser": {"locale": locale_profile.get("browser_locale", "en-US")},
                    },
                    tag="local-mock 3ds2_authenticate",
                )
                _log(
                    "      3DS2 结果: "
                    f"state={auth_resp.get('state', '?')}, transStatus={((auth_resp.get('ares') or {}).get('transStatus') or '?')}"
                )
                setup_intent_resp = _request_json(
                    "GET",
                    f"/v1/setup_intents/{seti_id}",
                    tag="local-mock setup_intent retrieve",
                )
                _log(f"      setup_intent 状态: {setup_intent_resp.get('status', '?')}")
            else:
                last_setup_error = verify_resp.get("last_setup_error") or {}
                _log(
                    "      challenge 被拒绝: "
                    f"[{last_setup_error.get('code', '?')}] {last_setup_error.get('message', '')}"
                )

        poll_resp = _request_json(
            "GET",
            f"/v1/checkout/sessions/{session_id}/poll",
            tag="local-mock poll",
        )
        terminal_result = _normalize_terminal_result(poll_resp.get("terminal_result") or {})
        err = terminal_result.get("error", {})

        if artifact_path:
            try:
                artifact = {
                    "scenario": scenario,
                    "checkout_input": effective_checkout_input,
                    "gateway_state": gateway.export_state(),
                    "poll_response": poll_resp,
                    "terminal_result": terminal_result,
                }
                with open(artifact_path, "w", encoding="utf-8") as f:
                    json.dump(artifact, f, ensure_ascii=False, indent=2)
                _log(f"      [local-mock] 回放工件已写入: {artifact_path}")
            except Exception as e:
                _log(f"      [local-mock] 回放工件写入失败，忽略: {e}")

        _log(f"\n{'='*60}")
        _log("  支付已落到终态失败")
        _log(f"  source_kind:     {terminal_result.get('source_kind', '?')}")
        _log(f"  payment_status:  {terminal_result.get('payment_object_status', '?')}")
        _log(f"  code:            {err.get('code', '?')}")
        _log(f"  decline_code:    {err.get('decline_code', '?')}")
        _log(f"  message:         {err.get('message', '')}")
        _log(f"{'='*60}\n")
        _log(f"\n日志已保存到: {LOG_FILE}")
        return terminal_result
    finally:
        gateway.stop()


def run(
    checkout_input: str,
    card_index: int = 0,
    config_path: str = "config.json",
    manual_token: str = "",
    force_fresh: bool = False,
    fresh_only: bool = False,
    offline_replay: bool = False,
    local_mock: bool = False,
    use_paypal: bool = False,
    use_gopay: bool = False,
    gopay_otp_file: str = "",
):
    _init_log()  # 初始化日志文件

    cfg = load_config(config_path)
    runtime_cfg = cfg.get("runtime", {})
    behavior_cfg = cfg.get("behavior", {})
    pre_solve_passive_captcha = cfg.get("pre_solve_passive_captcha", True)
    browser_challenge_cfg = cfg.get("browser_challenge", {})
    cards = cfg["cards"]
    if card_index >= len(cards):
        raise ValueError(f"卡索引 {card_index} 超出范围，共 {len(cards)} 张卡")
    card = json.loads(json.dumps(cards[card_index]))
    captcha_cfg = cfg["captcha"]
    resolved_config_path = cfg.get("_loaded_from", config_path)
    if offline_replay:
        cfg.setdefault("offline_replay", {})
        cfg["offline_replay"]["enabled"] = True
    if local_mock:
        cfg.setdefault("local_mock", {})
        cfg["local_mock"]["enabled"] = True

    # PayPal / GoPay 模式校验
    if use_paypal and use_gopay:
        raise ValueError("--paypal 与 --gopay 互斥")
    paypal_cfg = cfg.get("paypal") or {}
    if use_paypal:
        has_login_creds = paypal_cfg.get("email") and paypal_cfg.get("password")
        has_cookies = paypal_cfg.get("cookies")
        if not has_login_creds and not has_cookies:
            raise ValueError("PayPal 模式需要提供 paypal.email + paypal.password，或 paypal.cookies")
        billing_country = card.get("address", {}).get("country", "").upper()
        if billing_country and billing_country not in EU_COUNTRIES:
            _log(
                f"  [警告] PayPal 通常仅支持欧盟国家地址，当前 billing country={billing_country}。"
                f"继续尝试，但可能被 Stripe 拒绝。"
            )
    if use_gopay:
        gopay_cfg = cfg.get("gopay") or {}
        if not all(gopay_cfg.get(k) for k in ("country_code", "phone_number", "pin")):
            raise ValueError("GoPay 模式需 cfg.gopay 提供 country_code / phone_number / pin")

    _FIRST_NAMES = [
        "JAMES", "JOHN", "ROBERT", "MICHAEL", "WILLIAM", "DAVID", "RICHARD", "JOSEPH",
        "THOMAS", "CHARLES", "DANIEL", "MATTHEW", "ANTHONY", "MARK", "STEVEN",
        "MARY", "PATRICIA", "JENNIFER", "LINDA", "ELIZABETH", "BARBARA", "SUSAN",
        "JESSICA", "SARAH", "KAREN", "NANCY", "LISA", "BETTY", "MARGARET", "SANDRA",
    ]
    _LAST_NAMES = [
        "SMITH", "JOHNSON", "WILLIAMS", "BROWN", "JONES", "GARCIA", "MILLER",
        "DAVIS", "RODRIGUEZ", "MARTINEZ", "WILSON", "ANDERSON", "TAYLOR", "THOMAS",
        "MOORE", "JACKSON", "MARTIN", "LEE", "THOMPSON", "WHITE", "HARRIS", "CLARK",
    ]
    _EMAIL_DOMAINS = [
        "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "protonmail.com",
    ]

    def _gen_name() -> str:
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    def _gen_email() -> str:
        email_user = "".join(
            random.choices(string.ascii_lowercase + string.digits, k=random.randint(8, 12))
        )
        return f"{email_user}@{random.choice(_EMAIL_DOMAINS)}"

    addr = dict(card.get("address", {}) or {})
    card["address"] = addr

    if cfg.get("randomize_identity", False):
        card["name"] = _gen_name()
        card["email"] = _gen_email()
        line1 = addr.get("line1", "")
        if line1:
            new_line1 = re.sub(r"^\d+", str(random.randint(100, 999)), line1)
            if new_line1 == line1:
                new_line1 = f"{random.randint(100, 999)} {line1}"
            addr["line1"] = new_line1
    else:
        if not card.get("name"):
            card["name"] = _gen_name()
        if not card.get("email"):
            card["email"] = _gen_email()

    locale_key = cfg.get("locale", addr.get("country", "US")).upper()
    locale_profile = LOCALE_PROFILES.get(locale_key, LOCALE_PROFILES["US"])
    _log(f"  地域: {locale_key} (tz={locale_profile['browser_timezone']}, lang={locale_profile['browser_locale']})")

    _log(f"\n{'='*60}")
    if use_paypal:
        _log(f"  Stripe 自动化支付 (PayPal 渠道)")
        _log(f"  PayPal 账号: {paypal_cfg['email']}")
    else:
        _log(f"  Stripe 自动化支付")
        _log(f"  使用卡: ****{card['number'][-4:]}  ({card['name']})")
    _log(f"  邮箱: {card['email']}")
    _log(f"  地址: {addr.get('line1', '')} ({addr.get('country', '')})")
    _log(f"  配置文件: {resolved_config_path}")
    _log(f"{'='*60}\n")

    if (cfg.get("offline_replay") or {}).get("enabled", False):
        return _run_offline_replay(
            checkout_input,
            cfg=cfg,
            card=card,
            locale_profile=locale_profile,
            force_fresh=force_fresh,
            fresh_only=fresh_only,
        )
    if (cfg.get("local_mock") or {}).get("enabled", False):
        return _run_local_mock_gateway(
            checkout_input,
            cfg=cfg,
            card=card,
            locale_profile=locale_profile,
            force_fresh=force_fresh,
            fresh_only=fresh_only,
        )

    http = requests.Session()
    http.headers.update(_browser_like_session_headers(locale_profile["browser_locale"]))
    stage_proxy_cfg = cfg.get("stage_proxies") or {}

    # 代理配置
    proxy_cfg = cfg.get("proxy")
    if proxy_cfg:
        proxy_url = _build_proxy_url_from_cfg(proxy_cfg)
        _apply_proxy_to_http_session(http, proxy_url)
        _log(f"      代理: {_describe_proxy_cfg(proxy_cfg)}")
    else:
        _log("      代理: 无 (直连)")
    if stage_proxy_cfg:
        _log("      stage_proxies:")
        for stage_name in sorted(stage_proxy_cfg):
            _log(f"        - {stage_name}: {_describe_proxy_cfg(stage_proxy_cfg.get(stage_name))}")

    with _http_session_stage_proxy(http, stage_proxy_cfg, "fingerprint"):
        reg_guid, reg_muid, reg_sid = register_fingerprint(http)

    effective_checkout_input = checkout_input
    fresh_cfg = cfg.get("fresh_checkout") or {}
    if _should_generate_fresh_checkout(checkout_input, force_fresh):
        fresh_info = generate_fresh_checkout(http, cfg, locale_profile=locale_profile)
        effective_checkout_input = fresh_info["url"]
        if fresh_only:
            _log(f"\n日志已保存到: {LOG_FILE}")
            print(fresh_info["url"])
            return fresh_info

    init_attempt = 0
    inactive_refresh_limit = 2 if fresh_cfg.get("auto_refresh_on_inactive", False) else 1
    inactive_refresh_count = 0
    expected_due = _resolve_expected_checkout_due(fresh_cfg) if fresh_cfg.get("enabled", False) else None
    due_refresh_limit = int(fresh_cfg.get("max_due_mismatch_refreshes", 3) or 0)
    if not fresh_cfg.get("auto_refresh_on_due_mismatch", True):
        due_refresh_limit = 0
    due_refresh_count = 0
    while True:
        init_attempt += 1
        _log("[1/6] 解析 checkout session ID ...")
        session_id, stripe_checkout_url = parse_checkout_url(effective_checkout_input)
        _log(f"      session_id: {session_id}")
        if "chatgpt.com" in effective_checkout_input:
            _log("      输入格式: ChatGPT 嵌入式链接 → 转换为 Stripe URL")
        elif _should_generate_fresh_checkout(checkout_input, force_fresh):
            _log("      输入格式: fresh/auto → 已从 ChatGPT 后端生成新的 checkout")
        _log(f"      stripe_url: {stripe_checkout_url}")

        try:
            with _http_session_stage_proxy(http, stage_proxy_cfg, "fetch_publishable_key"):
                pk = fetch_publishable_key(http, session_id, stripe_checkout_url)
            with _http_session_stage_proxy(http, stage_proxy_cfg, "stripe_init"):
                init_resp, stripe_ver, init_ctx = init_checkout(http, session_id, pk, locale_profile=locale_profile)
            pricing = _extract_checkout_totals(init_resp)
            _log(
                "      pricing: "
                f"due={pricing.get('due')} "
                f"subtotal={pricing.get('subtotal')} "
                f"total={pricing.get('total')} "
                f"currency={pricing.get('currency') or '?'}"
            )
            if expected_due is not None:
                actual_due = pricing.get("due")
                if actual_due is None:
                    raise RuntimeError("无法从 Stripe init 响应提取 due，无法校验优惠链路")
                if actual_due != expected_due:
                    if due_refresh_count < due_refresh_limit and fresh_cfg.get("enabled", False):
                        due_refresh_count += 1
                        _log(
                            "      fresh checkout 金额未命中预期，"
                            f"expected_due={expected_due} actual_due={actual_due}，"
                            f"自动重刷 fresh checkout ({due_refresh_count}/{due_refresh_limit}) ..."
                        )
                        fresh_info = generate_fresh_checkout(http, cfg, locale_profile=locale_profile)
                        effective_checkout_input = fresh_info["url"]
                        continue
                    raise RuntimeError(
                        f"fresh checkout 金额未命中预期: expected_due={expected_due}, actual_due={actual_due}"
                    )
            init_ctx["pricing"] = pricing
            break
        except CheckoutSessionInactive as e:
            if (inactive_refresh_count + 1) >= inactive_refresh_limit or not fresh_cfg.get("enabled", False):
                raise
            inactive_refresh_count += 1
            _log(f"      {e}")
            _log("      当前 checkout 已失活，自动生成 fresh checkout 后重试 ...")
            fresh_info = generate_fresh_checkout(http, cfg, locale_profile=locale_profile)
            effective_checkout_input = fresh_info["url"]
            continue
    init_ctx["guid"] = reg_guid
    init_ctx["muid"] = reg_muid
    init_ctx["sid"] = reg_sid
    init_ctx["page_load_ts"] = int(time.time() * 1000)
    init_ctx["runtime_version"] = runtime_cfg.get("version") or DEFAULT_STRIPE_RUNTIME_VERSION
    init_ctx["js_checksum"] = runtime_cfg.get("js_checksum", "")
    init_ctx["rv_timestamp"] = runtime_cfg.get("rv_timestamp", "")
    http.headers.update(_browser_like_session_headers(init_ctx.get("locale") or locale_profile["browser_locale"]))
    init_ctx["top_checkout_config_id"] = (
        runtime_cfg.get("top_checkout_config_id")
        or init_ctx.get("config_id", "")
    )
    init_ctx["payment_method_checkout_config_id"] = (
        runtime_cfg.get("payment_method_checkout_config_id")
        or init_ctx.get("config_id", "")
    )
    if use_paypal:
        # PayPal 必须走 shared_payment_method 模式（先创建 pm，再 confirm 引用）
        init_ctx["confirm_mode"] = "shared_payment_method"
    elif use_gopay:
        init_ctx["confirm_mode"] = "shared_payment_method"
        init_ctx["payment_method_type"] = "gopay"
    else:
        init_ctx["confirm_mode"] = runtime_cfg.get("confirm_mode", "inline_payment_method_data")
    # 把 processor_entity 透传给 manual_approval 阶段；默认 openai_llc（IDR/Plus 用）
    if fresh_info and fresh_info.get("processor_entity"):
        init_ctx["processor_entity"] = fresh_info["processor_entity"]
    init_ctx["frontend_execution"] = (
        runtime_cfg.get("frontend_execution")
        or DEFAULT_FRONTEND_EXECUTION
    )
    init_ctx["pasted_fields"] = behavior_cfg.get("pasted_fields", "number")
    init_ctx["min_time_on_page_ms"] = int(behavior_cfg.get("min_time_on_page_ms", 0) or 0)
    init_ctx["include_terms_of_service_consent"] = behavior_cfg.get("include_terms_of_service_consent")
    init_ctx["merchant_account_id"] = init_resp.get("account_settings", {}).get("account_id", "")
    # 全局代理 URL 传入 ctx，供 PayPal Playwright 浏览器使用
    if proxy_cfg:
        init_ctx["proxy_url"] = _build_proxy_url_from_cfg(proxy_cfg)
    init_ctx["captcha_api_key"] = captcha_cfg.get("api_key", "")
    init_ctx["stage_proxies"] = stage_proxy_cfg
    effective_external_solver_cfg = dict(browser_challenge_cfg.get("external_solver") or {})
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    should_autofill_external_solver = (
        browser_challenge_cfg.get("enabled", True)
        and not effective_external_solver_cfg
        and (not browser_challenge_cfg.get("auto_launch_browser", True) or not has_display)
    )
    if should_autofill_external_solver:
        bundled_solver = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hcaptcha_auto_solver.py")
        python_candidates = [
            str(os.environ.get("CTFML_PYTHON") or "").strip(),
            "~/.venvs/ctfml/bin/python",
            sys.executable,
        ]
        solver_python = next((p for p in python_candidates if p and os.path.exists(p)), sys.executable)
        effective_external_solver_cfg = {
            "enabled": True,
            "python": solver_python,
            "script": bundled_solver,
            "out_dir": "/tmp/hcaptcha_auto_solver_live",
            "timeout_s": max(180, int(browser_challenge_cfg.get("timeout_ms", 300000) / 1000)),
            "headed": not bool(browser_challenge_cfg.get("headless", False)),
        }
        _log(
            "      未显式配置 browser_challenge.external_solver，"
            "已自动启用项目内置 solver"
        )
    effective_vlm_cfg = dict(effective_external_solver_cfg.get("vlm") or {})
    effective_vlm_cfg.setdefault("enabled", True)
    effective_vlm_cfg.setdefault("model", "gpt-5.4")
    effective_vlm_cfg.setdefault("base_url", "https://YOUR_VLM_ENDPOINT/api")
    effective_vlm_cfg.setdefault("api_key", "")
    effective_vlm_cfg.setdefault("timeout_s", 45)
    effective_external_solver_cfg["vlm"] = effective_vlm_cfg
    init_ctx["browser_challenge"] = {
        "enabled": browser_challenge_cfg.get("enabled", True),
        "auto_launch_browser": browser_challenge_cfg.get("auto_launch_browser", True),
        "headless": browser_challenge_cfg.get("headless", False),
        "use_for_passive_captcha": browser_challenge_cfg.get("use_for_passive_captcha", True),
        "passive_headless": browser_challenge_cfg.get("passive_headless", True),
        "passive_timeout_ms": int(browser_challenge_cfg.get("passive_timeout_ms", 90000)),
        "timeout_ms": int(browser_challenge_cfg.get("timeout_ms", 300000)),
        "auto_click_checkbox": browser_challenge_cfg.get("auto_click_checkbox", True),
        "viewport": browser_challenge_cfg.get("viewport") or {"width": 1280, "height": 960},
        "external_solver": effective_external_solver_cfg,
        "proxy_url": str(browser_challenge_cfg.get("proxy_url") or "").strip(),
    }
    mode = init_resp.get("mode", "unknown")
    display_name = init_resp.get("account_settings", {}).get("display_name", "?")
    _log(f"      商户: {display_name}  |  模式: {mode}")
    _log(
        "      runtime: "
        f"confirm_mode={init_ctx['confirm_mode']}, "
        f"version={init_ctx['runtime_version']}, "
        f"js_checksum={'yes' if init_ctx.get('js_checksum') else 'no'}, "
        f"rv_timestamp={'yes' if init_ctx.get('rv_timestamp') else 'no'}"
    )
    if init_ctx["browser_challenge"]["enabled"]:
        _log(
            "      browser_challenge: "
            f"enabled(auto_launch={init_ctx['browser_challenge']['auto_launch_browser']}, "
            f"headless={init_ctx['browser_challenge']['headless']}, "
            f"timeout_ms={init_ctx['browser_challenge']['timeout_ms']})"
        )
        if init_ctx["browser_challenge"]["external_solver"].get("enabled"):
            _log(
                "      browser_challenge.external_solver: "
                f"python={init_ctx['browser_challenge']['external_solver'].get('python') or sys.executable}, "
                f"script={init_ctx['browser_challenge']['external_solver'].get('script') or 'hcaptcha_auto_solver.py'}"
            )
            vlm_cfg = init_ctx["browser_challenge"]["external_solver"].get("vlm") or {}
            _log(
                "      browser_challenge.external_solver.vlm: "
                f"enabled={bool(vlm_cfg.get('enabled', True))}, "
                f"model={vlm_cfg.get('model') or 'gpt-5.4'}, "
                f"base_url={vlm_cfg.get('base_url') or 'https://YOUR_VLM_ENDPOINT/api'}"
            )


    with _http_session_stage_proxy(http, stage_proxy_cfg, "telemetry_init"):
        send_telemetry_batch(http, session_id, init_ctx, phase="init")

   
    _log("[2c/6] 获取 elements session ...")
    with _http_session_stage_proxy(http, stage_proxy_cfg, "elements"):
        elements_resp = fetch_elements_session(
            http, pk, session_id, init_ctx, stripe_ver=stripe_ver, locale_profile=locale_profile
        )

   
    _log("[2d/6] 查询 Link 消费者 ...")
    with _http_session_stage_proxy(http, stage_proxy_cfg, "link_lookup"):
        lookup_consumer(
            http,
            pk,
            card["email"],
            session_id,
            stripe_ver=stripe_ver,
            ctx=init_ctx,
            init_resp=init_resp,
        )

  
    _log("[2e/6] 逐字段提交地址 ...")
    with _http_session_stage_proxy(http, stage_proxy_cfg, "address"):
        update_payment_page_address(http, pk, session_id, card, init_ctx, stripe_ver=stripe_ver)

    
    with _http_session_stage_proxy(http, stage_proxy_cfg, "telemetry_address"):
        send_telemetry_batch(http, session_id, init_ctx, phase="address")


    init_ctx["time_on_page"] = int(time.time() * 1000) - init_ctx.get("page_load_ts", int(time.time() * 1000))

    hcaptcha_cfg = extract_hcaptcha_config(init_resp)
    passive_captcha_cfg = extract_passive_captcha_config(init_resp, elements_resp)
    _log(f"      hCaptcha site_key: {hcaptcha_cfg['site_key']}")
    if hcaptcha_cfg.get("rqdata"):
        _log(f"      hCaptcha rqdata: {hcaptcha_cfg['rqdata'][:50]}...")
    _log(f"      passive captcha site_key: {passive_captcha_cfg['site_key']}")
    if passive_captcha_cfg.get("rqdata"):
        _log(f"      passive captcha rqdata: {passive_captcha_cfg['rqdata'][:50]}...")

    with _http_session_stage_proxy(http, stage_proxy_cfg, "telemetry_card_input"):
        send_telemetry_batch(http, session_id, init_ctx, phase="card_input")

    def _submit_confirm(captcha_token: str, captcha_ekey: str):
        measured_time_on_page = int(time.time() * 1000) - init_ctx.get(
            "page_load_ts", int(time.time() * 1000)
        )
        min_time_on_page_ms = int(init_ctx.get("min_time_on_page_ms") or 0)
        if min_time_on_page_ms > 0 and measured_time_on_page < min_time_on_page_ms:
            _log(
                f"      [behavior] time_on_page 从 {measured_time_on_page}ms 提升到最小阈值 {min_time_on_page_ms}ms"
            )
            measured_time_on_page = min_time_on_page_ms
        init_ctx["time_on_page"] = measured_time_on_page
        pm_id = ""
        if use_paypal:
            # PayPal 模式: 创建 type=paypal 的 payment_method，走 shared 模式
            init_ctx["payment_method_type"] = "paypal"
            with _http_session_stage_proxy(http, stage_proxy_cfg, "payment_method"):
                pm_id = create_paypal_payment_method(
                    http, pk, card, session_id, stripe_ver, ctx=init_ctx
                )
        elif use_gopay:
            with _http_session_stage_proxy(http, stage_proxy_cfg, "payment_method"):
                pm_id = create_gopay_payment_method(
                    http, pk, card, session_id, stripe_ver, ctx=init_ctx
                )
        elif init_ctx.get("confirm_mode") != "inline_payment_method_data":
            with _http_session_stage_proxy(http, stage_proxy_cfg, "payment_method"):
                pm_id = create_payment_method(
                    http, pk, card, captcha_token, session_id, stripe_ver, ctx=init_ctx
                )
        with _http_session_stage_proxy(http, stage_proxy_cfg, "telemetry_confirm"):
            send_telemetry_batch(http, session_id, init_ctx, phase="confirm")
        with _http_session_stage_proxy(http, stage_proxy_cfg, "confirm"):
            confirm_data = confirm_payment(
                http,
                pk,
                session_id,
                pm_id,
                card if (not use_paypal and not use_gopay and init_ctx.get("confirm_mode") == "inline_payment_method_data") else None,
                captcha_token,
                init_resp,
                stripe_ver,
                captcha_cfg,
                captcha_ekey=captcha_ekey,
                ctx=init_ctx,
                locale_profile=locale_profile,
            )

        # PayPal 模式: 检测 redirect_to_url 并启动浏览器授权
        if use_paypal or use_gopay:
            next_action = None
            for source_key in ("next_action", "payment_intent", "setup_intent"):
                obj = confirm_data.get(source_key)
                if isinstance(obj, dict):
                    na = obj.get("next_action") if source_key != "next_action" else obj
                    if isinstance(na, dict) and na.get("type") == "redirect_to_url":
                        next_action = na
                        break
            if not next_action:
                # 也检查 _find_setup_intent
                seti = _find_setup_intent(confirm_data)
                if seti and isinstance(seti, dict):
                    na = seti.get("next_action")
                    if isinstance(na, dict) and na.get("type") == "redirect_to_url":
                        next_action = na

            if next_action:
                redirect_info = next_action.get("redirect_to_url", {})
                paypal_redirect_url = redirect_info.get("url", "")
                if paypal_redirect_url:
                    _log(f"      redirect URL: {paypal_redirect_url[:100]}...")
                    if use_gopay:
                        _drive_gopay_from_redirect(
                            paypal_redirect_url, cfg, gopay_otp_file,
                            session_id=session_id,
                        )
                        _log("      GoPay 授权 + 扣款完成，继续 poll 结果 ...")
                    else:
                        success = _handle_paypal_redirect(
                            paypal_redirect_url,
                            paypal_cfg,
                            locale_profile=locale_profile,
                            ctx=init_ctx,
                        )
                        if not success:
                            raise RuntimeError("PayPal 授权失败或超时")
                        _log("      PayPal 授权完成，继续 poll 结果 ...")
                else:
                    raise RuntimeError("PayPal confirm 返回了 redirect_to_url 但缺少 url 字段")
            else:
                # manual_approval beta 新流程：
                #   1. confirm 返回 requires_approval 和 submission_attempt
                #   2. 调用 ChatGPT 的 /backend-api/payments/checkout/approve 批准
                #   3. 再 GET /payment_pages/<session>?client_betas=... 取 redirect_to_url
                submission = confirm_data.get("submission_attempt") or {}
                if submission.get("state") == "requires_approval":
                    _log("      [manual_approval] 调 ChatGPT approve 端点 ...")
                    try:
                        fresh_cfg = cfg.get("fresh_checkout") or {}
                        auth_cfg = fresh_cfg.get("auth") or {}
                        access_token = (auth_cfg.get("access_token") or "").strip()
                        oai_device_id = (
                            auth_cfg.get("oai_device_id")
                            or auth_cfg.get("device_id")
                            or ""
                        ).strip()
                        cookie_header = (auth_cfg.get("cookie_header") or "").strip()
                        processor_entity = init_ctx.get("processor_entity") or "openai_ie"
                        # 推断: processor_entity 可能在 init_resp 或 fresh_info
                        if not processor_entity:
                            processor_entity = init_resp.get("merchant_of_record_country", "openai_ie")
                        approve_headers = {
                            "content-type": "application/json",
                            "accept": "*/*",
                            "authorization": f"Bearer {access_token}",
                            "origin": "https://chatgpt.com",
                            "referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
                            "x-openai-target-path": "/backend-api/payments/checkout/approve",
                            "x-openai-target-route": "/backend-api/payments/checkout/approve",
                        }
                        if oai_device_id:
                            approve_headers["oai-device-id"] = oai_device_id
                        if isinstance(cookie_header, str) and cookie_header:
                            approve_headers["cookie"] = cookie_header
                        approve_body = {
                            "checkout_session_id": session_id,
                            "processor_entity": processor_entity,
                        }
                        # 创建独立 HTTP session 走 ChatGPT 代理
                        chatgpt_http_for_approve, _transport = _create_chatgpt_http_session(cfg)
                        ar = chatgpt_http_for_approve.post(
                            "https://chatgpt.com/backend-api/payments/checkout/approve",
                            json=approve_body, headers=approve_headers, timeout=20,
                        )
                        _log(f"      [manual_approval] ChatGPT approve: {ar.status_code} body={ar.text[:200]}")
                        if ar.status_code != 200:
                            raise RuntimeError(f"ChatGPT approve 失败: {ar.status_code} {ar.text[:200]}")
                        try:
                            approve_payload = ar.json() or {}
                        except Exception:
                            approve_payload = {}
                        approve_result = str(approve_payload.get("result") or "").lower()
                        if approve_result and approve_result != "approved":
                            # result=blocked is the signal that this confirm path needs
                            # an hCaptcha-backed retry. Surface the literal word so the
                            # outer confirm retry handler falls into the existing solver
                            # path instead of polling Stripe until timeout.
                            raise RuntimeError(f"manual_approval approve blocked: result={approve_result}")
                    except Exception as e_ap:
                        _log(f"      [manual_approval] approve 异常: {e_ap}")
                        raise

                    _log("      [manual_approval] 再 GET 取 redirect ...")
                    get_params = {
                        "key": pk,
                        "_stripe_version": STRIPE_VERSION_FULL,
                        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
                        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
                        "elements_session_client[elements_init_source]": "custom_checkout",
                        "elements_session_client[referrer_host]": "chatgpt.com",
                    }
                    got_redirect = False
                    for poll_i in range(15):
                        gr = http.get(
                            f"https://api.stripe.com/v1/payment_pages/{session_id}",
                            params=get_params, timeout=20,
                        )
                        if gr.status_code != 200:
                            _log(f"      [manual_approval] GET {gr.status_code}")
                            time.sleep(1); continue
                        try:
                            gj = gr.json()
                        except Exception:
                            time.sleep(1); continue
                        na = None
                        for src in ("next_action", "setup_intent", "payment_intent"):
                            obj = gj.get(src)
                            if isinstance(obj, dict):
                                candidate = obj.get("next_action") if src != "next_action" else obj
                                if isinstance(candidate, dict) and candidate.get("type") == "redirect_to_url":
                                    na = candidate
                                    break
                        if not na:
                            seti = _find_setup_intent(gj)
                            if seti and isinstance(seti, dict):
                                c2 = seti.get("next_action")
                                if isinstance(c2, dict) and c2.get("type") == "redirect_to_url":
                                    na = c2
                        if na:
                            url = (na.get("redirect_to_url") or {}).get("url", "")
                            if url:
                                _log(f"      [manual_approval] 拿到 redirect: {url[:100]}")
                                if use_gopay:
                                    _drive_gopay_from_redirect(
                                        url, cfg, gopay_otp_file,
                                        session_id=session_id,
                                    )
                                    got_redirect = True
                                    break
                                success = _handle_paypal_redirect(
                                    url, paypal_cfg,
                                    locale_profile=locale_profile, ctx=init_ctx,
                                )
                                if not success:
                                    raise RuntimeError("PayPal 授权失败或超时")
                                got_redirect = True
                                break
                        sa2 = (gj.get("submission_attempt") or {}).get("state")
                        _log(f"      [manual_approval] poll {poll_i+1}: sub_state={sa2}")
                        if sa2 and sa2 not in ("requires_approval",):
                            break
                        time.sleep(1)
                    if not got_redirect:
                        raise RuntimeError("manual_approval approve 后仍未拿到 redirect_to_url")
                else:
                    _log("      PayPal confirm 未返回 redirect，可能不需要授权 (直接完成)")

        return confirm_data

    def _solve_passive_confirm_captcha() -> tuple[str, str]:
        if manual_token:
            return manual_token, ""
        if not pre_solve_passive_captcha:
            return "", ""
        _log("      预先解 passive captcha ...")

        passive_browser_cfg = dict(init_ctx.get("browser_challenge") or {})
        passive_browser_enabled = bool(
            passive_browser_cfg.get("enabled")
            and passive_browser_cfg.get("use_for_passive_captcha", True)
        )
        if passive_browser_enabled:
            auto_browser_cfg = dict(passive_browser_cfg)
            auto_browser_cfg["auto_launch_browser"] = True
            auto_browser_cfg["headless"] = bool(
                passive_browser_cfg.get("passive_headless", True)
            )
            auto_browser_cfg["auto_click_checkbox"] = False
            auto_browser_cfg["timeout_ms"] = int(
                passive_browser_cfg.get("passive_timeout_ms", 90000)
            )
            auto_browser_cfg["proxy_url"] = str(
                passive_browser_cfg.get("passive_proxy_url")
                or ""
            ).strip()
            try:
                token, ekey, _ = solve_stripe_hcaptcha_in_browser(
                    passive_captcha_cfg,
                    merchant_id=init_ctx.get("merchant_account_id", ""),
                    locale=locale_profile.get("browser_locale", "en-US"),
                    browser_cfg=auto_browser_cfg,
                )
                return token, ekey
            except Exception as e:
                _log(f"      浏览器 passive captcha 未拿到 token，回退打码平台: {e}")

        return solve_hcaptcha(
            captcha_cfg,
            passive_captcha_cfg,
            session=http,
        )

    if manual_token:
        _log(f"[3/6] 使用手动传入的 token (长度: {len(manual_token)})")
        max_confirm_attempts = 3
        for confirm_attempt in range(1, max_confirm_attempts + 1):
            try:
                captcha_token, captcha_ekey = _solve_passive_confirm_captcha()
                _submit_confirm(captcha_token, captcha_ekey)
                break
            except ChallengeReconfirmRequired as e:
                if confirm_attempt >= max_confirm_attempts:
                    raise
                _log(f"      {e}")
                _log(f"      重新 confirm 获取新的 challenge ({confirm_attempt}/{max_confirm_attempts}) ...")
    else:
        if pre_solve_passive_captcha:
            _log("[3/6] 先按真实链路解 passive captcha，再提交 confirm ...")
        else:
            _log("[3/6] 尝试不带 hCaptcha 直接提交 ...")
        max_confirm_attempts = 3
        for confirm_attempt in range(1, max_confirm_attempts + 1):
            try:
                captcha_token, captcha_ekey = _solve_passive_confirm_captcha()
                _submit_confirm(captcha_token, captcha_ekey)
                break
            except ChallengeReconfirmRequired as e:
                if confirm_attempt >= max_confirm_attempts:
                    raise
                _log(f"      {e}")
                _log(f"      重新 confirm 获取新的 challenge ({confirm_attempt}/{max_confirm_attempts}) ...")
                continue
            except RuntimeError as e:
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in ["captcha", "hcaptcha", "blocked", "denied", "radar", "challenge_response"]):
                    _log("      需要 captcha，开始解题 ...")
                    captcha_token, captcha_ekey = solve_hcaptcha(
                        captcha_cfg,
                        passive_captcha_cfg,
                        session=http,
                    )
                    try:
                        _submit_confirm(captcha_token, captcha_ekey)
                        break
                    except ChallengeReconfirmRequired as challenge_error:
                        if confirm_attempt >= max_confirm_attempts:
                            raise
                        _log(f"      {challenge_error}")
                        _log(f"      重新 confirm 获取新的 challenge ({confirm_attempt}/{max_confirm_attempts}) ...")
                        continue
                raise

  
    with _http_session_stage_proxy(http, stage_proxy_cfg, "telemetry_poll"):
        send_telemetry_batch(http, session_id, init_ctx, phase="poll")

    terminal_result = init_ctx.get("terminal_result")
    if terminal_result:
        err = terminal_result.get("error", {})
        _log(f"\n{'='*60}")
        _log("  支付已落到终态失败")
        _log(f"  source_kind:     {terminal_result.get('source_kind', '?')}")
        _log(f"  payment_status:  {terminal_result.get('payment_object_status', '?')}")
        _log(f"  code:            {err.get('code', '?')}")
        _log(f"  decline_code:    {err.get('decline_code', '?')}")
        _log(f"  message:         {err.get('message', '')}")
        _log(f"{'='*60}\n")
        _log(f"\n日志已保存到: {LOG_FILE}")
        return terminal_result

    # Step 6
    with _http_session_stage_proxy(http, stage_proxy_cfg, "poll"):
        result = poll_result(http, pk, session_id, stripe_ver)

    # 记录结果
    chatgpt_email = fresh_cfg.get("_chatgpt_email", card.get("email", ""))
    payment_channel = "gopay" if use_gopay else ("paypal" if use_paypal else "card")
    result_state = result.get("state", "unknown")

    # 从数据库查最近一条匹配 email 的账号凭证。
    extra_info = {}
    # 支付成功时记录 Team workspace account_id
    try:
        ru = result.get("return_url", "") if isinstance(result, dict) else ""
        if ru:
            import urllib.parse as _up
            qs = _up.parse_qs(_up.urlparse(ru).query)
            aid = (qs.get("account_id") or [""])[0]
            if aid:
                extra_info["team_account_id"] = aid
    except Exception:
        pass

    # 支付成功才拿 refresh_token（失败不拿）
    if result_state == "succeeded" and chatgpt_email:
        try:
            # 从 SQLite 主存储取本账号的 password。
            import os as _os
            _password = ""
            try:
                _password = (get_db().find_latest_registered_account(chatgpt_email) or {}).get("password", "") or ""
            except Exception:
                _password = ""

            # 加载 CTF-reg/config.paypal-proxy.json 里的 mail 配置（供 IMAP 取 OTP）
            _mail_cfg = {}
            reg_cfg_path = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                "CTF-reg", "config.paypal-proxy.json",
            )
            if _os.path.exists(reg_cfg_path):
                try:
                    with open(reg_cfg_path, "r", encoding="utf-8") as rf:
                        _reg_cfg = json.load(rf)
                    _mail_cfg = _reg_cfg.get("mail", {}) or {}
                except Exception as e:
                    _log(f"      [RT] 读取 mail 配置失败: {e}")

            if _password and _mail_cfg:
                _log("      [RT] 支付成功，重新登录拿 refresh_token ...")
                rt_value = _exchange_refresh_token_with_session(
	                    email=chatgpt_email,
	                    password=_password,
	                    mail_cfg=_mail_cfg,
	                    proxy_url=_build_proxy_url_from_cfg(cfg.get("proxy")) if isinstance(cfg, dict) else "",
	                    oauth_client_id=_codex_oauth_client_id_from_config(cfg),
	                )
                if rt_value:
                    extra_info["refresh_token"] = rt_value
                    _log(f"      [RT] ✅ 获得 refresh_token 长度={len(rt_value)}")
                else:
                    _log("      [RT] ❌ refresh_token 获取失败（不影响支付结果）")
            else:
                _log(f"      [RT] 缺少条件，跳过 (password={bool(_password)} mail_cfg={bool(_mail_cfg)})")
        except Exception as e:
            _log(f"      [RT] 获取异常: {e}")

    _record_result(
        status=result_state,
        chatgpt_email=chatgpt_email,
        session_id=session_id,
        payment_channel=payment_channel,
        processor_entity=init_resp.get("account_settings", {}).get("display_name", ""),
        config_path=resolved_config_path,
        extra=extra_info if extra_info else None,
    )
    _log(f"\n日志已保存到: {LOG_FILE}")
    return result



def main():
    parser = argparse.ArgumentParser(
        description="Stripe Checkout 自动化支付",
        epilog=(
            "示例:\n"
            "  python card.py cs_live_xxx\n"
            "  python card.py fresh --fresh-only\n"
            "  python card.py auto --config config.auto.json"
        ),
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        default="fresh",
        help="Checkout Session URL / cs_live_xxx；传 fresh/auto 则自动生成新的 checkout",
    )
    parser.add_argument("--card", type=int, default=0, help="使用第 N 张卡 (0-based, 默认 0)")
    parser.add_argument("--config", default="config.json", help="配置文件路径 (默认 config.json)")
    parser.add_argument("--token", default="", help="手动传入 hCaptcha token (跳过打码平台)")
    parser.add_argument("--fresh", action="store_true", help="忽略传入 session，先生成 fresh checkout")
    parser.add_argument("--fresh-only", action="store_true", help="只生成并输出 fresh checkout URL")
    parser.add_argument(
        "--offline-replay",
        action="store_true",
        help="仅使用本地 flows/fixture 回放，不发起外部网络请求",
    )
    parser.add_argument(
        "--local-mock",
        action="store_true",
        help="启动本地 HTTP mock gateway，并仅通过 127.0.0.1 回放 checkout/challenge/3DS 状态机",
    )
    parser.add_argument(
        "--paypal",
        action="store_true",
        help="使用 PayPal 支付（需要配置文件中包含 paypal 段，仅支持欧盟国家地址）",
    )
    parser.add_argument(
        "--gopay",
        action="store_true",
        help="使用 GoPay tokenization (印尼 e-wallet, ChatGPT Plus)",
    )
    parser.add_argument(
        "--gopay-otp-file",
        default="",
        help="webui 模式: gopay 从这个文件读 WhatsApp OTP",
    )
    parser.add_argument(
        "--json-result",
        action="store_true",
        help="输出结构化 JSON 结果到 stdout（供 pipeline 解析）",
    )
    args = parser.parse_args()

    try:
        result = run(
            args.session_id,
            card_index=args.card,
            config_path=args.config,
            manual_token=args.token,
            force_fresh=args.fresh,
            fresh_only=args.fresh_only,
            offline_replay=args.offline_replay,
            local_mock=args.local_mock,
            use_paypal=args.paypal,
            use_gopay=args.gopay,
            gopay_otp_file=args.gopay_otp_file,
        )
        if args.json_result and result:
            print("CARD_RESULT_JSON=" + json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as e:
        import traceback as _tb
        err_msg = f"\n[ERROR] {type(e).__name__}: {e}\n{_tb.format_exc()}"
        print(err_msg, file=sys.stderr)
        # 记录失败
        _record_result(
            status="error",
            payment_channel="paypal" if args.paypal else "card",
            config_path=args.config,
            error_msg=str(e),
        )
        # 也写入日志
        try:
            import traceback
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n{'!'*60}\n")
                f.write(err_msg + "\n")
                f.write(traceback.format_exc())
                f.write(f"{'!'*60}\n")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
