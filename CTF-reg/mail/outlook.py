"""Outlook web login + inbox OTP scrape (replaces dead IMAP OAuth path).

Usage:
    from mail.outlook import scrape_otp  # Wave H: outlook_web_otp.py → mail/outlook.py
    otp = scrape_otp(email, password, timeout=120, threshold_ts=time.time()-30, proxy_url='socks5://127.0.0.1:18898')
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
    window.chrome = { runtime: {}, app: { isInstalled: false } };
"""


def _parse_proxy(proxy_url: str) -> Optional[dict]:
    """Playwright proxy dict. socks5 auth not supported by chromium → caller use relay."""
    if not proxy_url:
        return None
    p = urlparse(proxy_url)
    out = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        out["username"] = p.username
        out["password"] = p.password or ""
    return out


def _login_outlook(page, email: str, password: str) -> bool:
    page.goto("https://login.live.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector('input[type="email"], input[name="loginfmt"]', timeout=30000)
    page.fill('input[type="email"], input[name="loginfmt"]', email)
    page.click('#idSIButton9, button[data-testid="primaryButton"], button[type="submit"], input[type="submit"]')
    page.wait_for_selector('input[type="password"], input[name="passwd"]', timeout=30000)
    page.wait_for_timeout(1200)
    page.fill('input[type="password"], input[name="passwd"]', password)
    page.click('#idSIButton9, button[data-testid="primaryButton"], button[type="submit"], input[type="submit"]')
    deadline = time.time() + 90
    last = ""
    while time.time() < deadline:
        url = page.url
        if url != last:
            logger.info(f"[outlook] {url[:100]}")
            last = url
        if re.search(r"account\.microsoft\.com|outlook\.live\.com/(mail|owa)", url) and not re.search(r"proofs|Consent|privacynotice|signin|login\.", url):
            return True
        # detect explicit error
        try:
            body = page.locator('body').inner_text(timeout=2000)[:300]
            if "Bad user credential" in body or "too many" in body or "サインインの試行" in body:
                logger.error(f"[outlook] MS throttle/auth fail: {body[:200]}")
                return False
        except Exception:
            pass
        # Privacy notice
        if "privacynotice.account.microsoft.com" in url:
            page.evaluate("""
                () => {
                    for (const e of document.querySelectorAll('button,input[type=submit],input[type=button],a[role=button]')) {
                        const t = (e.innerText||e.value||'').trim().toLowerCase();
                        if (['ok','続行','了解','accept','agree','同意'].some(x => t.includes(x))) {
                            if (e.offsetParent !== null) { e.click(); return; }
                        }
                    }
                }
            """)
        elif "proofs/Add" in url:
            page.evaluate("""
                () => {
                    const f = document.querySelector('form'); if (!f) return;
                    const ensure = (n,v) => { let e=f.querySelector(`input[name="${n}"]`); if(!e){e=document.createElement('input');e.type='hidden';e.name=n;f.appendChild(e);} e.value=v; };
                    ensure('action','Skip'); ensure('iProofOptions','Email'); ensure('DisplayPhoneNumber','');
                    ensure('DisplayPhoneCountryISO','JP'); ensure('EmailAddress',''); ensure('PhoneNumber',''); ensure('PhoneCountryISO','');
                    f.submit();
                }
            """)
        elif "Consent" in url:
            page.evaluate("""
                () => {
                    for (const e of document.querySelectorAll('button,input[type=submit]')) {
                        const t = (e.innerText||e.value||'').trim();
                        if (['はい','承諾','Yes','Accept','同意','OK'].some(x => t.includes(x))) {
                            if (e.offsetParent !== null) { e.click(); return; }
                        }
                    }
                }
            """)
        else:
            try:
                btn = page.query_selector('#idSIButton9, button[data-testid="primaryButton"], input[type="submit"]')
                if btn:
                    btn.click(timeout=2000)
            except Exception:
                pass
        page.wait_for_timeout(2000)
    return False


def _scrape_inbox_for_otp(page, threshold_ts: float, timeout: int) -> Optional[str]:
    page.goto("https://outlook.live.com/mail/0/inbox", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            for sel in [
                'div[role="option"]:has-text("ChatGPT")',
                'div[role="option"]:has-text("OpenAI")',
                '[aria-label*="ChatGPT" i]',
                '[aria-label*="OpenAI" i]',
                '[aria-label*="verification" i]',
            ]:
                loc = page.locator(sel).first
                if loc.count():
                    loc.click(timeout=3000)
                    page.wait_for_timeout(2500)
                    body = page.locator('body').inner_text()
                    m = re.search(r"\b(\d{6})\b", body)
                    if m:
                        logger.info(f"[outlook] OTP {m.group(1)}")
                        return m.group(1)
        except Exception as e:
            logger.warning(f"[outlook] inbox poll: {e}")
        elapsed = int(time.time() - (deadline - timeout))
        logger.info(f"[outlook] still waiting ({elapsed}s)")
        page.wait_for_timeout(5000)
    return None


def manual_file_otp(email: str, timeout: int = 600) -> str:
    """Polling-based manual OTP fallback. User writes OTP to /tmp/manual_otp_<email>.txt."""
    import os
    path = f"/tmp/manual_otp_{email.replace('@','_at_').replace('/','_')}.txt"
    if os.path.exists(path):
        os.remove(path)
    logger.error(f"[mail] MANUAL OTP NEEDED → echo '<6-digit>' > {path}")
    logger.error(f"[mail] you have {timeout}s to provide OTP for {email}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            otp = open(path).read().strip()
            os.remove(path)
            if re.match(r"^\d{6}$", otp):
                logger.info(f"[mail] manual OTP received: {otp}")
                return otp
            logger.warning(f"[mail] invalid OTP format in file: {otp!r}, retry")
        time.sleep(2)
    raise TimeoutError(f"manual OTP file {path} not provided within {timeout}s")


def scrape_otp(
    email: str,
    password: str,
    timeout: int = 120,
    threshold_ts: Optional[float] = None,
    # Default refers to port 18898 started by pipeline._ensure_gost_alive (reachable within container).
    # 18899 is an independent relay started by webui preflight, but may not be alive after webui restart.
    proxy_url: str = "socks5://127.0.0.1:18898",
) -> str:
    """Login outlook web with password + scrape OTP from inbox. Raises on failure."""
    from playwright.sync_api import sync_playwright

    if threshold_ts is None:
        threshold_ts = time.time() - 30

    with sync_playwright() as p:
        # firefox first — chromium socks5 typically ERR_PROXY_CONNECTION_FAILED
        try:
            browser = p.firefox.launch(headless=True, proxy=_parse_proxy(proxy_url))
        except Exception:
            browser = p.chromium.launch(
                headless=True,
                proxy=_parse_proxy(proxy_url),
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            )
        ctx = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1920, "height": 1080},
            user_agent=CHROME_UA,
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()
        try:
            ok = _login_outlook(page, email, password)
            if not ok:
                raise RuntimeError(f"outlook login failed for {email}")
            otp = _scrape_inbox_for_otp(page, threshold_ts, timeout)
            if not otp:
                raise TimeoutError(f"no OTP in inbox within {timeout}s for {email}")
            return otp
        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if len(sys.argv) < 3:
        print("usage: outlook_web_otp.py <email> <password>")
        sys.exit(2)
    try:
        otp = scrape_otp(sys.argv[1], sys.argv[2], timeout=180)
        print(f"OTP: {otp}")
    except Exception as e:
        print(f"ERR: {e}")
        sys.exit(1)
