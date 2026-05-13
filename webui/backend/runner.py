"""鍗?active-run 鐨?pipeline 杩涚▼鎺у埗鍣ㄣ€?

灏佽 `xvfb-run -a python pipeline.py [args]` 瀛愯繘绋嬶細spawn / 娴佸紡鏀?stdout
鍒扮幆褰㈡棩蹇楃紦鍐?/ SIGTERM-浼樺厛 stop / 鏆撮湶 status + log 缁欒矾鐢卞眰銆?

GoPay 妯″紡涓嬮澶栨敮鎸?OTP 涓浆锛歐ebUI Run 界面使用本地
file provider 等待 OTP；手机 webhook POST 到 `/api/whatsapp/external-otp`
后，runner 将验证码写入 gopay.py 正在等待的文件。
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import settings as s
from . import wa_relay
from . import gopay_auto_unbind


_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_started_at: Optional[float] = None
_ended_at: Optional[float] = None
_exit_code: Optional[int] = None
_cmd: Optional[list[str]] = None
_mode: Optional[str] = None
_run_id = 0
_log_lines: list[dict] = []  # {seq, ts, line}
_seq_counter = 0
_MAX_LOG_LINES = 1500
_otp_file: Optional[Path] = None       # file provider path, if used
_otp_to_db: bool = False               # legacy DB relay mode; Run uses file mode
_otp_pending: bool = False             # set when gopay.py asks/waits for OTP
_otp_pending_since: Optional[float] = None
_otp_pending_phone: str = ""
_otp_pending_country_code: str = ""
_otp_file_is_temp: bool = False
_OTP_RELAY_POLL_INTERVAL_SECONDS = 0.5
_OTP_RELAY_POLL_TIMEOUT_SECONDS = 300.0
_OTP_RELAY_LOOKBACK_SECONDS = 15.0
# card.py runs GoPay auto-unbind with the exact selected account config.  The
# runner only sees shared stdout, so doing it here would use the global config
# and can unlink the wrong wallet when multiple GoPay accounts are configured.
_RUN_GOPAY_AUTO_UNBIND_ENABLED = False


def _log_channel(line: str) -> str:
    text = str(line or "").lower()
    if any(
        marker in text
        for marker in (
            "[gopay",
            "[sms]",
            "gopay_otp_target",
            "gopay_result_json",
            "gopay_qr_",
        )
    ):
        return "gopay"
    return "account"


def _append_log(line: str) -> None:
    global _seq_counter, _log_lines
    with _lock:
        _seq_counter += 1
        _log_lines.append({"seq": _seq_counter, "ts": time.time(), "line": line, "channel": _log_channel(line)})
        if len(_log_lines) > _MAX_LOG_LINES:
            _log_lines = _log_lines[-_MAX_LOG_LINES:]


def _gopay_auto_otp_enabled() -> bool:
    """Return True when config has a non-manual gopay.otp provider.

    Legacy helper kept for old tests/tools. Current WebUI Run uses the
    file-backed provider and feeds it from the external OTP webhook.
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
              gopay_otp_file: str = "", count: int = 0,
              backfill_rt_ids: Optional[list[int]] = None,
              push_server: bool = False) -> list[str]:
    """根据参数拼出最终命令行。"""
    if register_only and pay_only:
        raise RuntimeError("--register-only and --pay-only cannot be used together")
    cmd = ["xvfb-run", "-a", sys.executable, "-u", "pipeline.py",
           "--config", str(s.PAY_CONFIG_PATH)]
    # free_only 涓や釜瀛愭ā寮忎笉闇€瑕?paypal / gopay 鏀粯娈?
    if mode == "free_register":
        cmd.append("--free-register")
        if count > 0:
            cmd.extend(["--count", str(count)])
        return cmd
    if mode == "backfill_rt":
        cmd.append("--free-backfill-rt")
        ids = [str(int(x)) for x in (backfill_rt_ids or [])]
        if ids:
            cmd.extend(["--free-backfill-rt-ids", ",".join(ids)])
        return cmd
    if gopay:
        cmd.append("--gopay")
        if gopay_otp_file:
            cmd.extend(["--gopay-otp-file", gopay_otp_file])
    elif paypal:
        cmd.append("--paypal")
    # mode 鍐冲畾寰幆缁撴瀯锛坉aemon 鈭?/ self_dealer / batch N / 鍗曟锛?
    if mode == "daemon":
        cmd.append("--daemon")
    elif mode == "self_dealer":
        cmd.extend(["--self-dealer", str(self_dealer)])
    elif mode == "singlexn":
        cmd.extend(["--singlexn", str(count)])
    elif mode == "batch":
        cmd.extend(["--batch", str(batch), "--workers", str(workers)])
    # mode == "single" 鈫?no extra flags
    # register_only / pay_only 鏄?modifier锛岃窡 mode 姝ｄ氦锛坆atch + register-only
    # = 鎵归噺娉ㄥ唽 N 涓紱single + register-only = 鍗曟娉ㄥ唽锛?
    if register_only:
        cmd.append("--register-only")
    elif pay_only:
        cmd.append("--pay-only")
    if push_server:
        cmd.append("--push-server")
    return cmd


def status() -> dict:
    global _proc
    is_running = _proc is not None and _proc.poll() is None
    return {
        "running": is_running,
        "run_id": _run_id,
        "started_at": _started_at,
        "ended_at": _ended_at,
        "exit_code": _exit_code if not is_running else None,
        "cmd": _cmd,
        "mode": _mode,
        "pid": _proc.pid if is_running and _proc else None,
        "log_count": _seq_counter,
        "otp_pending": _otp_pending,
        "otp_pending_since": _otp_pending_since,
        "otp_pending_phone": _otp_pending_phone,
        "otp_pending_country_code": _otp_pending_country_code,
    }


def start(*, mode: str, paypal: bool = True, batch: int = 0, workers: int = 3,
          self_dealer: int = 0, register_only: bool = False, pay_only: bool = False,
          gopay: bool = False, count: int = 0, push_server: bool = False) -> dict:
    gopay_otp_file = ""
    if gopay:
        otp_dir = s.get_data_dir() / "gopay_otp"
        otp_dir.mkdir(parents=True, exist_ok=True)
        gopay_otp_file = str(otp_dir / f"run_{int(time.time() * 1000)}.txt")
    cmd = build_cmd(mode, paypal, batch, workers, self_dealer,
                    register_only, pay_only, gopay=gopay,
                    gopay_otp_file=gopay_otp_file, count=count,
                    push_server=push_server)
    return _start_cmd(cmd, mode, gopay=gopay, gopay_otp_file=gopay_otp_file)



def _start_cmd(cmd: list[str], mode: str, *, gopay: bool = False, gopay_otp_file: str = "") -> dict:
    global _proc, _started_at, _ended_at, _exit_code, _cmd, _mode, _run_id
    global _log_lines, _seq_counter, _otp_file, _otp_to_db, _otp_pending, _otp_pending_since, _otp_pending_phone, _otp_pending_country_code, _otp_file_is_temp
    with _lock:
        if _proc is not None and _proc.poll() is None:
            raise RuntimeError("a pipeline is already running")

        otp_p = Path(gopay_otp_file) if gopay_otp_file else None

        # Reset.  Bump run_id before spawning so any late drain thread from a
        # previous process cannot write into this run's fresh log buffer.
        _run_id += 1
        run_id = _run_id
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
        _otp_pending_since = None
        _otp_pending_phone = ""
        _otp_pending_country_code = ""

        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        env["WEBUI_DATA_DIR"] = str(s.get_data_dir())
        env["WEBUI_DB_PATH"] = str(s.get_data_dir() / "webui.db")
        env["WEBUI_DATABASE_PATH"] = str(s.get_data_dir() / "webui.db")
        try:
            popen_kwargs = {
                "cwd": str(s.ROOT),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
                "env": env,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                # Batch runs spawn child scripts/browsers.  Put the whole tree
                # in its own session so stop() can terminate the process group.
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(
                cmd,
                **popen_kwargs,
            )
        except FileNotFoundError as e:
            _ended_at = time.time()
            _exit_code = -1
            raise RuntimeError(f"failed to spawn: {e}") from e
        _proc = proc

        threading.Thread(target=_drain, args=(proc, run_id), daemon=True).start()
    return status()


def start_backfill_rt(ids: list[int]) -> dict:
    clean = [int(x) for x in ids if str(x).strip().lstrip("-").isdigit()]
    if not clean:
        raise RuntimeError("no account ids selected")
    cmd = build_cmd("backfill_rt", False, 0, 1, 0, False, False,
                    backfill_rt_ids=clean)
    return _start_cmd(cmd, "backfill_rt")


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

    # Legacy DB-backed provider, kept for old command-line runs.
    if re.search(r"\[gopay\]\s+waiting WhatsApp OTP from relay:", line):
        return "db", None
    return "", None


def _detect_gopay_otp_target(line: str) -> tuple[str, str]:
    if "GOPAY_OTP_TARGET" not in line:
        return "", ""
    phone_match = re.search(r"\bphone=([+\d][+\d\-\s().]*)", line)
    cc_match = re.search(r"\bcountry_code=([+\d]+)", line)
    phone = "".join(ch for ch in (phone_match.group(1) if phone_match else "") if ch.isdigit())
    country_code = "".join(ch for ch in (cc_match.group(1) if cc_match else "") if ch.isdigit())
    return phone, country_code


def _otp_from_item(item: dict | None) -> str:
    return "".join(ch for ch in str((item or {}).get("otp") or "") if ch.isdigit())


def _feed_pending_otp_item(item: dict | None, *, via_poll: bool = False) -> bool:
    """Write a stored webhook OTP into the active GoPay OTP wait target."""
    global _otp_pending, _otp_pending_since, _otp_pending_phone, _otp_pending_country_code
    otp = _otp_from_item(item)
    if not otp:
        return False
    with _lock:
        if not _otp_pending:
            return False
        if _otp_pending_phone and item and not wa_relay._phone_matches(
            item,
            _otp_pending_phone,
            _otp_pending_country_code,
        ):
            return False
        run_id = _run_id
        path = _otp_file
        use_db = _otp_to_db
        phone = _otp_pending_phone
        country_code = _otp_pending_country_code

    if use_db:
        wa_relay.submit_manual_otp(otp, phone=phone, country_code=country_code)
    else:
        if path is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(otp, encoding="utf-8")

    should_log = False
    with _lock:
        if run_id == _run_id and _otp_pending:
            _otp_pending = False
            _otp_pending_since = None
            _otp_pending_phone = ""
            _otp_pending_country_code = ""
            should_log = True
    if should_log:
        source = str((item or {}).get("source") or "external")
        suffix = " via local poll" if via_poll else ""
        _append_log(f"[webui] external OTP from {source} forwarded to GoPay{suffix}")
    return True


def _poll_pending_otp_from_relay(run_id: int) -> None:
    """Poll local SQLite OTP history while GoPay is waiting on a file provider."""
    deadline = time.time() + _OTP_RELAY_POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        with _lock:
            if run_id != _run_id or not _otp_pending or _otp_to_db or _otp_file is None:
                return
            since = max(0.0, (_otp_pending_since or time.time()) - _OTP_RELAY_LOOKBACK_SECONDS)
            phone = _otp_pending_phone
            country_code = _otp_pending_country_code
        try:
            item = wa_relay.latest_otp(since=since, phone=phone, country_code=country_code)
        except Exception:
            item = None
        if item and _feed_pending_otp_item(item, via_poll=True):
            return
        time.sleep(_OTP_RELAY_POLL_INTERVAL_SECONDS)


def _is_pay_success_line(line: str) -> bool:
    return bool(re.search(r"\[pay(?::[^\]]+)?\].*结果:\s*state=succeeded\b", line))


def _run_gopay_auto_unbind() -> None:
    try:
        result = gopay_auto_unbind.run_from_config(s.PAY_CONFIG_PATH, log=_append_log)
        if result.get("skipped"):
            return
        if result.get("ok"):
            _append_log(
                "[webui] GoPay auto-unbind succeeded "
                f"status={result.get('unlink_status_code')} url={result.get('unlink_url')}"
            )
        else:
            _append_log(
                "[webui] GoPay auto-unbind failed "
                f"reason={result.get('reason', 'unknown')} "
                f"linkedapps_status={result.get('linkedapps_status_code')} "
                f"unlink_status={result.get('unlink_status_code')}"
            )
    except Exception as e:
        _append_log(f"[webui] GoPay auto-unbind error: {e}")


def _drain(proc: subprocess.Popen, run_id: int) -> None:
    global _ended_at, _exit_code, _seq_counter, _log_lines, _otp_pending, _otp_pending_since, _otp_pending_phone, _otp_pending_country_code, _otp_file, _otp_to_db, _otp_file_is_temp
    try:
        if proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip()
            if not line:
                continue
            # Automatic GoPay unlink now re-signs x-e1 per request, so the
            # saved linkedapps/PATCH headers from the wizard can be reused as a
            # baseline after payment succeeds.
            should_auto_unbind = _RUN_GOPAY_AUTO_UNBIND_ENABLED and _is_pay_success_line(line)
            should_start_otp_poll = False
            with _lock:
                if run_id != _run_id or _proc is not proc:
                    continue
                _seq_counter += 1
                _log_lines.append({"seq": _seq_counter, "ts": time.time(), "line": line, "channel": _log_channel(line)})
                if len(_log_lines) > _MAX_LOG_LINES:
                    _log_lines = _log_lines[-_MAX_LOG_LINES:]
                target_phone, target_country_code = _detect_gopay_otp_target(line)
                if target_phone:
                    _otp_pending_phone = target_phone
                    _otp_pending_country_code = target_country_code
                # Detect GoPay OTP request/wait markers.  The second form is
                # used by the configured WhatsApp relay provider; making it
                # pending lets the existing WebUI OTP modal act as a fallback
                # when WhatsApp hides OTP bodies from linked devices.
                wait_kind, wait_path = _detect_otp_wait_target(line)
                if wait_kind:
                    was_pending = _otp_pending
                    _otp_to_db = wait_kind == "db"
                    _otp_file = wait_path
                    _otp_file_is_temp = _otp_file_is_temp or "GOPAY_OTP_REQUEST" in line
                    if not _otp_pending:
                        _otp_pending_since = time.time()
                    _otp_pending = True
                    should_start_otp_poll = wait_kind == "file" and not was_pending
            if should_auto_unbind:
                threading.Thread(target=_run_gopay_auto_unbind, daemon=True).start()
            if should_start_otp_poll:
                threading.Thread(target=_poll_pending_otp_from_relay, args=(run_id,), daemon=True).start()
    finally:
        proc.wait()
        with _lock:
            if run_id != _run_id or _proc is not proc:
                return
            _ended_at = time.time()
            _exit_code = proc.returncode
            _otp_pending = False
            _otp_pending_since = None
            _otp_pending_phone = ""
            _otp_pending_country_code = ""
            # Cleanup OTP file.  For the auto relay path this intentionally
            # removes stale OTPs too; future waits use mtime checks, but an
            # empty/clean file is easier to reason about.
            if _otp_file is not None:
                try:
                    _otp_file.unlink(missing_ok=True)
                except Exception:
                    pass


def _collect_descendant_pids(pid: int, seen: Optional[set[int]] = None) -> list[int]:
    if os.name == "nt":
        return []
    seen = seen or set()
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except Exception:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        try:
            child = int(line.strip())
        except ValueError:
            continue
        if child in seen:
            continue
        seen.add(child)
        pids.extend(_collect_descendant_pids(child, seen))
        pids.append(child)
    return pids


def _signal_descendants(pid: int, sig: int) -> None:
    for child in _collect_descendant_pids(pid):
        try:
            os.kill(child, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _terminate_process_tree(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        descendants = _collect_descendant_pids(proc.pid)
        try:
            pgid = os.getpgid(proc.pid)
            if pgid == os.getpgrp():
                raise RuntimeError("child is in backend process group")
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        for child in descendants:
            try:
                os.kill(child, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        descendants = _collect_descendant_pids(proc.pid)
        try:
            pgid = os.getpgid(proc.pid)
            if pgid == os.getpgrp():
                raise RuntimeError("child is in backend process group")
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for child in descendants:
            try:
                os.kill(child, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        pass


def stop() -> dict:
    global _proc, _ended_at, _exit_code, _otp_pending, _otp_pending_since, _otp_pending_phone, _otp_pending_country_code
    with _lock:
        proc = _proc
        run_id = _run_id
        if proc is None or proc.poll() is not None:
            return status()
    _append_log("[webui] stop requested: terminating process group")
    _terminate_process_tree(proc, timeout=5)
    with _lock:
        if run_id == _run_id and _proc is proc:
            _ended_at = time.time()
            _exit_code = proc.returncode if proc.poll() is not None else None
            _otp_pending = False
            _otp_pending_since = None
            _otp_pending_phone = ""
            _otp_pending_country_code = ""
    return status()


def submit_otp(value: str) -> dict:
    """Front-end calls this with the OTP user typed. Stores it in DB by default."""
    global _otp_pending, _otp_pending_since, _otp_pending_phone, _otp_pending_country_code
    with _lock:
        if not _otp_pending:
            raise RuntimeError("no OTP currently requested")
        path = _otp_file
        use_db = _otp_to_db
        phone = _otp_pending_phone
        country_code = _otp_pending_country_code
    if use_db:
        wa_relay.submit_manual_otp(value, phone=phone, country_code=country_code)
    else:
        if path is None:
            raise RuntimeError("no OTP file currently requested")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.strip(), encoding="utf-8")
    with _lock:
        _otp_pending = False
        _otp_pending_since = None
        _otp_pending_phone = ""
        _otp_pending_country_code = ""
    return status()


def notify_external_otp(item: dict | None = None) -> dict:
    """Feed a webhook OTP to the active GoPay wait, if one is pending."""
    _feed_pending_otp_item(item)
    return status()


def notify_otp_consumed(item: dict | None = None) -> dict:
    """Mark a legacy DB-backed OTP wait as resolved after GoPay reads it."""
    global _otp_pending, _otp_pending_since, _otp_pending_phone, _otp_pending_country_code
    global _seq_counter, _log_lines
    with _lock:
        if not (_otp_pending and _otp_to_db):
            return status()
        if _otp_pending_phone and item and not wa_relay._phone_matches(
            item,
            _otp_pending_phone,
            _otp_pending_country_code,
        ):
            return status()
        _otp_pending = False
        _otp_pending_since = None
        _otp_pending_phone = ""
        _otp_pending_country_code = ""
        _seq_counter += 1
        _log_lines.append({
            "seq": _seq_counter,
            "ts": time.time(),
            "line": "[webui] external OTP consumed by GoPay relay",
        })
        if len(_log_lines) > _MAX_LOG_LINES:
            _log_lines = _log_lines[-_MAX_LOG_LINES:]
    return status()


def get_lines_since(since_seq: int = 0, limit: int = 1000) -> list[dict]:
    with _lock:
        return [e for e in _log_lines if e["seq"] > since_seq][:limit]


def get_tail(n: int = 200) -> list[dict]:
    with _lock:
        return _log_lines[-n:]
