"""OTP provider factory: five sources (CLI / file / WhatsApp HTTP / subcommand / config auto-dispatch).

Return value is uniformly `Callable[[], str]`: call once to get the current available OTP, timeout throws OTPCancelled.
Decoupled from gopay/sentinel/webui, only depends on otp_extractor for payload parsing."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from core.otp_extractor import (
    DEFAULT_OTP_REGEX,
    _extract_otp_from_payload,
    _extract_otp_from_text,
)


class OTPProviderError(RuntimeError):
    pass


class OTPCancelled(OTPProviderError):
    pass


def _float_cfg(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _headers_cfg(raw: Any) -> dict:
    return raw if isinstance(raw, dict) else {}


def cli_otp_provider() -> str:
    """Read OTP from stdin (CLI mode)."""
    sys.stdout.write("\n[OTP] Enter verification code: ")
    sys.stdout.flush()
    return sys.stdin.readline().strip()


def file_watch_otp_provider(watch_path: Path, timeout: float = 1800.0) -> Callable[[], str]:
    """Build an OTP provider that polls a file for the OTP value.

    Used by webui runner: emits 'OTP_REQUEST' marker on stdout, then
    blocks reading watch_path until it appears. The webui runner writes the
    OTP into the file when the user submits via the modal.
    """

    def provider() -> str:
        print(f"GOPAY_OTP_REQUEST path={watch_path}", flush=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if watch_path.exists():
                otp = watch_path.read_text(encoding="utf-8").strip()
                try:
                    watch_path.unlink()
                except FileNotFoundError:
                    pass
                if otp:
                    return otp
            time.sleep(0.5)
        raise OTPCancelled(f"OTP timeout after {timeout}s (file={watch_path})")

    return provider


def whatsapp_file_otp_provider(
    path: Path,
    *,
    timeout: float = 300.0,
    interval: float = 1.0,
    code_regex: str = DEFAULT_OTP_REGEX,
    json_path: str = "",
    issued_after_slack_s: float = 15.0,
    delete_after_read: bool = False,
    log: Callable[[str], None] = print,
) -> Callable[[], str]:
    """Poll a local WhatsApp relay state/log file and extract a fresh OTP."""

    def provider() -> str:
        issued_after = time.time() - max(0.0, issued_after_slack_s)
        deadline = time.time() + timeout
        last_error = ""
        log(f"[otp] waiting WhatsApp OTP from file: {path}")
        while time.time() < deadline:
            try:
                if path.exists():
                    stat = path.stat()
                    if stat.st_mtime >= issued_after:
                        text = path.read_text(encoding="utf-8", errors="replace")
                        code = _extract_otp_from_payload(
                            text,
                            code_regex=code_regex,
                            json_path=json_path,
                            issued_after=issued_after,
                        )
                        if code:
                            if delete_after_read:
                                try:
                                    path.unlink()
                                except FileNotFoundError:
                                    pass
                            return code
                last_error = ""
            except Exception as exc:
                last_error = str(exc)
            time.sleep(max(0.2, interval))
        detail = f"; last_error={last_error}" if last_error else ""
        raise OTPCancelled(f"OTP timeout after {timeout}s (file={path}{detail})")

    return provider


def whatsapp_http_otp_provider(
    url: str,
    *,
    timeout: float = 300.0,
    interval: float = 1.0,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    code_regex: str = DEFAULT_OTP_REGEX,
    json_path: str = "",
    issued_after_slack_s: float = 15.0,
    log: Callable[[str], None] = print,
) -> Callable[[], str]:
    """Poll a local/owned WhatsApp relay HTTP endpoint for the latest OTP.

    The endpoint may return plain text or JSON. JSON can either expose the code
    directly (for example {"otp":"123456"}) or contain a WhatsApp Cloud API-like
    message payload; timestamps are honored when present.
    """

    def provider() -> str:
        issued_after = time.time() - max(0.0, issued_after_slack_s)
        deadline = time.time() + timeout
        sess = requests.Session()
        base_params = dict(params or {})
        last_error = ""
        log(f"[otp] waiting WhatsApp OTP from relay: {url}")
        while time.time() < deadline:
            try:
                req_params = dict(base_params)
                if "since" not in req_params:
                    req_params["since"] = str(int(issued_after))
                resp = sess.get(
                    url,
                    headers=headers or {},
                    params=req_params,
                    timeout=min(10.0, max(2.0, interval + 1.0)),
                )
                if resp.status_code in (204, 404):
                    time.sleep(max(0.2, interval))
                    continue
                resp.raise_for_status()
                try:
                    payload: Any = resp.json()
                except ValueError:
                    payload = resp.text
                code = _extract_otp_from_payload(
                    payload,
                    code_regex=code_regex,
                    json_path=json_path,
                    issued_after=issued_after,
                )
                if code:
                    return code
                last_error = ""
            except Exception as exc:
                last_error = str(exc)
            time.sleep(max(0.2, interval))
        detail = f"; last_error={last_error}" if last_error else ""
        raise OTPCancelled(f"OTP timeout after {timeout}s (url={url}{detail})")

    return provider


def command_otp_provider(
    command: Any,
    *,
    timeout: float = 300.0,
    interval: float = 2.0,
    code_regex: str = DEFAULT_OTP_REGEX,
    log: Callable[[str], None] = print,
) -> Callable[[], str]:
    """Poll a user-owned command that prints the latest WhatsApp OTP."""
    argv = command if isinstance(command, list) else shlex.split(str(command or ""))
    if not argv:
        raise OTPProviderError("otp.command is empty")

    def provider() -> str:
        deadline = time.time() + timeout
        last_error = ""
        log(f"[otp] waiting WhatsApp OTP from command: {argv[0]}")
        while time.time() < deadline:
            try:
                proc = subprocess.run(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=min(20.0, max(2.0, interval + 1.0)),
                    check=False,
                )
                text = (proc.stdout or "") + "\n" + (proc.stderr or "")
                code = _extract_otp_from_text(text, code_regex=code_regex)
                if code:
                    return code
                if proc.returncode not in (0, 1):
                    last_error = f"exit={proc.returncode}: {text.strip()[:160]}"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(max(0.2, interval))
        detail = f"; last_error={last_error}" if last_error else ""
        raise OTPCancelled(f"OTP timeout after {timeout}s (command{detail})")

    return provider


def build_configured_otp_provider(
    otp_cfg: dict,
    *,
    fallback_provider: Callable[[], str] = cli_otp_provider,
    log: Callable[[str], None] = print,
) -> Callable[[], str]:
    """Build OTP provider from `otp` config block, falling back to manual input.

    Supported config:
      {
        "source": "http" | "file" | "command" | "manual" | "auto",
        "url": "http://127.0.0.1:8765/api/whatsapp/latest-otp?token=...",
        "path": "/path/to/wa_relay.log",
        "command": ["python", "scripts/get_wa_otp.py"],
        "timeout": 300,
        "interval": 1,
        "code_regex": "(?<!\\d)(\\d{6})(?!\\d)",
        "issued_after_slack_s": 15
      }
    """
    if not isinstance(otp_cfg, dict) or not otp_cfg:
        return fallback_provider

    source = str(otp_cfg.get("source") or otp_cfg.get("type") or "auto").strip().lower()
    if source in ("", "manual", "cli", "stdin"):
        return fallback_provider

    timeout = _float_cfg(otp_cfg, "timeout", _float_cfg(otp_cfg, "timeout_s", 300.0))
    interval = _float_cfg(otp_cfg, "interval", _float_cfg(otp_cfg, "poll_interval_s", 1.0))
    code_regex = str(otp_cfg.get("code_regex") or DEFAULT_OTP_REGEX)
    json_path = str(otp_cfg.get("json_path") or "")
    slack = _float_cfg(otp_cfg, "issued_after_slack_s", 15.0)

    env_url = os.getenv("WEBUI_GOPAY_OTP_URL", "").strip()
    url = str(otp_cfg.get("url") or otp_cfg.get("relay_url") or env_url or "").strip()
    path = str(
        otp_cfg.get("path")
        or otp_cfg.get("state_file")
        or otp_cfg.get("log_file")
        or ""
    ).strip()
    command = otp_cfg.get("command") or otp_cfg.get("cmd")

    if url and (source in ("auto", "http", "https", "relay", "whatsapp_http", "wa_http") or env_url):
        return whatsapp_http_otp_provider(
            url,
            timeout=timeout,
            interval=interval,
            headers=_headers_cfg(otp_cfg.get("headers")),
            params=otp_cfg.get("params") if isinstance(otp_cfg.get("params"), dict) else None,
            code_regex=code_regex,
            json_path=json_path,
            issued_after_slack_s=slack,
            log=log,
        )

    if source in ("auto", "file", "state_file", "log", "whatsapp_file", "wa_file"):
        if path:
            return whatsapp_file_otp_provider(
                Path(path).expanduser(),
                timeout=timeout,
                interval=interval,
                code_regex=code_regex,
                json_path=json_path,
                issued_after_slack_s=slack,
                delete_after_read=bool(otp_cfg.get("delete_after_read", False)),
                log=log,
            )
        if source != "auto":
            raise OTPProviderError("otp source=file requires path/state_file/log_file")

    if source in ("auto", "command", "cmd"):
        if command:
            return command_otp_provider(
                command,
                timeout=timeout,
                interval=interval,
                code_regex=code_regex,
                log=log,
            )
        if source != "auto":
            raise OTPProviderError("otp source=command requires command")

    if source == "auto":
        return fallback_provider
    raise OTPProviderError(f"unsupported otp source: {source}")
