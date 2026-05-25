"""HTTP Client - TLS Fingerprint Simulation Using curl_cffi with Cloudflare Bypass, Fallback to requests"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Attempt to use curl_cffi (recommended, has built-in TLS fingerprint simulation)
try:
    from curl_cffi.requests import Session as CffiSession

    _HAS_CFFI = True
    logger.debug("curl_cffi 可用，使用 TLS 指纹模拟")
except ImportError:
    _HAS_CFFI = False
    logger.debug("curl_cffi 不可用，降级到 requests")

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Generic UA
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def create_http_session(proxy: Optional[str] = None, impersonate: str = "chrome136"):
    """Create an HTTP session. Prioritize curl_cffi for simulating browser TLS fingerprints,
    fall back to requests when unavailable."""
    if _HAS_CFFI:
        session = CffiSession(impersonate=impersonate)
        # Use explicit configuration to avoid implicit pollution from system HTTP(S)_PROXY.
        session.trust_env = False
        if proxy:
            # For curl_cffi with SOCKS proxy, socks5h is recommended to route DNS resolution through the proxy endpoint.
            # This reduces TLS handshake exceptions caused by local DNS/link issues.
            normalized_proxy = proxy
            if proxy.startswith("socks5://"):
                normalized_proxy = "socks5h://" + proxy[len("socks5://"):]
                logger.info("代理协议已标准化: socks5:// -> socks5h://")
            session.proxies = {"https": normalized_proxy, "http": normalized_proxy}
        else:
            # Explicitly set null proxy to override system environment variables (trust_env=False is insufficient for libcurl)
            session.proxies = {"https": "", "http": ""}
        return session
    else:
        session = requests.Session()
        session.trust_env = False
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        if proxy:
            session.proxies = {"https": proxy, "http": proxy}
        session.headers["User-Agent"] = USER_AGENT
        return session
