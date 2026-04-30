"""WhatsApp Web sidecar (Node) lifecycle + state reader.

Single-instance daemon manager: at most one Node process runs.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import settings as s


_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_mode: str = ""        # "qr" | "pairing" | ""
_started_at: Optional[float] = None


def _data_dir() -> Path:
    d = s.get_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path() -> Path:
    return _data_dir() / "wa_state.json"


def _otp_path() -> Path:
    return _data_dir() / "wa_otp.txt"


def _session_dir() -> Path:
    p = _data_dir() / "wa_session"
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_running() -> bool:
    global _proc
    return _proc is not None and _proc.poll() is None


def status() -> dict:
    """Read state file written by the Node sidecar."""
    sp = _state_path()
    base = {
        "running": is_running(),
        "pid": (_proc.pid if is_running() and _proc else None),
        "mode": _mode,
        "started_at": _started_at,
    }
    if sp.exists():
        try:
            base.update(json.loads(sp.read_text(encoding="utf-8")))
        except Exception as e:
            base["state_read_error"] = str(e)
    else:
        base.setdefault("status", "stopped" if not is_running() else "starting")
    return base


def start(mode: str = "qr", pairing_phone: str = "") -> dict:
    """Spawn the Node sidecar in qr or pairing mode. Idempotent: if already
    running with same mode, no-op."""
    global _proc, _mode, _started_at

    mode = (mode or "qr").lower()
    if mode not in ("qr", "pairing"):
        raise ValueError(f"mode must be qr or pairing, got {mode!r}")
    if mode == "pairing":
        digits = "".join(ch for ch in (pairing_phone or "") if ch.isdigit())
        if len(digits) < 10:
            raise ValueError("pairing 模式需要 pairing_phone（含国家码，10+ 位数字）")
        pairing_phone = digits

    with _lock:
        # If running with same mode, just report status
        if is_running() and _mode == mode:
            return status()
        # Stop any prior instance before spawning a new one
        _stop_locked()

        relay_dir = s.WA_RELAY_DIR
        index_js = relay_dir / "index.js"
        if not index_js.exists():
            raise RuntimeError(f"relay sidecar 缺失: {index_js}")

        node_modules = relay_dir / "node_modules"
        if not node_modules.exists():
            raise RuntimeError(
                f"未安装 sidecar 依赖；先跑 `cd {relay_dir} && npm install`"
            )

        # Clean stale state file so frontend doesn't show old data
        try:
            _state_path().unlink()
        except FileNotFoundError:
            pass
        try:
            _otp_path().unlink()
        except FileNotFoundError:
            pass

        env = {
            **os.environ,
            "WA_LOGIN_MODE": mode,
            "WA_STATE_FILE": str(_state_path()),
            "WA_OTP_FILE": str(_otp_path()),
            "WA_SESSION_DIR": str(_session_dir()),
            "WA_HEADLESS": "1",
        }
        if mode == "pairing":
            env["WA_PAIRING_PHONE"] = pairing_phone

        log_path = _data_dir() / "wa_relay.log"
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            proc = subprocess.Popen(
                ["node", str(index_js)],
                cwd=str(relay_dir),
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        finally:
            os.close(log_fd)

        _proc = proc
        _mode = mode
        _started_at = time.time()

    # Brief grace period so first state file write happens before caller polls
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if _state_path().exists():
            break
        if not is_running():
            break
        time.sleep(0.1)

    return status()


def _stop_locked() -> None:
    global _proc, _mode, _started_at
    proc = _proc
    if proc is None:
        return
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()
    _proc = None
    _mode = ""
    _started_at = None


def stop() -> dict:
    with _lock:
        _stop_locked()
    return status()


def logout() -> dict:
    """Stop sidecar AND remove session dir so next start prompts fresh QR."""
    with _lock:
        _stop_locked()
        sd = _session_dir()
        if sd.exists():
            shutil.rmtree(sd, ignore_errors=True)
        # Recreate empty so subsequent start() works
        sd.mkdir(parents=True, exist_ok=True)
        try:
            _state_path().unlink()
        except FileNotFoundError:
            pass
    return {"status": "logged_out"}


def otp_path() -> Path:
    """Path that gopay.py file_watch_otp_provider should poll for relay-fed OTPs."""
    return _otp_path()
