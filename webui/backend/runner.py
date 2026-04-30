"""单 active-run 的 pipeline 进程控制器。

封装 `xvfb-run -a python pipeline.py [args]` 子进程：spawn / 流式收 stdout
到环形日志缓冲 / SIGTERM-优先 stop / 暴露 status + log 给路由层。

GoPay 模式下额外支持 OTP 中转：gopay.py 在 stdout 打印
`GOPAY_OTP_REQUEST path=<file>` 标记后阻塞，runner 记下 file 路径，
等前端 POST /run/otp 提交 OTP 后写入 file，gopay.py 读取继续。
"""
import glob
import os
import shutil
import signal
import subprocess
import tempfile
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
_otp_file: Optional[Path] = None       # path passed via --gopay-otp-file
_otp_pending: bool = False             # set when gopay.py emits OTP_REQUEST


def build_cmd(mode: str, paypal: bool, batch: int, workers: int, self_dealer: int,
              register_only: bool, pay_only: bool, gopay: bool = False,
              gopay_otp_file: str = "") -> list[str]:
    """根据参数拼出最终命令行。"""
    cmd = ["xvfb-run", "-a", "python", "-u", "pipeline.py",
           "--config", str(s.PAY_CONFIG_PATH)]
    if gopay:
        cmd.append("--gopay")
        if gopay_otp_file:
            cmd.extend(["--gopay-otp-file", gopay_otp_file])
    elif paypal:
        cmd.append("--paypal")
    if register_only:
        cmd.append("--register-only")
    elif pay_only:
        cmd.append("--pay-only")
    elif mode == "daemon":
        cmd.append("--daemon")
    elif mode == "self_dealer":
        cmd.extend(["--self-dealer", str(self_dealer)])
    elif mode == "batch":
        cmd.extend(["--batch", str(batch), "--workers", str(workers)])
    # mode == "single" → no extra flags
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
          gopay: bool = False) -> dict:
    global _proc, _started_at, _ended_at, _exit_code, _cmd, _mode
    global _log_lines, _seq_counter, _otp_file, _otp_pending
    with _lock:
        if _proc is not None and _proc.poll() is None:
            raise RuntimeError("a pipeline is already running")
    # 启动前先清上次崩溃留下的 Xvfb/Camoufox/profile，防止累积
    cleanup_orphans()
    with _lock:

        # Allocate OTP fifo path. Prefer the WhatsApp relay's OTP file when
        # the relay is connected — that way OTPs flow in fully automatic.
        # Otherwise fall back to a fresh temp path the modal will write to.
        otp_path = ""
        otp_p: Optional[Path] = None
        if gopay:
            wa_st = wa_relay.status()
            if wa_st.get("running") and wa_st.get("status") == "connected":
                otp_p = wa_relay.otp_path()
                otp_p.parent.mkdir(parents=True, exist_ok=True)
                # Clear any stale OTP from previous run
                try:
                    otp_p.unlink()
                except FileNotFoundError:
                    pass
                otp_path = str(otp_p)
            else:
                tmp = tempfile.NamedTemporaryFile(
                    prefix="gopay_otp_", suffix=".txt", delete=False,
                )
                tmp.close()
                otp_p = Path(tmp.name)
                otp_p.unlink(missing_ok=True)  # gopay.py polls for existence
                otp_path = str(otp_p)

        cmd = build_cmd(mode, paypal, batch, workers, self_dealer,
                        register_only, pay_only, gopay=gopay,
                        gopay_otp_file=otp_path)

        # Reset
        _log_lines = []
        _seq_counter = 0
        _started_at = time.time()
        _ended_at = None
        _exit_code = None
        _cmd = cmd
        _mode = mode
        _otp_file = otp_p
        _otp_pending = False

        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(s.ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                # 关键：自己的 session/process-group，stop() 才能 killpg 整组
                start_new_session=True,
            )
        except FileNotFoundError as e:
            _ended_at = time.time()
            _exit_code = -1
            raise RuntimeError(f"failed to spawn: {e}") from e
        _proc = proc

        threading.Thread(target=_drain, args=(proc,), daemon=True).start()
    return status()


def _drain(proc: subprocess.Popen) -> None:
    global _ended_at, _exit_code, _seq_counter, _log_lines, _otp_pending
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
                # Detect gopay OTP request / consume markers
                if "GOPAY_OTP_REQUEST" in line:
                    _otp_pending = True
                elif "GOPAY_OTP_CONSUMED" in line:
                    _otp_pending = False
    finally:
        proc.wait()
        with _lock:
            _ended_at = time.time()
            _exit_code = proc.returncode
            _otp_pending = False
            # Cleanup OTP file
            if _otp_file is not None:
                try:
                    _otp_file.unlink(missing_ok=True)
                except Exception:
                    pass
        # 后台收尾：杀残留 Xvfb/Camoufox + 清 /tmp profile（在锁外，安全）
        try:
            cleanup_orphans()
        except Exception:
            pass


def _kill_pg(proc: subprocess.Popen, term_timeout: float = 5.0) -> None:
    """Send SIGTERM to whole process group; SIGKILL after timeout. Idempotent."""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=term_timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


def cleanup_orphans() -> dict:
    """Hunt down stray Xvfb / Camoufox / temp profiles that leaked from
    crashed/killed pipeline runs. Safe to call any time; never touches the
    currently-active pipeline (that one's process-group survives because
    runner is still tracking it)."""
    killed = {"xvfb": 0, "camoufox": 0, "browser_register": 0,
              "playwright_driver": 0, "puppeteer_chrome": 0, "tmp_dirs": 0}

    # 当前活跃 pipeline 的 PGID（保护它不被误杀）
    protect_pgid = None
    if _proc is not None and _proc.poll() is None:
        try:
            protect_pgid = os.getpgid(_proc.pid)
        except ProcessLookupError:
            pass

    def _kill_matching(pattern: str, key: str) -> None:
        try:
            out = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            return
        for line in out.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            try:
                if protect_pgid is not None and os.getpgid(pid) == protect_pgid:
                    continue
            except ProcessLookupError:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed[key] += 1
            except ProcessLookupError:
                pass
            except PermissionError:
                pass

    _kill_matching(r"Xvfb :10[0-9]", "xvfb")
    _kill_matching(r"camoufox-bin", "camoufox")
    _kill_matching(r"browser_register", "browser_register")
    _kill_matching(r"playwright/driver", "playwright_driver")
    # puppeteer 的 chromium（whatsapp_relay 用），但保护当前 relay 的 chromium
    # —— 通过 protect_pgid 已经过滤过了，可以直接 grep
    _kill_matching(r"puppeteer/chrome.*chrome-linux64/chrome", "puppeteer_chrome")

    # 清临时目录（profile / xvfb auth）
    for pat in ("/tmp/chatgpt_reg_*", "/tmp/xvfb-run.*",
                "/tmp/probe_*", "/tmp/rt_login_*"):
        for path in glob.glob(pat):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
                killed["tmp_dirs"] += 1
            except Exception:
                pass

    return killed


def stop() -> dict:
    global _proc
    with _lock:
        proc = _proc
    if proc is None or proc.poll() is not None:
        return status()
    _kill_pg(proc)
    # 主动收尾：杀残留的 Xvfb/Camoufox + 清 /tmp profile
    cleanup_orphans()
    return status()


def submit_otp(value: str) -> dict:
    """Front-end calls this with the OTP user typed. Writes to fifo path."""
    global _otp_pending
    with _lock:
        if not _otp_pending or _otp_file is None:
            raise RuntimeError("no OTP currently requested")
        path = _otp_file
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
