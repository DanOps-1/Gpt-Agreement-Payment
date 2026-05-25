"""Device Code Flow to obtain outlook refresh_token (Thunderbird client_id, v2 IMAP scope).

Why Device Code instead of Auth Code Flow:
- Auth Code uses Playwright to launch firefox + webshare proxy IP, Microsoft server-side risk
  score marks IP as "UnfamiliarLocation" → forces identity/confirm + requires code from mailbox, dead end.
- Device Code: you enter user_code in your own trusted browser, Microsoft only checks your IP/cookie,
  doesn't check our webshare. Perfect workaround.

# webui calls: POST /api/outlook/device-code/start → returns user_code + URL
# User enters code at microsoft.com/link + logs into outlook + consents to Thunderbird IMAP access
# POST /api/outlook/device-code/poll {device_code, target_email} → obtains RT and writes to DB
#
# History: previously used Auth Code Flow + Playwright (suffered from ROPC not supporting consumer,
# and blocked by webshare IP UnfamiliarLocation challenge). Device Code is the only fully automated
# path that doesn't rely on IP trust, but requires user to authorize once on their own trusted device.
# Current webui doesn't expose UI (users refuse manual steps), this module serves as backend fallback;
# old Auth Code Flow functions also retained for compatibility."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# Thunderbird publicly exposed client_id, declared v2 IMAP scope during registration (consistent with supplier's 9e5f94bc batch)
THUNDERBIRD_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"

# v2 endpoint (old wl.imap v1 token outlook IMAP no longer accepts)
OAUTH_DEVICECODE = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
OAUTH_TOKEN = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OAUTH_AUTHORIZE = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"  # Old Auth Code path reserved for compatibility
OOB_REDIRECT = "https://login.live.com/oauth20_desktop.srf"
SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"


# ────────────────────────── Device Code Flow ──────────────────────────


def device_code_start(client_id: str = THUNDERBIRD_CLIENT_ID, scope: str = SCOPE) -> dict:
    """Step 1: Request device_code + user_code. Return fields:
    {user_code, device_code, verification_uri, expires_in, interval, message}.
    The frontend displays user_code and verification_uri to the user, 
    allowing the user to enter them in the browser."""
    import urllib.request, urllib.parse
    data = urllib.parse.urlencode({"client_id": client_id, "scope": scope}).encode()
    req = urllib.request.Request(OAUTH_DEVICECODE, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except Exception as e:
        raise RuntimeError(f"devicecode endpoint 请求失败: {e}")


def device_code_poll(device_code: str, target_email: str = "",
                     client_id: str = THUNDERBIRD_CLIENT_ID) -> dict:
    """Step 2: Poll the token endpoint. Returns:
      - {"status": "pending"}         User hasn't completed authorize in browser yet
      - {"status": "ok", ...}         Got token, already written to DB (e.g. target_email in pool)
      - {"status": "error", "error"}  Failed (user rejected / device_code expired / IMAP rejected)"""
    import urllib.request, urllib.parse, urllib.error
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id,
        "device_code": device_code,
    }).encode()
    req = urllib.request.Request(OAUTH_TOKEN, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        token_data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err_data = json.loads(body)
            err = err_data.get("error", "unknown")
        except Exception:
            err = body[:80]
        if err == "authorization_pending":
            return {"status": "pending"}
        if err == "authorization_declined":
            return {"status": "error", "error": "用户拒绝授权"}
        if err == "expired_token":
            return {"status": "error", "error": "device_code 过期, 重新启动 flow"}
        return {"status": "error", "error": f"token endpoint: {err}"}
    except Exception as e:
        return {"status": "error", "error": f"token endpoint exception: {e}"}

    new_rt = token_data.get("refresh_token", "")
    at = token_data.get("access_token", "")
    if not new_rt or not at:
        return {"status": "error", "error": f"token dict 缺 fields: {list(token_data.keys())}"}

    # decode access_token JWT to get email for verification (prevent users from entering a different account)
    actual_email = ""
    try:
        import base64
        parts = at.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
            actual_email = (payload.get("upn") or payload.get("preferred_username")
                            or payload.get("unique_name") or "").lower()
    except Exception:
        pass

    target_email = (target_email or "").strip().lower()
    if target_email and actual_email and target_email != actual_email:
        return {"status": "error",
                "error": f"授权邮箱不匹配: 期望 {target_email}, 实际 {actual_email}. "
                         f"请用浏览器登出后用对应邮箱重新 authorize."}

    # Select update object: target_email > actual_email > raise error
    email_to_update = target_email or actual_email
    if not email_to_update:
        return {"status": "error", "error": "无法确定邮箱 (target_email + JWT 都空)"}

    # Verify IMAP once
    imap_alive = False
    imap_err = ""
    try:
        import imaplib
        M = imaplib.IMAP4_SSL("outlook.office365.com", 993)
        auth = f"user={email_to_update}\x01auth=Bearer {at}\x01\x01"
        typ, _ = M.authenticate("XOAUTH2", lambda x: auth.encode())
        imap_alive = (typ == "OK")
        if not imap_alive:
            imap_err = f"XOAUTH2 returned {typ}"
        try:
            M.logout()
        except Exception:
            pass
    except Exception as e:
        imap_err = f"{type(e).__name__}: {e}"

    # Write to DB (if the email is already in the pool)
    from . import outlook_pool
    import time as _time
    con = outlook_pool.get_db()._conn()
    existing = con.execute(
        "SELECT email FROM outlook_accounts WHERE email=?", (email_to_update,)
    ).fetchone()
    if existing:
        new_status = "available" if imap_alive else "dead"
        new_fail = "" if imap_alive else f"Device Code 拿到新 RT 但 IMAP 仍拒: {imap_err}"
        con.execute(
            "UPDATE outlook_accounts SET refresh_token=?, client_id=?, status=?, "
            "fail_reason=?, claimed_at=0 WHERE email=?",
            (new_rt, client_id, new_status, new_fail, email_to_update),
        )
        con.commit()
        db_action = f"updated existing row → status={new_status}"
    else:
        db_action = "邮箱不在池子, RT 已生成但未写 DB (请先 import 邮箱)"

    return {
        "status": "ok",
        "email": email_to_update,
        "actual_email_from_jwt": actual_email,
        "new_rt_prefix": new_rt[:25] + "...",
        "imap_alive": imap_alive,
        "imap_err": imap_err,
        "db_action": db_action,
        "client_id": client_id,
    }


# ────────────────────────── Legacy Auth Code Flow (Playwright) ──────────────────────────


def _parse_proxy(proxy_url: str) -> Optional[dict]:
    if not proxy_url:
        return None
    p = urllib.parse.urlparse(proxy_url)
    out = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        out["username"] = p.username
        out["password"] = p.password or ""
    return out


def refresh_token_via_oauth(
    email: str,
    password: str,
    client_id: str,
    proxy_url: str = "socks5://127.0.0.1:18898",
    timeout_s: int = 90,
) -> Optional[str]:
    """Run Auth Code Flow to get new refresh_token. Return None on failure.

    Steps: Firefox login → skip proofs/Add → accept Consent → capture redirect code
           → POST token endpoint to exchange for RT."""
    from playwright.sync_api import sync_playwright

    authorize_url = OAUTH_AUTHORIZE + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": OOB_REDIRECT,
        "scope": SCOPE,
        "response_mode": "query",
    })
    logger.info(f"[{email}] OAuth refresh: 打开 firefox 走 authorize URL")

    code: Optional[str] = None
    with sync_playwright() as p:
        try:
            browser = p.firefox.launch(headless=True, proxy=_parse_proxy(proxy_url))
        except Exception as e:
            logger.error(f"[{email}] firefox launch fail: {e}")
            return None
        try:
            ctx = browser.new_context(
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0",
            )
            page = ctx.new_page()
            try:
                page.goto(authorize_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.error(f"[{email}] navigate authorize fail (代理是否 alive?): {e}")
                return None

            # Auto login
            try:
                page.wait_for_selector('input[type="email"], input[name="loginfmt"]', timeout=30000)
                page.fill('input[type="email"], input[name="loginfmt"]', email)
                page.click('#idSIButton9, button[type="submit"], input[type="submit"]')

                # Wait for the password field, but Microsoft's new flow will first show "メールをご確認ください" (passwordless priority, send code to recovery email)
                # Need to click "パスワードを使用する" / "Use password" toggle. Loop for 30s to see which one appears first.
                use_pwd_selectors = [
                    'span[role="button"]:has-text("パスワードを使用する")',
                    'span[role="button"]:has-text("Use your password")',
                    'span[role="button"]:has-text("Use password")',
                    'span[role="button"]:has-text("使用密码")',
                    'a:has-text("パスワードを使用する")',
                    'a:has-text("Use your password")',
                    'a:has-text("Use password")',
                    'button:has-text("パスワードを使用する")',
                ]
                import time as _t
                deadline_pwd = _t.time() + 30
                pwd_input_found = False
                while _t.time() < deadline_pwd:
                    if page.query_selector('input[type="password"], input[name="passwd"]'):
                        pwd_input_found = True
                        break
                    clicked = False
                    for sel in use_pwd_selectors:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            logger.info(f"[{email}] 切回密码登录 via {sel}")
                            el.click(timeout=2000)
                            page.wait_for_timeout(1500)
                            clicked = True
                            break
                    if clicked:
                        continue
                    page.wait_for_timeout(700)
                if not pwd_input_found:
                    raise RuntimeError("等不到密码输入框 (passwordless-only? 截图见 /tmp)")

                page.wait_for_timeout(800)
                page.fill('input[type="password"], input[name="passwd"]', password)
                page.click('#idSIButton9, button[type="submit"], input[type="submit"]')
            except Exception as e:
                # dump current URL + screenshot + HTML snippet for user judgment
                try:
                    cur_url = page.url
                    safe_name = email.replace("@", "_at_").replace("/", "_")
                    shot_path = f"/tmp/oauth_fail_{safe_name}.png"
                    html_path = f"/tmp/oauth_fail_{safe_name}.html"
                    page.screenshot(path=shot_path, full_page=True)
                    Path = __import__("pathlib").Path
                    Path(html_path).write_text(page.content()[:50000], encoding="utf-8", errors="replace")
                    logger.error(
                        f"[{email}] 登入填表失败: {e}; URL={cur_url[:200]}; "
                        f"截图={shot_path}; html={html_path}"
                    )
                except Exception as _dbg:
                    logger.error(f"[{email}] 登入填表失败 + dump 也炸: {e} / {_dbg}")
                return None

            deadline = time.time() + timeout_s
            last = ""
            block_reason = ""
            while time.time() < deadline:
                url = page.url
                if url != last:
                    logger.info(f"[{email}] {url[:120]}")
                    last = url
                if url.startswith(OOB_REDIRECT) and "code=" in url:
                    qs = urllib.parse.urlparse(url).query
                    code = urllib.parse.parse_qs(qs).get("code", [None])[0]
                    logger.info(f"[{email}] 拿到 code")
                    break
                if "identity/confirm" in url:
                    block_reason = ("Microsoft 异常登录二次验证: 要求填 recovery 邮箱 + 收 OTP. "
                                    "supplier 卖号常配 mailtb.com / xxx-mailbox 作 recovery, "
                                    "你没有 recovery 邮箱收件权时绕不开. 临时解: 用固定 IP 手工登几次让 MS 信任此号.")
                    logger.error(f"[{email}] {block_reason}")
                    break
                if url.startswith(OOB_REDIRECT) and "error=" in url:
                    qs = urllib.parse.urlparse(url).query
                    err = urllib.parse.parse_qs(qs).get("error", ["unknown"])[0]
                    err_desc = urllib.parse.parse_qs(qs).get("error_description", [""])[0]
                    block_reason = f"OAuth 拒绝授权 ({err}): {err_desc[:120]}"
                    logger.error(f"[{email}] {block_reason}")
                    break
                try:
                    if "Consent/Update" in url or "consent.live.com" in url:
                        for sel in ['#idBtn_Accept',
                                    'input[type="submit"][value="Yes"]', 'input[type="submit"][value="はい"]',
                                    'button:has-text("Yes")', 'button:has-text("はい")',
                                    'button:has-text("Accept")', 'button:has-text("同意")']:
                            btn = page.query_selector(sel)
                            if btn and btn.is_visible():
                                logger.info(f"[{email}] consent accept via {sel}")
                                btn.click(timeout=2000)
                                page.wait_for_timeout(1500)
                                break
                    elif "proofs/Add" in url or "account.live.com" in url:
                        # proofs/Add: Points "Later"/"Skip" will be interpreted by Microsoft as "deny authorization" returning access_denied,
                        # Must navigate directly to the post= URL in the query to continue the OAuth flow.
                        qs = urllib.parse.urlparse(url).query
                        post = urllib.parse.parse_qs(qs).get("post", [None])[0]
                        if post:
                            target = urllib.parse.unquote(post)
                            logger.info(f"[{email}] bypass proofs → post={target[:80]}")
                            page.goto(target, wait_until="domcontentloaded", timeout=30000)
                        else:
                            # Without post=, fall back to clicking iCancel (only available in English UI)
                            for sel in ['#iCancel', 'a[id="iCancel"]']:
                                btn = page.query_selector(sel)
                                if btn and btn.is_visible():
                                    logger.info(f"[{email}] proofs no post, click {sel}")
                                    btn.click(timeout=2000)
                                    page.wait_for_timeout(1200)
                                    break
                    else:
                        # stay-signed-in / Other primary button fallback
                        for sel in ['#idSIButton9', 'button[data-testid="primaryButton"]']:
                            btn = page.query_selector(sel)
                            if btn and btn.is_visible():
                                btn.click(timeout=2000)
                                page.wait_for_timeout(800)
                                break
                except Exception:
                    pass
                page.wait_for_timeout(1500)

            # Main loop ends (got code or timeout). Dump the last page when timeout so user can see it
            if not code:
                try:
                    safe_name = email.replace("@", "_at_").replace("/", "_")
                    page.screenshot(path=f"/tmp/oauth_timeout_{safe_name}.png", full_page=True)
                    from pathlib import Path as _P
                    _P(f"/tmp/oauth_timeout_{safe_name}.html").write_text(
                        page.content()[:80000], encoding="utf-8", errors="replace"
                    )
                    logger.error(
                        f"[{email}] timeout dump → /tmp/oauth_timeout_{safe_name}.png "
                        f"+ html, last URL={page.url[:200]}"
                    )
                except Exception as _dbg:
                    logger.warning(f"[{email}] timeout dump 失败: {_dbg}")
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if not code:
        # Assemble a precise error message to return to the caller
        reason = locals().get("block_reason") or "登入超时 / 密码错 / 异常挑战"
        # Stuff reason into logger and bring it back through the new return value
        logger.error(f"[{email}] 未拿到 OAuth code: {reason}")
        # Using global variables this way, refresh_and_update_db can access it; the simple approach is to attach it as a property to the function
        refresh_token_via_oauth.last_block_reason = reason  # type: ignore[attr-defined]
        return None
    # Clear block_reason on success
    refresh_token_via_oauth.last_block_reason = ""  # type: ignore[attr-defined]

    # code → refresh_token
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": OOB_REDIRECT,
        "scope": SCOPE,
    }).encode()
    req = urllib.request.Request(OAUTH_TOKEN, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        new_rt = data.get("refresh_token")
        if not new_rt:
            logger.error(f"[{email}] token endpoint 返 dict 无 refresh_token: {list(data.keys())}")
            return None
        logger.info(f"[{email}] ✓ 新 refresh_token 已拿到")
        return new_rt
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()[:300]
        logger.error(f"[{email}] token endpoint {e.code}: {body_err}")
        return None
    except Exception as e:
        logger.error(f"[{email}] token endpoint exception: {e}")
        return None


def _ensure_proxy_alive(listen_port: int = 18898) -> tuple[bool, str]:
    """Ensure socks5://127.0.0.1:<port> has gost listening; if not, reuse the pipeline main logic to start it."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", listen_port))
            return True, "already alive"
        except Exception:
            pass
    # Not heard → Go pipeline._ensure_gost_alive to start up (it will read pay config to get webshare upstream)
    import json
    from pathlib import Path
    from . import settings as s
    try:
        cfg = json.loads(Path(s.PAY_CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"读 pay config 失败: {e}"
    try:
        import sys as _sys
        if str(s.ROOT) not in _sys.path:
            _sys.path.insert(0, str(s.ROOT))
        from pipeline import _ensure_gost_alive  # type: ignore
        # _ensure_gost_alive(card_cfg, team_client=None) — listen_port is read from cfg.webshare.gost_listen_port
        ok = _ensure_gost_alive(cfg)
        if not ok:
            return False, "_ensure_gost_alive 返回 False (webshare 未启用 / 无 api_key / 上游探活失败?)"
    except Exception as e:
        return False, f"_ensure_gost_alive 异常: {e}"
    # Explore once more
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
        s2.settimeout(2.0)
        try:
            s2.connect(("127.0.0.1", listen_port))
            return True, "spawned"
        except Exception as e:
            return False, f"启 gost 后仍无监听: {e}"


def refresh_and_update_db(email: str, proxy_url: str = "socks5://127.0.0.1:18898") -> dict:
    """Read email/password/client_id from DB → OAuth flow → if successful then UPDATE DB.

    Returns {ok, email, error?, new_rt_prefix?, imap_alive?}."""
    from . import outlook_pool
    con = outlook_pool.get_db()._conn()
    row = con.execute(
        "SELECT email, password, client_id FROM outlook_accounts WHERE email=?",
        (email,),
    ).fetchone()
    if not row:
        return {"ok": False, "email": email, "error": "邮箱不在池子里"}
    if not row["password"]:
        return {"ok": False, "email": email, "error": "DB 里没保存密码, 无法 OAuth flow"}

    # Ensure proxy is alive before running firefox (go through webshare → outlook not blocked by Microsoft GeoIP risk control)
    import urllib.parse
    proxy_port = urllib.parse.urlparse(proxy_url).port or 18898
    alive, msg = _ensure_proxy_alive(proxy_port)
    if not alive:
        return {"ok": False, "email": email,
                "error": f"代理 {proxy_url} 不可用 ({msg}); 先在 Run 页跑一次或确认 webshare 配置"}
    logger.info(f"[{email}] 代理 {proxy_url}: {msg}")

    new_rt = refresh_token_via_oauth(row["email"], row["password"], row["client_id"], proxy_url=proxy_url)
    if not new_rt:
        reason = getattr(refresh_token_via_oauth, "last_block_reason", "") or "OAuth 流程未拿到 refresh_token (见日志)"
        # identity/confirm class challenge → mark status as dead so users can directly see the root cause on the UI
        if "二次验证" in reason or "拒绝授权" in reason:
            con.execute(
                "UPDATE outlook_accounts SET status='dead', fail_reason=? WHERE email=?",
                (reason[:500], email),
            )
            con.commit()
            return {"ok": False, "email": email, "error": reason, "status": "dead"}
        return {"ok": False, "email": email, "error": reason}

    # Immediately verify that IMAP XOAUTH2 can log in; if it can't log in, mark it as dead even if the RT is written to the DB.
    imap_alive = False
    imap_err = ""
    try:
        at = outlook_pool.get_outlook_access_token(new_rt, row["client_id"])
        import imaplib
        M = imaplib.IMAP4_SSL("outlook.office365.com", 993)
        auth = f"user={row['email']}\x01auth=Bearer {at}\x01\x01"
        typ, _ = M.authenticate("XOAUTH2", lambda x: auth.encode())
        imap_alive = (typ == "OK")
        if not imap_alive:
            imap_err = f"XOAUTH2 returned {typ}"
        try:
            M.logout()
        except Exception:
            pass
    except Exception as e:
        imap_err = f"{type(e).__name__}: {e}"
    if imap_alive:
        con.execute(
            "UPDATE outlook_accounts SET refresh_token=?, status='available', "
            "fail_reason='', claimed_at=0 WHERE email=?",
            (new_rt, email),
        )
        con.commit()
        return {"ok": True, "email": email, "new_rt_prefix": new_rt[:25] + "...",
                "imap_alive": True, "status": "available"}
    else:
        # RT received but IMAP rejected — mark dead but keep new RT, fail_reason explains
        con.execute(
            "UPDATE outlook_accounts SET refresh_token=?, status='dead', "
            "fail_reason=? WHERE email=?",
            (new_rt, f"OAuth 拿到新 RT 但 IMAP 仍拒 (supplier client_id 未声明 v2 IMAP scope): {imap_err}", email),
        )
        con.commit()
        return {"ok": False, "email": email, "new_rt_prefix": new_rt[:25] + "...",
                "imap_alive": False, "error": f"IMAP 拒绝新 token: {imap_err}",
                "status": "dead"}
