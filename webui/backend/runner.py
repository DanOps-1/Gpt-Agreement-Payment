"""A single active-run pipeline process controller.

Wraps `xvfb-run -a python pipeline.py [args]` subprocess: spawn / stream stdout to circular log buffer / SIGTERM-prioritized stop / expose status + log to routing layer.

In GoPay mode, additionally supports OTP proxy: by default writes WhatsApp / manual OTP entries to SQLite via WebUI internal HTTP endpoint, gopay.py polls that endpoint. Retains `GOPAY_OTP_REQUEST path=<file>` legacy format recognition, solely as explicit fallback for legacy file provider compatibility."""
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import link_state, settings as s
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
_active_gopay_phone: str = ""          # digits-only phone for the running gopay flow
_preserve_log_on_next_start: bool = False  # auto-loop sets True so log scrolls across iterations


def _read_pay_config() -> dict:
    try:
        return json.loads(s.PAY_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


_LINK_OK_RE = re.compile(r"\[gopay\]\s+midtrans linking ok\s+reference=(\S+)")
_CHARGE_SETTLED_RE = re.compile(r"\[gopay\]\s+charge settled")
# 406 "account already linked" signal —— Midtrans server confirms phone is bound
# Regardless of retry success, local should mark linked to sync with server
_LINK_406_RE = re.compile(r"\[gopay\]\s+midtrans linking 406")

# QRIS: extract PNG path + remote URL + reference + settled from qris.py standard logs
_QRIS_PNG_RE = re.compile(r"\[qris\]\s+PNG:\s+(\S+)")
_QRIS_URL_RE = re.compile(r"\[qris\]\s+远端预览:\s+(\S+)")
_QRIS_DEEPLINK_RE = re.compile(r"\[qris\]\s+DEEPLINK:\s+(\S+)")
_QRIS_REF_RE = re.compile(r"\[qris\]\s+QR 已生成 reference=(\S+)")
_QRIS_SETTLED_RE = re.compile(r"\[qris\]\s+settled")
_QRIS_EXPIRY_RE = re.compile(r"\[qris\]\s+过期:\s+(\S.*)")

# Current QRIS run artifacts: PNG / qr_image_url / reference / expiry.
# Frontend polls /api/qris/state; also returned in status() to avoid multiple endpoints.
_qris_state: dict = {}


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
              gopay_otp_file: str = "", qris: bool = False, count: int = 0,
              target_emails: Optional[list] = None, rt_only: bool = False,
              promo_plan: str = "plus", promo_country: str = "ID",
              promo_currency: str = "IDR", promo_campaign_id: str = "",
              # no_card_plus parameter (uses standalone scripts/no_card_paypal_plus.py, not pipeline.py)
              no_card_promo_link_id: int = 0,
              no_card_phone: str = "",
              no_card_otp_timeout: int = 240,
              no_card_signup_retries: int = 3,
              no_card_node_rpa_timeout: int = 900,
              no_card_max_due: int = 100,
              no_card_allow_already_paid: bool = False,
              no_card_allow_full_price: bool = False,
              no_card_paypal_country: str = "US",
              no_card_paypal_lang: str = "en",
              no_card_inventory_mail_source: str = "any") -> list[str]:
    """Compose final command line from parameters."""
    # no_card_plus mode: standalone script, runs Chromium RPA on promo link → PayPal → ChatGPT plus.
    # SMS API URL read from config.paypal.json::paypal.sms_api_url or environment variable, not via cmdline
    # to avoid exposing token (sensitive, prevent leaking into ps/log).
    if mode == "no_card_plus":
        cmd = ["xvfb-run", "-a", "python", "-u", "scripts/no_card_paypal_plus.py",
               "--config", str(s.PAY_CONFIG_PATH),
               "--paypal-node-rpa",
               "--paypal-node-rpa-timeout", str(int(no_card_node_rpa_timeout)),
               "--paypal-signup-retries", str(int(no_card_signup_retries)),
               "--paypal-country", (no_card_paypal_country or "US").upper(),
               "--paypal-lang", (no_card_paypal_lang or "en").lower(),
               "--phone", (no_card_phone or ""),
               "--otp-timeout", str(int(no_card_otp_timeout)),
               "--max-due", str(int(no_card_max_due))]
        if int(no_card_promo_link_id) > 0:
            cmd.extend(["--promo-link-id", str(int(no_card_promo_link_id))])
        if no_card_allow_already_paid:
            cmd.append("--allow-already-paid")
        if no_card_allow_full_price:
            cmd.append("--allow-full-price")
        src = (no_card_inventory_mail_source or "any").strip().lower()
        if src in ("outlook", "catch_all"):
            cmd.extend(["--inventory-mail-source", src])
        return cmd

    cmd = ["xvfb-run", "-a", "python", "-u", "pipeline.py",
           "--config", str(s.PAY_CONFIG_PATH)]
    # free_only two sub-modes + promo_link all skip paypal / gopay / qris payment stage
    if mode in ("free_register", "free_backfill_rt"):
        if mode == "free_register":
            cmd.append("--free-register")
            if count > 0:
                cmd.extend(["--count", str(count)])
        else:
            cmd.append("--free-backfill-rt")
        return cmd
    if mode == "promo_link":
        cmd.append("--promo-link")
        if count > 0:
            cmd.extend(["--count", str(count)])
        plan = (promo_plan or "plus").strip().lower()
        if plan not in {"plus", "team"}:
            plan = "plus"
        country = (promo_country or "ID").strip().upper()
        currency = (promo_currency or "IDR").strip().upper()
        cmd.extend(["--promo-plan", plan])
        cmd.extend(["--promo-country", country])
        cmd.extend(["--promo-currency", currency])
        if promo_campaign_id and promo_campaign_id.strip():
            cmd.extend(["--promo-campaign-id", promo_campaign_id.strip()])
        return cmd
    if qris:
        cmd.append("--qris")
    elif gopay:
        cmd.append("--gopay")
        if gopay_otp_file:
            cmd.extend(["--gopay-otp-file", gopay_otp_file])
    elif paypal:
        cmd.append("--paypal")
    # mode determines loop structure (daemon ∞ / self_dealer / batch N / single)
    if mode == "daemon":
        cmd.append("--daemon")
    elif mode == "self_dealer":
        cmd.extend(["--self-dealer", str(self_dealer)])
    elif mode == "batch":
        cmd.extend(["--batch", str(batch), "--workers", str(workers)])
    # mode == "single" → no extra flags
    # register_only / pay_only are modifiers, orthogonal to mode (batch + register-only
    # = bulk register N; single + register-only = single register)
    if register_only:
        cmd.append("--register-only")
    elif pay_only:
        cmd.append("--pay-only")
    if rt_only:
        cmd.append("--rt-only")
    if target_emails:
        joined = ",".join(e.strip() for e in target_emails if e and e.strip())
        if joined:
            cmd.extend(["--target-emails", joined])
    return cmd


def qris_state() -> dict:
    """Artifacts captured from current/most recent QRIS run. Frontend uses to render QR + status."""
    return dict(_qris_state)


def qris_png_bytes() -> Optional[bytes]:
    """Read latest QR PNG file bytes; return None if absent/read fails."""
    p = _qris_state.get("png_path")
    if not p:
        return None
    try:
        return Path(p).read_bytes()
    except Exception:
        return None


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
        "qris": qris_state(),
    }


def start(*, mode: str, paypal: bool = True, batch: int = 0, workers: int = 3,
          self_dealer: int = 0, register_only: bool = False, pay_only: bool = False,
          gopay: bool = False, qris: bool = False, count: int = 0,
          promo_plan: str = "plus", promo_country: str = "ID",
          promo_currency: str = "IDR", promo_campaign_id: str = "",
          register_mode: str = "protocol",
          env_overrides: Optional[dict] = None,
          target_emails: Optional[list] = None, rt_only: bool = False,
          mail_source: str = "outlook", outlook_email: str = "",
          # no_card_plus parameter
          no_card_promo_link_id: int = 0,
          no_card_phone: str = "",
          no_card_sms_api_url: str = "",
          no_card_otp_timeout: int = 240,
          no_card_signup_retries: int = 1,
          no_card_node_rpa_timeout: int = 900,
          no_card_max_due: int = 100,
          no_card_allow_already_paid: bool = False,
          no_card_allow_full_price: bool = False,
          no_card_paypal_country: str = "US",
          no_card_paypal_lang: str = "en",
          no_card_inventory_mail_source: str = "any") -> dict:
    global _proc, _started_at, _ended_at, _exit_code, _cmd, _mode
    global _log_lines, _seq_counter, _otp_file, _otp_to_db, _otp_pending, _otp_file_is_temp
    global _active_gopay_phone
    with _lock:
        if _proc is not None and _proc.poll() is None:
            raise RuntimeError("a pipeline is already running")

        # OTP defaults to WebUI SQLite endpoint; no longer creates temp FIFO file.
        otp_p: Optional[Path] = None

        cmd = build_cmd(mode, paypal, batch, workers, self_dealer,
                        register_only, pay_only, gopay=gopay,
                        gopay_otp_file="", qris=qris, count=count,
                        target_emails=target_emails, rt_only=rt_only,
                        promo_plan=promo_plan, promo_country=promo_country,
                        promo_currency=promo_currency,
                        promo_campaign_id=promo_campaign_id,
                        no_card_promo_link_id=no_card_promo_link_id,
                        no_card_phone=no_card_phone,
                        no_card_otp_timeout=no_card_otp_timeout,
                        no_card_signup_retries=no_card_signup_retries,
                        no_card_node_rpa_timeout=no_card_node_rpa_timeout,
                        no_card_max_due=no_card_max_due,
                        no_card_allow_already_paid=no_card_allow_already_paid,
                        no_card_allow_full_price=no_card_allow_full_price,
                        no_card_paypal_country=no_card_paypal_country,
                        no_card_paypal_lang=no_card_paypal_lang,
                        no_card_inventory_mail_source=no_card_inventory_mail_source)

        # GoPay link-state pre-flight: if the configured phone is currently
        # linked from a prior successful charge, GoPay will reject the next
        # linking attempt with 406 "account already linked". Refuse to start
        # until an external service POSTs to /api/gopay/link-state/unlink.
        active_phone = ""
        if gopay:
            cfg = _read_pay_config()
            active_phone = link_state.phone_from_gopay_config(cfg)
            if active_phone and link_state.is_linked(active_phone):
                raise RuntimeError(
                    f"gopay phone {active_phone} is currently linked; "
                    "external service must POST /api/gopay/link-state/unlink first"
                )

        # Reset (auto-loop preserves prior iteration logs across iterations for continuous user visibility)
        global _preserve_log_on_next_start
        if not _preserve_log_on_next_start:
            _log_lines = []
            _seq_counter = 0
        _preserve_log_on_next_start = False
        _started_at = time.time()
        _ended_at = None
        _exit_code = None
        _cmd = cmd
        _mode = mode
        _otp_file = otp_p
        _otp_to_db = False
        _otp_file_is_temp = otp_p is not None
        _otp_pending = False
        _active_gopay_phone = active_phone
        # Clear QRIS previous artifacts before each run start, prevent frontend from reading stale QR
        _qris_state.clear()

        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if gopay:
            env["WEBUI_GOPAY_OTP_URL"] = wa_relay.otp_url()
        # Registration/login path: respect register_mode passed by frontend (protocol / browser).
        # protocol = AuthFlow HTTP direct + Node/QuickJS Sentinel; RT refresh uses AuthFlow.run_protocol_login.
        # browser = Camoufox/Playwright, triggers real browser; post-run RT uses card._exchange_refresh_token_with_session.
        # Both paths fail under forced add-phone scenario (OpenAI risk control), but equivalent capability otherwise.
        rm = (register_mode or "protocol").strip().lower()
        if rm not in ("protocol", "browser"):
            print(f"[runner] register_mode={rm!r} 不识别，回退 protocol")
            rm = "protocol"
        env["WEBUI_REG_MODE"] = rm
        env.setdefault("OPENAI_SENTINEL_REQUIRE_QUICKJS", "1")
        # Email source (strict binary choice) → CTF-reg/mail/provider.py:create_mailbox() read
        src = (mail_source or "outlook").strip().lower()
        env["WEBUI_MAIL_SOURCE"] = "catch_all" if src == "catch_all" else "outlook"
        env.pop("WEBUI_MAIL_MODE", None)  # Remove legacy obfuscation prevention
        if outlook_email and outlook_email.strip() and src == "outlook":
            env["WEBUI_OUTLOOK_EMAIL"] = outlook_email.strip().lower()
        else:
            env.pop("WEBUI_OUTLOOK_EMAIL", None)
        # QRIS demo mode: webui startup sets WEBUI_QRIS_FORCE_MOCK=1 to let qris=true run
        # built-in mock charge (bypass OpenAI/Stripe risk control, demo QR rendering to frontend). Don't set in production.
        if qris and os.getenv("WEBUI_QRIS_FORCE_MOCK", "").strip().lower() in ("1", "true", "yes"):
            env["QRIS_MOCK"] = "1"
            print("[runner] WEBUI_QRIS_FORCE_MOCK=1 → 子进程 QRIS_MOCK=1 (demo 模式，绕过 OpenAI)")
        # no_card_plus mode: scripts/no_card_paypal_plus.py requires SMS API URL.
        # Not via cmdline (token sensitive), three-layer injection fallback in env:
        #   1. no_card_sms_api_url input by user in webui form (priority)
        #   2. host process env PAYPAL_SMS_API_URL / PPS_SMS_API_URL
        #   3. config.paypal.json::paypal.sms_api_url
        if mode == "no_card_plus":
            sms_url = (no_card_sms_api_url or "").strip()
            if not sms_url:
                sms_url = (os.environ.get("PAYPAL_SMS_API_URL")
                           or os.environ.get("PPS_SMS_API_URL") or "").strip()
            if not sms_url:
                try:
                    pay_cfg = _read_pay_config()
                    sms_url = ((pay_cfg.get("paypal") or {}).get("sms_api_url") or "").strip()
                except Exception as e:
                    print(f"[runner] no_card_plus 读 sms_api_url 失败: {e}")
            if sms_url:
                env["PAYPAL_SMS_API_URL"] = sms_url
                env.setdefault("PPS_SMS_API_URL", sms_url)
            else:
                print("[runner] no_card_plus: SMS API URL 缺失, 子进程会 fail (在 UI 输 SMS API URL 或 config.paypal.json::paypal.sms_api_url)")
        if env_overrides:
            for k, v in env_overrides.items():
                if v is None:
                    env.pop(str(k), None)
                else:
                    env[str(k)] = str(v)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(s.ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                # Make pipeline subprocess an independent session leader; when webui backend restarts,
                # won't be killed alongside. stop() explicitly terminates entire process group with killpg.
                start_new_session=True,
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
    last_link_ref = ""
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

                # Track the merchant reference from the linking step so we can
                # store it alongside the linked-state record on charge settle.
                m = _LINK_OK_RE.search(line)
                if m:
                    last_link_ref = m.group(1).strip().strip(",.")

                # Pre-detect 406: Midtrans already determined phone is bound, local should sync.
                # Don't wait for charge settled before updating —— failed payments also leave linked status,
                # else next charge blindly retries, blocked by same 406.
                if _LINK_406_RE.search(line) and _active_gopay_phone:
                    try:
                        link_state.mark_linked(
                            _active_gopay_phone,
                            payment_ref=last_link_ref or "auto_from_406",
                            source="pipeline_406_detect",
                        )
                    except Exception:
                        pass

                # Mark the configured phone as linked when a charge settles.
                # GoPay treats the phone as bound at this point, so subsequent
                # linking attempts return 406 unless an external service has
                # called /api/gopay/link-state/unlink in the meantime.
                if _CHARGE_SETTLED_RE.search(line) and _active_gopay_phone:
                    try:
                        link_state.mark_linked(
                            _active_gopay_phone,
                            payment_ref=last_link_ref,
                            source="pipeline",
                        )
                    except Exception:
                        pass

                # QRIS artifacts —— drop PNG path / remote URL / reference into _qris_state
                # for GET /api/qris/state and /api/qris/qr.png to use
                m_png = _QRIS_PNG_RE.search(line)
                if m_png:
                    _qris_state["png_path"] = m_png.group(1).strip()
                    _qris_state["ready_at"] = time.time()
                m_url = _QRIS_URL_RE.search(line)
                if m_url:
                    _qris_state["qr_image_url"] = m_url.group(1).strip()
                m_dl = _QRIS_DEEPLINK_RE.search(line)
                if m_dl:
                    _qris_state["deeplink_url"] = m_dl.group(1).strip()
                m_ref = _QRIS_REF_RE.search(line)
                if m_ref:
                    _qris_state["reference"] = m_ref.group(1).strip()
                m_exp = _QRIS_EXPIRY_RE.search(line)
                if m_exp:
                    _qris_state["expiry"] = m_exp.group(1).strip()
                if _QRIS_SETTLED_RE.search(line):
                    _qris_state["settled"] = True
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
    # subprocess is an independent session leader (start_new_session=True), use killpg
    # to terminate the entire group, otherwise only SIGTERM parent process will leave xvfb-run/python pipeline orphans.
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
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
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
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


def append_log(line: str) -> None:
    """Append a synthetic line into the rolling log (used by auto-loop to inject
    [auto-loop] progress markers between subprocess iterations)."""
    global _seq_counter, _log_lines
    with _lock:
        _seq_counter += 1
        _log_lines.append({"seq": _seq_counter, "ts": time.time(), "line": line})
        if len(_log_lines) > 3000:
            _log_lines = _log_lines[-2000:]


def preserve_log_on_next_start() -> None:
    """Auto-loop calls before each runner.start() to keep the rolling log
    instead of wiping it on every iteration."""
    global _preserve_log_on_next_start
    _preserve_log_on_next_start = True


def get_lines_since(since_seq: int = 0, limit: int = 1000) -> list[dict]:
    with _lock:
        return [e for e in _log_lines if e["seq"] > since_seq][:limit]


def get_tail(n: int = 200) -> list[dict]:
    with _lock:
        return _log_lines[-n:]
