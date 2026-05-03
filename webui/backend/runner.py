"""单 active-run 的 pipeline 进程控制器。

封装 `xvfb-run -a python pipeline.py [args]` 子进程：spawn / 流式收 stdout
到环形日志缓冲 / SIGTERM-优先 stop / 暴露 status + log 给路由层。

GoPay 模式下额外支持 OTP 中转：默认通过 WebUI 内部 HTTP endpoint
把 WhatsApp / 手动补录 OTP 写入 SQLite，gopay.py 轮询该 endpoint。
保留 `GOPAY_OTP_REQUEST path=<file>` 旧格式识别，只作为显式 legacy
file provider 的兼容 fallback。
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import settings as s
from . import wa_relay


_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_started_at: Optional[float] = None
_ended_at: Optional[float] = None
_exit_code: Optional[int] = None
_cmd: Optional[list[str]] = None
_mode: Optional[str] = None
_log_lines: list[dict] = []  # {seq, ts, line}
_seq_counter = 0
_otp_file: Optional[Path] = None       # legacy file provider path, if used
_otp_to_db: bool = False               # True when gopay.py waits on WebUI SQLite OTP endpoint
_otp_pending: bool = False             # set when gopay.py asks/waits for OTP
_otp_file_is_temp: bool = False


def _gopay_auto_otp_enabled() -> bool:
    """Return True when config has a non-manual gopay.otp provider.

    Legacy helper kept for old tests/tools. Current WebUI injects
    WEBUI_GOPAY_OTP_URL and uses the SQLite-backed HTTP provider by default.
    """
    try:
        cfg = json.loads(s.PAY_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    gp = cfg.get("gopay") or {}
    if not isinstance(gp, dict):
        return False
    otp = gp.get("otp") or gp.get("otp_provider") or {}
    if not isinstance(otp, dict):
        return False
    source = str(otp.get("source") or otp.get("type") or "auto").strip().lower()
    if source in ("", "manual", "cli", "stdin"):
        return False
    has_url = bool((otp.get("url") or otp.get("relay_url") or "").strip())
    has_path = bool((otp.get("path") or otp.get("state_file") or otp.get("log_file") or "").strip())
    has_command = bool(otp.get("command") or otp.get("cmd"))
    if source in ("http", "https", "relay", "whatsapp_http", "wa_http"):
        return has_url
    if source in ("file", "state_file", "log", "whatsapp_file", "wa_file"):
        return has_path
    if source in ("command", "cmd"):
        return has_command
    if source == "auto":
        return has_url or has_path or has_command
    return False


def build_cmd(mode: str, paypal: bool, batch: int, workers: int, self_dealer: int,
              register_only: bool, pay_only: bool, gopay: bool = False,
              gopay_otp_file: str = "", count: int = 0) -> list[str]:
    """根据参数拼出最终命令行。"""
    cmd = ["xvfb-run", "-a", "python", "-u", "pipeline.py",
           "--config", str(s.PAY_CONFIG_PATH)]
    # free_only 两个子模式不需要 paypal / gopay 支付段
    if mode in ("free_register", "free_backfill_rt"):
        if mode == "free_register":
            cmd.append("--free-register")
            if count > 0:
                cmd.extend(["--count", str(count)])
        else:
            cmd.append("--free-backfill-rt")
        return cmd
    if gopay:
        cmd.append("--gopay")
        if gopay_otp_file:
            cmd.extend(["--gopay-otp-file", gopay_otp_file])
    elif paypal:
        cmd.append("--paypal")
    # mode 决定循环结构（daemon ∞ / self_dealer / batch N / 单次）
    if mode == "daemon":
        cmd.append("--daemon")
    elif mode == "self_dealer":
        cmd.extend(["--self-dealer", str(self_dealer)])
    elif mode == "batch":
        cmd.extend(["--batch", str(batch), "--workers", str(workers)])
    # mode == "single" → no extra flags
    # register_only / pay_only 是 modifier，跟 mode 正交（batch + register-only
    # = 批量注册 N 个；single + register-only = 单次注册）
    if register_only:
        cmd.append("--register-only")
    elif pay_only:
        cmd.append("--pay-only")
    return cmd


def status() -> dict:
    global _proc
    is_running = _proc is not None and _proc.poll() is None
    return {
        "running": is_running,
        "started_at": _started_at,
        "ended_at": _ended_at,
        "exit_code": _exit_code if not is_running else None,
        "cmd": _cmd,
        "mode": _mode,
        "pid": _proc.pid if is_running and _proc else None,
        "log_count": _seq_counter,
        "otp_pending": _otp_pending,
    }


def start(*, mode: str, paypal: bool = True, batch: int = 0, workers: int = 3,
          self_dealer: int = 0, register_only: bool = False, pay_only: bool = False,
          gopay: bool = False, count: int = 0) -> dict:
    global _proc, _started_at, _ended_at, _exit_code, _cmd, _mode
    global _log_lines, _seq_counter, _otp_file, _otp_to_db, _otp_pending, _otp_file_is_temp
    with _lock:
        if _proc is not None and _proc.poll() is None:
            raise RuntimeError("a pipeline is already running")

        # OTP 默认走 WebUI SQLite endpoint；不再创建临时 FIFO 文件。
        otp_p: Optional[Path] = None

        cmd = build_cmd(mode, paypal, batch, workers, self_dealer,
                        register_only, pay_only, gopay=gopay,
                        gopay_otp_file="", count=count)

        # Reset
        _log_lines = []
        _seq_counter = 0
        _started_at = time.time()
        _ended_at = None
        _exit_code = None
        _cmd = cmd
        _mode = mode
        _otp_file = otp_p
        _otp_to_db = False
        _otp_file_is_temp = otp_p is not None
        _otp_pending = False

        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if gopay:
            env["WEBUI_GOPAY_OTP_URL"] = wa_relay.otp_url()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(s.ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
        except FileNotFoundError as e:
            _ended_at = time.time()
            _exit_code = -1
            raise RuntimeError(f"failed to spawn: {e}") from e
        _proc = proc

        threading.Thread(target=_drain, args=(proc,), daemon=True).start()
    return status()


def _detect_otp_wait_target(line: str) -> tuple[str, Optional[Path]]:
    """Return (kind, path) from GoPay OTP wait markers."""
    if "GOPAY_OTP_REQUEST" in line:
        m = re.search(r"\bpath=(.+?)\s*$", line)
        if m:
            return "file", Path(m.group(1).strip().strip("'\""))
        return "file", _otp_file

    # Legacy configured file provider path.
    m = re.search(r"\[gopay\]\s+waiting WhatsApp OTP from file:\s*(.+?)\s*$", line)
    if m:
        return "file", Path(m.group(1).strip().strip("'\""))

    # New DB-backed WebUI provider, e.g.
    # [gopay] waiting WhatsApp OTP from relay: http://127.0.0.1:8765/api/whatsapp/latest-otp?...
    if re.search(r"\[gopay\]\s+waiting WhatsApp OTP from relay:", line):
        return "db", None
    return "", None


def _drain(proc: subprocess.Popen) -> None:
    global _ended_at, _exit_code, _seq_counter, _log_lines, _otp_pending, _otp_file, _otp_to_db, _otp_file_is_temp
    try:
        if proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip()
            if not line:
                continue
            with _lock:
                _seq_counter += 1
                _log_lines.append({"seq": _seq_counter, "ts": time.time(), "line": line})
                if len(_log_lines) > 3000:
                    _log_lines = _log_lines[-2000:]
                # Detect GoPay OTP request/wait markers.  The second form is
                # used by the configured WhatsApp relay provider; making it
                # pending lets the existing WebUI OTP modal act as a fallback
                # when WhatsApp hides OTP bodies from linked devices.
                wait_kind, wait_path = _detect_otp_wait_target(line)
                if wait_kind:
                    _otp_to_db = wait_kind == "db"
                    _otp_file = wait_path
                    _otp_file_is_temp = _otp_file_is_temp or "GOPAY_OTP_REQUEST" in line
                    _otp_pending = True
    finally:
        proc.wait()
        with _lock:
            _ended_at = time.time()
            _exit_code = proc.returncode
            _otp_pending = False
            # Cleanup OTP file.  For the auto relay path this intentionally
            # removes stale OTPs too; future waits use mtime checks, but an
            # empty/clean file is easier to reason about.
            if _otp_file is not None:
                try:
                    _otp_file.unlink(missing_ok=True)
                except Exception:
                    pass


def stop() -> dict:
    global _proc
    with _lock:
        proc = _proc
        if proc is None or proc.poll() is not None:
            return status()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return status()


def submit_otp(value: str) -> dict:
    """Front-end calls this with the OTP user typed. Stores it in DB by default."""
    global _otp_pending
    with _lock:
        if not _otp_pending:
            raise RuntimeError("no OTP currently requested")
        path = _otp_file
        use_db = _otp_to_db
    if use_db:
        wa_relay.submit_manual_otp(value)
    else:
        if path is None:
            raise RuntimeError("no OTP file currently requested")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.strip(), encoding="utf-8")
    with _lock:
        _otp_pending = False
    return status()


def get_lines_since(since_seq: int = 0, limit: int = 1000) -> list[dict]:
    with _lock:
        return [e for e in _log_lines if e["seq"] > since_seq][:limit]


def get_tail(n: int = 200) -> list[dict]:
    with _lock:
        return _log_lines[-n:]
