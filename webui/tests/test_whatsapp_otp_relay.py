"""Tests for CTF-pay/whatsapp_otp_relay.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PAY_DIR = ROOT / "CTF-pay"
if str(PAY_DIR) not in sys.path:
    sys.path.insert(0, str(PAY_DIR))

SPEC = importlib.util.spec_from_file_location(
    "wa_relay_mod",
    PAY_DIR / "whatsapp_otp_relay.py",
)
wa_relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wa_relay)  # type: ignore[union-attr]


def test_iter_events_cloud_api_no_duplicate_generic_fallback():
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": "6281234567890",
                        "timestamp": "1777740000",
                        "type": "text",
                        "text": {"body": "Kode verifikasi GoPay Anda 445566"},
                    }],
                },
            }],
        }],
    }

    events = list(wa_relay._iter_events(payload))

    assert len(events) == 1
    assert events[0]["source"] == "whatsapp_cloud_api"
    assert events[0]["from"] == "6281234567890"
    assert events[0]["text"] == "Kode verifikasi GoPay Anda 445566"


def test_iter_events_generic_ingest():
    events = list(wa_relay._iter_events({
        "from": "gopay",
        "text": "Kode verifikasi GoPay Anda 112233",
    }))

    assert len(events) == 1
    assert events[0]["source"] == "generic"
    assert events[0]["from"] == "gopay"
