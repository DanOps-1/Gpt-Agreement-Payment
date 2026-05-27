"""
HTTP 客户端 - 使用 curl_cffi 实现 TLS 指纹模拟
支持 Cloudflare 绕过，降级到 requests
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 尝试使用 curl_cffi（推荐，自带 TLS 指纹模拟）
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

# 通用 UA
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def create_http_session(proxy: Optional[str] = None, impersonate: str = "chrome136"):
    """
    创建 HTTP 会话。优先使用 curl_cffi 模拟浏览器 TLS 指纹，
    不可用时降级到 requests。
    """
    if _HAS_CFFI:
        session = CffiSession(impersonate=impersonate)
        # 使用显式配置，避免被系统 HTTP(S)_PROXY 隐式污染。
        session.trust_env = False
        if proxy:
            # curl_cffi 在 SOCKS 代理下建议使用 socks5h，让 DNS 走代理端解析。
            # 这能减少本地 DNS/链路导致的 TLS 握手异常。
            normalized_proxy = proxy
            if proxy.startswith("socks5://"):
                normalized_proxy = "socks5h://" + proxy[len("socks5://"):]
                logger.info("代理协议已标准化: socks5:// -> socks5h://")
            session.proxies = {"https": normalized_proxy, "http": normalized_proxy}
        else:
            # 显式设置空代理，覆盖系统环境变量 (trust_env=False 对 libcurl 不够)
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
