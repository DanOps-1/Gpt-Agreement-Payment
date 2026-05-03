#!/usr/bin/env python3
"""Small WhatsApp OTP relay for GoPay.

This process does not log in to WhatsApp by itself. It receives messages from a
user-controlled source (for example Meta WhatsApp Cloud API webhook, an Android
notification forwarder, or another local WhatsApp bridge), extracts the latest
OTP, stores it in SQLite runtime_meta[wa_state], and exposes `/latest` for
`gopay.py` to poll.

Examples:

  # Start local relay
  python CTF-pay/whatsapp_otp_relay.py --port 8765

  # Test with a generic POST
  curl -X POST http://127.0.0.1:8765/ingest \
    -H 'Content-Type: application/json' \
    -d '{"from":"gopay","text":"Kode verifikasi GoPay Anda 123456"}'

  # GoPay config:
  # "gopay": {
  #   "otp": {"source": "http", "url": "http://127.0.0.1:8765/latest"}
  # }
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from gopay import (  # local module
    DEFAULT_OTP_REGEX,
    _extract_otp_from_payload,
    _parse_payload_timestamp,
)


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webui.backend.db import get_db  # noqa: E402

DEFAULT_STATE_KEY = "wa_state"
DEFAULT_LOG_FILE = ROOT / "output" / "wa_relay.log"

_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _load_state(state_key: str = DEFAULT_STATE_KEY) -> dict:
    data = get_db().get_runtime_json(state_key, {"latest": None, "history": []})
    if isinstance(data, dict):
        data.setdefault("history", [])
        return data
    return {"latest": None, "history": []}


def _save_state(data: dict, state_key: str = DEFAULT_STATE_KEY) -> None:
    get_db().set_runtime_json(state_key, data if isinstance(data, dict) else {})


def _append_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def _event_from_generic_dict(obj: dict) -> dict | None:
    text = (
        obj.get("otp")
        or obj.get("code")
        or obj.get("text")
        or obj.get("body")
        or obj.get("message")
        or obj.get("content")
    )
    if isinstance(text, dict):
        text = text.get("body") or text.get("text") or text.get("message")
    if text in (None, ""):
        return None
    ts = None
    for key in ("ts", "timestamp", "time", "created_at", "received_at", "date"):
        ts = _parse_payload_timestamp(obj.get(key))
        if ts is not None:
            break
    return {
        "from": obj.get("from") or obj.get("sender") or obj.get("wa_id") or "",
        "text": str(text),
        "ts": ts or _now(),
        "source": "generic",
    }


def _iter_events(payload: Any) -> Any:
    """Yield normalized message events from Cloud API-like or generic payloads."""
    if isinstance(payload, dict):
        # Meta WhatsApp Cloud API webhook:
        # entry[].changes[].value.messages[].text.body
        entries = payload.get("entry")
        if isinstance(entries, list):
            for entry in entries:
                for change in (entry or {}).get("changes", []) or []:
                    value = (change or {}).get("value") or {}
                    for msg in value.get("messages", []) or []:
                        text = ""
                        msg_type = msg.get("type")
                        if msg_type == "text":
                            text = ((msg.get("text") or {}).get("body") or "")
                        elif msg_type == "button":
                            text = ((msg.get("button") or {}).get("text") or "")
                        elif msg_type == "interactive":
                            interactive = msg.get("interactive") or {}
                            text = json.dumps(interactive, ensure_ascii=False)
                        else:
                            text = json.dumps(msg, ensure_ascii=False)
                        yield {
                            "from": msg.get("from") or "",
                            "text": text,
                            "ts": _parse_payload_timestamp(msg.get("timestamp")) or _now(),
                            "source": "whatsapp_cloud_api",
                            "message_id": msg.get("id") or "",
                        }
            return

        generic = _event_from_generic_dict(payload)
        if generic:
            yield generic
            return
        for value in payload.values():
            yield from _iter_events(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_events(item)
    elif isinstance(payload, str):
        yield {"from": "", "text": payload, "ts": _now(), "source": "text"}


def _store_events(
    events: list[dict],
    *,
    state_key: str,
    log_file: Path,
    history_limit: int,
    code_regex: str,
) -> int:
    stored = 0
    with _lock:
        state = _load_state(state_key)
        history = state.setdefault("history", [])
        for event in events:
            code = _extract_otp_from_payload(event, code_regex=code_regex)
            if not code:
                continue
            item = {
                "otp": code,
                "ts": float(event.get("ts") or _now()),
                "from": event.get("from") or "",
                "source": event.get("source") or "",
                "message_id": event.get("message_id") or "",
                "text": str(event.get("text") or "")[:500],
            }
            state["latest"] = item
            history.append(item)
            stored += 1
            _append_log(
                log_file,
                f"{int(item['ts'])} source={item['source']} from={item['from']} otp={item['otp']}",
            )
        if len(history) > history_limit:
            state["history"] = history[-history_limit:]
        _save_state(state, state_key)
    return stored


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _text_response(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
    raw = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class RelayHandler(BaseHTTPRequestHandler):
    state_key: str = DEFAULT_STATE_KEY
    log_file: Path = DEFAULT_LOG_FILE
    verify_token: str = ""
    history_limit: int = 50
    code_regex: str = DEFAULT_OTP_REGEX

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802 (stdlib API)
        sys.stderr.write("[wa-relay] " + (fmt % args) + "\n")

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path in ("/healthz", "/health"):
            _json_response(self, 200, {"ok": True})
            return

        if parsed.path == "/webhook" and qs.get("hub.mode", [""])[0] == "subscribe":
            token = qs.get("hub.verify_token", [""])[0]
            challenge = qs.get("hub.challenge", [""])[0]
            if self.verify_token and token != self.verify_token:
                _text_response(self, 403, "verify token mismatch")
                return
            _text_response(self, 200, challenge)
            return

        if parsed.path == "/latest":
            since = _parse_payload_timestamp(qs.get("since", [""])[0]) or 0.0
            with _lock:
                latest = (_load_state(self.state_key).get("latest") or {})
            if not latest:
                self.send_response(204)
                self.end_headers()
                return
            if since and float(latest.get("ts") or 0.0) < since:
                self.send_response(204)
                self.end_headers()
                return
            _json_response(self, 200, latest)
            return

        _json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib API)
        parsed = urlparse(self.path)
        if parsed.path not in ("/webhook", "/ingest", "/"):
            _json_response(self, 404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length)
        text = raw.decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(text)
        except Exception:
            payload = text

        events = list(_iter_events(payload))
        stored = _store_events(
            events,
            state_key=self.state_key,
            log_file=self.log_file,
            history_limit=self.history_limit,
            code_regex=self.code_regex,
        )
        _json_response(self, 200, {"ok": True, "events": len(events), "stored": stored})


def main() -> None:
    parser = argparse.ArgumentParser(description="Local WhatsApp OTP relay for GoPay")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-key", default=DEFAULT_STATE_KEY, help="SQLite runtime_meta key")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_FILE))
    parser.add_argument("--verify-token", default="", help="Meta webhook verify token")
    parser.add_argument("--history", type=int, default=50)
    parser.add_argument("--code-regex", default=DEFAULT_OTP_REGEX)
    args = parser.parse_args()

    RelayHandler.state_key = args.state_key
    RelayHandler.log_file = Path(args.log_file).expanduser()
    RelayHandler.verify_token = args.verify_token
    RelayHandler.history_limit = max(1, args.history)
    RelayHandler.code_regex = args.code_regex

    RelayHandler.log_file.parent.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), RelayHandler)
    print(
        f"[wa-relay] listening http://{args.host}:{args.port} "
        f"state=sqlite:runtime_meta/{RelayHandler.state_key}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[wa-relay] stopped", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
