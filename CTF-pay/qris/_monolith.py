#!/usr/bin/env python3
"""QRIS payment flow for ChatGPT Plus subscriptions.

Replace GoPay tokenization: QRIS is Indonesia's central bank-mandated unified QR code standard (EMVCo QRCPS). Users scan with any e-wallet (GoPay/DANA/OVO/ShopeePay/LinkAja) or bank app to pay—**no** binding/OTP/PIN/WhatsApp required. Mobile dependency for bulk scenarios is nearly zero.

Flow (reuses gopay.py steps 1-6: Stripe → Midtrans Snap token bootstrap):

    1.  POST chatgpt.com/backend-api/payments/checkout              ← cs_live_xxx
    2.  POST api.stripe.com/v1/payment_methods (type=gopay)          ← pm_xxx
    3.  POST api.stripe.com/v1/payment_pages/{cs}/confirm            ← status:requires_action
    4.  POST chatgpt.com/backend-api/payments/checkout/approve       ← approved
    5.  GET  pm-redirects.stripe.com/authorize/{nonce}               → 302 → midtrans
    6.  GET  app.midtrans.com/snap/v1/transactions/{snap_token}      ← enabled_payments
    --- QRIS branch below (replaces gopay.py steps 7-14) ---
    7q. POST app.midtrans.com/snap/v2/transactions/{snap}/charge
            body: {payment_type: "qris", qris:{acquirer:"gopay"}}    ← qr_string + actions
        Fallback on failure: {payment_type: "gopay", tokenization: false}
    8q. Locally render PNG + terminal ASCII using qrcode library based on qr_string;
        simultaneously output URL merchants-app.midtrans.com/v4/qris/gopay/{ref}/qr-code,
        users can also open directly in browser.
    9q. Dual-track polling:
          Primary axis GET app.midtrans.com/snap/v1/transactions/{snap}/status
               → transaction_status in ("settlement","capture") treated as settled
          Secondary axis GET chatgpt.com/checkout/verify?stripe_session_id=...
               triggered once per N primary polls, as settlement liveness fallback
    15. GET  chatgpt.com/checkout/verify?stripe_session_id=...       ← Plus active"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

# Let `python3 CTF-pay/qris.py` / `python3 -m qris` / run from anywhere in repo find gopay/card
# Wave E: qris.py → qris/_monolith.py, _HERE is now CTF-pay/qris/, what we really need is CTF-pay/
_HERE = Path(__file__).resolve().parent
_CTF_PAY = _HERE.parent
if str(_CTF_PAY) not in sys.path:
    sys.path.insert(0, str(_CTF_PAY))

from gopay import (
    DEFAULT_MIDTRANS_CLIENT_ID,
    DEFAULT_STRIPE_PK,
    DEFAULT_TIMEOUT,
    GoPayCharger,
    GoPayError,
    _build_chatgpt_session,
    _load_cfg,
)

# qrcode is optional dependency: if installed, render PNG + ASCII locally; if not, only output qr_string + remote URL,
# prompt user to install. MIT licensed, ~50KB, pure Python (PIL backend optional).
try:
    import qrcode  # type: ignore
    _QRCODE_AVAILABLE = True
except ImportError:
    qrcode = None  # type: ignore
    _QRCODE_AVAILABLE = False

try:
    from PIL import Image  # type: ignore  # noqa: F401  (qrcode renders PNG using PIL)
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


# ──────────────────────────── constants ───────────────────────────

DEFAULT_POLL_INTERVAL_S = 3.0
DEFAULT_POLL_TIMEOUT_S = 600.0  # 10 minutes, QRIS default validity period
DEFAULT_VERIFY_EVERY_N_POLLS = 4
# Midtrans Snap charge response actions[].url looks like
#   https://merchants-app.midtrans.com/v4/qris/gopay/{ref}/qr-code
# This is an HTML page, can be viewed or iframed, but not raw PNG. Local generation is reliable.
QR_DOWNLOAD_URL_RE = re.compile(
    r"merchants-app\.midtrans\.com/v\d+/qris/[a-z]+/[A-Za-z0-9]+/qr-code"
)


# ──────────────────────────── exceptions ──────────────────────────


class QrisError(GoPayError):
    """QRIS flow error (inherits GoPayError for unified error handling at upper layer)."""


# ──────────────────────────── core ────────────────────────────────


class QrisCharger(GoPayCharger):
    """Snap payment using QRIS instead of tokenization.

    Reuses GoPayCharger's Stripe + Midtrans Snap bootstrap, **bypasses** linking/OTP/PIN,
    directly charges out QR then polls for settlement.

    Construction:
        chatgpt_session: chatgpt session with injected cookies (same as GoPay)
        qris_cfg: {"output_dir": str, "poll_interval_s": float, "poll_timeout_s": float,
                   "verify_every_n_polls": int, "acquirer_preference": list[str]}
        log: print-like
        proxy: optional proxy URL
        runtime_cfg: stripe runtime config"""

    def __init__(
        self,
        chatgpt_session: Any,
        qris_cfg: dict,
        *,
        log=print,
        proxy: Optional[str] = None,
        runtime_cfg: Optional[dict] = None,
    ):
        # GoPayCharger.__init__ requires country_code/phone_number/pin. QRIS doesn't need these,
        # so pass stub values for parent class init; subsequent paths never read these three fields.
        stub_cfg = {
            "country_code": "00",
            "phone_number": "00000000000",
            "pin": "000000",
            "midtrans_client_id": qris_cfg.get("midtrans_client_id"),
        }
        super().__init__(
            chatgpt_session,
            stub_cfg,
            otp_provider=lambda: "",  # Never called
            log=log,
            proxy=proxy,
            runtime_cfg=runtime_cfg,
        )
        self.qris_cfg = dict(qris_cfg or {})
        # Must be absolute path: webui runner grabs PNG path from log [qris] PNG: <path> then
        # `Path(p).read_bytes()` reads it; relative paths fail ENOENT when cwd differs across processes.
        self.output_dir = Path(
            self.qris_cfg.get("output_dir") or "./qris_artifacts"
        ).expanduser().resolve()
        self.poll_interval = float(
            self.qris_cfg.get("poll_interval_s") or DEFAULT_POLL_INTERVAL_S
        )
        self.poll_timeout = float(
            self.qris_cfg.get("poll_timeout_s") or DEFAULT_POLL_TIMEOUT_S
        )
        self.verify_every_n = max(
            1, int(self.qris_cfg.get("verify_every_n_polls") or DEFAULT_VERIFY_EVERY_N_POLLS)
        )
        pref = self.qris_cfg.get("acquirer_preference") or ["qris", "gopay"]
        self.acquirer_preference = [str(p).strip().lower() for p in pref if p]

    # ───── Step 7q: Midtrans QRIS charge ─────

    def _midtrans_create_qris_charge(self, snap_token: str) -> dict:
        """POST snap/v2/transactions/{snap}/charge with QRIS payload.

        Returns dict with keys: charge_ref, qr_string, qr_image_url, expiry_time,
        transaction_status, raw.

        Tries payment_type=qris (qris.acquirer=gopay) first; falls back to
        payment_type=gopay + tokenization=false on 405/406/400 (early Midtrans GoPay QRIS method)."""
        url = f"https://app.midtrans.com/snap/v2/transactions/{snap_token}/charge"
        headers = {
            **self._midtrans_basic_auth(),
            "Content-Type": "application/json",
            "Origin": "https://app.midtrans.com",
            "Referer": f"https://app.midtrans.com/snap/v4/redirection/{snap_token}",
        }

        attempts: list[tuple[str, dict]] = []
        for acq in self.acquirer_preference:
            if acq == "qris":
                attempts.append(("qris", {
                    "payment_type": "qris",
                    "qris": {"acquirer": "gopay"},
                    "promo_details": None,
                }))
            elif acq == "gopay":
                attempts.append(("gopay-untokenized", {
                    "payment_type": "gopay",
                    "tokenization": "false",
                    "promo_details": None,
                }))

        last_err = ""
        for label, body in attempts:
            r = self.ext.post(url, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
            if r.status_code in (200, 201):
                try:
                    data = r.json()
                except Exception as e:
                    last_err = f"{label}: bad json {e!s}"
                    continue
                parsed = self._parse_qris_charge_response(data)
                if parsed:
                    self.log(
                        f"[qris] charge ok via {label} ref={parsed['charge_ref']} "
                        f"expiry={parsed.get('expiry_time') or '?'}"
                    )
                    return parsed
                last_err = f"{label}: response missing qr_string/charge_ref: {str(data)[:200]}"
                continue
            last_err = f"{label}: status={r.status_code} body={r.text[:200]}"
            self.log(f"[qris] charge attempt {label} failed → {last_err}")
        raise QrisError(f"midtrans qris charge 全部失败: {last_err}")

    @staticmethod
    def _parse_qris_charge_response(data: dict) -> Optional[dict]:
        qr_string = data.get("qr_string") or data.get("qris_string") or ""
        actions = data.get("actions") or []
        qr_image_url = ""
        deeplink_url = ""
        for act in actions:
            if not isinstance(act, dict):
                continue
            name = str(act.get("name") or "").lower()
            u = str(act.get("url") or "")
            if "qr" in name and u and not qr_image_url:
                qr_image_url = u
            elif "deeplink" in name and u and not deeplink_url:
                deeplink_url = u
        if not qr_image_url:
            qr_image_url = (
                data.get("qr_code_url")
                or data.get("qris_url")
                or data.get("gopay_verification_link_url")
                or ""
            )
        # GoPay untokenized mode: midtrans gives deeplink_url at top level; user taps on mobile to open
        # GoPay app payment confirmation popup (bypasses QR scan + WhatsApp OTP)
        if not deeplink_url:
            deeplink_url = data.get("deeplink_url") or data.get("gopay_deeplink_url") or ""

        # charge_ref source (by priority):
        # 1. transaction_id at top level
        # 2. Extract from qr_image_url (pattern: /v4/qris/gopay/{ref}/qr-code)
        # 3. Extract reference= param from gopay_verification_link_url (GoPay compatible path)
        charge_ref = str(data.get("transaction_id") or "").strip()
        if not charge_ref and qr_image_url:
            m = re.search(r"/qris/[a-z]+/([A-Za-z0-9]+)/qr-code", qr_image_url)
            if m:
                charge_ref = m.group(1)
        if not charge_ref:
            link = data.get("gopay_verification_link_url") or ""
            m = re.search(r"reference=([A-Za-z0-9]+)", link)
            if m:
                charge_ref = m.group(1)

        if not (qr_string or qr_image_url) or not charge_ref:
            return None
        return {
            "charge_ref": charge_ref,
            "qr_string": qr_string,
            "qr_image_url": qr_image_url,
            "deeplink_url": deeplink_url,
            "expiry_time": data.get("expiry_time") or data.get("expires_at") or "",
            "transaction_status": data.get("transaction_status") or "pending",
            "raw": data,
        }

    # ───── Step 8q: local persist + ASCII render ─────

    def _save_qr_artifacts(self, parsed: dict) -> dict:
        """Output local artifacts from charge response, return paths dict."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ref = parsed["charge_ref"]
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = self.output_dir / f"qris_{ref}_{ts}"

        out: dict[str, str] = {}

        # 1) qr_string text (raw EMVCo QRCPS payload, user can paste into any QR renderer)
        qr_string = parsed.get("qr_string") or ""
        if qr_string:
            txt_path = prefix.with_suffix(".txt")
            txt_path.write_text(qr_string, encoding="utf-8")
            out["qr_string_path"] = str(txt_path)
            out["qr_string"] = qr_string

        # 2) PNG (local qrcode library preferred; fallback to download remote URL if failed/no qr_string)
        png_path = prefix.with_suffix(".png")
        rendered_local = False
        if qr_string and _QRCODE_AVAILABLE and _PIL_AVAILABLE:
            try:
                _render_qr_png(qr_string, png_path)
                out["qr_png_path"] = str(png_path)
                out["qr_png_source"] = "local-qrcode-lib"
                rendered_local = True
            except Exception as e:
                self.log(f"[qris] 本地 qrcode 渲染失败: {e}; 改用远端下载")

        if not rendered_local and parsed.get("qr_image_url"):
            try:
                self._download_qr_image(parsed["qr_image_url"], png_path)
                out["qr_png_path"] = str(png_path)
                out["qr_png_source"] = "midtrans-merchants-app"
            except Exception as e:
                self.log(f"[qris] 远端 QR 下载失败: {e}")

        # 3) Remote URL (the one in screenshots, browser-friendly)
        if parsed.get("qr_image_url"):
            out["qr_image_url"] = parsed["qr_image_url"]

        # 4) Metadata json (reference / expiry / status / full charge response persisted for audit trail)
        meta_path = prefix.with_suffix(".json")
        meta_path.write_text(
            json.dumps({
                "charge_ref": ref,
                "expiry_time": parsed.get("expiry_time"),
                "transaction_status": parsed.get("transaction_status"),
                "qr_image_url": parsed.get("qr_image_url"),
                "qr_string": qr_string,
                "raw": parsed.get("raw"),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        out["meta_path"] = str(meta_path)
        return out

    def _download_qr_image(self, url: str, save_path: Path) -> None:
        # merchants-app URL is HTML page by default, not raw PNG; try downloading,
        # check Content-Type to decide suffix. Content-Type=image/png stores PNG,
        # otherwise save as .html so user can open in browser.
        r = self.ext.get(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if "image" in ctype:
            save_path.write_bytes(r.content)
            return
        # Not an image: save as HTML
        html_path = save_path.with_suffix(".html")
        html_path.write_bytes(r.content)
        raise QrisError(
            f"远端不是 PNG 而是 {ctype or 'unknown'}，已存为 {html_path}（浏览器打开看 QR）",
        )

    def _print_qr_ascii(self, qr_string: str) -> None:
        if not qr_string:
            return
        if not _QRCODE_AVAILABLE:
            self.log(
                "[qris] qrcode 库未安装，跳过 ASCII 打印；"
                "qr_string 内容已落盘，可贴进任意 QR 渲染器：\n"
                f"{qr_string}"
            )
            return
        try:
            qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(qr_string)
            qr.make(fit=True)
            buf = io.StringIO()
            qr.print_ascii(out=buf, invert=True)
            self.log("\n" + buf.getvalue())
        except Exception as e:
            self.log(f"[qris] ASCII 渲染失败: {e}; qr_string={qr_string[:80]}…")

    # ───── Step 9q: dual-track polling ─────

    def _midtrans_poll_status(self, snap_token: str) -> dict:
        """Single GET snap/v1/transactions/{snap_token}/status;
        midtrans occasionally closes connection abruptly, swallow error return unknown for outer retry."""
        for attempt in range(3):
            try:
                r = self.ext.get(
                    f"https://app.midtrans.com/snap/v1/transactions/{snap_token}/status",
                    headers={
                        **self._midtrans_basic_auth(),
                        "x-source": "snap",
                        "x-source-app-type": "redirection",
                        "x-source-version": "2.3.0",
                    },
                    timeout=DEFAULT_TIMEOUT,
                )
                if r.status_code != 200:
                    return {"transaction_status": "unknown", "_http_status": r.status_code}
                try:
                    return r.json() or {}
                except Exception:
                    return {"transaction_status": "unknown", "_http_status": 200}
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                self.log(f"[qris] poll status 异常 (吃掉继续): {e}")
                return {"transaction_status": "unknown", "_error": str(e)[:120]}

    def _chatgpt_verify(self, cs_id: str, *, retries: int = 6,
                        sleep: float = 1.5) -> dict:
        """After settle, fallback verify: short-term retry _chatgpt_verify_once for plan=plus,
        wrap as dict for upper layer. Stripe webhook → OpenAI backend → chatgpt_plan_type
        usually within seconds, occasionally 5-10s delay; verify failure shouldn't fail entire payment flow.

        Returns: {state: "verified"|"not_verified"|"verify_error", attempts, [error]}"""
        if not cs_id:
            return {"state": "settled_no_verify"}
        err = ""
        for i in range(max(1, retries)):
            try:
                if self._chatgpt_verify_once(cs_id):
                    return {"state": "verified", "attempts": i + 1}
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:120]}"
            if i < retries - 1:
                time.sleep(sleep)
        if err:
            return {"state": "verify_error", "attempts": retries, "error": err}
        return {"state": "not_verified", "attempts": retries}

    def _chatgpt_verify_once(self, cs_id: str) -> bool:
        """Single verify: check if ChatGPT plan upgraded to plus.
        Old impl returned r.status_code == 200 was buggy: free account hits this endpoint also returns 200,
        causing _wait_for_settlement false positive success. Changed to parse JSON and check plan_type."""
        try:
            r = self.cs.get(
                "https://chatgpt.com/checkout/verify",
                params={
                    "stripe_session_id": cs_id,
                    "processor_entity": "openai_llc",
                    "plan_type": "plus",
                },
                timeout=DEFAULT_TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code != 200:
                return False
            try:
                data = r.json() or {}
            except Exception:
                # Returns HTML (unregistered redirect) also 200, judge false
                return False
            # Any one marked paid/active counts as pass
            plan = str(data.get("plan_type") or data.get("planType") or "").lower()
            state = str(data.get("state") or data.get("status") or "").lower()
            paid = bool(data.get("is_paid") or data.get("paid"))
            if plan in ("plus", "pro", "team") or paid or state in ("succeeded", "active", "paid"):
                return True
            return False
        except Exception:
            return False

    def _wait_for_settlement(self, snap_token: str, cs_id: str) -> dict:
        """Dual-track wait for settlement: Midtrans status primary axis + ChatGPT verify secondary."""
        deadline = time.time() + self.poll_timeout
        attempt = 0
        last_status = "pending"
        last_fraud = ""
        last_verify = False
        while time.time() < deadline:
            attempt += 1
            data = self._midtrans_poll_status(snap_token)
            status = str(data.get("transaction_status") or "").lower()
            fraud = str(data.get("fraud_status") or "").lower()
            if status and status != last_status:
                self.log(f"[qris] poll #{attempt} midtrans status={status} fraud={fraud}")
                last_status = status
                last_fraud = fraud

            if status in ("settlement", "capture"):
                self.log(f"[qris] settled (midtrans): status={status}")
                return {"settled_via": "midtrans", "midtrans_status": data}

            if status in ("expire", "deny", "cancel", "failure"):
                raise QrisError(
                    f"midtrans transaction terminal: status={status} "
                    f"fraud={fraud} body={str(data)[:200]}"
                )

            # Auxiliary axis: Trigger chatgpt verify once every N midtrans polling cycles.
            # ChatGPT's cs_id must exist to be meaningful; it may not exist in semi-automatic mode.
            if cs_id and attempt % self.verify_every_n == 0:
                ok = self._chatgpt_verify_once(cs_id)
                if ok and not last_verify:
                    self.log(f"[qris] poll #{attempt} chatgpt verify ok")
                    last_verify = True
                if ok:
                    return {"settled_via": "chatgpt-verify", "midtrans_status": data}

            time.sleep(self.poll_interval)

        raise QrisError(
            f"等待入账超时 {self.poll_timeout}s "
            f"(last_status={last_status} fraud={last_fraud} verify_ok={last_verify})"
        )

    # ───── Top-level driver ─────

    def run(self, stripe_pk: str, billing: Optional[dict] = None) -> dict:
        billing = billing or {}
        cs_id = self._chatgpt_create_checkout()
        pm_id = self._stripe_create_pm(cs_id, stripe_pk, billing)
        self._stripe_confirm(cs_id, pm_id, stripe_pk)
        self._chatgpt_approve(cs_id)
        snap_token = self._follow_redirect_to_midtrans(cs_id, stripe_pk)
        return self._run_midtrans_qris(snap_token, cs_id)

    def run_from_redirect(self, pm_redirect_url: str, cs_id: str = "") -> dict:
        """Semi-automatic: Take over from pm-redirects.stripe.com URL."""
        snap_token = self._fetch_pm_redirect_snap_token(pm_redirect_url)
        self.log(f"[qris] midtrans snap_token={snap_token}")
        return self._run_midtrans_qris(snap_token, cs_id)

    def _run_midtrans_qris(self, snap_token: str, cs_id: str) -> dict:
        self._midtrans_load_transaction(snap_token)
        # Idiot-proof check: when promo 'plus-1-month-free' is triggered, invoice.amount_due should be ≤ 100 IDR
        # (1 IDR test charge); if full price (~34900000) is shown, it indicates that the export IP / account qualification does not meet requirements
        # promo, will really deduct ¥150 if you don't set it. Unless user explicitly enables allow_charge_when_coupon_ineligible
        amount = getattr(self, "_last_amount_due", 0) or 0
        promo_ok = amount and amount <= 100  # IDR cents (100 = 1 IDR)
        allow = bool(self.qris_cfg.get("allow_charge_when_coupon_ineligible"))
        if amount and not promo_ok and not allow:
            raise QrisError(
                f"promo 'plus-1-month-free' 未命中：invoice.amount_due={amount} (IDR cents, "
                f"~{amount//100} IDR ≈ ${amount/100/15500:.2f}). 出口 IP 或账号资格不满足 promo。"
                " 要强行真扣款继续，在 config.qris.allow_charge_when_coupon_ineligible=true。"
                " 否则换印尼出口 IP 再跑。"
            )
        if amount and promo_ok:
            self.log(f"[qris] ✓ promo 命中 amount_due={amount} IDR cents (test charge)")
        parsed = self._midtrans_create_qris_charge(snap_token)
        artifacts = self._save_qr_artifacts(parsed)

        # User-visible prompts
        ref = parsed["charge_ref"]
        self.log("─" * 64)
        self.log(f"[qris] QR 已生成 reference={ref}")
        if artifacts.get("qr_png_path"):
            self.log(f"[qris] PNG: {artifacts['qr_png_path']} ({artifacts.get('qr_png_source')})")
        if artifacts.get("qr_string_path"):
            self.log(f"[qris] qr_string: {artifacts['qr_string_path']}")
        if artifacts.get("qr_image_url"):
            self.log(f"[qris] 远端预览: {artifacts['qr_image_url']}")
        if parsed.get("deeplink_url"):
            self.log(f"[qris] DEEPLINK: {parsed['deeplink_url']}")
        if parsed.get("expiry_time"):
            self.log(f"[qris] 过期: {parsed['expiry_time']}")
        self.log(f"[qris] meta: {artifacts['meta_path']}")
        self.log("─" * 64)
        # Terminal ASCII (Optional)
        self._print_qr_ascii(parsed.get("qr_string") or "")
        self.log("─" * 64)

        # ═══ adb automation: replace the 30-second manual bottleneck of "waiting for user to scan code" ═══
        # Config: qris_cfg.adb_auto = {"enabled": bool, "pin": "<6-digit>", "deeplink_only": true,
        #                            "serial": "<adb-serial>", "adb_port": int}
        # Process: Use adb am start -d <deeplink> to let GoPay on the emulator take over,
        # Then input tap numeric keypad enter PIN + tap confirm → equivalent to scanning QR + entering PIN.
        adb_auto_cfg = self.qris_cfg.get("adb_auto") or {}
        adb_auto_enabled = bool(adb_auto_cfg.get("enabled"))
        if adb_auto_enabled:
            deeplink = parsed.get("deeplink_url") or ""
            pin = str(adb_auto_cfg.get("pin") or "")
            if not deeplink:
                self.log("[qris] adb_auto 启用但 midtrans 未返 deeplink_url，跳过自动化")
            elif len(pin) != 6 or not pin.isdigit():
                self.log("[qris] adb_auto 启用但 pin 配置无效（必须 6 位数字），跳过自动化")
            else:
                try:
                    import sys as _sys
                    from pathlib import Path as _P
                    _ctf = str(_P(__file__).resolve().parent.parent)  # Wave E: CTF-pay/qris/ → CTF-pay/
                    if _ctf not in _sys.path:
                        _sys.path.insert(0, _ctf)
                    from adb.driver import GoPayAuto  # Wave G: gopay_adb.py → adb/driver.py
                    g = GoPayAuto(
                        serial=adb_auto_cfg.get("serial") or None,
                        adb_port=adb_auto_cfg.get("adb_port") or None,
                        log=self.log,
                    )
                    self.log(f"[qris] adb_auto 启用 → 驱动 emulator 上 GoPay 自动支付…")
                    auto = g.pay_with_deeplink(
                        deeplink=deeplink,
                        pin=pin,
                        screenshot_dir=str(self.output_dir / "adb_shots"),
                    )
                    self.log(f"[qris] adb_auto 结果: state={auto.get('state')} msg={auto.get('message')}")
                    if auto.get("state") not in ("success", "unknown"):
                        # Automation failed (expired/insufficient/blocked/timeout) → Fallback to legacy path
                        self.log(f"[qris] adb_auto 未成功，降级到等用户扫码…")
                except Exception as e:
                    self.log(f"[qris] adb_auto 异常 (降级老路): {type(e).__name__}: {e}")
        else:
            self.log("[qris] adb_auto 未启用 → 等用户扫码")

        self.log(
            f"[qris] 轮询 midtrans 入账（每 {self.poll_interval:g}s, "
            f"每 {self.verify_every_n} 次触发一次 chatgpt verify）…"
        )

        settled = self._wait_for_settlement(snap_token, cs_id)

        # Double confirmation: even if midtrans reports settle, go through chatgpt verify once more to get the final plan status
        verify_result: dict[str, Any] = {"state": "settled_no_verify"}
        if cs_id:
            verify_result = self._chatgpt_verify(cs_id)

        return {
            "state": "succeeded",
            "snap_token": snap_token,
            "charge_ref": ref,
            "settled_via": settled.get("settled_via"),
            "midtrans_status": settled.get("midtrans_status"),
            "verify": verify_result,
            "artifacts": artifacts,
        }


# ──────────────────────────── QR rendering ─────────────────────────


class _QrisHookSuccess(Exception):
    """Sentinel thrown from the card._drive_gopay_from_redirect hook,
    blocking subsequent polling in card.run, allowing main() to get the result and emit JSON."""
    def __init__(self, result: dict):
        super().__init__(result.get("charge_ref", ""))
        self.result = result


def _run_via_card(charger: "QrisCharger", config_path: str, cs_id_hint: str = "") -> dict:
    """Walk through the complete card.py flow of fresh_checkout + manual_approval beta + check_coupon,
    monkey-patch _drive_gopay_from_redirect to let card.py obtain the pm-redirects URL and
    hand it off to us for untokenized charge → QR + deeplink.

    qris.py's own simplified _stripe_create_pm/_stripe_confirm/_chatgpt_approve are blocked by result=blocked
    under OpenAI's new fraud prevention, must reuse card.py's complete 500-line flow."""
    import card  # in-process import is required to monkey-patch
    captured: dict = {}

    def _hook(redirect_url: str, _cfg: dict, _otp_file: str = "", session_id: str = "") -> None:
        charger.log(f"[qris] 接管 redirect: {redirect_url[:100]}...")
        snap_token = charger._fetch_pm_redirect_snap_token(redirect_url)
        charger.log(f"[qris] midtrans snap_token={snap_token}")
        charger._midtrans_load_transaction(snap_token)
        parsed = charger._midtrans_create_qris_charge(snap_token)
        # Expose deeplink to charger instance for _wait_with_adb_auto to call adb auto payment
        charger._last_charge_deeplink = parsed.get("deeplink_url", "")
        artifacts = charger._save_qr_artifacts(parsed)
        ref = parsed["charge_ref"]
        charger.log("─" * 64)
        charger.log(f"[qris] QR 已生成 reference={ref}")
        if artifacts.get("qr_png_path"):
            charger.log(f"[qris] PNG: {artifacts['qr_png_path']} ({artifacts.get('qr_png_source')})")
        if artifacts.get("qr_image_url"):
            charger.log(f"[qris] 远端预览: {artifacts['qr_image_url']}")
        if parsed.get("deeplink_url"):
            charger.log(f"[qris] DEEPLINK: {parsed['deeplink_url']}")
        if parsed.get("expiry_time"):
            charger.log(f"[qris] 过期: {parsed['expiry_time']}")
        charger.log(f"[qris] meta: {artifacts['meta_path']}")
        charger.log("─" * 64)
        charger._print_qr_ascii(parsed.get("qr_string") or "")
        charger.log(
            f"[qris] 等用户扫码入账（每 {charger.poll_interval:g}s 轮询 midtrans）…"
        )

        # Dual-track settlement: webui runner captures [qris] settled logs → frontend badge turns green
        try:
            settled = charger._wait_for_settlement(snap_token, session_id or "")
            settled_via = settled.get("settled_via", "midtrans")
            verify = (charger._chatgpt_verify(session_id) if session_id
                      else {"state": "settled_no_verify"})
        except QrisError as wait_err:
            charger.log(f"[qris] 等待入账失败 / 过期: {wait_err}")
            settled_via = "expired_or_failed"
            verify = {"state": "wait_failed", "error": str(wait_err)[:200]}

        captured.update({
            "state": "succeeded",
            "snap_token": snap_token,
            "charge_ref": ref,
            "settled_via": settled_via,
            "verify": verify,
            "artifacts": artifacts,
            "deeplink_url": parsed.get("deeplink_url", ""),
            "qr_image_url": artifacts.get("qr_image_url", ""),
            "expiry_time": parsed.get("expiry_time", ""),
            "session_id": session_id,
        })
        # Block subsequent polling of card.run (it will poll paypal/gopay status, which we have already handled ourselves)
        raise _QrisHookSuccess(captured)

    # Early hook: Drive emulator GoPay automatic payment before wait_for_settlement
    # Make _wait_for_settlement see settlement immediately (instead of waiting for user to scan)
    _orig_wait = charger._wait_for_settlement

    def _wait_with_adb_auto(snap_token, sess_id):
        adb_cfg = (charger.qris_cfg.get("adb_auto") or {})
        if adb_cfg.get("enabled"):
            try:
                # Get the deeplink + pin of this charge
                deeplink = ""
                # captured is filled within _hook, here it is retrieved through charger's internal cache or re-queried
                # Simplification: Get from _last_charge_deeplink (set in hook)
                deeplink = getattr(charger, "_last_charge_deeplink", "") or ""
                pin = str(adb_cfg.get("pin") or "")
                if deeplink and len(pin) == 6 and pin.isdigit():
                    import sys as _sys
                    from pathlib import Path as _P
                    _ctf = str(_P(__file__).resolve().parent.parent)  # Wave E: CTF-pay/qris/ → CTF-pay/
                    if _ctf not in _sys.path:
                        _sys.path.insert(0, _ctf)
                    from adb.driver import GoPayAuto  # Wave G: gopay_adb.py → adb/driver.py
                    g = GoPayAuto(
                        serial=adb_cfg.get("serial") or None,
                        adb_port=adb_cfg.get("adb_port") or None,
                        log=charger.log,
                    )
                    charger.log("[qris] adb_auto → 驱动 emulator GoPay 自动支付…")
                    auto = g.pay_with_deeplink(
                        deeplink=deeplink,
                        pin=pin,
                        screenshot_dir=str(charger.output_dir / "adb_shots"),
                    )
                    charger.log(f"[qris] adb_auto 结果: state={auto.get('state')} msg={auto.get('message','')[:80]}")
            except Exception as e:
                charger.log(f"[qris] adb_auto 异常 (降级人工): {type(e).__name__}: {e}")
        return _orig_wait(snap_token, sess_id)

    charger._wait_for_settlement = _wait_with_adb_auto

    # Wave F (5/18) After card is a package: card/__init__.py + card/_monolith.py.
    # card.run/manual_approval internal bare call `_drive_gopay_from_redirect(...)` goes through
    # Looking up card._monolith module scope, patching `card.*` namespace doesn't work (will make QRIS
    # Downgrade to GoPay tokenization OTP linking, as fallback implementation still in place.
    # Must patch _monolith module scope to allow hook to truly take over.
    import card._monolith as _card_inner
    _card_inner._drive_gopay_from_redirect = _hook
    card._drive_gopay_from_redirect = _hook  # Compatible: any references going through the `card.*` namespace
    charger.log("[qris] monkey-patched card._monolith._drive_gopay_from_redirect → untokenized hook")

    try:
        card.run(
            checkout_input="auto",
            card_index=0,
            config_path=config_path,
            use_gopay=True,
        )
    except _QrisHookSuccess as e:
        return e.result
    raise QrisError("card.run 完成但 hook 未被调用（可能 approve blocked / coupon not eligible）")


def _run_mock_charge(charger: "QrisCharger") -> dict:
    """Offline mock: use built-in EMVCo specimens to walk _save_qr_artifacts + simulate settle after 5s,
    used to verify webui runner log parsing + frontend QR rendering. Do not modify OpenAI/Stripe/Midtrans."""
    import time as _t
    ref = "A2MOCK" + _dt.datetime.now().strftime("%Y%m%d%H%M%S") + "DEMO"
    # Real QRIS specimen (EMV QRCPS Merchant Presented, adapted version from OpenAI LLC GoPay acquirer)
    qr_string = (
        "00020101021126570011ID.DANA.WWW011893600914000000000004215abcdef"
        "520440005303360540510.005802ID5910OpenAI LLC6011Jakarta ID6304ABCD"
    )
    parsed = {
        "charge_ref": ref,
        "qr_string": qr_string,
        "qr_image_url": f"https://merchants-app.midtrans.com/v4/qris/gopay/{ref}/qr-code",
        "expiry_time": (_dt.datetime.now() + _dt.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S +0000"),
        "transaction_status": "pending",
        "raw": {"_mock": True},
    }
    charger.log("[qris] [MOCK] 跳过 ChatGPT/Stripe/Midtrans，使用内置 demo 数据")
    artifacts = charger._save_qr_artifacts(parsed)
    charger.log("─" * 64)
    charger.log(f"[qris] QR 已生成 reference={ref}")
    if artifacts.get("qr_png_path"):
        charger.log(f"[qris] PNG: {artifacts['qr_png_path']} ({artifacts.get('qr_png_source')})")
    if artifacts.get("qr_string_path"):
        charger.log(f"[qris] qr_string: {artifacts['qr_string_path']}")
    if artifacts.get("qr_image_url"):
        charger.log(f"[qris] 远端预览: {artifacts['qr_image_url']}")
    charger.log(f"[qris] 过期: {parsed['expiry_time']}")
    charger.log(f"[qris] meta: {artifacts['meta_path']}")
    charger.log(f"[qris] DEEPLINK: https://gopay.co.id/app/merchanttransfer?demo=mock&ref={ref}")
    charger.log("─" * 64)
    charger._print_qr_ascii(qr_string)
    charger.log("─" * 64)
    charger.log("[qris] [MOCK] 等 5s 模拟用户扫码入账 ...")
    _t.sleep(5)
    charger.log("[qris] settled (mock)")
    return {
        "state": "succeeded",
        "snap_token": "mock-snap-token",
        "charge_ref": ref,
        "settled_via": "mock",
        "midtrans_status": {"transaction_status": "settlement", "_mock": True},
        "verify": {"state": "mock_skipped"},
        "artifacts": artifacts,
    }


def _render_qr_png(qr_string: str, save_path: Path) -> None:
    """Generate PNG from EMVCo QR payload using qrcode library."""
    if not _QRCODE_AVAILABLE or not _PIL_AVAILABLE:
        raise QrisError("qrcode/Pillow 未安装，无法本地生成 PNG。pip install 'qrcode[pil]'")
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(str(save_path))


# ──────────────────────────── CLI entry ───────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="ChatGPT Plus 订阅 via QRIS（Midtrans QR 扫码即付，无需 OTP/PIN/绑定）",
    )
    parser.add_argument("--config", required=True, help="CTF-pay config json (qris block)")
    parser.add_argument("--json-result", action="store_true",
                        help="Emit QRIS_RESULT_JSON=... line on success")
    parser.add_argument("--from-redirect-url", default="", metavar="URL",
                        help="半自动模式：从 pm-redirects.stripe.com URL 接管 Midtrans+QRIS")
    parser.add_argument("--cs-id", default="", help="可选：cs_live_xxx，verify 阶段用")
    parser.add_argument("--output-dir", default="",
                        help="覆盖 config.qris.output_dir（QR 文件落盘目录）")
    parser.add_argument("--mock-charge", action="store_true",
                        help="离线 mock：跳过 ChatGPT/Stripe/Midtrans，用内置 EMVCo QR payload 跑 "
                             "QR 生成 + runner state 接管，验证前端集成。settle 在 5s 后自动触发。"
                             "也可通过环境变量 QRIS_MOCK=1 激活（pipeline.py spawn 时方便）")
    parser.add_argument("--legacy-direct", action="store_true",
                        help="（不推荐）走 qris.py 自己的 stripe→approve 简化版，OpenAI 新反欺诈下"
                             "100%% result=blocked。默认用 card.py 完整 fresh_checkout 路径")
    args = parser.parse_args()
    if not args.mock_charge and os.getenv("QRIS_MOCK", "").strip() in ("1", "true", "yes"):
        args.mock_charge = True

    cfg = _load_cfg(args.config)
    qris_cfg = cfg.get("qris") or {}
    if args.output_dir:
        qris_cfg = {**qris_cfg, "output_dir": args.output_dir}

    auth_cfg = (cfg.get("fresh_checkout") or {}).get("auth") or {}
    if args.mock_charge:
        # mock mode doesn't send any real requests, just provide an empty session as a placeholder (GoPayCharger.__init__ needs an object)
        cs_session = requests.Session()
    else:
        try:
            cs_session = _build_chatgpt_session(auth_cfg)
        except GoPayError as e:
            print(f"[error] {e}", file=sys.stderr)
            sys.exit(2)

    proxy_url = (cfg.get("proxy") or "").strip() or None
    stripe_pk = (
        (cfg.get("stripe") or {}).get("publishable_key")
        or auth_cfg.get("stripe_pk")
        or DEFAULT_STRIPE_PK
    )
    billing = cfg.get("billing") or {}

    if not _QRCODE_AVAILABLE:
        print(
            "[warn] python-qrcode 未安装，将跳过本地 PNG / ASCII 渲染。"
            " 安装: pip install 'qrcode[pil]'",
            file=sys.stderr,
        )

    charger = QrisCharger(
        cs_session, qris_cfg,
        proxy=proxy_url,
        runtime_cfg=cfg.get("runtime"),
    )
    try:
        if args.mock_charge:
            result = _run_mock_charge(charger)
        elif args.from_redirect_url:
            print(f"[qris] semi-auto mode: starting from {args.from_redirect_url[:80]}...")
            result = charger.run_from_redirect(args.from_redirect_url, cs_id=args.cs_id)
        elif args.legacy_direct:
            result = charger.run(stripe_pk=stripe_pk, billing=billing)
        else:
            # Default flow through card.py (fresh_checkout + manual_approval beta + check_coupon)
            # Then hook takes over untokenized charge to output QR + deeplink
            result = _run_via_card(charger, args.config, cs_id_hint=args.cs_id)
    except QrisError as e:
        print(f"[qris] FAILED: {e}", file=sys.stderr)
        if args.json_result:
            print(f"QRIS_RESULT_JSON={json.dumps({'state':'failed','error':str(e)})}")
        sys.exit(1)
    except GoPayError as e:
        print(f"[qris] FAILED (bootstrap): {e}", file=sys.stderr)
        if args.json_result:
            print(f"QRIS_RESULT_JSON={json.dumps({'state':'failed','error':str(e)})}")
        sys.exit(1)

    print(f"[qris] result: {result.get('state')} via {result.get('settled_via')}")
    if args.json_result:
        # Remove the raw field to avoid JSON being too large
        compact = {k: v for k, v in result.items() if k != "raw"}
        if isinstance(compact.get("midtrans_status"), dict):
            compact["midtrans_status"] = {
                k: v for k, v in compact["midtrans_status"].items()
                if k in ("transaction_status", "fraud_status", "transaction_id",
                         "transaction_time", "settlement_time", "issuer", "acquirer")
            }
        print(f"QRIS_RESULT_JSON={json.dumps(compact, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
