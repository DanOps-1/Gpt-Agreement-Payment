"""Concurrent execution of no_card_plus with multiple worker controller.

Design:
  - Users submit (phone, sms_url) lists + common parameters via routes/run_parallel.py,
    each pair generates an independent worker subprocess.
  - Each worker runs scripts/no_card_paypal_plus.py --worker-id w<i>,
    using their own phone/sms key respectively, isolating /tmp file paths via NCPP_WORKER_ID.
  - DB layer promo_links.status='in_use' atomic claim ensures workers don't contend for the same row.
  - Share the same IP/gost relay (current phase); can extend to independent IP rotation per worker later.

State machine:
  workers: dict[worker_id] = {
    proc, started_at, ended_at, exit_code,
    phone, sms_url, tag, log, current_event,
  }

Threading model:
  - Each worker has one stdout drainer thread, appending to worker.log (ring buffer 4000 lines).
  - One reaper thread checks dead children, setting exit_code + ended_at."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import settings as s


_lock = threading.Lock()
# worker_id -> state dict
_workers: dict[str, dict] = {}
# Single batch marker for convenient frontend distinction (e.g. generated when user clicks start).
_batch_started_at: Optional[float] = None
_batch_stopped_at: Optional[float] = None

# Phone OTP critical section mutex. PayPal sends multiple codes to the same number in short time causing garbling, must be serialized.
# Design: pre-OTP phase (all nav before form fill) is fully concurrent;
#       worker acquires(phone) before form submit (about to trigger SMS);
#       after OTP fill completes release(phone);
#       post-OTP (Hermes / Stripe return / ChatGPT landing) is concurrent again.
# Also set max-hold TTL to prevent worker crashes from leaking locks.
_phone_locks: dict[str, dict] = {}  # phone -> {worker_id, acquired_at}
_phone_locks_mu = threading.Lock()
_PHONE_LOCK_MAX_HOLD_S = 180.0


def _expire_stale_phone_lock(phone: str) -> None:
    """Force release beyond TTL to prevent worker crash leaks. Caller must already hold _phone_locks_mu."""
    holder = _phone_locks.get(phone)
    if holder and (time.time() - float(holder.get("acquired_at", 0)) > _PHONE_LOCK_MAX_HOLD_S):
        del _phone_locks[phone]


def acquire_phone_lock(phone: str, worker_id: str) -> dict:
    """Non-blocking try-acquire. Returns {'ok': True} if acquired, {'ok': False, 'holder': ...} if occupied.
    Repeated acquire by same worker is idempotent (returns ok)."""
    phone = (phone or "").strip()
    worker_id = (worker_id or "").strip()
    if not phone or not worker_id:
        return {"ok": False, "error": "phone/worker_id required"}
    with _phone_locks_mu:
        _expire_stale_phone_lock(phone)
        holder = _phone_locks.get(phone)
        if holder is None or holder.get("worker_id") == worker_id:
            _phone_locks[phone] = {"worker_id": worker_id, "acquired_at": time.time()}
            return {"ok": True, "phone": phone, "worker_id": worker_id}
        return {
            "ok": False,
            "phone": phone,
            "holder": holder.get("worker_id"),
            "holding_for_s": round(time.time() - float(holder.get("acquired_at", 0)), 1),
        }


def release_phone_lock(phone: str, worker_id: str) -> dict:
    phone = (phone or "").strip()
    worker_id = (worker_id or "").strip()
    if not phone:
        return {"ok": False, "error": "phone required"}
    with _phone_locks_mu:
        holder = _phone_locks.get(phone)
        if not holder:
            return {"ok": True, "released": False, "reason": "not_held"}
        if worker_id and holder.get("worker_id") != worker_id:
            return {"ok": False, "error": "not_held_by_you", "holder": holder.get("worker_id")}
        del _phone_locks[phone]
        return {"ok": True, "released": True}


def list_phone_locks() -> list[dict]:
    now = time.time()
    with _phone_locks_mu:
        return [
            {
                "phone": p,
                "worker_id": h.get("worker_id"),
                "acquired_at": h.get("acquired_at"),
                "held_for_s": round(now - float(h.get("acquired_at", 0)), 1),
            }
            for p, h in _phone_locks.items()
        ]

# Ring buffer size for each worker log.
_LOG_LIMIT = 4000

# Some stdout key fields → real-time sync to current_event, letting frontend/status see progress at a glance.
_EVENT_PATTERNS = [
    re.compile(r"^\[target\]"),
    re.compile(r"promo_link_id:\s*(\d+)"),
    re.compile(r"email:\s*(\S+)"),
    re.compile(r"\[node-rpa-full\]\s+(.+)"),
    re.compile(r"\[auto-gen\]\s+(.+)"),
    re.compile(r"\[claim\]\s+(.+)"),
    re.compile(r"\[release\]\s+(.+)"),
    re.compile(r"\[rotate\]\s+(.+)"),
    re.compile(r"\[result\]"),
    re.compile(r'"state":\s*"(\w+)"'),
    re.compile(r"OTP modal detected"),
    re.compile(r"OTP fill ok"),
    re.compile(r"paypal_datadome_blocked"),
    re.compile(r"CC_LINKED_TO_FULL_ACCOUNT"),
    re.compile(r"CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR"),
    re.compile(r"INSTRUMENT_SHARING_LIMIT_EXCEEDED"),
    re.compile(r"success url reached"),
    re.compile(r"plan=plus"),
]


def _sanitize_worker_id(raw: str) -> str:
    wid = re.sub(r"[^A-Za-z0-9_\-]", "_", (raw or "").strip())[:32]
    return wid or "w0"


def _alloc_worker_id(idx: int) -> str:
    base = f"w{idx + 1}"
    if base not in _workers:
        return base
    # Edge case name collision, append pid suffix.
    return f"w{idx + 1}_{os.getpid()}"


def list_workers() -> list[dict]:
    """Return snapshots of all workers (excluding complete logs, only tail + metadata)."""
    out: list[dict] = []
    with _lock:
        for wid, w in _workers.items():
            proc = w.get("proc")
            running = proc is not None and proc.poll() is None
            out.append({
                "worker_id": wid,
                "tag": w.get("tag", ""),
                "phone": w.get("phone", ""),
                "sms_url_redacted": _redact_sms_url(w.get("sms_url", "")),
                "started_at": w.get("started_at"),
                "ended_at": w.get("ended_at"),
                "exit_code": w.get("exit_code"),
                "running": running,
                "current_event": w.get("current_event", ""),
                "log_tail": list(w.get("log", []))[-50:],
                "log_size": len(w.get("log", [])),
            })
    out.sort(key=lambda x: x["worker_id"])
    return out


def get_worker_log(worker_id: str, since_seq: int = 0) -> dict:
    with _lock:
        w = _workers.get(worker_id)
        if not w:
            return {"worker_id": worker_id, "lines": [], "next_seq": since_seq}
        log = w.get("log") or []
        # Log element is (seq, line) tuple.
        out_lines: list[dict] = []
        next_seq = since_seq
        for seq, line in log:
            if seq <= since_seq:
                continue
            out_lines.append({"seq": seq, "line": line})
            next_seq = seq
        return {
            "worker_id": worker_id,
            "lines": out_lines,
            "next_seq": next_seq,
            "current_event": w.get("current_event", ""),
            "running": (w.get("proc") is not None and w["proc"].poll() is None),
            "exit_code": w.get("exit_code"),
        }


def batch_summary() -> dict:
    workers = list_workers()
    running = sum(1 for w in workers if w["running"])
    finished = sum(1 for w in workers if not w["running"] and w["started_at"])
    succeeded = sum(1 for w in workers if w["exit_code"] == 0)
    failed = sum(1 for w in workers if w["exit_code"] not in (None, 0))
    return {
        "batch_started_at": _batch_started_at,
        "batch_stopped_at": _batch_stopped_at,
        "total_workers": len(workers),
        "running": running,
        "finished": finished,
        "succeeded": succeeded,
        "failed": failed,
        "workers": workers,
    }


def _redact_sms_url(url: str) -> str:
    """Sanitize SMS API URL: only show host + path, key/token replaced with ***."""
    if not url:
        return ""
    s_url = url
    # Replace key=xxx or token=xxx.
    s_url = re.sub(r"(key|token|api_key|apikey)=[^&]+", r"\1=***", s_url, flags=re.I)
    return s_url[:120]


def _spawn_worker(
    worker_id: str,
    phone: str,
    sms_url: str,
    tag: str,
    common_args: list[str],
    common_env: dict[str, str],
) -> dict:
    """Spawn a worker subprocess; return worker state dict."""
    script = s.ROOT / "scripts" / "no_card_paypal_plus.py"
    cmd: list[str] = []
    xvfb = shutil.which("xvfb-run")
    if xvfb:
        cmd += [xvfb, "-a"]
    cmd += [
        "python", "-u", str(script),
        "--worker-id", worker_id,
        "--phone", phone,
        *common_args,
    ]
    env = os.environ.copy()
    env.update(common_env)
    env["NCPP_WORKER_ID"] = worker_id
    if sms_url:
        # Consistent with existing single-run, use env (avoid falling into ps cmdline).
        env["PPS_SMS_API_URL"] = sms_url
        env["PAYPAL_SMS_API_URL"] = sms_url
    # Phone-lock coordination address: Node RPA acquires before form submit, releases after OTP fill.
    # Same as webui in same container, use local loopback. If empty, Node side fallback to lock-free (single worker behavior).
    # Route prefix is /api/run/parallel (see routes/run_parallel.py), not /api/parallel.
    env.setdefault("NCPP_PHONE_LOCK_URL", "http://127.0.0.1:8765/api/run/parallel/phone-lock")

    log: list[tuple[int, str]] = []
    seq_box = [0]

    state: dict = {
        "worker_id": worker_id,
        "tag": tag,
        "phone": phone,
        "sms_url": sms_url,
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
        "current_event": "starting",
        "log": log,
        "proc": None,
        "cmd": list(cmd),
    }

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        cwd=str(s.ROOT),
    )
    state["proc"] = proc

    def _drain() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                with _lock:
                    seq_box[0] += 1
                    log.append((seq_box[0], line))
                    if len(log) > _LOG_LIMIT:
                        del log[: len(log) - _LOG_LIMIT]
                    for pat in _EVENT_PATTERNS:
                        if pat.search(line):
                            state["current_event"] = line[:200]
                            break
        except Exception:
            pass
        # Process exits, record exit_code.
        try:
            rc = proc.wait()
        except Exception:
            rc = -1
        with _lock:
            state["exit_code"] = rc
            state["ended_at"] = time.time()
            state["current_event"] = (
                f"finished rc={rc}" if rc == 0 else f"finished rc={rc} (failed)"
            )

    t = threading.Thread(target=_drain, name=f"ncpp-drain-{worker_id}", daemon=True)
    t.start()
    state["thread"] = t
    return state


def start_workers(
    workers_payload: list[dict],
    common: dict | None = None,
) -> dict:
    """Start N workers. workers_payload each entry = {phone, sms_url, tag?}.
    common = common CLI parameter dict, can include:
      config (default = s.PAY_CONFIG_PATH)
      paypal_country, paypal_lang
      signup_retries, otp_timeout, node_rpa_timeout, max_due
      allow_already_paid, allow_full_price
      inventory_mail_source ('any' / 'outlook' / 'catch_all')
      promo_link_id (empty = each worker auto claim)"""
    global _batch_started_at, _batch_stopped_at

    if not workers_payload:
        raise ValueError("workers list empty")
    common = common or {}

    # Reject restart: if running workers exist, let user stop first.
    with _lock:
        for w in _workers.values():
            proc = w.get("proc")
            if proc is not None and proc.poll() is None:
                raise RuntimeError(
                    "parallel batch already running; call /parallel/stop first"
                )
        _workers.clear()
        _batch_started_at = time.time()
        _batch_stopped_at = None
    # Clear residual phone locks before new batch (leaked from previous batch crash).
    with _phone_locks_mu:
        _phone_locks.clear()

    # Construct common CLI args (worker-specific phone/sms_url already handled in _spawn).
    config_path = common.get("config") or str(s.PAY_CONFIG_PATH)
    common_args: list[str] = [
        "--config", str(config_path),
        "--paypal-node-rpa",
        "--paypal-node-rpa-timeout", str(int(common.get("node_rpa_timeout", 900))),
        "--paypal-signup-retries", str(int(common.get("signup_retries", 3))),
        "--paypal-country", str(common.get("paypal_country", "US")).upper(),
        "--paypal-lang", str(common.get("paypal_lang", "en")).lower(),
        "--otp-timeout", str(int(common.get("otp_timeout", 240))),
        "--max-due", str(int(common.get("max_due", 100))),
    ]
    if int(common.get("promo_link_id") or 0) > 0:
        common_args.extend(["--promo-link-id", str(int(common["promo_link_id"]))])
    if common.get("allow_already_paid"):
        common_args.append("--allow-already-paid")
    if common.get("allow_full_price"):
        common_args.append("--allow-full-price")
    src = str(common.get("inventory_mail_source", "any")).strip().lower()
    if src in ("outlook", "catch_all"):
        common_args.extend(["--inventory-mail-source", src])

    common_env: dict[str, str] = {}
    # Put sms_url default value in env as fallback (if worker has no individual config).
    default_sms_url = str(common.get("default_sms_url") or "").strip()
    if default_sms_url:
        common_env["PPS_SMS_API_URL"] = default_sms_url

    spawned = []
    for idx, w_cfg in enumerate(workers_payload):
        phone = str(w_cfg.get("phone") or "").strip()
        sms_url = str(w_cfg.get("sms_url") or "").strip() or default_sms_url
        tag = str(w_cfg.get("tag") or "").strip()
        if not phone:
            raise ValueError(f"worker[{idx}] phone 缺失")
        if not sms_url:
            raise ValueError(f"worker[{idx}] sms_url 缺失 (也没默认值)")
        wid_raw = w_cfg.get("worker_id") or ""
        worker_id = _sanitize_worker_id(wid_raw) if wid_raw else _alloc_worker_id(idx)
        # Stagger startup to avoid hitting shared resources like gost / chatgpt API in the same second.
        if idx > 0:
            time.sleep(float(common.get("stagger_s", 1.0)))
        try:
            state = _spawn_worker(
                worker_id=worker_id,
                phone=phone,
                sms_url=sms_url,
                tag=tag,
                common_args=common_args,
                common_env=common_env,
            )
        except Exception as e:
            return {
                "ok": False,
                "error": f"spawn worker {worker_id} 失败: {e}",
                "spawned": spawned,
            }
        with _lock:
            _workers[worker_id] = state
        spawned.append({"worker_id": worker_id, "pid": state["proc"].pid, "phone": phone})

    return {"ok": True, "spawned": spawned, "batch_started_at": _batch_started_at}


def stop_all(grace_s: float = 8.0) -> dict:
    """SIGTERM all running workers; SIGKILL stragglers after grace_s seconds."""
    global _batch_stopped_at

    killed: list[str] = []
    with _lock:
        items = list(_workers.items())

    for wid, w in items:
        proc = w.get("proc")
        if proc is None or proc.poll() is not None:
            continue
        try:
            proc.terminate()
            killed.append(wid)
        except Exception:
            pass

    if killed:
        deadline = time.time() + grace_s
        while time.time() < deadline:
            still = []
            for wid, w in items:
                proc = w.get("proc")
                if proc is not None and proc.poll() is None:
                    still.append(wid)
            if not still:
                break
            time.sleep(0.5)
        # SIGKILL stragglers.
        for wid, w in items:
            proc = w.get("proc")
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    with _lock:
        _batch_stopped_at = time.time()
    # Clear phone locks together on stop to prevent leaks.
    with _phone_locks_mu:
        _phone_locks.clear()

    return {"ok": True, "stopped": killed}


def clear_finished() -> dict:
    """Clean up already-exited worker entries for frontend 'reset' button."""
    removed: list[str] = []
    with _lock:
        for wid in list(_workers.keys()):
            w = _workers[wid]
            proc = w.get("proc")
            if proc is None or proc.poll() is not None:
                removed.append(wid)
                del _workers[wid]
    return {"ok": True, "removed": removed}
