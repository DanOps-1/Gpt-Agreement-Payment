"""WhatsApp Web sidecar lifecycle + OTP state reader.

The WebUI exposes one user-facing "WhatsApp 登录" entry. Behind it, this module
manages a single Node sidecar (`webui/whatsapp_relay/index.js`) that logs in to
WhatsApp Web, watches incoming messages, extracts GoPay OTPs, and writes all
application state/OTP data into SQLite (`runtime_meta`). The only remaining
filesystem state is the WhatsApp client auth/session cache required by the
upstream WhatsApp libraries and the plain process log.
"""
from __future__ import annotations

import base64
import datetime as _dt
import io
import glob
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import tarfile
from pathlib import Path
from typing import Any, Optional

from . import settings as s
from .db import get_db


_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_mode: str = ""
_engine: str = ""
_started_at: Optional[float] = None

_STATE_KEY = "wa_state"
_SETTINGS_KEY = "wa_settings"
_TOKEN_KEY = "wa_relay_token"
_SESSION_SNAPSHOT_KEY = "wa_session_snapshot"
_NOTIFY_JSONL_CURSOR_KEY = "wa_notify_jsonl_cursor"
_DEFAULT_NOTIFY_JSONL = "/root/notify-relay-bridge/data/notifications.jsonl"
_NOTIFY_INITIAL_SCAN_BYTES = 4 * 1024 * 1024
_OTP_REGEX = r"(?<!\d)(\d{6})(?!\d)"
_notify_poll_lock = threading.Lock()


def _normalize_engine(engine: str = "", *, _from_env: bool = False) -> str:
    raw = (engine or "").strip().lower().replace("_", "-")
    if raw in ("", "default"):
        if _from_env:
            return "baileys"
        return _normalize_engine(os.environ.get("WEBUI_WA_ENGINE", "baileys"), _from_env=True)
    if raw in ("baileys",):
        return "baileys"
    if raw in ("wwebjs", "whatsapp-web.js", "whatsapp-web-js", "whatsappwebjs"):
        return "wwebjs"
    if _from_env:
        return "baileys"
    raise ValueError(f"engine must be baileys or wwebjs, got {engine!r}")


def _data_dir() -> Path:
    d = s.get_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_dir(*, create: bool = True) -> Path:
    p = _data_dir() / "wa_session"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def _session_snapshot_path() -> Path:
    return _session_dir(create=False)


def _safe_extract_tar(tar: tarfile.TarFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if dest not in target.parents and target != dest:
            raise RuntimeError(f"unsafe session snapshot entry: {member.name}")
    try:
        tar.extractall(dest, filter="data")
    except TypeError:
        tar.extractall(dest)


def _read_session_snapshot() -> dict:
    data = get_db().get_runtime_json(_SESSION_SNAPSHOT_KEY, {})
    return data if isinstance(data, dict) else {}


def _write_session_snapshot(payload: dict) -> None:
    if payload:
        get_db().set_runtime_json(_SESSION_SNAPSHOT_KEY, payload)
    else:
        get_db().delete_runtime_key(_SESSION_SNAPSHOT_KEY)


def _snapshot_session_dir() -> None:
    session_dir = _session_snapshot_path()
    if not session_dir.exists():
        _write_session_snapshot({})
        return
    entries = [p for p in session_dir.rglob("*") if p.is_file() and not p.is_symlink()]
    if not entries:
        _write_session_snapshot({})
        return
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tar:
        for p in entries:
            tar.add(p, arcname=str(p.relative_to(session_dir)))
    _write_session_snapshot({
        "format": "tar.gz+base64",
        "engine": _read_preferred_engine(),
        "updated_at": time.time(),
        "data": base64.b64encode(bio.getvalue()).decode("ascii"),
    })


def _restore_session_snapshot() -> bool:
    payload = _read_session_snapshot()
    data = str(payload.get("data") or "")
    if not data:
        return False
    try:
        raw = base64.b64decode(data.encode("ascii"))
    except Exception:
        return False
    session_dir = _session_dir(create=True)
    try:
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
        session_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            _safe_extract_tar(tar, session_dir)
        return True
    except Exception:
        shutil.rmtree(session_dir, ignore_errors=True)
        session_dir.mkdir(parents=True, exist_ok=True)
        return False


def _persist_session_snapshot() -> None:
    _snapshot_session_dir()
    session_dir = _session_snapshot_path()
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


def _clear_session_snapshot() -> None:
    _write_session_snapshot({})
    session_dir = _session_snapshot_path()
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


def _read_state() -> dict:
    data = get_db().get_runtime_json(_STATE_KEY, {})
    return data if isinstance(data, dict) else {}


def _write_state(data: dict) -> None:
    get_db().set_runtime_json(_STATE_KEY, data if isinstance(data, dict) else {})


def _read_preferred_engine() -> str:
    data = get_db().get_runtime_json(_SETTINGS_KEY, {})
    if isinstance(data, dict):
        try:
            return _normalize_engine(str(data.get("engine") or ""))
        except Exception:
            pass
    return _normalize_engine(os.environ.get("WEBUI_WA_ENGINE", "baileys"), _from_env=True)


def _write_preferred_engine(engine: str) -> None:
    get_db().set_runtime_json(_SETTINGS_KEY, {"engine": _normalize_engine(engine)})


def _notify_jsonl_path() -> Path:
    raw = os.environ.get("WEBUI_WA_NOTIFY_JSONL_PATH", _DEFAULT_NOTIFY_JSONL).strip()
    return Path(raw or _DEFAULT_NOTIFY_JSONL).expanduser()


def _parse_ts_value(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        if 946684800 <= ts <= 4102444800:
            return ts
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}(?:\.\d+)?", text):
        return _parse_ts_value(float(text))
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _clean_otp_candidate(value: Any) -> str:
    code = re.sub(r"\D", "", str(value or ""))
    if 4 <= len(code) <= 8:
        return code
    return ""


def _extract_otp_from_text(text: str) -> str:
    if not text:
        return ""
    patterns = [
        r"(?:otp|one[-\s]*time|verification|verify|code|kode|verifikasi|gopay|whatsapp|验证码|驗證碼)[^\d]{0,80}(\d{4,8})(?!\d)",
        r"(?<!\d)(\d{4,8})(?!\d)[^\n\r]{0,80}(?:otp|one[-\s]*time|verification|verify|code|kode|verifikasi|gopay|验证码|驗證碼)",
        _OTP_REGEX,
    ]
    for pattern in patterns:
        for match in reversed(list(re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL))):
            groups = match.groups() or (match.group(0),)
            for group in reversed(groups):
                code = _clean_otp_candidate(group)
                if code:
                    return code
    return ""


def _payload_text(payload: dict) -> str:
    pieces: list[str] = []

    def add(value: Any) -> None:
        if value in (None, ""):
            return
        if isinstance(value, list):
            for item in value:
                add(item)
            return
        pieces.append(str(value))

    for key in ("text", "body", "message", "content", "caption", "raw"):
        value = payload.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, dict):
            nested = value.get("body") or value.get("text") or value.get("message")
            add(nested)
        else:
            add(value)
    notification = payload.get("notification")
    if isinstance(notification, dict):
        for key in ("title", "text", "big_text", "sub_text", "summary_text", "text_lines"):
            add(notification.get(key))
    return "\n".join(dict.fromkeys(piece for piece in pieces if piece))


def _payload_ts(payload: dict) -> float:
    for key in ("ts", "timestamp", "time", "received_ts", "received_at", "post_time", "date"):
        ts = _parse_ts_value(payload.get(key))
        if ts is not None:
            return ts
    return time.time()


def _append_otp_item(item: dict) -> dict:
    with _lock:
        state = _read_state()
        history = state.get("history") if isinstance(state.get("history"), list) else []
        history.append(item)
        state.update({
            "latest": item,
            "history": history[-50:],
            "updated_at": time.time(),
        })
        _write_state(state)
    return item


def ingest_otp(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    text = _payload_text(payload)
    code = (
        _clean_otp_candidate(payload.get("otp"))
        or _clean_otp_candidate(payload.get("code"))
        or _extract_otp_from_text(text)
    )
    if not code:
        raise ValueError("no OTP found")
    item = {
        "otp": code,
        "ts": _payload_ts(payload),
        "from": payload.get("from") or payload.get("sender") or payload.get("package") or "",
        "source": payload.get("source") or "external",
        "engine": payload.get("engine") or "external",
        "message_id": payload.get("message_id") or "",
        "text": text[:500],
    }
    return _append_otp_item(item)


def _notification_jsonl_text(row: dict) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    notification = payload.get("notification") if isinstance(payload.get("notification"), dict) else {}
    pieces: list[str] = []

    def add(value: Any) -> None:
        if value in (None, ""):
            return
        if isinstance(value, list):
            for item in value:
                add(item)
            return
        pieces.append(str(value))

    for value in (row.get("content"), payload.get("text"), payload.get("body"), payload.get("message")):
        add(value)
    for key in ("title", "text", "big_text", "sub_text", "summary_text", "text_lines"):
        add(notification.get(key))
    return "\n".join(dict.fromkeys(piece for piece in pieces if piece))


def _notification_jsonl_is_relevant(row: dict, text: str) -> bool:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    notification = payload.get("notification") if isinstance(payload.get("notification"), dict) else {}
    meta = "\n".join(
        str(value or "")
        for value in (
            row.get("source"),
            row.get("remark"),
            payload.get("source"),
            payload.get("app"),
            payload.get("from"),
            payload.get("package"),
            notification.get("title"),
            notification.get("channel_id"),
            notification.get("category"),
        )
    ).lower()
    if "whatsapp" in meta or "com.whatsapp" in meta:
        return True
    return bool(re.search(r"\b(go-?pay|gojek|kode|verifikasi|verification|verify|otp)\b|验证码|驗證碼", text, re.I))


def _notification_jsonl_to_otp_payload(row: dict, *, since: float = 0.0) -> Optional[dict]:
    if not isinstance(row, dict):
        return None
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    notification = payload.get("notification") if isinstance(payload.get("notification"), dict) else {}
    ts = (
        _parse_ts_value(row.get("received_ts"))
        or _parse_ts_value(row.get("received_at"))
        or _parse_ts_value(payload.get("ts"))
        or _parse_ts_value(payload.get("post_time"))
        or time.time()
    )
    if since and ts < since:
        return None

    text = _notification_jsonl_text(row)
    if not text or not _notification_jsonl_is_relevant(row, text):
        return None
    if not _extract_otp_from_text(text):
        return None

    source = str(row.get("source") or payload.get("source") or "android_notification")
    sender = (
        payload.get("from")
        or notification.get("title")
        or payload.get("app")
        or payload.get("package")
        or "android_notification"
    )
    message_id = (
        payload.get("message_id")
        or payload.get("notification_key")
        or row.get("received_ts")
        or row.get("received_at")
        or ""
    )
    return {
        "text": text,
        "ts": ts,
        "from": sender,
        "package": payload.get("package") or "",
        "source": f"{source}:jsonl",
        "engine": "android_notification_jsonl",
        "message_id": str(message_id),
    }


def poll_notification_jsonl(*, since: float = 0.0, path: Optional[Path] = None) -> dict:
    """Tail Android notification JSONL and persist fresh WhatsApp OTPs to SQLite."""
    notify_path = path or _notify_jsonl_path()
    result = {"path": str(notify_path), "exists": False, "read": 0, "ingested": 0}
    if not notify_path.exists():
        return result
    result["exists"] = True
    if not _notify_poll_lock.acquire(blocking=False):
        result["skipped"] = "busy"
        return result
    try:
        try:
            stat = notify_path.stat()
        except OSError as exc:
            result["error"] = str(exc)
            return result

        cursor = get_db().get_runtime_json(_NOTIFY_JSONL_CURSOR_KEY, {})
        if not isinstance(cursor, dict):
            cursor = {}
        inode = getattr(stat, "st_ino", 0)
        pos = 0
        partial = False
        if cursor.get("path") == str(notify_path) and int(cursor.get("inode") or 0) == inode:
            try:
                pos = int(cursor.get("pos") or 0)
            except Exception:
                pos = 0
            if pos < 0 or pos > stat.st_size:
                pos = max(0, stat.st_size - _NOTIFY_INITIAL_SCAN_BYTES)
                partial = pos > 0
        else:
            pos = max(0, stat.st_size - _NOTIFY_INITIAL_SCAN_BYTES)
            partial = pos > 0

        try:
            with notify_path.open("rb") as f:
                f.seek(pos)
                if partial:
                    f.readline()
                while True:
                    line_start = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        f.seek(line_start)
                        break
                    raw = line.decode("utf-8", errors="replace").strip()
                    if not raw:
                        continue
                    result["read"] += 1
                    try:
                        row = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    otp_payload = _notification_jsonl_to_otp_payload(row, since=since)
                    if not otp_payload:
                        continue
                    try:
                        ingest_otp(otp_payload)
                    except ValueError:
                        continue
                    result["ingested"] += 1
                new_pos = f.tell()
        except OSError as exc:
            result["error"] = str(exc)
            return result

        get_db().set_runtime_json(_NOTIFY_JSONL_CURSOR_KEY, {
            "path": str(notify_path),
            "inode": inode,
            "pos": new_pos,
            "size": stat.st_size,
            "updated_at": time.time(),
        })
        result["pos"] = new_pos
        return result
    finally:
        _notify_poll_lock.release()


def set_preferred_engine(engine: str) -> dict:
    """Persist the preferred WhatsApp engine in SQLite.

    This replaces the old browser-local preference (`localStorage`) so the
    engine selector is consistent across browsers / nginx sessions and remains
    part of the server-side runtime database.
    """
    normalized = _normalize_engine(engine)
    _write_preferred_engine(normalized)
    st = status()
    st["preferred_engine"] = normalized
    return st


def relay_token() -> str:
    token = get_db().get_runtime_value(_TOKEN_KEY, "")
    if not token:
        token = secrets.token_urlsafe(32)
        get_db().set_runtime_value(_TOKEN_KEY, token)
    return token


def otp_url() -> str:
    base = os.environ.get("WEBUI_INTERNAL_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
    return f"{base}/api/whatsapp/latest-otp?token={relay_token()}"


def is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def status() -> dict:
    """Read state from SQLite.

    When the sidecar is not running, force status to `stopped` so callers do
    not treat stale `connected` state as live.
    """
    running = is_running()
    preferred_engine = _read_preferred_engine()
    base = {
        "running": running,
        "pid": _proc.pid if running and _proc else None,
        "mode": _mode,
        "engine": _engine if running and _engine else preferred_engine,
        "preferred_engine": preferred_engine,
        "started_at": _started_at,
        "state_store": "sqlite",
        "database": str(get_db().path),
        "otp_source": "sqlite_http",
        "otp_url_configured": bool(get_db().get_runtime_value(_TOKEN_KEY, "")),
        "session_store": "sqlite_snapshot",
        "session_snapshot_configured": get_db().has_runtime_key(_SESSION_SNAPSHOT_KEY),
        "session_dir": str(_session_dir(create=False)),
    }
    loaded = _read_state()
    if loaded:
        base.update(loaded)

    if not running:
        base["status"] = "stopped"
        base["engine"] = preferred_engine
        for key in ("qr", "qr_data_url", "qr_ascii", "code"):
            base.pop(key, None)
    elif "status" not in base:
        base["status"] = "starting"
    return base


def apply_sidecar_state(payload: dict) -> dict:
    if not isinstance(payload, dict):
        payload = {}
    state = _read_state()
    state.update(payload)
    state["updated_at"] = time.time()
    _write_state(state)
    return state


def submit_manual_otp(value: str) -> dict:
    code = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not code:
        raise ValueError("OTP 为空")
    item = {
        "otp": code,
        "ts": time.time(),
        "from": "webui_manual",
        "source": "manual_webui",
        "engine": _engine or _read_preferred_engine(),
        "text": "",
    }
    state = _read_state()
    history = state.get("history") if isinstance(state.get("history"), list) else []
    history.append(item)
    state.update({
        "status": "connected" if is_running() else state.get("status", "manual"),
        "latest": item,
        "history": history[-50:],
        "updated_at": time.time(),
    })
    _write_state(state)
    return item


def latest_otp(since: float = 0.0) -> dict | None:
    poll_notification_jsonl(since=since)
    latest = (_read_state().get("latest") or {})
    if not isinstance(latest, dict) or not latest.get("otp"):
        return None
    try:
        ts = float(latest.get("ts") or 0.0)
    except Exception:
        ts = 0.0
    if since and ts < since:
        return None
    return latest


def start(mode: str = "qr", pairing_phone: str = "", engine: str = "") -> dict:
    """Spawn the Node sidecar in QR mode.

    `pairing` mode is kept at the API level for compatibility, but the WebUI now
    exposes only the QR WhatsApp login entry.
    """
    global _proc, _mode, _engine, _started_at

    mode = (mode or "qr").lower()
    engine = _normalize_engine(engine or _read_preferred_engine())
    if mode not in ("qr", "pairing"):
        raise ValueError(f"mode must be qr or pairing, got {mode!r}")
    if mode == "pairing":
        digits = "".join(ch for ch in (pairing_phone or "") if ch.isdigit())
        if len(digits) < 10:
            raise ValueError("pairing 模式需要 pairing_phone（含国家码，10+ 位数字）")
        pairing_phone = digits

    with _lock:
        _write_preferred_engine(engine)

        if is_running() and _mode == mode and _engine == engine:
            return status()

        _stop_locked()
        session_dir = _session_dir(create=True)
        _purge_stale_chrome(session_dir)
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
        session_dir.mkdir(parents=True, exist_ok=True)
        _restore_session_snapshot()

        relay_dir = s.WA_RELAY_DIR
        index_js = relay_dir / "index.js"
        if not index_js.exists():
            raise RuntimeError(f"relay sidecar 缺失: {index_js}")
        node_modules = relay_dir / "node_modules"
        if not node_modules.exists():
            raise RuntimeError(f"未安装 sidecar 依赖；先跑 `cd {relay_dir} && npm install`")

        get_db().delete_runtime_key(_STATE_KEY)

        token = relay_token()
        internal_base = os.environ.get("WEBUI_INTERNAL_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
        env = {
            **os.environ,
            # Baileys listens on the raw WhatsApp multi-device socket and is
            # more suitable for OTP capture than DOM scraping via Chromium.
            # The WebUI can switch this per start request; WEBUI_WA_ENGINE only
            # acts as the initial default.
            "WA_ENGINE": engine,
            "WA_LOGIN_MODE": mode,
            "WA_STATE_URL": f"{internal_base}/api/whatsapp/sidecar/state",
            "WA_RELAY_TOKEN": token,
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
        _engine = engine
        _started_at = time.time()

        # Give the sidecar a short window to fail fast (missing browser, bad
        # dependency, etc.) so the UI gets a useful error instead of spinning.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if proc.poll() is not None:
                _proc = None
                _mode = ""
                _engine = ""
                _started_at = None
                detail = ""
                try:
                    detail = log_path.read_text(encoding="utf-8", errors="replace")[-1200:]
                except Exception:
                    pass
                raise RuntimeError(f"WhatsApp relay 启动后退出: rc={proc.returncode} {detail}")
            if _read_state():
                break
            time.sleep(0.1)
        return status()


def _purge_stale_chrome(session_dir: Path) -> None:
    """Kill orphan Chromium processes using our WhatsApp session dir."""
    pat = str(session_dir.resolve())
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"chrome.*user-data-dir={pat}"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        out = ""
    for line in out.splitlines():
        try:
            os.kill(int(line.strip()), signal.SIGKILL)
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        for p in session_dir.rglob(name):
            try:
                p.unlink()
            except (FileNotFoundError, IsADirectoryError):
                pass

    for d in glob.glob("/tmp/org.chromium.Chromium.*"):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def _stop_locked() -> None:
    global _proc, _mode, _engine, _started_at

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
    _engine = ""
    _started_at = None
    _persist_session_snapshot()


def stop() -> dict:
    with _lock:
        _stop_locked()
    return status()


def logout() -> dict:
    """Stop sidecar and remove WhatsApp session so the next start shows QR."""
    with _lock:
        _stop_locked()
        sd = _session_dir(create=False)
        _purge_stale_chrome(sd)
        _clear_session_snapshot()
        get_db().delete_runtime_key(_STATE_KEY)
    return {"status": "logged_out"}
