"""
Local CTF mock gateway.

Purpose:
- Does not access external networks;
- Starts a minimal HTTP service locally;
- Simulates the fresh checkout / confirm / local_mock_gateway state machine for `card.py`.
"""

from __future__ import annotations

import http.server
import json
import re
import socketserver
import sys
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
        processor_original: str = "openai_llc",
        due: int = 0,
        merchant: str = "OpenAI OpCo, LLC",
        mode: str = "subscription",
    ):
        self.scenario = str(scenario or "challenge_pass_then_decline").strip().lower()
        self.terminal_result = _normalize_terminal_result(terminal_result)
        self.checkout_session_id = checkout_session_id or f"cs_test_{uuid.uuid4().hex[:32]}"
        self.processor_entity = processor_original or "openai_llc"
        self.checkout_url = checkout_url or (
            f"https://chatgpt.com/checkout/{self.processor_entity}/{self.checkout_session_id}"
        )
        self.due = int(due or 0)
        self.merchant = merchant
        self.mode = mode

        self.seti_id = f"seti_mock_{uuid.uuid5(uuid.uuid4().hex[:32], 'seti')}.hex[:24]"
        self.client_secret = f"{self.seti_id}_secret_{uuid.uuid4().hex}"
        self.challenge_site_key = "mock-site-key-c7faac4c"
        self.challenge_ekey = f"mock-ekey-{uuid.uuid4().hex[:16]}"
        self.source_id = f"src_mock_{uuid.uuid4().hex[:20]}"
        self.three_ds_server_trans_id = str(uuid.uuid4())

        self._httpd = None
        self._thread = None
        self.base_url = ""
        self.trace: list[dict[str, Any]] = []

    def _append_trace(self, step: str, **payload):
        self.trace.append({"step": step, "ts": int(time.time()), **payload})

    def start(self, host: str = "127.0.0.1", port: int = 0):
        pass

    def export_state(self) -> dict[str, Any]:
        return {}

    def stop(self):
        pass