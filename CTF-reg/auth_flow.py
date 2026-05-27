"""
注册/登录流程 - 协议直连方式
完整链路:
  chatgpt_csrf -> chatgpt_signin_openai -> auth_oauth_init -> sentinel
  -> signup -> send_otp -> verify_otp -> create_account
  -> redirect_chain -> auth_session -> (optional) oauth_token_exchange
"""
import json
import base64
import hashlib
import logging
import os
import random
import re
import secrets
import subprocess
import time
import uuid
from datetime import datetime
from typing import Optional, Any
from urllib.parse import urlparse, parse_qs, parse_qsl, urljoin, urlencode, urlunparse

from config import Config
from mail_provider import MailProvider
from http_client import create_http_session, USER_AGENT

logger = logging.getLogger(__name__)


class AuthResult:
    """认证结果"""

    def __init__(self):
        self.email: str = ""
        self.password: str = ""
        self.session_token: str = ""
        self.access_token: str = ""
        self.device_id: str = ""
        self.csrf_token: str = ""
        self.id_token: str = ""
        self.refresh_token: str = ""
        self.cookie_header: str = ""

    def is_valid(self) -> bool:
        return bool(self.session_token and self.access_token)

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "password": self.password,
            "session_token": self.session_token,
            "access_token": self.access_token,
            "device_id": self.device_id,
            "csrf_token": self.csrf_token,
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "cookie_header": self.cookie_header,
        }


class AuthFlow:
    """注册/登录协议流"""

    def __init__(self, config: Config):
        self.config = config
        self._impersonate_candidates = ["chrome136", "chrome124", "chrome120"]
        self._impersonate_idx = 0
        self.session = create_http_session(
            proxy=config.proxy,
            impersonate=self._impersonate_candidates[self._impersonate_idx],
        )
        self.result = AuthResult()
        self._http_trace_enabled = str(os.getenv("AUTH_HTTP_TRACE", "0")).lower() in ("1", "true", "yes", "on")
        self._existing_email_verification_mode = ""
        self._existing_page_type = ""
        self._manual_login_verifier = (os.getenv("LOGIN_VERIFIER", "") or "").strip()
        self._captured_login_verifier = ""
        self._oauth_client_secret = (os.getenv("OAUTH_CLIENT_SECRET", "") or "").strip()
        self._oauth_client_id = "YOUR_OPENAI_WEB_CLIENT_ID"
        self._oauth_redirect_uri = "https://chatgpt.com/api/auth/callback/openai"
        self._oauth_scope = ""
        self._oauth_state = ""
        self._oauth_auth_url = ""
        self._client_auth_session_dump: dict[str, Any] = {}
        self._client_auth_session_id: str = ""
        self._dump_login_verifier: str = ""
        self._codex_rt_attempted: bool = False
        self._trace_dump_enabled = str(os.getenv("AUTH_TRACE_DUMP", "0")).lower() in ("1", "true", "yes", "on")
        self._trace_include_cookie = str(os.getenv("AUTH_TRACE_INCLUDE_COOKIE", "0")).lower() in (
            "1", "true", "yes", "on"
        )
        self._trace_dump_path = ""

    def _build_chatgpt_cookie_header(self) -> str:
        """
        导出当前会话中的 chatgpt.com 相关 cookie。

        说明：
        - `/backend-api/payments/checkout` 的 modern/custom 入口不仅依赖
          `__Secure-next-auth.session-token`，还会校验若干同域 cookie
          （如 csrf / oai-sc / Cloudflare 相关 cookie 等）。
        - 因此这里不能只回传 session_token，需要尽量保留当前会话里已经拿到的
          `chatgpt.com` 域 cookie 集合。
        """
        cookie_pairs: list[tuple[str, str]] = []
        seen: set[str] = set()

        try:
            jar_iter = list(self.session.cookies)
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
            if domain and "chatgpt.com" not in domain:
                continue
            if name in seen:
                continue
            seen.add(name)
            cookie_pairs.append((name, value))

        # 兜底补齐关键 cookie，避免某些 cookiejar 迭代行为差异导致遗漏
        critical_names = [
            "__Secure-next-auth.session-token",
            "__Host-next-auth.csrf-token",
            "__Secure-next-auth.callback-url",
            "oai-did",
            "oai-sc",
            "cf_clearance",
            "__cf_bm",
            "_cfuvid",
            "__cflb",
            "__stripe_mid",
            "__stripe_sid",
            "oai-client-auth-info",
            "oai-gn",
            "oai-nav-state",
            "oai-hlib",
            "_account_is_fedramp",
            "oai_consent_analytics",
            "oai_consent_marketing",
            "oai-allow-ne",
            "_ga",
            "_ga_9SHBSK2D9J",
            "_gcl_au",
            "_fbp",
            "_puid",
            "_dd_s",
            "g_state",
        ]
        for name in critical_names:
            if name in seen:
                continue
            try:
                value = self.session.cookies.get(name, "")
            except Exception:
                value = ""
            if value:
                seen.add(name)
                cookie_pairs.append((name, value))

        return "; ".join(f"{name}={value}" for name, value in cookie_pairs if name and value)
        if self._trace_dump_enabled:
            try:
                os.makedirs("outputs", exist_ok=True)
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                self._trace_dump_path = os.path.join("outputs", f"auth_trace_{ts}_{os.getpid()}.jsonl")
                logger.info(f"HTTP 明文抓包已启用: {self._trace_dump_path}")
            except Exception as e:
                logger.warning(f"初始化 HTTP 抓包文件失败: {e}")
                self._trace_dump_enabled = False

    def _trace_http(self, step: str, resp, extra_request: dict | None = None):
        """可选 HTTP 细粒度追踪（用于协议调试）"""
        if (not self._http_trace_enabled and not self._trace_dump_enabled) or resp is None:
            return
        try:
            req = getattr(resp, "request", None)
            method = getattr(req, "method", "") if req else ""
            req_url = getattr(req, "url", "") if req else ""
            req_body = ""
            req_headers = {}
            if req is not None:
                raw_req_body = getattr(req, "body", None)
                if raw_req_body is None:
                    raw_req_body = getattr(req, "content", None)
                if raw_req_body is None:
                    raw_req_body = getattr(req, "data", None)
                if isinstance(raw_req_body, bytes):
                    req_body = raw_req_body.decode("utf-8", errors="replace")
                elif raw_req_body is not None:
                    req_body = str(raw_req_body)
                try:
                    req_headers = dict(getattr(req, "headers", {}) or {})
                except Exception:
                    req_headers = {}

            # 手动补充请求信息（curl_cffi 某些场景 request.body/headers 为空）
            if isinstance(extra_request, dict):
                if not method:
                    method = str(extra_request.get("method", "") or "")
                if not req_url:
                    req_url = str(extra_request.get("url", "") or "")
                if not req_body:
                    maybe_body = extra_request.get("body", "")
                    if isinstance(maybe_body, bytes):
                        req_body = maybe_body.decode("utf-8", errors="replace")
                    else:
                        req_body = str(maybe_body or "")
                extra_headers = extra_request.get("headers", {})
                if isinstance(extra_headers, dict):
                    merged = dict(req_headers or {})
                    merged.update(extra_headers)
                    req_headers = merged

            status = getattr(resp, "status_code", "N/A")
            final_url = str(getattr(resp, "url", "") or "")
            req_cookie = (req_headers.get("Cookie", "") or "")
            location = (resp.headers.get("Location", "") or "")[:180]
            req_id = (resp.headers.get("x-request-id", "") or "")[:120]
            ctype = (resp.headers.get("Content-Type", "") or "")[:120]
            # 尽量保留完整 Set-Cookie（某些关键 cookie 可能在后续片段）
            set_cookie_list: list[str] = []
            try:
                get_list = getattr(resp.headers, "get_list", None) or getattr(resp.headers, "getlist", None)
                if callable(get_list):
                    vals = get_list("Set-Cookie")
                    if isinstance(vals, list):
                        set_cookie_list = [str(x) for x in vals if x]
            except Exception:
                set_cookie_list = []
            if not set_cookie_list:
                one = (resp.headers.get("Set-Cookie", "") or "")
                if one:
                    set_cookie_list = [one]
            set_cookie_raw = " || ".join(set_cookie_list)
            set_cookie = set_cookie_raw[:260]
            body = (resp.text or "").replace("\n", " ").replace("\r", " ")
            body = body[:260]
            req_headers_lc = {(str(k).lower()): v for k, v in (req_headers or {}).items()}

            if self._http_trace_enabled:
                logger.info(
                    "[HTTP TRACE] %s | %s %s -> %s | url=%s | location=%s | req_id=%s | ctype=%s | set_cookie=%s | body=%s",
                    step,
                    method,
                    req_url[:180],
                    status,
                    final_url[:180],
                    location,
                    req_id,
                    ctype,
                    set_cookie,
                    body,
                )
                if self._trace_include_cookie and req_cookie:
                    logger.info("[HTTP TRACE] %s | req_cookie=%s", step, req_cookie[:360])

            # 从多处信息中抓取 login_verifier/code_verifier
            self._sniff_login_verifier(req_url, f"{step}:req_url")
            self._sniff_login_verifier(req_body, f"{step}:req_body")
            self._sniff_login_verifier(final_url, f"{step}:final_url")
            self._sniff_login_verifier(location, f"{step}:location")
            raw_text = resp.text or ""
            self._sniff_login_verifier(raw_text, f"{step}:resp_body")

            # 明文 HTTP 抓包落盘（jsonl）
            if self._trace_dump_enabled and self._trace_dump_path:
                try:
                    include_req_cookie = self._env_flag("AUTH_TRACE_INCLUDE_REQ_COOKIE", "0")
                    record = {
                        "ts": datetime.utcnow().isoformat() + "Z",
                        "step": step,
                        "request": {
                            "method": method,
                            "url": req_url,
                            "body": req_body[:120000],
                            "headers": {
                                "Content-Type": (req_headers_lc.get("content-type", "") or "")[:240],
                                "Accept": (req_headers_lc.get("accept", "") or "")[:240],
                                "Referer": (req_headers_lc.get("referer", "") or "")[:500],
                                "Origin": (req_headers_lc.get("origin", "") or "")[:120],
                                **(
                                    {
                                        "Cookie": (req_headers_lc.get("cookie", "") or "")[:6000],
                                    }
                                    if include_req_cookie
                                    else {}
                                ),
                            },
                        },
                        "response": {
                            "status_code": status,
                            "url": final_url,
                            "location": resp.headers.get("Location", ""),
                            "x_request_id": resp.headers.get("x-request-id", ""),
                            "content_type": resp.headers.get("Content-Type", ""),
                            "set_cookie": set_cookie_raw,
                            "set_cookie_list": set_cookie_list,
                            "body": raw_text[:120000],
                        },
                        "captured_login_verifier": self._captured_login_verifier,
                    }
                    if self._trace_include_cookie and req_cookie:
                        record["request"]["headers"]["Cookie"] = req_cookie[:8000]
                    with open(self._trace_dump_path, "a", encoding="utf-8") as fw:
                        fw.write(json.dumps(record, ensure_ascii=False) + "\n")
                except Exception as e:
                    logger.debug(f"HTTP 抓包写入失败: {e}")
        except Exception as e:
            logger.debug(f"HTTP trace 输出失败: {e}")

    def _sniff_login_verifier(self, text: str, source: str = ""):
        """从任意文本中提取 login_verifier/code_verifier。"""
        if not text:
            return
        try:
            patterns = [
                r"(?:login_verifier|code_verifier|verifier)=([A-Za-z0-9._~-]{8,})",
                r'"(?:login_verifier|code_verifier|verifier)"\s*:\s*"([^"]{8,})"',
            ]
            for p in patterns:
                m = re.search(p, text)
                if not m:
                    continue
                v = (m.group(1) or "").strip()
                if not v:
                    continue
                if v != self._captured_login_verifier:
                    self._captured_login_verifier = v
                    logger.info("捕获 login_verifier 来源=%s len=%s", source or "unknown", len(v))
                return
        except Exception:
            return

    @staticmethod
    def _walk_collect_str_fields(obj: Any, wanted_keys: set[str], out: dict[str, str], depth: int = 0, max_depth: int = 6):
        """递归收集目标字段（仅字符串值）。"""
        if depth > max_depth or obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kk = (str(k) or "").strip().lower()
                if kk in wanted_keys and isinstance(v, str) and v.strip():
                    out[kk] = v.strip()
                AuthFlow._walk_collect_str_fields(v, wanted_keys, out, depth + 1, max_depth)
        elif isinstance(obj, list):
            for it in obj:
                AuthFlow._walk_collect_str_fields(it, wanted_keys, out, depth + 1, max_depth)

    def fetch_client_auth_session_dump(self, stage: str = "") -> dict:
        """
        尝试读取 auth.openai 的 client_auth_session_dump：
        - 可能包含 session_id / client_auth_session 的额外状态
        - 若出现 verifier/refresh 相关字段，自动注入当前流程
        """
        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Accept"] = "application/json"
        try:
            resp = self.session.get(
                "https://auth.openai.com/api/accounts/client_auth_session_dump",
                headers=headers,
                timeout=30,
            )
            self._trace_http(f"client_auth_session_dump_{stage or 'default'}", resp)
        except Exception as e:
            logger.debug(f"client_auth_session_dump 请求异常({stage}): {e}")
            return {}

        if resp.status_code != 200:
            logger.info(
                "client_auth_session_dump(%s) 非 200: %s",
                stage or "default",
                resp.status_code,
            )
            return {}

        try:
            data = resp.json()
        except Exception:
            logger.warning(f"client_auth_session_dump({stage}) JSON 解析失败")
            return {}

        if not isinstance(data, dict):
            return {}

        self._client_auth_session_dump = data
        cas = data.get("client_auth_session", {}) if isinstance(data.get("client_auth_session"), dict) else {}

        sid = (data.get("session_id", "") or "").strip() or (cas.get("session_id", "") or "").strip()
        if sid:
            self._client_auth_session_id = sid

        # 同步 OAuth client_id（若 dump 给出更准确值）
        dump_client_id = (cas.get("openai_client_id", "") or data.get("openai_client_id", "") or "").strip()
        if dump_client_id:
            self._oauth_client_id = dump_client_id

        wanted = {
            "login_verifier", "code_verifier", "verifier", "pkce_verifier", "oauth_code_verifier",
            "refresh_token", "oauth_refresh_token", "access_token", "id_token",
        }
        found: dict[str, str] = {}
        self._walk_collect_str_fields(data, wanted, found)

        # verifier 候选
        for key in ("login_verifier", "code_verifier", "verifier", "pkce_verifier", "oauth_code_verifier"):
            v = (found.get(key, "") or "").strip()
            if v and len(v) >= 8:
                self._dump_login_verifier = v
                self._captured_login_verifier = v
                logger.info("client_auth_session_dump 捕获 verifier: key=%s len=%s", key, len(v))
                break

        # token 候选（极少见，但若有直接收下）
        refresh = (found.get("refresh_token", "") or found.get("oauth_refresh_token", "")).strip()
        if refresh:
            self.result.refresh_token = refresh
        acc = (found.get("access_token", "") or "").strip()
        if acc:
            self.result.access_token = acc
        idt = (found.get("id_token", "") or "").strip()
        if idt:
            self.result.id_token = idt

        logger.info(
            "client_auth_session_dump(%s) 成功: top_keys=%s cas_keys=%s session_id=%s refresh=%s verifier=%s",
            stage or "default",
            list(data.keys())[:12],
            list(cas.keys())[:18] if isinstance(cas, dict) else [],
            (self._client_auth_session_id[:24] if self._client_auth_session_id else ""),
            "有" if self.result.refresh_token else "无",
            "有" if self._dump_login_verifier else "无",
        )
        return data

    @staticmethod
    def _is_tls_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        markers = ["curl: (35)", "tls connect error", "openssl_internal", "sslerror"]
        return any(m in msg for m in markers)

    @staticmethod
    def _is_registration_disallowed_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "registration_disallowed" in msg

    def _get_cookie_value_by_name(self, name: str) -> str:
        """按 cookie 名称获取值（忽略 domain 冲突）。"""
        try:
            jar = getattr(self.session.cookies, "jar", None)
            if jar is None:
                return ""
            target = (name or "").strip().lower()
            for c in jar:
                if (getattr(c, "name", "") or "").strip().lower() == target:
                    return (getattr(c, "value", "") or "").strip()
        except Exception:
            pass
        return ""

    def _extract_login_challenge_from_cookie(self) -> str:
        """
        从 login_session cookie 中提取 login_challenge。
        login_session 的第一段通常是 base64url(JSON)。
        """
        raw = self._get_cookie_value_by_name("login_session")
        if not raw:
            return ""
        try:
            p0 = raw.split(".")[0]
            p0 += "=" * (-len(p0) % 4)
            payload = json.loads(base64.urlsafe_b64decode(p0.encode("utf-8")).decode("utf-8"))
            return (payload.get("login_challenge", "") or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _extract_query_first(url: str, keys: list[str]) -> str:
        if not url:
            return ""
        try:
            qs = parse_qs(urlparse(url).query)
        except Exception:
            return ""
        for k in keys:
            val = qs.get(k, [None])[0]
            if val:
                return val
        return ""

    @staticmethod
    def _extract_page_type(resp_json: dict | None) -> str:
        if not isinstance(resp_json, dict):
            return ""
        page = resp_json.get("page", {})
        if not isinstance(page, dict):
            return ""
        return (page.get("type", "") or "").strip()

    @staticmethod
    def _extract_continue_url_from_step(resp_json: dict | None) -> str:
        """
        从 auth step 响应提取 continue_url：
        - 顶层 continue_url
        - page.type=external_url 时 payload.url
        """
        if not isinstance(resp_json, dict):
            return ""
        continue_url = (resp_json.get("continue_url", "") or "").strip()
        if continue_url:
            return continue_url
        page = resp_json.get("page", {})
        if not isinstance(page, dict):
            return ""
        if (page.get("type", "") or "").strip() != "external_url":
            return ""
        payload = page.get("payload", {})
        if not isinstance(payload, dict):
            return ""
        return (payload.get("url", "") or "").strip()

    @staticmethod
    def _env_flag(name: str, default: str = "0") -> bool:
        return str(os.getenv(name, default)).lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _b64url_no_pad(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    def _remember_oauth_params(self, auth_url: str):
        """从 authorize URL 记住 OAuth 参数，供后续 token exchange 使用。"""
        if not auth_url:
            return
        self._oauth_auth_url = auth_url
        try:
            qs = parse_qs(urlparse(auth_url).query)
            self._oauth_client_id = (qs.get("client_id", [self._oauth_client_id])[0] or self._oauth_client_id).strip()
            self._oauth_redirect_uri = (
                qs.get("redirect_uri", [self._oauth_redirect_uri])[0] or self._oauth_redirect_uri
            ).strip()
            self._oauth_scope = (qs.get("scope", [""])[0] or "").strip()
            self._oauth_state = (qs.get("state", [""])[0] or "").strip()
        except Exception:
            return

    def _build_pkce_pair(self, raw_bytes: int = 64) -> tuple[str, str]:
        """生成 (code_verifier, code_challenge)。"""
        verifier = self._b64url_no_pad(secrets.token_bytes(max(32, int(raw_bytes))))
        if len(verifier) < 43:
            verifier = (verifier + ("A" * 43))[:43]
        if len(verifier) > 128:
            verifier = verifier[:128]
        challenge = self._b64url_no_pad(hashlib.sha256(verifier.encode("utf-8")).digest())
        return verifier, challenge

    def _build_codex_authorize(self, prompt_override: Optional[str] = None) -> tuple[str, str, str, str, str]:
        """
        构建用于获取 refresh_token 的 Codex OAuth 授权 URL。
        参考 any-auto-register 的实现：独立 client_id + redirect_uri + 可控 PKCE。
        """
        client_id = (os.getenv("OAUTH_CODEX_CLIENT_ID", "") or "").strip() or "app_EMoamEEZ73f0CkXaXp7hrann"
        redirect_uri = (os.getenv("OAUTH_CODEX_REDIRECT_URI", "") or "").strip() or "http://localhost:1455/auth/callback"
        scope = (os.getenv("OAUTH_CODEX_SCOPE", "") or "").strip() or "openid email profile offline_access"
        state = self._b64url_no_pad(secrets.token_bytes(24))
        verifier, challenge = self._build_pkce_pair()
        prompt = (
            (os.getenv("OAUTH_CODEX_PROMPT", "login") or "").strip()
            if prompt_override is None
            else (prompt_override or "").strip()
        )
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        if prompt:
            params["prompt"] = prompt
        auth_url = f"https://auth.openai.com/oauth/authorize?{urlencode(params)}"
        return auth_url, state, verifier, redirect_uri, client_id

    @staticmethod
    def _callback_has_code(url: str, redirect_uri: str) -> bool:
        if not url:
            return False
        try:
            cb_base = (redirect_uri or "").split("?", 1)[0].rstrip("/")
            target = url.split("?", 1)[0].rstrip("/")
            if cb_base and target == cb_base:
                qs = parse_qs(urlparse(url).query)
                return bool((qs.get("code", [""])[0] or "").strip())
        except Exception:
            return False
        return False

    def _follow_authorize_for_callback(self, start_url: str, redirect_uri: str, trace_prefix: str) -> tuple[str, str]:
        """
        跟随 auth.openai.com 授权链路，捕获 callback（不消费 callback）。
        返回 (callback_url, final_url)。
        """
        current = start_url
        callback_url = ""
        for i in range(12):
            if self._callback_has_code(current, redirect_uri):
                callback_url = current
                break
            resp = self.session.get(
                current,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://chatgpt.com/",
                    "User-Agent": USER_AGENT,
                },
                timeout=30,
                allow_redirects=False,
            )
            self._trace_http(f"{trace_prefix}_hop_{i+1}", resp)

            # workspace/consent 页面 200 时，主动选择 workspace，拿下一跳 continue_url
            if resp.status_code == 200:
                is_workspace_like = (
                    ("/workspace" in current)
                    or ("/sign-in-with-chatgpt/" in current)
                    or ("/consent" in current)
                )
                if is_workspace_like:
                    workspace_id = self._extract_workspace_id() or self._extract_workspace_id_from_html(resp.text or "")
                    if workspace_id:
                        next_url = self._workspace_select(workspace_id)
                        if next_url:
                            if next_url.startswith("/"):
                                next_url = urljoin("https://auth.openai.com", next_url)
                            current = next_url
                            continue

            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            loc = (resp.headers.get("Location", "") or "").strip()
            if not loc:
                break
            if loc.startswith("/"):
                loc = urljoin(current, loc)
            if self._callback_has_code(loc, redirect_uri):
                callback_url = loc
                current = loc
                break
            current = loc
        return callback_url, current

    @staticmethod
    def _drop_query_keys(url: str, drop_keys: set[str]) -> str:
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            params = parse_qsl(parsed.query, keep_blank_values=True)
            kept = [(k, v) for (k, v) in params if (k or "").strip() not in drop_keys]
            return urlunparse(parsed._replace(query=urlencode(kept)))
        except Exception:
            return url

    def _exchange_codex_callback_code(
        self,
        callback_url: str,
        expected_state: str,
        verifier: str,
        redirect_uri: str,
        client_id: str,
    ) -> bool:
        qs = parse_qs(urlparse(callback_url).query)
        code = (qs.get("code", [""])[0] or "").strip()
        got_state = (qs.get("state", [""])[0] or "").strip()
        if not code:
            logger.warning("Codex callback 缺少 code")
            return False
        if expected_state and got_state and got_state != expected_state:
            logger.warning("Codex callback state 不匹配，期望=%s 实际=%s", expected_state[:20], got_state[:20])
            return False

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://auth.openai.com",
            "Referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            "User-Agent": USER_AGENT,
        }
        form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        encoded_form = urlencode(form)
        resp = self.session.post(
            "https://auth.openai.com/oauth/token",
            headers=headers,
            data=encoded_form,
            timeout=30,
        )
        self._trace_http(
            "oauth_token_exchange_codex_pkce",
            resp,
            extra_request={
                "method": "POST",
                "url": "https://auth.openai.com/oauth/token",
                "body": encoded_form,
                "headers": headers,
            },
        )
        if resp.status_code != 200:
            logger.warning("Codex oauth/token 失败: %s - %s", resp.status_code, (resp.text or "")[:220])
            return False
        data = resp.json() if resp is not None else {}
        self.result.id_token = data.get("id_token", self.result.id_token)
        self.result.access_token = data.get("access_token", self.result.access_token)
        self.result.refresh_token = data.get("refresh_token", self.result.refresh_token)
        logger.info(
            "Codex OAuth 交换成功: access=%s refresh=%s",
            "有" if self.result.access_token else "无",
            "有" if self.result.refresh_token else "无",
        )
        return True

    def _codex_drive_login_from_log_in(self, mail_provider: Optional[MailProvider] = None) -> str:
        """
        当 Codex 授权回落到 /log-in 时，补走一次纯协议登录推进状态机。
        返回可继续跟随的 continue_url（若无则返回空字符串）。
        """
        email = (self.result.email or "").strip()
        if not email:
            logger.warning("Codex 登录推进缺少 email")
            return ""
        password = (self.result.password or "").strip() or self._default_password_from_email(email)
        self.result.password = password

        device_id = (self.result.device_id or "").strip() or (self.session.cookies.get("oai-did", "") or "").strip()
        if not device_id:
            device_id = str(uuid.uuid4())
            self.result.device_id = device_id

        sentinel = self.get_sentinel_token(device_id)
        step = self.authorize_continue(
            email=email,
            sentinel_token=sentinel,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            trace_step="authorize_continue_login_codex",
        )
        page_type = self._extract_page_type(step)
        continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(step))

        if page_type == "login_password" or "/log-in/password" in continue_url:
            step = self.login_password_verify(password)
            page_type = self._extract_page_type(step)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(step))

        need_otp = (page_type == "email_otp_verification") or ("/email-verification" in (continue_url or ""))
        if need_otp:
            if mail_provider is None:
                logger.warning("Codex 登录推进需要 OTP，但未提供 mail_provider")
                return continue_url or ""
            try:
                otp_timeout = max(30, int(os.getenv("OTP_TIMEOUT", "180")))
            except Exception:
                otp_timeout = 180
            otp_sent_at = time.time()
            if not self.kickoff_otp_delivery("codex_login_need_otp"):
                self.send_otp()
            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            otp_resp = self.verify_otp(otp_code)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(otp_resp))

        # add-phone 分支（可选）：
        # 仅在配置了手机号与验证码获取方式时尝试自动推进
        if self._is_add_phone_state(page_type="", continue_url=continue_url):
            next_url = self._handle_add_phone_verification(continue_url=continue_url)
            if next_url:
                continue_url = self._normalize_continue_url(next_url)

        return continue_url or ""

    @staticmethod
    def _is_add_phone_state(page_type: str = "", continue_url: str = "") -> bool:
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "add_phone") or ("add-phone" in cu)

    def _phone_headers(self, referer: str) -> dict:
        headers = self._common_headers(referer)
        headers["Accept"] = "application/json"
        headers["Content-Type"] = "application/json"
        headers["Origin"] = "https://auth.openai.com"
        device_id = (self.result.device_id or "").strip() or (self.session.cookies.get("oai-did", "") or "").strip()
        if device_id:
            headers["oai-device-id"] = device_id
        return headers

    def _add_phone_send(self, phone_number: str) -> dict:
        headers = self._phone_headers("https://auth.openai.com/add-phone")
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/add-phone/send",
            headers=headers,
            json={"phone_number": phone_number},
            timeout=30,
        )
        self._trace_http("add_phone_send", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"add-phone/send 失败: {resp.status_code} - {(resp.text or '')[:220]}")
        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}

    def _phone_otp_resend(self) -> bool:
        headers = self._phone_headers("https://auth.openai.com/phone-verification")
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/phone-otp/resend",
            headers=headers,
            timeout=30,
        )
        self._trace_http("phone_otp_resend", resp)
        return resp.status_code == 200

    def _phone_otp_validate(self, code: str) -> dict:
        headers = self._phone_headers("https://auth.openai.com/phone-verification")
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/phone-otp/validate",
            headers=headers,
            json={"code": code},
            timeout=30,
        )
        self._trace_http("phone_otp_validate", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"phone-otp/validate 失败: {resp.status_code} - {(resp.text or '')[:220]}")
        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}

    @staticmethod
    def _extract_otp6(text: str) -> str:
        if not text:
            return ""
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        return (m.group(1) if m else "").strip()

    def _read_phone_otp_from_cmd(self) -> str:
        """
        从环境变量 OPENAI_PHONE_OTP_CMD 指定的命令读取手机验证码（stdout）。
        命令输出中只要出现 6 位数字即视为命中。
        """
        cmd = (os.getenv("OPENAI_PHONE_OTP_CMD", "") or "").strip()
        if not cmd:
            return ""
        try:
            out = subprocess.check_output(cmd, shell=True, text=True, timeout=20)
            return self._extract_otp6(out or "")
        except Exception:
            return ""

    def _wait_phone_otp(self, timeout: int = 180) -> str:
        static_otp = self._extract_otp6(os.getenv("OPENAI_PHONE_OTP", "") or "")
        if static_otp:
            return static_otp

        deadline = time.time() + max(20, int(timeout))
        while time.time() < deadline:
            code = self._read_phone_otp_from_cmd()
            if code:
                return code
            time.sleep(4)
        raise TimeoutError(f"等待手机 OTP 超时 ({timeout}s)")

    def _handle_add_phone_verification(self, continue_url: str = "") -> str:
        """
        处理 add-phone 验证分支（纯协议）：
        - 需要通过环境变量提供号码与验证码来源：
          - OPENAI_PHONE_NUMBER=+1...
          - OPENAI_PHONE_OTP_CMD='...返回短信内容...' 或 OPENAI_PHONE_OTP=123456
        """
        phone_raw = (os.getenv("OPENAI_PHONE_NUMBER", "") or "").strip()
        phone_candidates = [x.strip() for x in phone_raw.split(",") if x.strip()]
        if not phone_candidates:
            logger.warning("命中 add-phone，但未配置 OPENAI_PHONE_NUMBER，无法继续推进")
            return continue_url or ""

        try:
            otp_timeout = max(30, int(os.getenv("OPENAI_PHONE_OTP_TIMEOUT", "180")))
        except Exception:
            otp_timeout = 180

        last_err = ""
        for idx, phone in enumerate(phone_candidates, 1):
            try:
                logger.info("add-phone 尝试号码 %s/%s: %s", idx, len(phone_candidates), phone)
                send_resp = self._add_phone_send(phone)
                send_page_type = self._extract_page_type(send_resp)
                send_continue = self._normalize_continue_url(self._extract_continue_url_from_step(send_resp))
                if send_page_type not in ("phone_otp_verification", "external_url") and "phone-verification" not in (send_continue or ""):
                    logger.warning(
                        "add-phone/send 未进入手机验证码页: page=%s continue=%s",
                        send_page_type or "(empty)",
                        (send_continue or "")[:180],
                    )
                    continue

                phone_code = self._wait_phone_otp(timeout=otp_timeout)
                validate_resp = self._phone_otp_validate(phone_code)
                next_url = self._normalize_continue_url(self._extract_continue_url_from_step(validate_resp))
                logger.info("add-phone 验证通过，next=%s", (next_url or "")[:180])
                return next_url or continue_url or ""
            except Exception as e:
                last_err = str(e)
                logger.warning("add-phone 号码 %s 失败: %s", phone, e)
                try:
                    self._phone_otp_resend()
                except Exception:
                    pass

        if last_err:
            logger.warning("add-phone 阶段未成功: %s", last_err)
        return continue_url or ""

    def _codex_refresh_retry_after_add_phone(
        self,
        auth_url: str,
        redirect_uri: str,
        attempts: int = 3,
        sleep_seconds: float = 1.2,
    ) -> tuple[str, str]:
        """
        当命中 add-phone 时，按“刷新重试”策略重复发起 authorize，
        期望命中不需要 add-phone 的分支并直接拿 callback code。
        """
        callback_url = ""
        final_url = ""
        start_url = self._drop_query_keys(auth_url, {"prompt"}) or auth_url
        rounds = max(1, int(attempts))
        wait_s = max(0.0, float(sleep_seconds))

        for i in range(rounds):
            callback_url, final_url = self._follow_authorize_for_callback(
                start_url,
                redirect_uri,
                f"codex_add_phone_refresh_retry_{i+1}",
            )
            if callback_url:
                return callback_url, final_url
            if i < rounds - 1 and wait_s > 0:
                time.sleep(wait_s)

        return callback_url, final_url

    def oauth_codex_rt_exchange(self, mail_provider: Optional[MailProvider] = None) -> bool:
        """
        纯协议方式获取 RT（参考 any-auto-register）：
        - 使用独立 Codex OAuth 参数重新授权（可控 PKCE）
        - 捕获 callback code（不消费）
        - 直接调 /oauth/token 交换 access_token + refresh_token
        """
        allow_retry = self._env_flag("OAUTH_CODEX_RT_ALLOW_RETRY", "0")
        if self._codex_rt_attempted and (not allow_retry):
            logger.info("Codex RT 本轮已尝试过，跳过重复尝试（可用 OAUTH_CODEX_RT_ALLOW_RETRY=1 强制重试）")
            return False
        self._codex_rt_attempted = True

        logger.info("尝试 Codex OAuth 直连换取 refresh_token ...")
        try:
            auth_url, state, verifier, redirect_uri, client_id = self._build_codex_authorize()
            self._oauth_auth_url = auth_url
            self._oauth_client_id = client_id
            self._oauth_redirect_uri = redirect_uri
            self._oauth_state = state
            self._manual_login_verifier = verifier
            self._captured_login_verifier = verifier
            callback_url, final_url = self._follow_authorize_for_callback(
                auth_url, redirect_uri, "codex_authorize"
            )

            # 若被打回 /log-in，补走一次协议登录，再继续授权链路
            if (not callback_url) and "/log-in" in (final_url or ""):
                logger.info("Codex 授权回落到 /log-in，尝试协议推进登录状态...")
                continue_url = ""
                try:
                    continue_url = self._codex_drive_login_from_log_in(mail_provider=mail_provider)
                except Exception as e:
                    logger.warning(f"Codex 登录推进失败，改走 no-prompt 兜底: {e}")
                if continue_url:
                    # 命中 add-phone 时，支持“刷新重试”策略（不立刻放弃）
                    if self._is_add_phone_state(page_type="", continue_url=continue_url) and self._env_flag(
                        "OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY", "1"
                    ):
                        try:
                            retry_count = max(1, int(os.getenv("OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY_COUNT", "3")))
                        except Exception:
                            retry_count = 3
                        try:
                            retry_sleep = max(0.0, float(os.getenv("OAUTH_CODEX_ADD_PHONE_REFRESH_SLEEP", "1.2")))
                        except Exception:
                            retry_sleep = 1.2
                        logger.info("命中 add-phone，执行 authorize 刷新重试: count=%s sleep=%.1fs", retry_count, retry_sleep)
                        callback_url, final_url = self._codex_refresh_retry_after_add_phone(
                            auth_url=auth_url,
                            redirect_uri=redirect_uri,
                            attempts=retry_count,
                            sleep_seconds=retry_sleep,
                        )
                    else:
                        callback_url, final_url = self._follow_authorize_for_callback(
                            continue_url,
                            redirect_uri,
                            "codex_post_login",
                        )

            # 兜底：去掉 prompt=login 再发起一次授权
            if not callback_url:
                no_prompt_url = self._drop_query_keys(auth_url, {"prompt"})
                if no_prompt_url and no_prompt_url != auth_url:
                    callback_url, final_url = self._follow_authorize_for_callback(
                        no_prompt_url,
                        redirect_uri,
                        "codex_authorize_noprompt",
                    )

            if not callback_url:
                logger.warning("Codex OAuth 未捕获 callback code, final=%s", (final_url or "")[:180])
                return False
            return self._exchange_codex_callback_code(
                callback_url=callback_url,
                expected_state=state,
                verifier=verifier,
                redirect_uri=redirect_uri,
                client_id=client_id,
            )
        except Exception as e:
            logger.warning(f"Codex OAuth 交换异常: {e}")
            return False

    def _inject_pkce_into_auth_url(self, auth_url: str) -> str:
        """为 authorize URL 注入 PKCE 参数（可选）。"""
        if not auth_url:
            return auth_url
        if not self._env_flag("OAUTH_SECONDARY_PKCE", "0"):
            return auth_url

        try:
            parsed = urlparse(auth_url)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if params.get("code_challenge") and params.get("code_challenge_method"):
                return auth_url

            verifier, challenge = self._build_pkce_pair()
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
            new_url = urlunparse(parsed._replace(query=urlencode(params)))
            # 若用户未手动指定 verifier，则自动注入本轮 verifier
            if not self._manual_login_verifier:
                self._manual_login_verifier = verifier
            logger.info(
                "已启用二次 PKCE 注入: verifier_len=%s challenge=%s...",
                len(verifier),
                challenge[:16],
            )
            return new_url
        except Exception as e:
            logger.warning(f"注入 PKCE 参数失败，回退原始 auth_url: {e}")
            return auth_url

    @staticmethod
    def _safe_b64url_decode_text(data: str) -> str:
        if not data:
            return ""
        try:
            s = data + "=" * (-len(data) % 4)
            return base64.urlsafe_b64decode(s.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _extract_hydra_redirect_values(self) -> list[str]:
        """从 hydra_redirect cookie 中提取可能的会话值。"""
        raw = self._get_cookie_value_by_name("hydra_redirect")
        if not raw:
            return []
        out: list[str] = []
        try:
            p0 = (raw.split(".", 1)[0] or "").strip()
            text = self._safe_b64url_decode_text(p0)
            if text:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    for v in obj.values():
                        if isinstance(v, str) and v.strip():
                            vv = v.strip()
                            out.append(vv)
                            if "|" in vv:
                                out.extend([x for x in vv.split("|") if isinstance(x, str) and x.strip()])
        except Exception:
            return out
        return out

    def _collect_code_verifier_candidates(self, callback_url: str, continue_url: str) -> list[tuple[str, str]]:
        """收集 code_verifier 候选（来源 + 值）。"""
        raw_candidates: list[tuple[str, str]] = [
            ("query", self._extract_query_first(continue_url, ["login_verifier", "code_verifier", "verifier"])),
            ("query_callback", self._extract_query_first(callback_url, ["login_verifier", "code_verifier", "verifier"])),
            ("dump", self._dump_login_verifier),
            ("captured", self._captured_login_verifier),
            ("manual", self._manual_login_verifier),
            ("cookie_login_verifier", self._get_cookie_value_by_name("login_verifier")),
            ("cookie_code_verifier", self._get_cookie_value_by_name("code_verifier")),
            ("cookie_login_challenge", self._extract_login_challenge_from_cookie()),
            ("cookie_nextauth_state", self._get_cookie_value_by_name("__Secure-next-auth.state")),
        ]

        # hydra_redirect 中可能包含编码后的 csrf/session 串，作为实验候选
        for i, hv in enumerate(self._extract_hydra_redirect_values()):
            raw_candidates.append((f"hydra_{i}", hv))

        out: list[tuple[str, str]] = []
        seen: set[str] = set()

        max_len = max(128, int(os.getenv("OAUTH_MAX_VERIFIER_LEN", "4096")))
        for src, val in raw_candidates:
            v = (val or "").strip()
            if not v:
                continue
            if len(v) > max_len:
                v = v[:max_len]
            if v not in seen:
                seen.add(v)
                out.append((src, v))
            # PKCE 标准长度 43~128；对超长候选补一个截断版本
            if len(v) > 128:
                v128 = v[:128]
                if v128 not in seen:
                    seen.add(v128)
                    out.append((f"{src}_trunc128", v128))

        return out

    def _rotate_impersonate_session(self) -> bool:
        """仅在 curl_cffi 指纹模式内切换 UA 指纹版本重试。"""
        if self._impersonate_idx >= len(self._impersonate_candidates) - 1:
            return False
        self._impersonate_idx += 1
        imp = self._impersonate_candidates[self._impersonate_idx]
        logger.warning(f"TLS 异常，切换指纹重试: impersonate={imp}")
        self.session = create_http_session(proxy=self.config.proxy, impersonate=imp)
        return True

    @staticmethod
    def _datadog_trace_headers() -> dict:
        """生成 Datadog APM 追踪头。

        OpenAI 前端集成 Datadog RUM，所有真实浏览器请求都带这 6 个头；
        缺失会被风控判定为非浏览器会话，OTP 邮件等敏感操作会被 silent-drop
        （接口返 200 但邮件不下发）。

        参考 https://github.com/zc-zhangchen/any-auto-register
        platforms/chatgpt/utils.py:generate_datadog_trace（MIT）。
        """
        trace_id = str(random.getrandbits(64))
        parent_id = str(random.getrandbits(64))
        trace_hex = format(int(trace_id), "016x")
        parent_hex = format(int(parent_id), "016x")
        return {
            "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
            "tracestate": "dd=s:1;o:rum",
            "x-datadog-origin": "rum",
            "x-datadog-parent-id": parent_id,
            "x-datadog-sampling-priority": "1",
            "x-datadog-trace-id": trace_id,
        }

    def _common_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        """
        构造通用请求头。

        关键点：
        - Origin 必须与 Referer 同源（尤其 auth.openai.com 的状态机接口），
          否则容易触发 invalid_state / 风控分支。
        - 在 auth.openai.com 域下，尽量补充 oai-device-id，提升状态机连续性。
        - 全请求注入 Datadog trace 头，避免 OTP silent-drop。
        """
        origin = "https://chatgpt.com"
        try:
            parsed = urlparse(referer or "")
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass

        headers = {
            "Accept": "application/json",
            "Referer": referer,
            "Origin": origin,
            "User-Agent": USER_AGENT,
        }

        # auth.openai.com 侧请求补设备标识（若可得）
        try:
            host = (urlparse(origin).netloc or "").lower()
        except Exception:
            host = ""
        if "auth.openai.com" in host:
            device_id = (self.result.device_id or "").strip() or (self.session.cookies.get("oai-did", "") or "").strip()
            if device_id:
                headers["oai-device-id"] = device_id

        headers.update(self._datadog_trace_headers())
        return headers

    # ── Step 1: 检查代理连通性 ──
    def check_proxy(self) -> bool:
        logger.info("检查网络连通性...")
        try:
            resp = self.session.get("https://cloudflare.com/cdn-cgi/trace", timeout=15)
            if resp.status_code == 200:
                loc = re.search(r"loc=(\w+)", resp.text)
                ip = re.search(r"ip=([^\n]+)", resp.text)
                logger.info(f"网络正常 - IP: {ip.group(1) if ip else 'N/A'}, "
                            f"地区: {loc.group(1) if loc else 'N/A'}")
            else:
                logger.warning(f"网络探测异常: cloudflare trace {resp.status_code}")

            # 关键链路探测: chatgpt csrf
            csrf_headers = self._common_headers("https://chatgpt.com/auth/login")
            csrf_resp = self.session.get(
                "https://chatgpt.com/api/auth/csrf",
                headers=csrf_headers,
                timeout=20,
            )
            if csrf_resp.status_code == 200:
                logger.info("chatgpt csrf 连通正常")
                return True

            logger.warning(f"chatgpt csrf 连通异常: {csrf_resp.status_code}")
            return False
        except Exception as e:
            logger.error(f"网络检查失败: {e}")
        return False

    # ── Step 2: 获取 CSRF Token ──
    def get_csrf_token(self) -> str:
        logger.info("[1/10] 获取 CSRF Token...")
        headers = self._common_headers("https://chatgpt.com/auth/login")

        # Cloudflare 可能在短时间内多次请求后返回 403，重试 3 次
        for attempt in range(3):
            try:
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/csrf",
                    headers=headers,
                    timeout=30,
                )
            except Exception as e:
                if self._is_tls_error(e) and self._rotate_impersonate_session():
                    continue
                if self._is_tls_error(e):
                    raise RuntimeError(
                        "chatgpt.com TLS 握手失败，当前网络无法建立到 /api/auth/csrf 的 HTTPS 连接。"
                        "请切换可直连 chatgpt.com 的网络或在界面中配置可用代理后重试。"
                    ) from e
                raise
            if resp.status_code == 403 and attempt < 2:
                wait = (attempt + 1) * 5
                logger.warning(f"Cloudflare 403, {wait}s 后重试 ({attempt + 1}/3)...")
                import time
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break

        self._trace_http("chatgpt_csrf", resp)
        csrf = resp.json().get("csrfToken", "")
        if not csrf:
            raise RuntimeError("CSRF Token 获取失败")
        self.result.csrf_token = csrf
        logger.info(f"CSRF Token: {csrf[:20]}...")
        return csrf

    # ── Step 3: 获取 auth URL ──
    def get_auth_url(self, csrf_token: str) -> str:
        logger.info("[2/10] 获取 OpenAI 授权地址...")
        headers = self._common_headers("https://chatgpt.com/auth/login")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        resp = self.session.post(
            "https://chatgpt.com/api/auth/signin/openai",
            headers=headers,
            data={
                "csrfToken": csrf_token,
                "callbackUrl": "https://chatgpt.com/",
                "json": "true",
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._trace_http("chatgpt_signin_openai", resp)
        auth_url = resp.json().get("url", "")
        if not auth_url:
            raise RuntimeError("Auth URL 获取失败")
        # 记住 OAuth 参数，并根据开关可选注入 PKCE
        self._remember_oauth_params(auth_url)
        auth_url = self._inject_pkce_into_auth_url(auth_url)
        self._remember_oauth_params(auth_url)
        logger.info(f"Auth URL: {auth_url[:80]}...")
        return auth_url

    # ── Step 4: OAuth 初始化 & 获取 device_id ──
    def auth_oauth_init(self, auth_url: str) -> str:
        logger.info("[3/10] OAuth 初始化...")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://chatgpt.com/auth/login",
            "User-Agent": self._common_headers()["User-Agent"],
        }
        resp = self.session.get(auth_url, headers=headers, timeout=30, allow_redirects=True)
        self._trace_http("auth_oauth_init", resp)

        # 从 cookie 获取 oai-did
        device_id = ""
        for cookie in self.session.cookies:
            if hasattr(cookie, "name"):
                if cookie.name == "oai-did":
                    device_id = cookie.value
                    break
            elif isinstance(cookie, str) and cookie == "oai-did":
                device_id = self.session.cookies.get("oai-did", "")
                break

        # curl_cffi cookies 访问方式
        if not device_id:
            try:
                device_id = self.session.cookies.get("oai-did", "")
            except Exception:
                pass

        # fallback: 从 HTML 提取
        if not device_id:
            m = re.search(r'oai-did["\s:=]+([a-f0-9-]{36})', resp.text)
            if m:
                device_id = m.group(1)

        if not device_id:
            device_id = str(uuid.uuid4())
            logger.warning(f"未从响应中获取 device_id，使用生成值: {device_id}")

        self.result.device_id = device_id
        logger.info(f"Device ID: {device_id}")
        return device_id

    # ── Step 5: 获取 Sentinel Token ──
    def get_sentinel_token(self, device_id: str) -> str:
        logger.info("[4/10] 获取 Sentinel Token (PoW)...")
        from sentinel import get_sentinel_token
        token = get_sentinel_token(self.session, device_id=device_id, flow="authorize_continue")
        self._last_sentinel_token = token or ""
        logger.info("Sentinel Token 获取成功")
        return token

    # ── Step 6: 提交注册邮箱 ──
    def authorize_continue(
        self,
        email: str,
        sentinel_token: str,
        screen_hint: str = "signup",
        referer: str = "https://auth.openai.com/create-account",
        trace_step: str = "",
    ) -> dict:
        """调用 /api/accounts/authorize/continue，返回 JSON。"""
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token
        payload = {
            "username": {"value": email, "kind": "email"},
            "screen_hint": screen_hint,
        }
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=headers,
            json=payload,
            timeout=30,
        )
        self._trace_http(trace_step or f"authorize_continue_{screen_hint}", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:360]
            raise RuntimeError(
                f"authorize/continue 失败(screen_hint={screen_hint}): HTTP {resp.status_code} - {body}"
            )
        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}

    def signup(self, email: str, sentinel_token: str) -> bool:
        """提交注册邮箱。返回 True 表示走新注册流程，False 表示已有账号走 OTP 登录流程"""
        logger.info("[5/10] 提交注册邮箱...")
        data = self.authorize_continue(
            email=email,
            sentinel_token=sentinel_token,
            screen_hint="signup",
            referer="https://auth.openai.com/create-account",
            trace_step="authorize_continue_signup",
        )

        # 检测 page_type/continue_url，区分新账号与已有账号
        try:
            page = (data.get("page") or {}) if isinstance(data, dict) else {}
            page_type = (page.get("type") or "").strip()
            payload = (page.get("payload") or {}) if isinstance(page, dict) else {}
            continue_url = (data.get("continue_url") or "").strip()

            # 新账号标准分支
            if page_type == "create_account_password" or "/create-account/password" in continue_url:
                self._is_existing_account = False
                self._existing_email_verification_mode = ""
                self._existing_page_type = page_type
                logger.info("注册邮箱已提交")
                return True

            # 已有账号 OTP 分支
            if page_type == "email_otp_verification":
                self._existing_email_verification_mode = (payload.get("email_verification_mode", "") or "").strip()
                self._existing_page_type = page_type
                logger.info("检测到已有账号，切换到 OTP 登录流程")
                self._is_existing_account = True
                return False

            # 未知 page_type：通常是社交登录/风控分支，按已有账号处理，避免误进 register_password 导致 invalid_state
            self._existing_email_verification_mode = (payload.get("email_verification_mode", "") or "").strip()
            self._existing_page_type = page_type
            self._is_existing_account = True
            logger.warning(
                "authorize/continue 返回非标准注册页面: page_type=%s continue_url=%s，按已有账号流程处理",
                page_type or "(empty)",
                continue_url[:180] or "(empty)",
            )
            return False
        except Exception:
            # JSON 解析失败时保守按新注册处理
            self._is_existing_account = False
            self._existing_email_verification_mode = ""
            self._existing_page_type = ""
            logger.info("注册邮箱已提交")
            return True

    # ── Step 6.5: 注册密码 ──
    def register_password(self, email: str) -> bool:
        logger.info("[5.5/10] 注册密码...")
        # 按需求：密码默认使用注册邮箱，去掉 '@'
        # 例如: abc123@example.com -> abc123example.com
        password = self._default_password_from_email(email)
        self.result.password = password

        # 先访问 create-account/password 页面（HAR 确认需要此步建立服务端状态）
        try:
            pw_page = self.session.get(
                "https://auth.openai.com/create-account/password",
                headers=self._common_headers("https://auth.openai.com/create-account"),
                timeout=15,
            )
            logger.info(f"create-account/password 页面: {pw_page.status_code}")
        except Exception as e:
            logger.warning(f"访问 create-account/password 页面失败: {e}")

        # 注册前需要刷新 sentinel token，且 flow 必须为 username_password_create
        if self.result.device_id:
            try:
                from sentinel import get_sentinel_token as _get_st
                token = _get_st(self.session, device_id=self.result.device_id,
                                flow="username_password_create")
                self._last_sentinel_token = token or ""
                logger.info("Sentinel Token 获取成功")
            except Exception as e:
                logger.warning(f"注册前刷新 sentinel 失败: {e}")

        headers = self._common_headers("https://auth.openai.com/create-account/password")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/user/register",
            headers=headers,
            json={"password": password, "username": email},
            timeout=30,
        )
        self._trace_http("register_password", resp)
        if resp.status_code != 200:
            logger.warning(f"密码注册返回 {resp.status_code}: {resp.text[:200]}")
            return False
        logger.info("密码注册成功")
        return True

    # ── Step 7: 发送 OTP ──
    def send_otp(self):
        logger.info("[6/10] 发送 OTP...")
        headers = self._common_headers("https://auth.openai.com/create-account/password")
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        # zhuce6 用 GET /api/accounts/email-otp/send
        resp = self.session.get(
            "https://auth.openai.com/api/accounts/email-otp/send",
            headers=headers,
            timeout=30,
        )
        self._trace_http("send_email_otp", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"发送 OTP 失败: {resp.status_code} - {resp.text[:200]}")
        logger.info("OTP 已发送到邮箱")

    def send_passwordless_otp(self, referer: str = "https://auth.openai.com/create-account/password") -> bool:
        """
        走 passwordless 发码（create-account/password 页面可触发该路径）。
        """
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/passwordless/send-otp",
            headers=headers,
            timeout=30,
        )
        self._trace_http("send_passwordless_otp", resp)
        if resp.status_code == 200:
            logger.info("passwordless OTP 已发送")
            return True
        logger.warning(f"passwordless 发码失败: {resp.status_code} - {(resp.text or '')[:220]}")
        return False

    def resend_otp(self, referer: str = "https://auth.openai.com/email-verification") -> bool:
        """
        重发 OTP（适用于已有账号 passwordless/login_challenge）。
        返回 True 代表请求成功。
        """
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/resend",
            headers=headers,
            timeout=30,
        )
        self._trace_http("resend_email_otp", resp)
        if resp.status_code == 200:
            logger.info("OTP 已重发")
            return True
        logger.warning(f"重发 OTP 失败: {resp.status_code} - {(resp.text or '')[:200]}")
        return False

    def kickoff_otp_delivery(self, mode: str = "") -> bool:
        """
        统一发码策略（优先 passwordless，兼容 resend/send）：
        1) passwordless/send-otp
        2) email-otp/resend
        3) email-otp/send
        """
        mode_lc = (mode or "").strip().lower()
        if self.send_passwordless_otp("https://auth.openai.com/create-account/password"):
            return True
        if self.resend_otp("https://auth.openai.com/email-verification"):
            return True
        try:
            self.send_otp()
            return True
        except Exception as e:
            logger.warning(f"send_otp 兜底失败(mode={mode_lc or 'unknown'}): {e}")
            return False

    @staticmethod
    def _default_password_from_email(email: str) -> str:
        pwd = (email or "").replace("@", "")
        if len(pwd) < 8:
            pwd = f"{pwd}2026OpenAI"
        return pwd

    def login_password_verify(self, password: str) -> dict:
        """已有账号密码登录一步（/password/verify）。"""
        headers = self._common_headers("https://auth.openai.com/log-in/password")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/password/verify",
            headers=headers,
            json={"password": password},
            timeout=30,
        )
        self._trace_http("login_password_verify", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:260]
            raise RuntimeError(f"密码登录失败: {resp.status_code} - {body}")
        try:
            return resp.json()
        except Exception:
            return {}

    # ── Step 8: 验证 OTP ──
    def verify_otp(self, otp_code: str) -> dict:
        logger.info("[7/10] 验证 OTP...")
        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            headers=headers,
            json={"code": otp_code},
            timeout=30,
        )
        self._trace_http("validate_email_otp", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:260]
            raise RuntimeError(f"OTP 验证失败: {resp.status_code} - {body}")
        logger.info("OTP 验证成功")
        try:
            return resp.json()
        except Exception:
            return {}

    # ── Step 9: 创建账户 ──
    def create_account(self) -> str:
        logger.info("[8/10] 创建账户...")
        # 创建账户前刷新 sentinel token，flow 为 create_account
        if self.result.device_id:
            try:
                from sentinel import get_sentinel_token as _get_st
                token = _get_st(self.session, device_id=self.result.device_id,
                                flow="create_account")
                self._last_sentinel_token = token or ""
                logger.info("Sentinel Token 获取成功")
            except Exception as e:
                logger.warning(f"创建账户前刷新 sentinel 失败: {e}")
        headers = self._common_headers("https://auth.openai.com/about-you")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        _FIRST = ["James", "John", "Robert", "Michael", "William", "David", "Richard",
                  "Joseph", "Thomas", "Charles", "Mary", "Patricia", "Jennifer", "Linda",
                  "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen"]
        _LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
                 "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Thomas"]
        name = f"{random.choice(_FIRST)} {random.choice(_LAST)}"
        birthdate = f"{random.randint(1985, 2000)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/create_account",
            headers=headers,
            json={"name": name, "birthdate": birthdate},
            timeout=30,
        )
        self._trace_http("create_account", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:500]
            logger.error("创建账户失败: http=%s body=%s", resp.status_code, body)
            raise RuntimeError(f"创建账户失败: {resp.status_code} - {body[:260]}")
        data = resp.json()
        continue_url = data.get("continue_url", "")
        self._sniff_login_verifier(continue_url, "create_account_continue_url")

        # 尝试 workspace select
        if not continue_url:
            workspace_id = self._extract_workspace_id()
            if workspace_id:
                continue_url = self._workspace_select(workspace_id)

        if not continue_url:
            raise RuntimeError("创建账户后未获取到 continue_url")

        logger.info("账户创建成功")
        return continue_url

    def _extract_workspace_id(self) -> str:
        """从 cookie 中提取 workspace_id"""
        try:
            auth_session = self.session.cookies.get("oai-client-auth-session", "")
            if auth_session:
                parts = auth_session.split(".")
                # 兼容不同 cookie 形态：workspace_id 可能在第 1 段/第 2 段，也可能在 workspaces[0].id
                for idx in range(min(2, len(parts))):
                    segment = (parts[idx] or "").strip()
                    if not segment:
                        continue
                    payload_b64 = segment + "=" * (-len(segment) % 4)
                    decoded = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
                    if not isinstance(decoded, dict):
                        continue
                    wid = (decoded.get("workspace_id", "") or "").strip()
                    if wid:
                        return wid
                    workspaces = decoded.get("workspaces", [])
                    if isinstance(workspaces, list):
                        for it in workspaces:
                            if isinstance(it, dict):
                                wid = (it.get("id", "") or "").strip()
                                if wid:
                                    return wid
        except Exception:
            pass
        return ""

    def _workspace_select(self, workspace_id: str) -> str:
        logger.info("执行 workspace 选择...")
        headers = self._common_headers("https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers=headers,
            json={"workspace_id": workspace_id},
            timeout=30,
        )
        self._trace_http("workspace_select", resp)
        return resp.json().get("continue_url", "") if resp.status_code == 200 else ""

    def _normalize_continue_url(self, continue_url: str) -> str:
        """
        标准化 continue_url：
        1) 相对路径 -> 绝对路径
        2) workspace 页面 -> 调用 workspace/select 取下一跳
        """
        if not continue_url:
            return ""
        out = continue_url.strip()
        if out.startswith("/"):
            out = urljoin("https://auth.openai.com", out)
        if "/workspace" in out:
            workspace_id = self._extract_workspace_id() or self._extract_query_first(out, ["workspace_id", "id"])
            if workspace_id:
                logger.info("检测到 workspace 页面，尝试 workspace/select: workspace_id=%s", workspace_id)
                next_url = self._workspace_select(workspace_id)
                if next_url:
                    out = next_url
        return out

    @staticmethod
    def _extract_workspace_id_from_html(html_text: str) -> str:
        """从 workspace 页面 HTML 文本中提取 workspace_id（兜底）。"""
        if not html_text:
            return ""
        try:
            # 先把转义引号还原，便于正则匹配
            text = html_text.replace('\\"', '"')
            patterns = [
                r'workspaces".{0,1600}?"id","([0-9a-fA-F-]{36})"',
                r'"workspace_id"\s*:\s*"([0-9a-fA-F-]{36})"',
                r'"workspaceId"\s*:\s*"([0-9a-fA-F-]{36})"',
            ]
            for p in patterns:
                m = re.search(p, text, flags=re.DOTALL | re.IGNORECASE)
                if m:
                    return (m.group(1) or "").strip()
        except Exception:
            return ""
        return ""

    # ── Step 10: 跟踪重定向链 ──
    def follow_redirect_chain(self, start_url: str) -> tuple[str, str]:
        """手动跟踪重定向，返回 (callback_url, final_url)"""
        logger.info("[9/10] 跟踪重定向链...")
        current_url = start_url
        callback_url = ""
        max_hops = 12

        for i in range(max_hops):
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://chatgpt.com/",
                "User-Agent": self._common_headers()["User-Agent"],
            }
            resp = self.session.get(
                current_url, headers=headers, timeout=30, allow_redirects=False
            )
            self._trace_http(f"redirect_hop_{i+1}", resp)

            if "/api/auth/callback/openai" in current_url:
                callback_url = current_url
                self._sniff_login_verifier(current_url, f"redirect_hop_{i+1}_callback_url")

            # workspace 页面常见为 200，需要主动调 workspace/select 获取下一跳
            if "/workspace" in current_url and resp.status_code == 200:
                workspace_id = self._extract_workspace_id() or self._extract_workspace_id_from_html(resp.text or "")
                if workspace_id:
                    logger.info("workspace 页面提取到 workspace_id=%s，尝试继续授权", workspace_id)
                    next_url = self._workspace_select(workspace_id)
                    if next_url:
                        if next_url.startswith("/"):
                            next_url = urljoin("https://auth.openai.com", next_url)
                        current_url = next_url
                        continue

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if not location:
                    break
                if location.startswith("/"):
                    parsed = urlparse(current_url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                # 关键：不要主动 GET callback，避免 code 被服务端回调消费
                if "/api/auth/callback/openai" in location and "code=" in location:
                    callback_url = location
                    current_url = location
                    self._sniff_login_verifier(location, f"redirect_hop_{i+1}_location_callback")
                    logger.info("捕获 callback URL（未消费）")
                    break
                current_url = location
                logger.debug(f"  重定向 {i + 1}: {current_url[:80]}...")
            else:
                break

        # 补一跳首页
        if (not callback_url) and (not current_url.rstrip("/").endswith("chatgpt.com")):
            self.session.get(
                "https://chatgpt.com/",
                headers={"Referer": current_url},
                timeout=30,
            )

        logger.info(f"重定向链完成, callback: {'有' if callback_url else '无'}")
        return callback_url, current_url

    def _reauthorize_for_session(self, original_auth_url: str) -> str | None:
        """已有账号 OTP 验证后，重新发起 authorize 获取 callback URL"""
        logger.info("[9.5/10] 重新 authorize 获取 session ...")
        try:
            # 去掉 prompt=login 参数，利用已有的 auth session cookie
            from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
            parsed = urlparse(original_auth_url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            params.pop("prompt", None)
            # 重新构建 URL
            new_query = urlencode({k: v[0] for k, v in params.items()})
            authorize_url = urlunparse(parsed._replace(query=new_query))

            resp = self.session.get(
                authorize_url,
                allow_redirects=False,
                timeout=15,
            )
            self._trace_http("reauthorize_start", resp)
            logger.info(f"reauthorize status={resp.status_code}")

            # 跟随 redirect chain 找到 callback URL
            current_url = resp.headers.get("Location", "")
            logger.info(f"reauthorize Location: {current_url[:150]}")
            if resp.status_code in (301, 302, 303, 307, 308) and current_url:
                for hop in range(10):
                    logger.debug(f"reauthorize redirect hop {hop+1}: {current_url[:100]}")
                    if "code=" in current_url and "state=" in current_url:
                        logger.info("reauthorize: 找到 callback URL")
                        return current_url
                    try:
                        hop_resp = self.session.get(
                            current_url,
                            allow_redirects=False,
                            timeout=15,
                        )
                        self._trace_http(f"reauthorize_hop_{hop+1}", hop_resp)
                        next_loc = hop_resp.headers.get("Location", "")
                        if hop_resp.status_code not in (301, 302, 303, 307, 308) or not next_loc:
                            # 检查最终 URL
                            final_url = str(getattr(hop_resp, 'url', current_url))
                            if "code=" in final_url:
                                return final_url
                            break
                        current_url = next_loc
                        if not current_url.startswith("http"):
                            from urllib.parse import urljoin
                            current_url = urljoin(authorize_url, current_url)
                    except Exception:
                        break
            logger.warning("reauthorize: 未能获取 callback URL")
            return None
        except Exception as e:
            logger.warning(f"reauthorize 失败: {e}")
            return None

    # ── Step 11: 获取 session ──
    def get_auth_session(self) -> tuple[str, str]:
        """获取 session_token 和 access_token"""
        logger.info("[10/10] 获取认证 Session...")
        headers = self._common_headers("https://chatgpt.com/")
        resp = self.session.get(
            "https://chatgpt.com/api/auth/session",
            headers=headers,
            timeout=30,
        )
        self._trace_http("chatgpt_auth_session", resp)
        resp.raise_for_status()

        session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
        access_token = resp.json().get("accessToken", "")

        if session_token:
            self.result.session_token = session_token
        if access_token:
            self.result.access_token = access_token
        self.result.cookie_header = self._build_chatgpt_cookie_header()

        logger.info(f"session_token: {'有' if session_token else '无'}, "
                     f"access_token: {'有' if access_token else '无'}")
        return session_token, access_token

    # ── 可选: OAuth Token 交换 ──
    def oauth_token_exchange(self, callback_url: str, continue_url: str) -> bool:
        """
        交换 OAuth token（尽力模式）：
        1) 尝试多来源 code_verifier（query/cookie/dump/hydra）
        2) 回退无 verifier
        """
        auth_code = self._extract_query_first(callback_url, ["code"]) or self._extract_query_first(continue_url, ["code"])

        if not auth_code:
            logger.info("缺少 auth_code，跳过 token 交换")
            return False

        verifier_candidates = self._collect_code_verifier_candidates(callback_url, continue_url)
        if not verifier_candidates:
            logger.info("当前未获取到可用 code_verifier，将先尝试无 verifier 交换")
        else:
            show = ", ".join([f"{src}:{len(v)}" for src, v in verifier_candidates[:8]])
            logger.info("code_verifier 候选数=%s 示例=%s", len(verifier_candidates), show)

        logger.info("执行 OAuth Token 交换...")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://auth.openai.com",
            "Referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        }
        base_form = {
            "grant_type": "authorization_code",
            "client_id": self._oauth_client_id or "YOUR_OPENAI_WEB_CLIENT_ID",
            "code": auth_code,
            "redirect_uri": self._oauth_redirect_uri or "https://chatgpt.com/api/auth/callback/openai",
        }
        logger.info(
            "Token 交换参数: client_id=%s redirect_uri=%s",
            base_form["client_id"],
            base_form["redirect_uri"],
        )

        candidates: list[tuple[str, dict]] = []
        if self._oauth_client_secret:
            d = dict(base_form)
            d["client_secret"] = self._oauth_client_secret
            candidates.append(("with_client_secret", d))

        try:
            max_verifier_try = max(1, int(os.getenv("OAUTH_MAX_VERIFIER_TRY", "18")))
        except Exception:
            max_verifier_try = 18

        for src, verifier in verifier_candidates[:max_verifier_try]:
            d = dict(base_form)
            d["code_verifier"] = verifier
            candidates.append((f"with_verifier_{src}", d))
            if self._oauth_client_secret:
                d2 = dict(d)
                d2["client_secret"] = self._oauth_client_secret
                candidates.append((f"with_verifier_{src}_and_client_secret", d2))

        # 一些服务端可能要求额外参数（实验候选）
        audience = self._extract_query_first(self._oauth_auth_url, ["audience"])
        if audience:
            d = dict(base_form)
            d["audience"] = audience
            candidates.append(("without_verifier_with_audience", d))
        if self._oauth_scope:
            d = dict(base_form)
            d["scope"] = self._oauth_scope
            candidates.append(("without_verifier_with_scope", d))

        candidates.append(("without_verifier", dict(base_form)))

        seen_fingerprints: set[str] = set()
        for mode, form in candidates:
            fp = json.dumps(form, sort_keys=True, ensure_ascii=False)
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)
            try:
                self._sniff_login_verifier(urlencode(form), f"oauth_token_exchange_{mode}:form")
            except Exception:
                pass
            encoded_form = urlencode(form)
            extra_request = {
                "method": "POST",
                "url": "https://auth.openai.com/oauth/token",
                "body": encoded_form,
                "headers": headers,
            }

            resp = self.session.post(
                "https://auth.openai.com/oauth/token",
                headers=headers,
                data=encoded_form,
                timeout=30,
            )
            self._trace_http(f"oauth_token_exchange_{mode}", resp, extra_request=extra_request)
            if resp.status_code == 200:
                data = resp.json()
                self.result.id_token = data.get("id_token", "")
                self.result.access_token = data.get("access_token", self.result.access_token)
                self.result.refresh_token = data.get("refresh_token", "")
                logger.info(
                    "Token 交换成功(mode=%s): refresh_token=%s",
                    mode,
                    "有" if self.result.refresh_token else "无",
                )
                return True

            body = (resp.text or "")[:240]
            logger.warning("Token 交换失败(mode=%s): status=%s body=%s", mode, resp.status_code, body)

        return False

    def oauth_secondary_authorize_exchange(self) -> bool:
        """
        二次授权实验：
        - 在当前已登录会话上，重新发起一条带 PKCE 的 authorize
        - 仅提取 callback code，不消费 callback
        - 再走 oauth/token 交换
        """
        logger.info("尝试二次 authorize + PKCE 换 refresh_token ...")
        try:
            csrf = self.get_csrf_token()
            auth_url = self.get_auth_url(csrf)
        except Exception as e:
            logger.warning(f"二次 authorize 初始化失败: {e}")
            return False

        try:
            verifier, challenge = self._build_pkce_pair()
            parsed = urlparse(auth_url)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
            if not params.get("state"):
                params["state"] = self._b64url_no_pad(os.urandom(16))
            sec_url = urlunparse(parsed._replace(query=urlencode(params)))

            self._manual_login_verifier = verifier
            self._captured_login_verifier = verifier
            self._remember_oauth_params(sec_url)

            current = sec_url
            callback_url = ""
            max_hops = 10
            for i in range(max_hops):
                resp = self.session.get(
                    current,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://chatgpt.com/",
                        "User-Agent": self._common_headers()["User-Agent"],
                    },
                    timeout=30,
                    allow_redirects=False,
                )
                self._trace_http(f"secondary_authorize_hop_{i+1}", resp)

                loc = (resp.headers.get("Location", "") or "").strip()
                if loc and loc.startswith("/"):
                    loc = urljoin(current, loc)

                if loc and "/api/auth/callback/openai" in loc and "code=" in loc:
                    callback_url = loc
                    break
                if resp.status_code not in (301, 302, 303, 307, 308) or not loc:
                    break
                current = loc

            if not callback_url:
                logger.warning("二次 authorize 未捕获 callback code")
                return False

            ok = self.oauth_token_exchange(callback_url, callback_url)
            logger.info("二次 authorize 交换结果: %s", "成功" if ok else "失败")
            return ok
        except Exception as e:
            logger.warning(f"二次 authorize 交换异常: {e}")
            return False

    # ── 完整注册流程 ──
    def run_register(self, mail_provider: MailProvider) -> AuthResult:
        """执行完整注册流程"""
        # 检查网络
        if not self.check_proxy():
            logger.warning("网络预检查未通过，继续尝试注册链路以获取精确错误...")

        # 创建邮箱
        email = mail_provider.create_mailbox()
        self.result.email = email

        # 登录/注册链路
        csrf_token = self.get_csrf_token()
        auth_url = self.get_auth_url(csrf_token)
        device_id = self.auth_oauth_init(auth_url)
        sentinel = self.get_sentinel_token(device_id)
        is_new = self.signup(email, sentinel)

        if is_new:
            # 新账号：注册密码 → 发 OTP → 验证 → 创建账户
            password_registered = self.register_password(email)
            otp_sent_at = time.time()
            if password_registered:
                try:
                    self.send_otp()
                except RuntimeError as e:
                    # 部分账号会在 register 后直接转入 email-verification，send 接口会报 invalid_auth_step
                    if "invalid_auth_step" in str(e).lower():
                        logger.warning("send_otp 返回 invalid_auth_step，回退到统一发码策略")
                        if not self.kickoff_otp_delivery("register_password_invalid_auth_step"):
                            raise
                    else:
                        raise
            else:
                # 注册密码失败时优先按“已有账号 OTP”回退，避免卡死在 invalid_auth_step
                logger.warning("注册密码失败，回退到已有账号 OTP 路径")
                self.fetch_client_auth_session_dump("post_register_password_failed_new")
                if not self.kickoff_otp_delivery("register_password_failed_fallback"):
                    self.send_otp()

            try:
                otp_timeout = max(30, int(os.getenv("OTP_TIMEOUT", "180")))
            except Exception:
                otp_timeout = 180
            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            try:
                self.verify_otp(otp_code)
                self.fetch_client_auth_session_dump("post_verify_otp_new")
            except RuntimeError as e:
                # 偶发 401 错码，补发一次 OTP 并重试
                if "401" in str(e):
                    logger.warning(f"OTP 首次验证失败，补发重试: {e}")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("verify_otp_retry_new"):
                        self.send_otp()
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                    self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_retry_new")
                else:
                    raise

            try:
                continue_url = self.create_account()
            except Exception as e:
                # registration_disallowed 时尝试 reauthorize 兜底，若仍不可用再抛出
                if self._is_registration_disallowed_error(e):
                    logger.warning("create_account 被拒绝，尝试 reauthorize 兜底获取 session ...")
                    continue_url = self._reauthorize_for_session(auth_url) or ""
                    if not continue_url:
                        raise
                else:
                    raise
        else:
            # 已有账号：直接发 OTP → 验证 → 获取 session
            mode = (self._existing_email_verification_mode or "").lower()
            page_type = (self._existing_page_type or "").lower()
            continue_url = ""

            try:
                otp_timeout = max(30, int(os.getenv("OTP_TIMEOUT", "180")))
            except Exception:
                otp_timeout = 180

            if page_type == "login_password":
                logger.info("已有账号进入 login_password 分支，先走密码校验再 OTP")
                login_password = (os.getenv("LOGIN_PASSWORD", "") or "").strip()
                if not login_password:
                    login_password = self._default_password_from_email(email)
                self.result.password = login_password
                login_resp = self.login_password_verify(login_password)
                continue_url = self._normalize_continue_url(
                    (login_resp or {}).get("continue_url", "") if isinstance(login_resp, dict) else ""
                )

                # 部分账号密码校验后仍需 email otp（二次校验）
                if not continue_url or "/email-verification" in continue_url:
                    # password/verify 后推荐使用 resend，而不是 /email-otp/send
                    otp_sent_at = time.time()
                    self.kickoff_otp_delivery("existing_login_password")
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                    otp_resp = self.verify_otp(otp_code)
                    continue_url = self._normalize_continue_url(
                        (otp_resp or {}).get("continue_url", "") if isinstance(otp_resp, dict) else ""
                    )
            else:
                need_send_otp = mode not in ("passwordless_signup", "passwordless_login")
                if need_send_otp:
                    otp_sent_at = time.time()
                    self.send_otp()
                else:
                    # 某些模式在 /authorize/continue 已触发发码，不要重复 /email-otp/send 以免破坏 state
                    # 默认先尝试 /email-otp/resend 获取新码，失败再回看短窗口
                    forced_resend = self._env_flag("OTP_FORCE_RESEND", "1")
                    if forced_resend and self.kickoff_otp_delivery("existing_forced_resend"):
                        otp_sent_at = time.time()
                        logger.info(f"已有账号验证码模式={mode}，已主动 resend OTP")
                    else:
                        # 回看短窗口，避免误读上一轮旧验证码
                        otp_sent_at = time.time() - 8
                        logger.info(f"已有账号验证码模式={mode}，跳过额外 send_otp，直接等邮件")

                try:
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                except TimeoutError:
                    # 若本轮没等到，优先 resend，再兜底 send_otp
                    logger.warning("未等到已有账号 OTP，先重发后重试等待")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("existing_timeout_retry"):
                        self.send_otp()
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                try:
                    otp_resp = self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_existing")
                except RuntimeError as e:
                    if any(code in str(e) for code in ("401", "409")):
                        logger.warning(f"OTP 首次验证失败，重发重试: {e}")
                        otp_sent_at = time.time()
                        if not self.kickoff_otp_delivery("existing_verify_retry"):
                            self.send_otp()
                        otp_code = mail_provider.wait_for_otp(
                            email,
                            timeout=otp_timeout,
                            issued_after=otp_sent_at,
                        )
                        otp_resp = self.verify_otp(otp_code)
                        self.fetch_client_auth_session_dump("post_verify_otp_retry_existing")
                    else:
                        raise
                continue_url = (otp_resp or {}).get("continue_url", "") if isinstance(otp_resp, dict) else ""
                continue_url = self._normalize_continue_url(continue_url)
                if self._is_add_phone_state(page_type=self._extract_page_type(otp_resp), continue_url=continue_url):
                    continue_url = self._normalize_continue_url(
                        self._handle_add_phone_verification(continue_url=continue_url)
                    )

            # 某些已有账号在 OTP 后会进入 about-you，需要补一次 create_account
            if continue_url and "/about-you" in continue_url:
                try:
                    continue_url = self.create_account()
                except Exception as e:
                    if self._is_registration_disallowed_error(e):
                        logger.warning("about-you create_account 被拒绝，尝试 reauthorize 兜底获取 session ...")
                        continue_url = self._reauthorize_for_session(auth_url) or ""
                        if continue_url:
                            logger.info("reauthorize 兜底成功，继续后续 session 获取")
                            # 下游会走 follow_redirect_chain + get_auth_session
                            pass
                        else:
                            raise
                    else:
                        logger.warning(f"已有账号 about-you 创建信息失败，回退 reauthorize: {e}")
                        continue_url = ""

            # 若 otp 响应未给可用 continue_url，则回退到 reauthorize
            if not continue_url:
                # auth.openai.com 的 session cookie 已设置，直接拿 code
                continue_url = self._reauthorize_for_session(auth_url)

        if continue_url:
            continue_url = self._normalize_continue_url(continue_url)
            # 关键尝试：在 chatgpt callback 被消费前，先走一次 Codex OAuth（有助于保留 auth.openai 登录态）
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_BEFORE_CALLBACK", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            # 可选：在 callback 被消费前尝试 token 交换（可能影响后续 callback，默认关闭）
            refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")
            pre_exchange_default = "1" if refresh_only_mode else "0"
            pre_exchange = self._env_flag("OAUTH_EXCHANGE_BEFORE_CALLBACK", pre_exchange_default)
            if pre_exchange and not self._env_flag("SKIP_OAUTH_TOKEN_EXCHANGE", "0"):
                self.oauth_token_exchange(continue_url, continue_url)
            callback_url, final_url = self.follow_redirect_chain(continue_url)
            if (not callback_url) and final_url and ("/workspace" in final_url):
                normalized = self._normalize_continue_url(final_url)
                if normalized and normalized != final_url:
                    callback_url, final_url = self.follow_redirect_chain(normalized)
        else:
            callback_url, final_url = None, None

        refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")
        if not refresh_only_mode:
            # 获取 session
            self.get_auth_session()

        # 可选 token 交换
        if callback_url or continue_url:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_register")
            if not self._env_flag("SKIP_OAUTH_TOKEN_EXCHANGE", "0"):
                self.oauth_token_exchange(callback_url or "", continue_url or "")
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            if (not self.result.refresh_token) and self._env_flag("OAUTH_SECONDARY_AUTHORIZE_EXCHANGE", "0"):
                self.oauth_secondary_authorize_exchange()
            # 按需求：最终 access_token 以 chatgpt.com/api/auth/session 为准
            if not refresh_only_mode:
                self.get_auth_session()

        if refresh_only_mode:
            if not (self.result.refresh_token or self.result.access_token):
                raise RuntimeError("流程完成但未获取 refresh_token/access_token")
        elif not self.result.is_valid():
            raise RuntimeError("注册完成但未获取有效凭证")

        logger.info("注册流程完成!")
        return self.result

    # ── 纯协议已有账号登录流程（目标：拿 callback/session/refresh） ──
    def run_protocol_login(self, mail_provider: MailProvider, email: str, password: str = "") -> AuthResult:
        """
        纯协议登录（不创建随机邮箱）：
        - 适配 passwordless / login_password 两类已有账号入口
        - 可配合 OAUTH_EXCHANGE_BEFORE_CALLBACK / OAUTH_REFRESH_ONLY 尝试优先拿 refresh_token
        """
        if not (email or "").strip():
            raise RuntimeError("run_protocol_login 缺少邮箱")

        if not self.check_proxy():
            logger.warning("网络预检查未通过，继续尝试登录链路以获取精确错误...")

        email = email.strip()
        self.result.email = email
        login_password = (password or "").strip() or self._default_password_from_email(email)
        self.result.password = login_password

        csrf_token = self.get_csrf_token()
        auth_url = self.get_auth_url(csrf_token)
        device_id = self.auth_oauth_init(auth_url)
        sentinel = self.get_sentinel_token(device_id)

        continue_url = ""
        try:
            otp_timeout = max(30, int(os.getenv("OTP_TIMEOUT", "180")))
        except Exception:
            otp_timeout = 180

        page_type = ""
        mode = ""
        prefer_login_screen_first = str(
            os.getenv("LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT", "1")
        ).lower() in ("1", "true", "yes", "on")

        if prefer_login_screen_first:
            try:
                logger.info("已有账号协议登录：优先走 login screen_hint 探测 password/otp 分支")
                login_step = self.authorize_continue(
                    email=email,
                    sentinel_token=sentinel,
                    screen_hint="login",
                    referer="https://auth.openai.com/log-in",
                    trace_step="authorize_continue_login_protocol",
                )
                page_type = (self._extract_page_type(login_step) or "").lower()
                continue_url = self._normalize_continue_url(
                    self._extract_continue_url_from_step(login_step)
                )
                page = (login_step.get("page") or {}) if isinstance(login_step, dict) else {}
                payload = (page.get("payload") or {}) if isinstance(page, dict) else {}
                mode = (payload.get("email_verification_mode", "") or "").lower()
                self._existing_page_type = page_type
                self._existing_email_verification_mode = mode

                if page_type == "login_password" or "/log-in/password" in (continue_url or ""):
                    logger.info("登录分支: login_password -> password/verify")
                    login_resp = self.login_password_verify(login_password)
                    page_type = (self._extract_page_type(login_resp) or "").lower()
                    continue_url = self._normalize_continue_url(
                        self._extract_continue_url_from_step(login_resp)
                    )
                elif page_type == "email_otp_verification" or "/email-verification" in (continue_url or ""):
                    logger.info("登录分支: email_otp_verification")
                else:
                    logger.info(
                        "login screen_hint 未直接命中已有账号完成态: page_type=%s continue_url=%s",
                        page_type or "(empty)",
                        (continue_url or "")[:180] or "(empty)",
                    )
            except Exception as e:
                logger.warning(f"login screen_hint 探测失败，回退 signup 探测: {e}")
                continue_url = ""
                page_type = ""
                mode = ""

        if not continue_url and page_type not in ("login_password", "email_otp_verification"):
            is_new = self.signup(email, sentinel)
            if is_new:
                logger.warning("目标邮箱未命中已有账号分支，回退到注册链路")
                self.register_password(email)
                otp_sent_at = time.time()
                self.send_otp()
                otp_code = mail_provider.wait_for_otp(
                    email,
                    timeout=otp_timeout,
                    issued_after=otp_sent_at,
                )
                self.verify_otp(otp_code)
                continue_url = self.create_account()
            else:
                page_type = (self._existing_page_type or "").lower()
                mode = (self._existing_email_verification_mode or "").lower()
        else:
            page_type = (page_type or self._existing_page_type or "").lower()
            mode = (mode or self._existing_email_verification_mode or "").lower()

        if not continue_url or "/email-verification" in continue_url:
            # 仍需 OTP：优先 resend 获取新码
            otp_sent_at = time.time()
            resend_ok = self.kickoff_otp_delivery("protocol_need_otp")
            if not resend_ok and mode not in ("passwordless_signup", "passwordless_login"):
                self.send_otp()
                otp_sent_at = time.time()

            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            try:
                otp_resp = self.verify_otp(otp_code)
                self.fetch_client_auth_session_dump("post_verify_otp_protocol")
            except RuntimeError as e:
                if any(code in str(e) for code in ("401", "409")):
                    logger.warning(f"OTP 首次验证失败，重发重试: {e}")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("protocol_verify_retry"):
                        self.send_otp()
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                    otp_resp = self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_retry_protocol")
                else:
                    raise
            continue_url = self._extract_continue_url_from_step(otp_resp)
            continue_url = self._normalize_continue_url(continue_url)
            if self._is_add_phone_state(page_type=self._extract_page_type(otp_resp), continue_url=continue_url):
                continue_url = self._normalize_continue_url(
                    self._handle_add_phone_verification(continue_url=continue_url)
                )

        continue_url = self._normalize_continue_url(continue_url)
        # 某些边缘态 OTP 后未返回 callback，回退 reauthorize
        if not continue_url:
            continue_url = self._reauthorize_for_session(auth_url) or ""

        refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")
        callback_url = ""
        if continue_url:
            continue_url = self._normalize_continue_url(continue_url)
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_BEFORE_CALLBACK", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            pre_exchange_default = "1" if refresh_only_mode else "0"
            pre_exchange = self._env_flag("OAUTH_EXCHANGE_BEFORE_CALLBACK", pre_exchange_default)
            if pre_exchange:
                self.oauth_token_exchange(continue_url, continue_url)
            callback_url, final_url = self.follow_redirect_chain(continue_url)
            if (not callback_url) and final_url and ("/workspace" in final_url):
                normalized = self._normalize_continue_url(final_url)
                if normalized and normalized != final_url:
                    callback_url, final_url = self.follow_redirect_chain(normalized)

        if not refresh_only_mode:
            self.get_auth_session()

        if callback_url or continue_url:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_protocol")
            self.oauth_token_exchange(callback_url or "", continue_url or "")
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            if (not self.result.refresh_token) and self._env_flag("OAUTH_SECONDARY_AUTHORIZE_EXCHANGE", "0"):
                self.oauth_secondary_authorize_exchange()
            if not refresh_only_mode:
                self.get_auth_session()

        if refresh_only_mode:
            if not (self.result.refresh_token or self.result.access_token):
                raise RuntimeError("协议登录完成，但未拿到 refresh_token/access_token")
        elif not self.result.is_valid():
            raise RuntimeError("协议登录完成，但未拿到有效 session/access token")

        logger.info("纯协议登录流程完成")
        return self.result

    # ── 从已有凭证初始化 ──
    def from_existing_credentials(
        self, session_token: str, access_token: str, device_id: str
    ) -> AuthResult:
        """使用已有凭证（跳过注册）"""
        self.result.device_id = device_id or str(uuid.uuid4())
        self.session.cookies.set("oai-did", self.result.device_id, domain=".chatgpt.com")
        detected_email = ""

        # 如果有 session_token, 用它刷新 access_token (旧 access_token 可能已过期)
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
            logger.info("使用 session_token 刷新 access_token...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                session_data = resp.json() if resp is not None else {}
                new_access_token = session_data.get("accessToken", "")
                user_obj = session_data.get("user", {}) if isinstance(session_data, dict) else {}
                if isinstance(user_obj, dict):
                    detected_email = detected_email or (user_obj.get("email", "") or "")
                new_session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if new_access_token:
                    access_token = new_access_token
                    logger.info("access_token 刷新成功")
                else:
                    logger.warning(f"access_token 刷新失败 (status={resp.status_code}), 使用原 token")
                if new_session_token:
                    session_token = new_session_token
            except Exception as e:
                logger.warning(f"刷新 access_token 失败: {e}, 使用原 token")
        elif access_token:
            # 没有 session_token, 尝试通过 access_token 获取
            logger.info("未提供 session_token, 尝试通过 access_token 获取...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                headers["Authorization"] = f"Bearer {access_token}"
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                session_data = resp.json() if resp is not None else {}
                user_obj = session_data.get("user", {}) if isinstance(session_data, dict) else {}
                if isinstance(user_obj, dict):
                    detected_email = detected_email or (user_obj.get("email", "") or "")
                session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if session_token:
                    logger.info("通过 access_token 获取 session_token 成功")
                else:
                    logger.warning("未能获取 session_token, 可能需要手动提供")
            except Exception as e:
                logger.warning(f"获取 session_token 失败: {e}")

        self.result.access_token = access_token
        self.result.session_token = session_token
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
        self.result.cookie_header = self._build_chatgpt_cookie_header()

        # 回填 email（skip-register 模式下常用于账单 email）
        if not detected_email and access_token and access_token.count(".") >= 2:
            try:
                payload_b64 = access_token.split(".")[1]
                payload_b64 += "=" * (-len(payload_b64) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
                prof = payload.get("https://api.openai.com/profile", {}) if isinstance(payload, dict) else {}
                if isinstance(prof, dict):
                    detected_email = detected_email or (prof.get("email", "") or "")
            except Exception:
                pass
        self.result.email = detected_email or ""
        logger.info("使用已有凭证初始化完成")
        return self.result
