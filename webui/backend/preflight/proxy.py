from __future__ import annotations

import os
import socket as _sock
import subprocess
import time
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from ._common import CheckResult, PreflightResult, aggregate


GOST_RELAY_PORT = 18899


class ProxyInput(BaseModel):
    mode: str  # "webshare" | "manual" | "none"
    url: str | None = None
    urls: list[str] | None = None
    register_urls: list[str] | None = None
    payment_urls: list[str] | None = None
    expected_country: str | None = None
    register_expected_country: str | None = None
    payment_expected_country: str | None = None


def _is_socks5_with_auth(url: str) -> bool:
    pp = urlparse(url)
    return pp.scheme in ("socks5", "socks5h") and bool(pp.username)


def _normalize_manual_proxy_url(proxy_url: str) -> str:
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return ""
    if "://" in proxy_url:
        return proxy_url
    return f"http://{proxy_url}"


def _port_listening(port: int) -> bool:
    try:
        with _sock.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        return False


def _spawn_gost_relay(upstream_url: str, listen_port: int) -> tuple[bool, str]:
    """Spawn gost relay for Camoufox when upstream socks5 has auth."""
    if not subprocess.run(["which", "gost"], capture_output=True).stdout.strip():
        return False, "gost not installed; put the gost binary in /usr/local/bin"
    log_path = f"/tmp/gost-{listen_port}.log"
    cmd = ["gost", f"-L=socks5://:{listen_port}", f"-F={upstream_url}"]
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=fd,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            os.close(fd)
    except Exception as e:
        return False, f"spawn failed: {e}"
    deadline = time.time() + 4
    while time.time() < deadline:
        if proc.poll() is not None:
            return False, f"gost exited rc={proc.returncode}; see {log_path}"
        if _port_listening(listen_port):
            return True, f"started PID={proc.pid} log={log_path}"
        time.sleep(0.2)
    return False, f"gost did not listen on :{listen_port} within 4s; see {log_path}"


def _check_one_proxy(proxy_url: str, expected_country: str | None, prefix: str = "") -> list[CheckResult]:
    proxy_url = _normalize_manual_proxy_url(proxy_url)
    checks: list[CheckResult] = []
    try:
        with httpx.Client(proxy=proxy_url, timeout=15.0) as c:
            ip = c.get("https://api.ipify.org").text.strip()
    except Exception as e:
        return [CheckResult(name=f"{prefix}connect", status="fail", message=f"proxy connect failed: {e}")]
    checks.append(CheckResult(name=f"{prefix}exit_ip", status="ok", message=ip))

    try:
        with httpx.Client(timeout=10.0) as c:
            geo = c.get(f"http://ip-api.com/json/{ip}").json()
        country = geo.get("countryCode")
        country_name = geo.get("country")
        msg = f"{country} ({country_name})"
        if expected_country and country and country != expected_country:
            checks.append(CheckResult(
                name=f"{prefix}country",
                status="warn",
                message=f"got {msg}, expected {expected_country}",
            ))
        else:
            checks.append(CheckResult(name=f"{prefix}country", status="ok", message=msg))
    except Exception as e:
        checks.append(CheckResult(name=f"{prefix}country", status="warn", message=f"geo lookup failed: {e}"))

    if _is_socks5_with_auth(proxy_url):
        if _port_listening(GOST_RELAY_PORT):
            checks.append(CheckResult(
                name=f"{prefix}gost_relay",
                status="ok",
                message=f"relay on :{GOST_RELAY_PORT} already listening",
            ))
        else:
            ok, info = _spawn_gost_relay(proxy_url, GOST_RELAY_PORT)
            if ok:
                checks.append(CheckResult(name=f"{prefix}gost_relay", status="ok", message=f"auto-spawned: {info}"))
                try:
                    with httpx.Client(proxy=f"socks5://127.0.0.1:{GOST_RELAY_PORT}", timeout=10.0) as c:
                        ip2 = c.get("https://api.ipify.org").text.strip()
                    if ip2 == ip:
                        checks.append(CheckResult(name=f"{prefix}gost_forward", status="ok", message=f"relay -> {ip2}"))
                    else:
                        checks.append(CheckResult(
                            name=f"{prefix}gost_forward",
                            status="warn",
                            message=f"exit IP mismatch: direct={ip} relay={ip2}",
                        ))
                except Exception as e:
                    checks.append(CheckResult(
                        name=f"{prefix}gost_forward",
                        status="fail",
                        message=f"relay started but forwarding failed: {e}",
                    ))
            else:
                checks.append(CheckResult(name=f"{prefix}gost_relay", status="fail", message=info))
    return checks


def check(body: dict) -> PreflightResult:
    cfg = ProxyInput.model_validate(body)
    if cfg.mode == "none":
        return aggregate([CheckResult(name="proxy", status="ok", message="no proxy configured")])

    register_list = [
        _normalize_manual_proxy_url(str(x).strip())
        for x in (cfg.register_urls or [])
        if str(x).strip()
    ]
    payment_list = [
        _normalize_manual_proxy_url(str(x).strip())
        for x in (cfg.payment_urls or [])
        if str(x).strip()
    ]
    proxy_list = [
        _normalize_manual_proxy_url(str(x).strip())
        for x in (cfg.urls or ([cfg.url] if cfg.url else []))
        if str(x).strip()
    ]
    if register_list or payment_list:
        checks: list[CheckResult] = []
        reg_proxy = (register_list or proxy_list or payment_list)[0] if (register_list or proxy_list or payment_list) else ""
        pay_proxy = (payment_list or proxy_list or register_list)[0] if (payment_list or proxy_list or register_list) else ""
        if reg_proxy:
            checks.extend(_check_one_proxy(
                reg_proxy,
                cfg.register_expected_country or cfg.expected_country,
                "register_",
            ))
        if pay_proxy:
            checks.extend(_check_one_proxy(
                pay_proxy,
                cfg.payment_expected_country or cfg.expected_country,
                "payment_",
            ))
        return aggregate(checks)

    if not proxy_list:
        return aggregate([CheckResult(name="proxy", status="fail", message="proxy url required for mode=" + cfg.mode)])

    if len(proxy_list) > 1:
        checks: list[CheckResult] = []
        checks.extend(_check_one_proxy(
            proxy_list[0],
            cfg.register_expected_country or cfg.expected_country,
            "register_",
        ))
        checks.extend(_check_one_proxy(
            proxy_list[1],
            cfg.payment_expected_country or cfg.expected_country,
            "payment_",
        ))
        return aggregate(checks)

    return aggregate(_check_one_proxy(proxy_list[0], cfg.expected_country))
