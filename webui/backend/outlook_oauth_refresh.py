"""Device Code Flow 拿 outlook refresh_token (Thunderbird client_id, v2 IMAP scope).

为什么 Device Code 而不是 Auth Code Flow:
- Auth Code 走 Playwright 起 firefox + webshare 代理 IP, Microsoft server 端 risk
  score 把 IP 标 "UnfamiliarLocation" → 强制 identity/confirm + mailtb 收码, 死路.
- Device Code: 你在自己 trusted 浏览器输 user_code, Microsoft 只查你那边 IP/cookie,
  不查我们这边 webshare. 完美绕开.

# webui 调用: POST /api/outlook/device-code/start → 返 user_code + URL
# 用户在 microsoft.com/link 输 code + 登 outlook + 同意 Thunderbird IMAP 访问
# POST /api/outlook/device-code/poll {device_code, target_email} → 拿到 RT 写 DB
#
# 历史: 之前用 Auth Code Flow + Playwright (吃 ROPC 不支持 consumer 的亏, 又被 webshare IP
# 的 UnfamiliarLocation 挑战卡死). Device Code 是唯一不依赖 IP trust 的纯自动化路径,
# 但需要用户在自己 trusted device 一次性 authorize. 当前 webui 没暴露 UI (用户拒手工),
# 此模块只作 backend 备用; Auth Code Flow 老函数也保留兼容.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# Thunderbird 公开 client_id, 注册时声明了 v2 IMAP scope (跟 supplier 的 9e5f94bc 批一致)
THUNDERBIRD_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"

# v2 endpoint (旧 wl.imap v1 token outlook IMAP 不再接受)
OAUTH_DEVICECODE = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
OAUTH_TOKEN = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OAUTH_AUTHORIZE = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"  # 老 Auth Code 路径保留兼容
OOB_REDIRECT = "https://login.live.com/oauth20_desktop.srf"
SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"


# ────────────────────────── Device Code Flow ──────────────────────────


def device_code_start(client_id: str = THUNDERBIRD_CLIENT_ID, scope: str = SCOPE) -> dict:
    """Step 1: 请求 device_code + user_code. 返字段:
    {user_code, device_code, verification_uri, expires_in, interval, message}.
    前端把 user_code 跟 verification_uri 显示给用户, 让用户去浏览器输.
    """
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
    """Step 2: 轮询 token endpoint. 返:
      - {"status": "pending"}         用户还没在浏览器完成 authorize
      - {"status": "ok", ...}         拿到 token, 已写 DB (如 target_email 在池里)
      - {"status": "error", "error"}  失败 (用户拒绝 / device_code 过期 / IMAP 拒)
    """
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

    # decode access_token JWT 取 email 校验 (避免用户输了别的号)
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

    # 选择更新对象: target_email > actual_email > 报错
    email_to_update = target_email or actual_email
    if not email_to_update:
        return {"status": "error", "error": "无法确定邮箱 (target_email + JWT 都空)"}

    # IMAP 验证一遍
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

    # 写 DB (如果该 email 已在池子)
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


# ────────────────────────── 老 Auth Code Flow (Playwright) ──────────────────────────


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
    """跑 Auth Code Flow 拿新 refresh_token. 失败返 None.

    步骤: Firefox 登入 → skip proofs/Add → accept Consent → 抓 redirect code
         → POST token endpoint 换 RT.
    """
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

            # 自动登入
            try:
                page.wait_for_selector('input[type="email"], input[name="loginfmt"]', timeout=30000)
                page.fill('input[type="email"], input[name="loginfmt"]', email)
                page.click('#idSIButton9, button[type="submit"], input[type="submit"]')

                # 等密码框, 但 Microsoft 新流程会先弹 "メールをご確認ください" (passwordless 优先, 发码到 recovery 邮箱),
                # 需要点 "パスワードを使用する" / "Use password" 切回. 循环 30s 看到哪个先来.
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
                # dump 当前 URL + 截图 + HTML 切片让用户判断
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
                        # proofs/Add: 点 "後で"/"Skip" 会被微软解释为"拒绝授权"返 access_denied,
                        # 直接 navigate 到 query 里的 post= URL 才能继续 OAuth flow.
                        qs = urllib.parse.urlparse(url).query
                        post = urllib.parse.parse_qs(qs).get("post", [None])[0]
                        if post:
                            target = urllib.parse.unquote(post)
                            logger.info(f"[{email}] bypass proofs → post={target[:80]}")
                            page.goto(target, wait_until="domcontentloaded", timeout=30000)
                        else:
                            # 没 post= 时退而点 iCancel (英文 UI 才有)
                            for sel in ['#iCancel', 'a[id="iCancel"]']:
                                btn = page.query_selector(sel)
                                if btn and btn.is_visible():
                                    logger.info(f"[{email}] proofs no post, click {sel}")
                                    btn.click(timeout=2000)
                                    page.wait_for_timeout(1200)
                                    break
                    else:
                        # stay-signed-in / 其它 primary 按钮兜底
                        for sel in ['#idSIButton9', 'button[data-testid="primaryButton"]']:
                            btn = page.query_selector(sel)
                            if btn and btn.is_visible():
                                btn.click(timeout=2000)
                                page.wait_for_timeout(800)
                                break
                except Exception:
                    pass
                page.wait_for_timeout(1500)

            # 主循环结束 (拿到 code or timeout). timeout 时 dump 最后页面让用户看
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
        # 拼一份精确错误信息回给 caller
        reason = locals().get("block_reason") or "登入超时 / 密码错 / 异常挑战"
        # 把 reason 塞进 logger 然后通过新的 return value 把它带回去
        logger.error(f"[{email}] 未拿到 OAuth code: {reason}")
        # 借用全局变量这样 refresh_and_update_db 能拿到; 简单做法用属性挂到函数
        refresh_token_via_oauth.last_block_reason = reason  # type: ignore[attr-defined]
        return None
    # 成功时清掉 block_reason
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
    """确保 socks5://127.0.0.1:<port> 有 gost 在听; 没有则复用 pipeline 主逻辑拉起."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", listen_port))
            return True, "already alive"
        except Exception:
            pass
    # 没听 → 走 pipeline._ensure_gost_alive 拉起 (它会读 pay config 拿 webshare upstream)
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
        # _ensure_gost_alive(card_cfg, team_client=None) — listen_port 从 cfg.webshare.gost_listen_port 读
        ok = _ensure_gost_alive(cfg)
        if not ok:
            return False, "_ensure_gost_alive 返回 False (webshare 未启用 / 无 api_key / 上游探活失败?)"
    except Exception as e:
        return False, f"_ensure_gost_alive 异常: {e}"
    # 再探一次
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
        s2.settimeout(2.0)
        try:
            s2.connect(("127.0.0.1", listen_port))
            return True, "spawned"
        except Exception as e:
            return False, f"启 gost 后仍无监听: {e}"


def refresh_and_update_db(email: str, proxy_url: str = "socks5://127.0.0.1:18898") -> dict:
    """读 DB 拿 email/password/client_id → OAuth flow → 成功则 UPDATE DB.

    返回 {ok, email, error?, new_rt_prefix?, imap_alive?}.
    """
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

    # 跑 firefox 前先 ensure 代理活着 (走 webshare → outlook 不被微软 GeoIP 风控)
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
        # identity/confirm 类挑战 → 把 status 标 dead 让用户在 UI 上能直接看到根因
        if "二次验证" in reason or "拒绝授权" in reason:
            con.execute(
                "UPDATE outlook_accounts SET status='dead', fail_reason=? WHERE email=?",
                (reason[:500], email),
            )
            con.commit()
            return {"ok": False, "email": email, "error": reason, "status": "dead"}
        return {"ok": False, "email": email, "error": reason}

    # 立刻验证 IMAP XOAUTH2 真能登; 不能登的 RT 写进 DB 也没用, 标 dead.
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
        # RT 拿到了但 IMAP 拒 — 标 dead 但保留新 RT, fail_reason 说明
        con.execute(
            "UPDATE outlook_accounts SET refresh_token=?, status='dead', "
            "fail_reason=? WHERE email=?",
            (new_rt, f"OAuth 拿到新 RT 但 IMAP 仍拒 (supplier client_id 未声明 v2 IMAP scope): {imap_err}", email),
        )
        con.commit()
        return {"ok": False, "email": email, "new_rt_prefix": new_rt[:25] + "...",
                "imap_alive": False, "error": f"IMAP 拒绝新 token: {imap_err}",
                "status": "dead"}
