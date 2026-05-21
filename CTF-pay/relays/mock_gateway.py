"""
本地 CTF mock gateway。

用途：
- 不访问外部网络；
- 在本机起一个极简 HTTP 服务；
- 模拟 fresh checkout / confirm / verify_challenge / 3DS2 / poll 这条状态机；
- 便于 `card.py` 走“真实 HTTP 请求 -> 本地服务”的可回放链路。
"""

from __future__ import annotations

import http.server
import json
import re
import socketserver
import threading
import time
import urllib.parse
import uuid
from typing import Any


def _normalize_terminal_result(payload: dict | None) -> dict:
    data = json.loads(json.dumps(payload or {}))
    data.setdefault("source_kind", "setup_intent")
    data.setdefault("payment_object_status", "requires_payment_method")
    err = data.setdefault("error", {})
    err.setdefault("code", "card_declined")
    err.setdefault("decline_code", "generic_decline")
    err.setdefault("message", "Your card was declined.")
    return data


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class LocalMockGateway:
    def __init__(
        self,
        *,
        scenario: str,
        terminal_result: dict | None = None,
        checkout_url: str = "",
        checkout_session_id: str = "",
        processor_entity: str = "openai_llc",
        due: int = 0,
        merchant: str = "OpenAI OpCo, LLC",
        mode: str = "subscription",
    ):
        self.scenario = str(scenario or "challenge_pass_then_decline").strip().lower()
        self.terminal_result = _normalize_terminal_result(terminal_result)
        self.checkout_session_id = checkout_session_id or f"cs_test_{uuid.uuid4().hex[:32]}"
        self.processor_entity = processor_entity or "openai_llc"
        self.checkout_url = checkout_url or (
            f"https://chatgpt.com/checkout/{self.processor_entity}/{self.checkout_session_id}"
        )
        self.due = int(due or 0)
        self.merchant = merchant
        self.mode = mode

        self.seti_id = f"seti_mock_{uuid.uuid4().hex[:24]}"
        self.client_secret = f"{self.seti_id}_secret_{uuid.uuid4().hex}"
        self.challenge_site_key = "mock-site-key-c7faac4c"
        self.challenge_ekey = f"mock-ekey-{uuid.uuid4().hex[:16]}"
        self.challenge_token = f"mock-token-{uuid.uuid4().hex}"
        self.source_id = f"src_mock_{uuid.uuid4().hex[:20]}"
        self.three_ds_server_trans_id = str(uuid.uuid4())
        self.created_at = int(time.time())

        self._httpd = None
        self._thread = None
        self.base_url = ""
        self.trace: list[dict[str, Any]] = []
        self.last_checkout_payload: dict[str, Any] = {}
        self.last_confirm_payload: dict[str, Any] = {}
        self.last_verify_payload: dict[str, Any] = {}
        self.last_authenticate_payload: dict[str, Any] = {}

    def _append_trace(self, step: str, **payload):
        self.trace.append(
            {
                "step": step,
                "ts": int(time.time()),
                **payload,
            }
        )

    def export_state(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "base_url": self.base_url,
            "checkout_url": self.checkout_url,
            "checkout_session_id": self.checkout_session_id,
            "processor_entity": self.processor_entity,
            "seti_id": self.seti_id,
            "client_secret": self.client_secret,
            "trace": list(self.trace),
            "terminal_result": self.terminal_result,
            "last_checkout_payload": self.last_checkout_payload,
            "last_confirm_payload": self.last_confirm_payload,
            "last_verify_payload": self.last_verify_payload,
            "last_authenticate_payload": self.last_authenticate_payload,
        }

    def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("content-length") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return {"_raw": raw.decode("utf-8", errors="replace")}

            def _send(self, payload: dict[str, Any], status: int = 200):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                if path == "/healthz":
                    return self._send({"ok": True, "scenario": gateway.scenario})
                if path == "/trace":
                    return self._send(gateway.export_state())

                m = re.fullmatch(r"/v1/checkout/sessions/([^/]+)/init", path)
                if m:
                    gateway._append_trace("init", session_id=m.group(1))
                    return self._send(
                        {
                            "session_id": gateway.checkout_session_id,
                            "merchant": gateway.merchant,
                            "mode": gateway.mode,
                            "payment_method_types": ["card"],
                            "confirm_mode": "inline_payment_method_data",
                            "total_summary": {
                                "amount": gateway.due,
                                "due": gateway.due,
                                "currency": "usd",
                            },
                        }
                    )

                m = re.fullmatch(r"/v1/setup_intents/([^/]+)$", path)
                if m:
                    gateway._append_trace("setup_intent_retrieve", seti_id=m.group(1))
                    return self._send(
                        {
                            "object": "setup_intent",
                            "id": gateway.seti_id,
                            "client_secret": gateway.client_secret,
                            "status": gateway.terminal_result.get("payment_object_status", "requires_payment_method"),
                            "last_setup_error": gateway.terminal_result.get("error", {}),
                        }
                    )

                m = re.fullmatch(r"/v1/checkout/sessions/([^/]+)/poll", path)
                if m:
                    gateway._append_trace("poll", session_id=m.group(1))
                    return self._send(
                        {
                            "object": "checkout_session",
                            "id": gateway.checkout_session_id,
                            "state": "failed",
                            "payment_status": gateway.terminal_result.get("payment_object_status", "requires_payment_method"),
                            "terminal_result": gateway.terminal_result,
                        }
                    )

                return self._send({"error": {"message": f"unknown GET path: {path}"}}, status=404)

            def do_POST(self):
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                payload = self._read_json()

                if path == "/backend-api/payments/checkout":
                    gateway.last_checkout_payload = payload
                    gateway._append_trace("fresh_checkout", payload=payload)
                    return self._send(
                        {
                            "checkout_session_id": gateway.checkout_session_id,
                            "processor_entity": gateway.processor_entity,
                            "checkout_url": gateway.checkout_url,
                            "amount_due": gateway.due,
                            "currency": "USD",
                            "status": "open",
                        }
                    )

                m = re.fullmatch(r"/v1/setup_intents/([^/]+)/confirm", path)
                if m:
                    gateway.last_confirm_payload = payload
                    gateway._append_trace("confirm", seti_id=m.group(1), payload=payload)
                    if gateway.scenario in {"no_3ds_card_declined", "direct_decline"}:
                        return self._send(
                            {
                                "object": "setup_intent",
                                "id": gateway.seti_id,
                                "client_secret": gateway.client_secret,
                                "status": gateway.terminal_result.get("payment_object_status", "requires_payment_method"),
                                "last_setup_error": gateway.terminal_result.get("error", {}),
                            }
                        )
                    return self._send(
                        {
                            "object": "setup_intent",
                            "id": gateway.seti_id,
                            "client_secret": gateway.client_secret,
                            "status": "requires_action",
                            "next_action": {
                                "type": "captcha_challenge",
                                "captcha_challenge": {
                                    "site_key": gateway.challenge_site_key,
                                    "ekey": gateway.challenge_ekey,
                                    "rqdata": "mock-rqdata",
                                },
                            },
                        }
                    )

                m = re.fullmatch(r"/v1/setup_intents/([^/]+)/verify_challenge", path)
                if m:
                    gateway.last_verify_payload = payload
                    gateway._append_trace("verify_challenge", seti_id=m.group(1), payload=payload)
                    if gateway.scenario == "challenge_failed":
                        return self._send(
                            {
                                "object": "setup_intent",
                                "id": gateway.seti_id,
                                "client_secret": gateway.client_secret,
                                "status": "requires_payment_method",
                                "last_setup_error": {
                                    "code": "setup_intent_authentication_failure",
                                    "message": "Captcha challenge failed. Try again with a different payment method.",
                                },
                            }
                        )
                    gateway._append_trace("network_checkcaptcha", pass_result=True)
                    return self._send(
                        {
                            "object": "setup_intent",
                            "id": gateway.seti_id,
                            "client_secret": gateway.client_secret,
                            "status": "requires_action",
                            "next_action": {
                                "type": "use_stripe_sdk",
                                "use_stripe_sdk": {
                                    "type": "three_d_secure_redirect",
                                    "source": gateway.source_id,
                                    "server_transaction_id": gateway.three_ds_server_trans_id,
                                },
                            },
                        }
                    )

                if path == "/v1/3ds2/authenticate":
                    gateway.last_authenticate_payload = payload
                    gateway._append_trace("3ds2_authenticate", payload=payload, state="succeeded", trans_status="Y")
                    return self._send(
                        {
                            "state": "succeeded",
                            "source": gateway.source_id,
                            "ares": {"transStatus": "Y"},
                            "three_ds_server_trans_id": gateway.three_ds_server_trans_id,
                        }
                    )

                return self._send({"error": {"message": f"unknown POST path: {path}"}}, status=404)

        self._httpd = _ThreadingTCPServer((host, port), Handler)
        self.base_url = f"http://{host}:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self._append_trace("server_started", base_url=self.base_url)
        return self.base_url

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._append_trace("server_stopped")
            self._httpd = None

