"""GoPay phone link-state tracker.

Tracks whether a configured GoPay phone has an active link on GoPay's
servers. Once a phone has a successful charge, GoPay treats it as bound to
the merchant account and rejects subsequent linking attempts with
406 ``account already linked``. To avoid hitting that 406 path, this module
keeps a record of the linked state in SQLite (``runtime_meta``) and exposes
mark/query helpers consumed by the runner pre-flight gate, the WebUI, and an
external HTTP API:

- ``mark_linked(phone, ...)`` — called by the runner when it sees
  ``[gopay] charge settled``.
- ``mark_unlinked(phone, ...)`` — called by an external service after it has
  manually unlinked the phone on GoPay's side.
- ``is_linked(phone)`` / ``get_status(phone)`` / ``list_all()`` — read paths.

Storage is one runtime_meta key (``gopay_link_state``) holding a dict keyed
by digits-only phone (country code + phone number concatenated).
"""
from __future__ import annotations

import time
from typing import Any

from .db import get_db


_KEY = "gopay_link_state"


def _normalize(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def _read_all() -> dict[str, dict]:
    raw = get_db().get_runtime_json(_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _write_all(data: dict[str, dict]) -> None:
    get_db().set_runtime_json(_KEY, data if isinstance(data, dict) else {})


def _entry(phone: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    return {
        "phone": phone,
        "linked": bool(raw.get("linked")),
        "linked_at": raw.get("linked_at"),
        "unlinked_at": raw.get("unlinked_at"),
        "payment_ref": raw.get("payment_ref") or "",
        "last_changed_by": raw.get("last_changed_by") or "",
    }


def get_status(phone: str) -> dict:
    p = _normalize(phone)
    if not p:
        return _entry("", {})
    return _entry(p, _read_all().get(p))


def is_linked(phone: str) -> bool:
    return bool(get_status(phone).get("linked"))


def mark_linked(phone: str, *, payment_ref: str = "", source: str = "pipeline") -> dict:
    p = _normalize(phone)
    if not p:
        raise ValueError("phone is empty")
    all_data = _read_all()
    prev = all_data.get(p) if isinstance(all_data.get(p), dict) else {}
    all_data[p] = {
        "linked": True,
        "linked_at": time.time(),
        "unlinked_at": None,
        "payment_ref": str(payment_ref or "") or prev.get("payment_ref") or "",
        "last_changed_by": source or "pipeline",
    }
    _write_all(all_data)
    return _entry(p, all_data[p])


def mark_unlinked(phone: str, *, source: str = "external") -> dict:
    p = _normalize(phone)
    if not p:
        raise ValueError("phone is empty")
    all_data = _read_all()
    prev = all_data.get(p) if isinstance(all_data.get(p), dict) else {}
    all_data[p] = {
        "linked": False,
        "linked_at": prev.get("linked_at"),
        "unlinked_at": time.time(),
        "payment_ref": prev.get("payment_ref") or "",
        "last_changed_by": source or "external",
    }
    _write_all(all_data)
    return _entry(p, all_data[p])


def list_all() -> list[dict]:
    return [_entry(phone, raw) for phone, raw in sorted(_read_all().items())]


def set_status(phone: str, linked: bool, *, source: str = "manual", payment_ref: str = "") -> dict:
    """通用双向写入。linked=True 走 mark_linked，False 走 mark_unlinked。

    `payment_ref` 仅在 linked=True 时尊重；linked=False 保留旧值。
    """
    if linked:
        return mark_linked(phone, payment_ref=payment_ref, source=source)
    return mark_unlinked(phone, source=source)


def reset(phone: str = "") -> None:
    """Test/admin helper: drop a single phone entry, or all entries when phone=''."""
    if not phone:
        get_db().delete_runtime_key(_KEY)
        return
    p = _normalize(phone)
    if not p:
        return
    all_data = _read_all()
    if p in all_data:
        all_data.pop(p, None)
        _write_all(all_data)


def phone_from_gopay_config(cfg: Any) -> str:
    """Build the canonical phone key from a parsed pay config dict.

    Returns "" when the config has no usable gopay block.
    """
    if not isinstance(cfg, dict):
        return ""
    gp = cfg.get("gopay")
    if not isinstance(gp, dict):
        return ""
    cc = _normalize(gp.get("country_code") or "")
    pn = _normalize(gp.get("phone_number") or "")
    if not cc or not pn:
        return ""
    return cc + pn
