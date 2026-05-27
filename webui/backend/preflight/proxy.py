import os
import subprocess
import socket as _sock
import time
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel
from ._common import CheckResult, PreflightResult, aggregate


# Camoufox 不支持 socks5+auth；CTF-reg/browser_register.py 期望本地
# 127.0.0.1:18899 上有一条无 auth 的 gost socks5 中继转发到上游。
GOST_RELAY_PORT = 18899


class ProxyInput(BaseModel):
    mode: str  # "webshare" | "manual" | "none"
    url: str | None = None
    expected_country: str | None = None


def _is_socks5_with_auth(url: str) -> bool:
    pp = urlparse(url)
    return pp.scheme in ("socks5", "socks5h") and bool(pp.username)


def _port_listening(port: int) -> bool:
    try:
        with _sock.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        return False


def _spawn_gost_relay(upstream_url: str, listen_port: int) -> tuple[bool, str]:
    """Spawn `gost -L=socks5://:N -F=<upstream>` as a daemon. Returns (ok, msg)."""
    if not subprocess.run(["which", "gost"], capture_output=True).stdout.strip():
        return False, "gost 未安装：apt 不带，到 https://github.com/go-gost/gost/releases 下二进制扔到 /usr/local/bin/"
    log_path = f"/tmp/gost-{listen_port}.log"
    cmd = ["gost", f"-L=socks5://:{listen_port}", f"-F={upstream_url}"]
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            proc = subprocess.Popen(
                cmd, stdout=fd, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        finally:
            os.close(fd)
    except Exception as e:
        return False, f"spawn 失败: {e}"
    # 等监听就绪
    deadline = time.time() + 4
    while time.time() < deadline:
        if proc.poll() is not None:
            return False, f"gost 启动后立即退出 (rc={proc.returncode})，见 {log_path}"
        if _port_listening(listen_port):
            return True, f"started PID={proc.pid} log={log_path}"
        time.sleep(0.2)
    return False, f"gost 4s 内未监听 :{listen_port}，见 {log_path}"


def check(body: dict) -> PreflightResult:
    cfg = ProxyInput.model_validate(body)
    if cfg.mode == "none":
        return aggregate([CheckResult(name="proxy", status="ok",
                                      message="no proxy configured")])

    proxy_url = cfg.url
    if not proxy_url:
        return aggregate([CheckResult(name="proxy", status="fail",
                                      message="proxy url required for mode=" + cfg.mode)])

    checks: list[CheckResult] = []

    # 直连上游：先确认 proxy 本身能用
    try:
        with httpx.Client(proxy=proxy_url, timeout=15.0) as c:
            ip = c.get("https://api.ipify.org").text.strip()
    except Exception as e:
        return aggregate([CheckResult(name="connect", status="fail",
                                      message=f"proxy connect failed: {e}")])
    checks.append(CheckResult(name="exit_ip", status="ok", message=ip))

    # 国家检测
    try:
        with httpx.Client(timeout=10.0) as c:
            geo = c.get(f"http://ip-api.com/json/{ip}").json()
        country = geo.get("countryCode")
        country_name = geo.get("country")
        msg = f"{country} ({country_name})"
        if cfg.expected_country and country and country != cfg.expected_country:
            checks.append(CheckResult(name="country", status="warn",
                                      message=f"got {msg}, expected {cfg.expected_country}"))
        else:
            checks.append(CheckResult(name="country", status="ok", message=msg))
    except Exception as e:
        checks.append(CheckResult(name="country", status="warn",
                                  message=f"geo lookup failed: {e}"))

    # socks5+auth → CTF-reg 走 Camoufox，需要本地 :18899 无 auth 中继
    if _is_socks5_with_auth(proxy_url):
        if _port_listening(GOST_RELAY_PORT):
            checks.append(CheckResult(name="gost_relay", status="ok",
                                      message=f"relay on :{GOST_RELAY_PORT} already listening"))
        else:
            ok, info = _spawn_gost_relay(proxy_url, GOST_RELAY_PORT)
            if ok:
                checks.append(CheckResult(name="gost_relay", status="ok",
                                          message=f"auto-spawned: {info}"))
                # 验证中继真能转发
                try:
                    with httpx.Client(proxy=f"socks5://127.0.0.1:{GOST_RELAY_PORT}",
                                      timeout=10.0) as c:
                        ip2 = c.get("https://api.ipify.org").text.strip()
                    if ip2 == ip:
                        checks.append(CheckResult(name="gost_forward", status="ok",
                                                  message=f"relay → {ip2}"))
                    else:
                        checks.append(CheckResult(name="gost_forward", status="warn",
                                                  message=f"exit IP mismatch: direct={ip} relay={ip2}"))
                except Exception as e:
                    checks.append(CheckResult(name="gost_forward", status="fail",
                                              message=f"relay 起来了但转发失败: {e}"))
            else:
                checks.append(CheckResult(name="gost_relay", status="fail",
                                          message=info))

    return aggregate(checks)
