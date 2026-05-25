"""ChatGPT registration flow based on Camoufox real browser.
Purpose: Let Turnstile/anti-fraud fingerprints pass through real browser execution, avoid account being flagged by internal risk control
(resulting in successful registration but subsequent Team invite feature being disabled).

Flow:
  1. Camoufox startup → goto https://chatgpt.com/
  2. Click Sign up → redirect to auth.openai.com
  3. Fill email → Continue
  4. Fill password → Continue (may trigger Turnstile, Camoufox fingerprint can pass)
  5. IMAP fetch OTP → fill in → Continue
  6. Fill name/birthday → Continue
  7. Return to chatgpt.com → get access_token from /api/auth/session
  8. Get session_token / oai-did from Cookie

Return: {email, password, session_token, access_token, device_id, cookie_header}"""
import os
import random
import string
import time
import logging
import tempfile
import shutil
import json
import re
import hashlib
import base64
import secrets
from typing import Optional
from urllib.parse import urlparse, urlencode, parse_qs

logger = logging.getLogger(__name__)


def _gen_name() -> tuple[str, str]:
    first_names = ["James", "John", "Emily", "Sophia", "Michael", "Oliver", "Emma",
                   "William", "Amelia", "Lucas", "Mia", "Ethan"]
    last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
                  "Miller", "Davis", "Rodriguez", "Martinez"]
    return random.choice(first_names), random.choice(last_names)


def _gen_birthday() -> tuple[str, str, str]:
    # Adult, 1980-2000 random
    year = random.randint(1980, 2000)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return str(month).zfill(2), str(day).zfill(2), str(year)


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _build_pkce_pair(raw_bytes: int = 64) -> tuple[str, str]:
    verifier = _b64url_no_pad(secrets.token_bytes(raw_bytes))
    challenge = _b64url_no_pad(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _parse_proxy(proxy_url: str):
    """Camoufox requires socks5 + no-auth format. socks5 + auth needs to go through gost relay."""
    if not proxy_url:
        return None
    pp = urlparse(proxy_url)
    if pp.scheme in ("socks5", "socks5h") and pp.username:
        import socket as _sock
        relay_port = 18899
        try:
            with _sock.create_connection(("127.0.0.1", relay_port), timeout=2):
                pass
            return {"server": f"socks5://127.0.0.1:{relay_port}"}
        except Exception:
            raise RuntimeError(
                f"需要 gost 中继: gost -L=socks5://:{relay_port} -F={proxy_url}"
            )
    return {
        "server": f"{pp.scheme}://{pp.hostname}:{pp.port}",
        "username": pp.username or "",
        "password": pp.password or "",
    }


def _page_text(page) -> str:
    try:
        return page.inner_text("body", timeout=3000)
    except Exception:
        return ""


def _blocking_challenge_reason(page) -> str:
    title = ""
    try:
        title = page.title() or ""
    except Exception:
        pass
    url = getattr(page, "url", "") or ""
    text = _page_text(page)
    haystack = "\n".join([title, url, text]).lower()

    if "just a moment" in haystack and ("cloudflare" in haystack or "verifying" in haystack):
        return "Cloudflare challenge"
    if "cf-turnstile" in haystack or "turnstile" in haystack:
        return "Turnstile challenge"
    if "verify you are human" in haystack or "verifying you are human" in haystack:
        return "human verification challenge"
    return ""


def _raise_if_blocking_challenge(page, *, stage: str, screenshot_path) -> None:
    reason = _blocking_challenge_reason(page)
    if not reason:
        return
    try:
        page.screenshot(path=str(screenshot_path))
    except Exception:
        pass
    raise RuntimeError(
        f"{reason} detected during {stage}; saved diagnostic screenshot to {screenshot_path}. "
        "This is a target-site verification page, not a missing form selector. "
        "Retry later, change network conditions, or complete verification manually if supported."
    )


def browser_register(cfg, mail_provider) -> dict:
    """Use real browser to go through registration flow.
    cfg: Config instance (needs proxy field)
    mail_provider: MailProvider instance (call create_mailbox + wait_for_otp)
    Return dict: compatible with AuthResult.to_dict() format"""
    from camoufox.sync_api import Camoufox
    from browserforge.fingerprints import Screen

    email = mail_provider.create_mailbox()
    # Prioritize reusing mail_provider algorithm-generated same-origin persona (email prefix matches first/last + password=local reversed)
    persona = getattr(mail_provider, "last_persona", None)
    if persona is not None:
        password = persona.password
        first_name = persona.first
        last_name = persona.last
        logger.info(f"[browser-reg] 使用 mail_provider 同源 persona")
    else:
        # Backward compatible with resume / old path: email without @ as password + independently pick name
        password = email.replace("@", "")
        if len(password) < 8:
            password = f"{password}2026OpenAI"
        first_name, last_name = _gen_name()
    bmonth, bday, byear = _gen_birthday()
    logger.info(f"[browser-reg] 创建账号: {email}")
    logger.info(f"[browser-reg] 密码: {password}  姓名: {first_name} {last_name}")

    cf_proxy = _parse_proxy(cfg.proxy)
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    tmp_profile = tempfile.mkdtemp(prefix="chatgpt_reg_")
    logger.info(f"[browser-reg] 临时 profile: {tmp_profile}")

    result = {
        "email": email,
        "password": password,
        "session_token": "",
        "access_token": "",
        "device_id": "",
        "csrf_token": "",
        "id_token": "",
        "refresh_token": "",
        "cookie_header": "",
    }

    try:
        with Camoufox(
            headless=not has_display,
            humanize=True,
            persistent_context=True,
            user_data_dir=tmp_profile,
            os="windows",
            screen=Screen(max_width=1920, max_height=1080),
            proxy=cf_proxy,
            geoip=True,
            locale="en-US",
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # [1] Open ChatGPT homepage, click "Sign up for free"
            logger.info("[browser-reg] 打开 ChatGPT 首页 ...")
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            _raise_if_blocking_challenge(
                page,
                stage="opening ChatGPT home",
                screenshot_path="/tmp/browser_reg_cloudflare_challenge.png",
            )
            # Wait for React rendering complete + Sign up button interactive
            try:
                page.wait_for_selector('button[data-testid="signup-button"], a[data-testid="signup-button"]',
                                       state='visible', timeout=20000)
            except Exception:
                _raise_if_blocking_challenge(
                    page,
                    stage="waiting for signup button",
                    screenshot_path="/tmp/browser_reg_cloudflare_challenge.png",
                )
                pass
            time.sleep(3)

            # Click Sign up button — find "Sign up for free" in top right corner
            clicked_signup = False
            for sel in ['a[data-testid="signup-button"]',
                        'button[data-testid="signup-button"]',
                        'button:has-text("Sign up for free")',
                        'a:has-text("Sign up for free")',
                        'button:has-text("Sign up")',
                        'a:has-text("Sign up")']:
                try:
                    btns = page.query_selector_all(sel)
                except Exception:
                    continue
                for btn in btns:
                    try:
                        if not btn.is_visible():
                            continue
                        text = btn.inner_text().lower()
                        if "sign up" not in text:
                            continue
                        # Use 5s timeout click, prevent 30s hang
                        try:
                            btn.click(timeout=5000)
                        except Exception:
                            # If click hangs, trigger with JS
                            btn.evaluate("el => el.click()")
                        clicked_signup = True
                        logger.info(f"[browser-reg] 点击 Sign up ({sel}): {text[:40]}")
                        break
                    except Exception as e_click:
                        if "attached to the DOM" in str(e_click) or "detached" in str(e_click).lower():
                            continue
                        logger.warning(f"[browser-reg] click 异常: {e_click}")
                if clicked_signup:
                    break
            if not clicked_signup:
                page.screenshot(path="/tmp/browser_reg_no_signup.png")
                raise RuntimeError(f"未找到 Sign up 按钮, URL={page.url[:120]}")

            # Wait for redirect to auth.openai.com or modal loading (with retry click)
            pre_url = page.url
            for i in range(20):
                time.sleep(1)
                if "auth.openai.com" in page.url or page.query_selector('input[type="email"]'):
                    break
                # If still no change after 5s, retry clicking Sign up
                if i == 5 and page.url == pre_url:
                    logger.info("[browser-reg] Sign up 点击未生效，重试")
                    try:
                        btn = page.query_selector('button[data-testid="signup-button"], a[data-testid="signup-button"]')
                        if btn:
                            btn.click(timeout=3000)
                    except Exception:
                        try:
                            btn.evaluate("el => el.click()")
                        except Exception:
                            pass
            logger.info(f"[browser-reg] 当前 URL: {page.url[:120]}")
            page.screenshot(path="/tmp/browser_reg_before_email.png")
            _raise_if_blocking_challenge(
                page,
                stage="before email form",
                screenshot_path="/tmp/browser_reg_cloudflare_challenge.png",
            )

            # [2a] New OpenAI (from 2026-05): after clicking Sign up, don't redirect to auth.openai.com,
            # instead pop "Log in or sign up" modal on chatgpt.com, inside it is
            # Continue with Google / Apple / Phone + OR + Continue with email。
            # Old script directly waits for input[type=email] will timeout in 30s, so first identify modal,
            # close Google One-Tap, then click "Continue with email".
            try:
                # Google One-Tap iframe (title contains "Sign in with Google") covers modal,
                # close it first to prevent blocking clicks.
                for ot_sel in [
                    'iframe[src*="accounts.google.com/gsi"]',
                    'div#credential_picker_container button[aria-label*="Close"]',
                    '[aria-label="Close"][role="button"]',
                ]:
                    try:
                        f = page.query_selector(ot_sel)
                        if f and f.is_visible():
                            if "iframe" in ot_sel:
                                # iframe can't be clicked directly, use JS to delete the container
                                page.evaluate(
                                    "() => document.querySelectorAll("
                                    "'iframe[src*=\"accounts.google.com/gsi\"]')"
                                    ".forEach(el => el.remove())"
                                )
                            else:
                                try:
                                    f.click(timeout=2000)
                                except Exception:
                                    pass
                    except Exception:
                        pass
            except Exception:
                pass

            if not page.query_selector('input[type="email"], input[name="email"]'):
                # If email input box not visible now, find email entry button in modal and click again.
                # Order: first exact match "Continue with email", then relax to buttons containing email.
                modal_email_clicked = False
                for sel in [
                    'button:has-text("Continue with email")',
                    'button:has-text("Sign up with email")',
                    'a:has-text("Continue with email")',
                    'a:has-text("Sign up with email")',
                    'button:has-text("Email")',
                    'button[data-testid*="email"]',
                ]:
                    try:
                        btns = page.query_selector_all(sel)
                    except Exception:
                        continue
                    for b in btns:
                        try:
                            if not b.is_visible():
                                continue
                            label = (b.inner_text() or "").lower().strip()
                            # Exclude social buttons like Google/Apple/Phone that happen to contain "email" text
                            if any(skip in label for skip in ("google", "apple", "phone")):
                                continue
                            try:
                                b.scroll_into_view_if_needed(timeout=2000)
                            except Exception:
                                pass
                            try:
                                b.click(timeout=5000)
                            except Exception:
                                b.evaluate("el => el.click()")
                            modal_email_clicked = True
                            logger.info(f"[browser-reg] 点击 modal email 入口 ({sel}): {label[:40]}")
                            break
                        except Exception:
                            continue
                    if modal_email_clicked:
                        break

            # [2] Fill email (click + fill in steps, React re-render may invalidate handle → re-query each step)
            logger.info("[browser-reg] 填邮箱 ...")
            page.wait_for_selector('input[type="email"], input[name="email"]', timeout=30000)
            for _try in range(4):
                try:
                    ei = page.query_selector('input[type="email"]') or \
                         page.query_selector('input[name="email"]')
                    if not ei: time.sleep(0.5); continue
                    ei.click(timeout=5000)
                    time.sleep(0.3)
                    ei2 = page.query_selector('input[type="email"]') or \
                          page.query_selector('input[name="email"]')
                    (ei2 or ei).fill(email)
                    break
                except Exception as e:
                    if "not attached" in str(e).lower() or "detached" in str(e).lower():
                        logger.info(f"[browser-reg] email input 脱链 重试 {_try+1}/4")
                        time.sleep(0.5)
                        continue
                    raise
            time.sleep(random.uniform(0.5, 1.2))
            # Continue
            for sel in ['button[type="submit"]', 'button:has-text("Continue")',
                        'button:has-text("Next")']:
                b = page.query_selector(sel)
                if b and b.is_visible():
                    b.click()
                    logger.info(f"[browser-reg] 点击 email 继续: {sel}")
                    break
            time.sleep(3)

            # [3] Fill password (new account will see password field)
            logger.info("[browser-reg] 等待密码框 ...")
            try:
                page.wait_for_selector(
                    'input[type="password"], input[name="password"]',
                    state="visible", timeout=30000,
                )
                pwd_input = page.query_selector('input[type="password"]:visible') or \
                            page.query_selector('input[name="password"]:visible')
                pwd_input.click()
                time.sleep(0.3)
                pwd_input.fill(password)
                time.sleep(random.uniform(0.5, 1.2))
                for sel in ['button[type="submit"]', 'button:has-text("Continue")',
                            'button:has-text("Create")', 'button:has-text("Next")']:
                    b = page.query_selector(sel)
                    if b and b.is_visible():
                        b.click()
                        logger.info(f"[browser-reg] 点击 password 继续: {sel}")
                        break
            except Exception as e:
                logger.warning(f"[browser-reg] 密码框异常: {e}，可能走无密码 OTP 路径")

            time.sleep(3)
            logger.info(f"[browser-reg] 密码后 URL: {page.url[:120]}")

            # [4] Turnstile / hCaptcha wait (Camoufox fingerprint usually auto-passes)
            logger.info("[browser-reg] 等待反欺诈检查 ...")
            for wait_i in range(30):
                time.sleep(1)
                cur = page.url
                # Reach OTP input or continue step → pass
                if page.query_selector('input[autocomplete="one-time-code"]') or \
                   page.query_selector('input[name="code"]') or \
                   page.query_selector('input[inputmode="numeric"]'):
                    logger.info(f"[browser-reg] 已到达 OTP 页面")
                    break
                if "chatgpt.com" in cur and "auth.openai.com" not in cur:
                    logger.info(f"[browser-reg] 已直接登录到 chatgpt.com")
                    break
                if wait_i == 15:
                    page.screenshot(path="/tmp/browser_reg_wait15.png")
                    logger.info(f"[browser-reg] 15s 等待中: {cur[:80]}")

            # [5] OTP step
            if page.query_selector('input[autocomplete="one-time-code"]') or \
               page.query_selector('input[inputmode="numeric"]'):
                logger.info("[browser-reg] 等待 IMAP OTP ...")
                otp_sent_at = time.time()
                try:
                    otp_timeout = max(30, int(os.getenv("OTP_TIMEOUT", "180")))
                except Exception:
                    otp_timeout = 180
                otp_code = mail_provider.wait_for_otp(email, timeout=otp_timeout, issued_after=otp_sent_at)
                logger.info(f"[browser-reg] 收到 OTP: {otp_code}")
                # Fill OTP
                otp_filled = False
                # May be single box / multiple boxes
                single = page.query_selector('input[autocomplete="one-time-code"]') or \
                         page.query_selector('input[name="code"]') or \
                         page.query_selector('input[inputmode="numeric"]:not([maxlength="1"])')
                if single:
                    single.click()
                    time.sleep(0.3)
                    single.fill(otp_code)
                    otp_filled = True
                else:
                    digits = page.query_selector_all('input[maxlength="1"][inputmode="numeric"]') or \
                             page.query_selector_all('input[maxlength="1"]')
                    if len(digits) >= 6:
                        for i, ch in enumerate(otp_code[:6]):
                            digits[i].click()
                            time.sleep(0.1)
                            digits[i].fill(ch)
                        otp_filled = True
                if not otp_filled:
                    page.screenshot(path="/tmp/browser_reg_otp_fail.png")
                    raise RuntimeError("OTP 输入框未找到")
                time.sleep(0.8)
                # Continue
                for sel in ['button[type="submit"]', 'button:has-text("Continue")',
                            'button:has-text("Verify")', 'button:has-text("Next")']:
                    b = page.query_selector(sel)
                    if b and b.is_visible():
                        b.click()
                        logger.info(f"[browser-reg] 点击 OTP 继续: {sel}")
                        break
                time.sleep(4)

                # OpenAI shows red "Incorrect code" on OTP error, repeatedly clicking
                # Continue triggers max_check_attempts rate limit (permanently stuck). Exit early.
                try:
                    err = page.query_selector(
                        'text=/incorrect code|invalid code|wrong code|验证码不正确|验证码错误/i'
                    )
                    if err and err.is_visible():
                        page.screenshot(path="/tmp/browser_reg_otp_rejected.png")
                        raise RuntimeError(
                            f"OpenAI 拒绝 OTP {otp_code}（OTP 抽取错误，可能是 hex 颜色/tracking id 假阳性）"
                        )
                except RuntimeError:
                    raise
                except Exception:
                    pass

            # [6] /about-you: Full name + Age (single box)
            logger.info(f"[browser-reg] OTP 后 URL: {page.url[:120]}")
            time.sleep(5)  # Wait for redirect to /about-you
            logger.info(f"[browser-reg] 稳定后 URL: {page.url[:120]}")

            # Wait for /about-you form to fully load. First wait for URL to stabilize
            for _ in range(20):
                time.sleep(1)
                if "about-you" in page.url or "chatgpt.com" in page.url:
                    break

            # OpenAI about-you variants:
            #   Old version: Full name + Age (number box)
            #   New version (from 2026-04): Full name + Birthday (date box, pre-filled today)
            # Use JS to export metadata of all inputs at once, avoid visibility detection inconsistency
            def _enum_inputs():
                try:
                    return page.evaluate('''() => {
                        return Array.from(document.querySelectorAll('input')).map((el, idx) => {
                            const r = el.getBoundingClientRect();
                            const cs = getComputedStyle(el);
                            return {
                                idx,
                                type: (el.type || '').toLowerCase(),
                                name: el.name || '',
                                placeholder: el.placeholder || '',
                                ariaLabel: el.getAttribute('aria-label') || '',
                                label: (el.labels && el.labels[0] && el.labels[0].innerText) || '',
                                value: el.value || '',
                                visible: (r.width > 0 && r.height > 0 &&
                                          cs.visibility !== 'hidden' && cs.display !== 'none'),
                            };
                        });
                    }''') or []
                except Exception:
                    return []

            def _is_birthday(meta: dict) -> bool:
                blob = " ".join([meta.get("type",""), meta.get("name",""),
                                  meta.get("placeholder",""), meta.get("ariaLabel",""),
                                  meta.get("label","")]).lower()
                if meta.get("type") == "date":
                    return True
                return any(kw in blob for kw in ("birth", "birthday", "dob",
                                                  "mm/dd/yyyy", "mm / dd / yyyy"))

            def _is_name_input(meta: dict) -> bool:
                blob = " ".join([meta.get("name",""), meta.get("placeholder",""),
                                  meta.get("ariaLabel",""), meta.get("label","")]).lower()
                # Old about-you uses "age" number box; new version uses "Full name" + "Birthday"
                return any(kw in blob for kw in ("name", "first", "last", "full",
                                                  "given", "family", "age"))

            def _looks_like_chat_ui() -> bool:
                """chatgpt.com home page characteristics: chat input box in the bottom-right corner + "New chat" on the sidebar.
                This type of page is not an about-you form; seeing 2 inputs doesn't mean you can fill them randomly."""
                try:
                    return bool(page.evaluate('''() => {
                        const url = location.href;
                        if (url.includes("/about-you")) return false;
                        // chat 输入框：textarea 或 contenteditable，placeholder 含 "Ask"
                        const ta = document.querySelector(
                            'textarea[placeholder*="Ask"], div[contenteditable="true"]'
                        );
                        // 左侧 New chat 链接
                        const nc = Array.from(document.querySelectorAll("a, button"))
                            .some(el => /new chat/i.test(el.textContent || ""));
                        return !!(ta || nc);
                    }'''))
                except Exception:
                    return False

            full_name_input = None
            birthday_input = None
            birthday_meta = None
            for attempt in range(30):
                metas = _enum_inputs()
                visible_metas = [m for m in metas if m["visible"]
                                  and m["type"] not in ("hidden","submit","button",
                                                         "checkbox","radio","password")]
                # First pick Birthday + name input with keyword matches — both keywords must match to count.
                bd = next((m for m in visible_metas if _is_birthday(m)), None)
                name_m = next((m for m in visible_metas
                                if m is not bd
                                and _is_name_input(m)
                                and not _is_birthday(m)), None)
                if bd and name_m:
                    all_inputs_el = page.query_selector_all('input')
                    full_name_input = all_inputs_el[name_m["idx"]]
                    birthday_input = all_inputs_el[bd["idx"]]
                    birthday_meta = bd
                    logger.info(f"[browser-reg] 表单: name.idx={name_m['idx']} "
                                f"birthday.idx={bd['idx']} type={bd['type']} "
                                f"placeholder={bd['placeholder'][:30]!r}")
                    break
                # Compatible with old age version: 2 inputs + at least one matching name keyword + URL not in
                # chatgpt.com main chat page (avoid mistakenly treating chat textarea + search as a form).
                if (
                    not bd
                    and len(visible_metas) >= 2
                    and any(_is_name_input(m) for m in visible_metas)
                    and not _looks_like_chat_ui()
                ):
                    all_inputs_el = page.query_selector_all('input')
                    full_name_input = all_inputs_el[visible_metas[0]["idx"]]
                    birthday_input = all_inputs_el[visible_metas[1]["idx"]]
                    birthday_meta = visible_metas[1]
                    logger.info(f"[browser-reg] 表单 (legacy age): {len(visible_metas)} inputs")
                    break
                # Already on chatgpt.com home page (not about-you subpath), and cannot see about-you form
                # —— registration may have completed directly, exit loop to let the outer layer check accessToken.
                if (
                    "chatgpt.com" in page.url
                    and "auth" not in page.url
                    and "/about-you" not in page.url
                    and _looks_like_chat_ui()
                ):
                    logger.info("[browser-reg] URL 在 chatgpt.com 主页，无 about-you 表单 → 跳过表单填写")
                    break
                if attempt == 5:
                    page.screenshot(path="/tmp/browser_reg_about_you_wait.png")
                    logger.info(f"[browser-reg] 等待 about-you 输入框 5s, URL={page.url[:100]} "
                                f"inputs visible={len(visible_metas)}")
                time.sleep(1)

            if full_name_input and birthday_input:
                page.screenshot(path="/tmp/browser_reg_about_you.png")
                full_name = f"{first_name} {last_name}"
                # Birthday: January 15 between ages 26-40 (sufficiently >18, fixed date for consistent fingerprint)
                import datetime as _dt
                year = _dt.datetime.now().year - random.randint(26, 40)
                mm, dd = "01", "15"
                # native date input uses YYYY-MM-DD, text boxes are mostly MM/DD/YYYY
                bd_type = (birthday_meta or {}).get("type", "")
                if bd_type == "date":
                    birthday_str = f"{year}-{mm}-{dd}"
                else:
                    birthday_str = f"{mm}/{dd}/{year}"
                legacy_age = str(random.randint(26, 40))
                logger.info(f"[browser-reg] 填 Full name={full_name}  "
                            f"Birthday={birthday_str} (legacy_age={legacy_age})")
                try:
                    full_name_input.focus(); time.sleep(0.3)
                    page.keyboard.type(full_name, delay=random.randint(30, 80))
                    time.sleep(random.uniform(0.4, 0.9))
                    birthday_input.focus(); time.sleep(0.3)
                    # Clear first (prefilled may have today's date)
                    try:
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Delete")
                    except Exception:
                        pass
                    # Use fill to write ISO directly for native date input; use keyboard.type for text boxes
                    if bd_type == "date":
                        try:
                            birthday_input.fill(birthday_str)
                        except Exception:
                            page.keyboard.type(birthday_str, delay=random.randint(30, 70))
                    else:
                        # MM/DD/YYYY: for compatibility with old age version, if it looks like number/age, only type age
                        if _is_birthday(birthday_meta or {}):
                            page.keyboard.type(birthday_str, delay=random.randint(30, 70))
                        else:
                            page.keyboard.type(legacy_age, delay=random.randint(40, 100))
                    time.sleep(random.uniform(0.4, 0.9))
                    clicked = False
                    for sel in ['button:has-text("Finish")', 'button:has-text("Create")',
                                'button:has-text("Agree")', 'button[type="submit"]',
                                'button:has-text("Continue")']:
                        b = page.query_selector(sel)
                        if b and b.is_visible():
                            b.click()
                            clicked = True
                            logger.info(f"[browser-reg] 点击 about-you 继续: {sel}")
                            break
                    if not clicked:
                        page.screenshot(path="/tmp/browser_reg_no_finish_btn.png")
                except Exception as e:
                    logger.warning(f"[browser-reg] about-you 填写异常: {e}")
                    page.screenshot(path="/tmp/browser_reg_name_fail.png")
            else:
                page.screenshot(path="/tmp/browser_reg_no_name_form.png")
                logger.warning(f"[browser-reg] 未找到 about-you 表单，URL={page.url[:120]}")

            # [7] Wait to return to chatgpt.com (may have intermediate pages like email-verification / success-page)
            logger.info("[browser-reg] 等待跳转回 chatgpt.com ...")
            arrived = False
            last_url = ""
            for i in range(120):
                time.sleep(1)
                cur = page.url
                if cur != last_url:
                    logger.info(f"[browser-reg] URL@{i}s: {cur[:120]}")
                    last_url = cur
                # Back at chatgpt.com and React main interface already loaded
                if "chatgpt.com" in cur and "auth.openai.com" not in cur:
                    # Wait for /api/auth/session to return accessToken normally to count as complete
                    try:
                        info = page.evaluate('''async () => {
                            try {
                                const r = await fetch("/api/auth/session", {credentials: "include"});
                                const d = await r.json();
                                return d.accessToken ? d.accessToken.length : 0;
                            } catch(e){ return -1; }
                        }''')
                        if info and info > 100:
                            arrived = True
                            logger.info(f"[browser-reg] 到达 + session accessToken 长度={info}")
                            break
                    except Exception:
                        pass
                # If still on auth.openai.com, may have /email-verification or other redirects, continue clicking continue
                if "auth.openai.com" in cur and i % 10 == 5:
                    for sel in ['button:has-text("Continue")', 'button:has-text("Next")',
                                'button[type="submit"]']:
                        try:
                            b = page.query_selector(sel)
                            if b and b.is_visible():
                                b.click()
                                logger.info(f"[browser-reg] 中转点击: {sel}")
                                break
                        except Exception:
                            # Page navigation causes context destroyed, ignore
                            pass
            if not arrived:
                page.screenshot(path="/tmp/browser_reg_no_chatgpt.png")
                raise RuntimeError(f"未跳转回 chatgpt.com，当前: {page.url[:120]}")

            # [8] Wait for JS initialization to complete, get access_token
            time.sleep(5)
            logger.info("[browser-reg] 拉取 /api/auth/session ...")
            session_info = page.evaluate('''async () => {
                const r = await fetch("/api/auth/session", {credentials: "include"});
                return await r.json();
            }''')
            result["access_token"] = session_info.get("accessToken", "")
            result["id_token"] = session_info.get("idToken", "") if isinstance(session_info, dict) else ""
            logger.info(f"[browser-reg] access_token 长度: {len(result['access_token'])}")

            # [9] Extract cookies
            all_cookies = ctx.cookies()
            chatgpt_cookies = [c for c in all_cookies if "chatgpt.com" in c.get("domain", "")]
            for c in chatgpt_cookies:
                n = c["name"]
                if n == "__Secure-next-auth.session-token":
                    result["session_token"] = c["value"]
                if n in ("oai-did", "oai-device-id"):
                    result["device_id"] = c["value"]
                if n == "__Host-next-auth.csrf-token":
                    result["csrf_token"] = c["value"].split("|")[0] if "|" in c["value"] else c["value"]
            result["cookie_header"] = "; ".join(
                f"{c['name']}={c['value']}" for c in chatgpt_cookies
            )
            logger.info(
                f"[browser-reg] session_token={'yes' if result['session_token'] else 'no'} "
                f"device_id={result['device_id'][:16]}..."
            )

            # [10] Codex OAuth to get refresh_token
            # Known limitation: after signup completes, auth.openai.com's hydra session cannot exchange tokens for Codex
            # (login_session is just signup challenge state, not a complete user session)
            # Current refresh_token will be empty; if refresh_token is needed, need to login to account and re-run Codex OAuth
            #
            # Empirically verified (2026-04 recent daemon + self-dealer full logs), signup-state Codex OAuth
            # 100% returns token_exchange_user_error, wasting ~30s each time. Skipped by default; if retaining old path
            # as reverse engineering reference, set SKIP_SIGNUP_CODEX_RT=0. Subsequently _exchange_refresh_token_with_session
            # (card.py) or self-dealer's member re-login will normally get RT.
            if str(os.environ.get("SKIP_SIGNUP_CODEX_RT", "1")).lower() in ("1", "true", "yes", "on"):
                logger.info("[browser-reg] 跳过 signup 态 Codex OAuth（SKIP_SIGNUP_CODEX_RT=1，已知 100% 失败）")
                result["refresh_token"] = result.get("refresh_token", "") or ""
            else:
                try:
                    codex_client_id = (os.getenv("OAUTH_CODEX_CLIENT_ID", "") or "").strip() or "app_EMoamEEZ73f0CkXaXp7hrann"
                    codex_redirect = "http://localhost:1455/auth/callback"
                    codex_scope = "openid email profile offline_access"
                    codex_state = _b64url_no_pad(secrets.token_bytes(24))
                    verifier, challenge = _build_pkce_pair()
                    auth_params = {
                        "client_id": codex_client_id,
                        "response_type": "code",
                        "redirect_uri": codex_redirect,
                        "scope": codex_scope,
                        "state": codex_state,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "id_token_add_organizations": "true",
                        "codex_cli_simplified_flow": "true",
                        # Without prompt=none: session already established through browser registration,
                        # let server automatically recognize session, auto-approve when consent page appears
                    }
                    auth_url = f"https://auth.openai.com/oauth/authorize?{urlencode(auth_params)}"
                    logger.info("[browser-reg] Codex OAuth 获取 refresh_token ...")
                    # Real browser goto + route intercept localhost
                    cb_url = ""
                    callback_holder = {"url": ""}

                    def _codex_intercept(route):
                        url = route.request.url
                        if "localhost:1455" in url and "code=" in url:
                            callback_holder["url"] = url
                            logger.info(f"[browser-reg] 拦截到 Codex callback: {url[:150]}")
                        try:
                            route.fulfill(status=200, content_type="text/html", body="<html>OK</html>")
                        except Exception:
                            try: route.abort()
                            except: pass

                    page.route("**/localhost:1455/**", _codex_intercept)
                    page.route("http://localhost:1455/**", _codex_intercept)
                    page.route("**localhost:1455**", _codex_intercept)

                    try:
                        page.goto(auth_url, wait_until="commit", timeout=30000)
                    except Exception as e_nav:
                        logger.info(f"[browser-reg] Codex goto: {str(e_nav)[:120]}")

                    for _ in range(30):
                        if callback_holder["url"]:
                            break
                        if "localhost:1455" in page.url and "code=" in page.url:
                            callback_holder["url"] = page.url
                            break
                        time.sleep(0.5)

                    try:
                        page.unroute("**/localhost:1455/**")
                        page.unroute("http://localhost:1455/**")
                        page.unroute("**localhost:1455**")
                    except Exception:
                        pass

                    cb_url = callback_holder["url"]
                    logger.info(f"[browser-reg] Codex callback URL: {cb_url[:150] if cb_url else '<空>'}")
                    if not cb_url:
                        logger.info(f"[browser-reg] 当前 page.url: {page.url[:200]}")
                    if cb_url:
                        qs = parse_qs(urlparse(cb_url).query)
                        code = (qs.get("code") or [""])[0]
                        if code:
                            logger.info(f"[browser-reg] 获得 auth code, 换 refresh_token ...")
                            import curl_cffi.requests as cr
                            http_token = cr.Session(impersonate="chrome136")
                            if cf_proxy and cf_proxy.get("server"):
                                pu = cf_proxy["server"]
                                http_token.proxies = {"http": pu, "https": pu}
                            resp_token = http_token.post(
                                "https://auth.openai.com/oauth/token",
                                data={
                                    "grant_type": "authorization_code",
                                    "client_id": codex_client_id,
                                    "code": code,
                                    "redirect_uri": codex_redirect,
                                    "code_verifier": verifier,
                                },
                                headers={
                                    "Content-Type": "application/x-www-form-urlencoded",
                                    "Accept": "application/json",
                                },
                                timeout=30,
                            )
                            logger.info(f"[browser-reg] /oauth/token: {resp_token.status_code}")
                            if resp_token.status_code == 200:
                                try:
                                    tj = resp_token.json()
                                    result["refresh_token"] = tj.get("refresh_token", "") or ""
                                    if tj.get("access_token"):
                                        result["codex_access_token"] = tj["access_token"]
                                    logger.info(f"[browser-reg] refresh_token 长度: {len(result['refresh_token'])}")
                                except Exception as e_tok:
                                    logger.warning(f"[browser-reg] 解析 token 响应失败: {e_tok}")
                            else:
                                logger.warning(f"[browser-reg] token 交换失败: {resp_token.status_code} {resp_token.text[:200]}")
                        else:
                            logger.warning(f"[browser-reg] callback 无 code: {cb_url[:120]}")
                    else:
                        logger.warning("[browser-reg] 未捕获到 callback URL")
                except Exception as e_codex:
                    logger.warning(f"[browser-reg] Codex OAuth 异常: {e_codex}")

            if not result["access_token"] or not result["session_token"]:
                page.screenshot(path="/tmp/browser_reg_missing_token.png")
                raise RuntimeError(
                    f"缺少凭证: access_token={bool(result['access_token'])} "
                    f"session_token={bool(result['session_token'])}"
                )
    finally:
        try:
            shutil.rmtree(tmp_profile, ignore_errors=True)
        except Exception:
            pass

    return result
