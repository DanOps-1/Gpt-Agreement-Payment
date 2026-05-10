"""Account planning pool service.

The pool is intentionally separate from the legacy inventory tables.  Existing
records are copied in only when the user explicitly runs a migration action.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from .db import get_db


ROTATION_CONFIG_KEY = "account_pool_rotation_config"


POOL_STATUS_LABELS = {
    "email_unused": "未激活池",
    "plus_with_rt": "已激活池",
    "plus_missing_rt": "待检测池",
    "registered_pending_plus": "待激活池",
    "registration_failed": "失败池",
    "in_progress": "任务中",
    "quarantined": "已隔离",
}

VISIBLE_POOL_STATUSES = [
    "email_unused",
    "plus_with_rt",
    "plus_missing_rt",
    "registered_pending_plus",
    "registration_failed",
]

_ITEM_COLUMNS = [
    "id",
    "email",
    "email_password",
    "mail_client_id",
    "mail_refresh_token",
    "email_source",
    "import_batch",
    "chatgpt_email",
    "account_password",
    "session_token",
    "access_token",
    "id_token",
    "device_id",
    "csrf_token",
    "cookie_header",
    "refresh_token",
    "account_id",
    "team_account_id",
    "team_gpt_account_pk",
    "invite_permission",
    "plan_type",
    "payment_status",
    "payment_channel",
    "payment_session_id",
    "email_domain",
    "pool_status",
    "task_id",
    "round_id",
    "attempt_count",
    "last_stage",
    "last_error",
    "source_registered_account_id",
    "source_card_result_id",
    "source_outlook_mail_id",
    "reserved_at",
    "registered_at",
    "activated_at",
    "rt_obtained_at",
    "failed_at",
    "created_at",
    "updated_at",
]

_WRITE_COLUMNS = [col for col in _ITEM_COLUMNS if col != "id"]
_SECRET_COLUMNS = {
    "email_password",
    "mail_refresh_token",
    "account_password",
    "session_token",
    "access_token",
    "id_token",
    "csrf_token",
    "cookie_header",
    "refresh_token",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _email(value: Any) -> str:
    return _text(value).strip().lower()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask(value: Any, keep: int = 4) -> str:
    text = _text(value)
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}...{text[-keep:]}"


def _public_item(row: dict, *, reveal: bool = False) -> dict:
    item = dict(row)
    for key in _SECRET_COLUMNS:
        raw = _text(item.get(key))
        item[f"has_{key}"] = bool(raw)
        if not reveal:
            item[key] = _mask(raw)
    item["status_label"] = POOL_STATUS_LABELS.get(item.get("pool_status"), item.get("pool_status") or "")
    item["primary_email"] = item.get("chatgpt_email") or item.get("email") or ""
    return item


def _event_payload(payload: dict | None) -> str:
    if not payload:
        return ""
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:4000]
    except Exception:
        return ""


def _parse_email_line(line: str) -> dict | None:
    text = _text(line).strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split("----")]
    if len(parts) >= 4:
        email, password, client_id, refresh_token = parts[:4]
        if email:
            return {
                "email": email,
                "email_password": password,
                "mail_client_id": client_id,
                "mail_refresh_token": refresh_token,
            }
    parts = [p.strip() for p in text.split(",")]
    if len(parts) >= 4 and "@" in parts[0]:
        return {
            "email": parts[0],
            "email_password": parts[1],
            "mail_client_id": parts[2],
            "mail_refresh_token": parts[3],
        }
    if "@" in text:
        return {"email": text}
    return None


def _event(c, item_id: int, *, from_status: str = "", to_status: str, stage: str = "",
           reason: str = "", task_id: str = "", round_id: str = "", payload: dict | None = None) -> None:
    c.execute(
        """
        INSERT INTO account_pool_events(
          item_id, ts, from_status, to_status, stage, reason, task_id, round_id, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(item_id),
            time.time(),
            _text(from_status),
            _text(to_status),
            _text(stage),
            _text(reason)[:500],
            _text(task_id),
            _text(round_id),
            _event_payload(payload),
        ),
    )


def _insert_or_update_item(c, item: dict, *, reason: str, stage: str = "upsert") -> str:
    now = time.time()
    email = _email(item.get("email") or item.get("chatgpt_email"))
    if not email:
        return "invalid"
    item = dict(item)
    item["email"] = email
    item.setdefault("pool_status", "email_unused")
    item.setdefault("created_at", now)
    item["updated_at"] = now

    existing = c.execute(
        "SELECT * FROM account_pool_items WHERE lower(email) = ?",
        (email,),
    ).fetchone()
    if existing:
        existing_d = dict(existing)
        updates: dict[str, Any] = {}
        for col in _WRITE_COLUMNS:
            if col in ("created_at", "attempt_count"):
                continue
            value = item.get(col)
            if value is None:
                continue
            if col not in ("pool_status", "last_error") and _text(value) == "":
                continue
            updates[col] = value
        updates["updated_at"] = now
        if not updates:
            return "skipped"
        if (
            stage == "email_import"
            and updates.get("pool_status") == "email_unused"
            and existing_d.get("pool_status") != "email_unused"
        ):
            updates.pop("pool_status", None)
        set_sql = ", ".join(f"{col} = ?" for col in updates)
        c.execute(
            f"UPDATE account_pool_items SET {set_sql} WHERE id = ?",
            [*updates.values(), existing_d["id"]],
        )
        to_status = _text(updates.get("pool_status") or existing_d.get("pool_status"))
        _event(
            c,
            int(existing_d["id"]),
            from_status=_text(existing_d.get("pool_status")),
            to_status=to_status,
            stage=stage,
            reason=reason,
            task_id=_text(updates.get("task_id") or existing_d.get("task_id")),
            round_id=_text(updates.get("round_id") or existing_d.get("round_id")),
            payload={"updated_fields": sorted(updates.keys())},
        )
        return "updated"

    cols = [col for col in _WRITE_COLUMNS if col in item or col in ("created_at", "updated_at")]
    values = [item.get(col, now if col in ("created_at", "updated_at") else "") for col in cols]
    placeholders = ", ".join("?" for _ in cols)
    cur = c.execute(
        f"INSERT INTO account_pool_items({', '.join(cols)}) VALUES ({placeholders})",
        values,
    )
    item_id = int(cur.lastrowid)
    _event(
        c,
        item_id,
        to_status=_text(item.get("pool_status") or "email_unused"),
        stage=stage,
        reason=reason,
        task_id=_text(item.get("task_id")),
        round_id=_text(item.get("round_id")),
        payload={"email": email},
    )
    return "created"


def import_email_lines(lines: list[str], *, source: str = "manual", import_batch: str = "") -> dict:
    parsed = [item for item in (_parse_email_line(line) for line in lines) if item]
    counts = {"parsed": len(parsed), "created": 0, "updated": 0, "skipped": 0, "invalid": 0}
    with get_db()._conn() as c:
        for item in parsed:
            item["email_source"] = source
            item["import_batch"] = import_batch or _now_iso()
            item["pool_status"] = "email_unused"
            result = _insert_or_update_item(c, item, reason="manual email import", stage="email_import")
            counts[result] = counts.get(result, 0) + 1
    counts["total"] = count_items()
    return counts


def count_items() -> int:
    with get_db()._conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM account_pool_items").fetchone()[0])


def list_pool_items(*, status: str = "all", query: str = "", limit: int = 200,
                    offset: int = 0, reveal: bool = False) -> dict:
    params: list[Any] = []
    where: list[str] = []
    if status and status != "all":
        where.append("pool_status = ?")
        params.append(status)
    q = _text(query).strip().lower()
    if q:
        where.append("(lower(email) LIKE ? OR lower(chatgpt_email) LIKE ? OR lower(account_id) LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit or 200), 1000))
    offset = max(0, int(offset or 0))

    with get_db()._conn() as c:
        rows = c.execute(
            f"""
            SELECT {', '.join(_ITEM_COLUMNS)}
            FROM account_pool_items
            {where_sql}
            ORDER BY updated_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        total = int(c.execute(f"SELECT COUNT(*) FROM account_pool_items {where_sql}", params).fetchone()[0])
        counts_rows = c.execute(
            "SELECT pool_status, COUNT(*) n FROM account_pool_items GROUP BY pool_status"
        ).fetchall()
    counts = {key: 0 for key in [*VISIBLE_POOL_STATUSES, "in_progress", "quarantined"]}
    for row in counts_rows:
        counts[str(row["pool_status"])] = int(row["n"])
    return {
        "generated_at": _now_iso(),
        "statuses": [{"key": key, "label": POOL_STATUS_LABELS[key]} for key in VISIBLE_POOL_STATUSES],
        "counts": counts,
        "total": total,
        "items": [_public_item(dict(row), reveal=reveal) for row in rows],
    }


def get_pool_item(item_id: int, *, reveal: bool = False) -> dict:
    with get_db()._conn() as c:
        row = c.execute(
            f"SELECT {', '.join(_ITEM_COLUMNS)} FROM account_pool_items WHERE id = ?",
            (int(item_id),),
        ).fetchone()
        if not row:
            return {}
        events = c.execute(
            """
            SELECT id, ts, from_status, to_status, stage, reason, task_id, round_id, payload
            FROM account_pool_events
            WHERE item_id = ?
            ORDER BY ts DESC, id DESC
            LIMIT 100
            """,
            (int(item_id),),
        ).fetchall()
    out = _public_item(dict(row), reveal=reveal)
    out["events"] = [dict(ev) for ev in events]
    return out


def get_pool_items_by_ids(ids: list[int], *, reveal: bool = True) -> list[dict]:
    clean = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
    if not clean:
        return []
    placeholders = ",".join("?" for _ in clean)
    with get_db()._conn() as c:
        rows = c.execute(
            f"""
            SELECT {', '.join(_ITEM_COLUMNS)}
            FROM account_pool_items
            WHERE id IN ({placeholders})
            ORDER BY id ASC
            """,
            clean,
        ).fetchall()
    return [_public_item(dict(row), reveal=reveal) for row in rows]


def move_items(ids: list[int], *, to_status: str, reason: str = "manual move") -> dict:
    if to_status not in POOL_STATUS_LABELS:
        raise ValueError(f"unknown pool status: {to_status}")
    clean = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
    moved = 0
    now = time.time()
    with get_db()._conn() as c:
        for item_id in clean:
            row = c.execute(
                "SELECT id, pool_status, task_id, round_id FROM account_pool_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if not row:
                continue
            c.execute(
                """
                UPDATE account_pool_items
                SET pool_status = ?, last_stage = 'manual_move', last_error = ?,
                    failed_at = CASE WHEN ? = 'registration_failed' THEN ? ELSE failed_at END,
                    updated_at = ?
                WHERE id = ?
                """,
                (to_status, _text(reason)[:500], to_status, now, now, item_id),
            )
            _event(
                c,
                item_id,
                from_status=row["pool_status"],
                to_status=to_status,
                stage="manual_move",
                reason=reason,
                task_id=row["task_id"],
                round_id=row["round_id"],
            )
            moved += 1
    return {"requested": len(clean), "moved": moved, "to_status": to_status}


def delete_items_by_status(statuses: list[str]) -> dict:
    requested = [str(s or "").strip() for s in statuses if str(s or "").strip()]
    protected = {"email_unused"}
    allowed = [
        s for s in requested
        if s in POOL_STATUS_LABELS and s not in protected
    ]
    skipped = [
        s for s in requested
        if s not in POOL_STATUS_LABELS or s in protected
    ]
    if not allowed:
        return {"requested": requested, "deleted": 0, "statuses": [], "skipped": skipped}
    placeholders = ",".join("?" for _ in allowed)
    with get_db()._conn() as c:
        ids = [
            int(row["id"])
            for row in c.execute(
                f"SELECT id FROM account_pool_items WHERE pool_status IN ({placeholders})",
                allowed,
            ).fetchall()
        ]
        if not ids:
            return {"requested": requested, "deleted": 0, "statuses": allowed, "skipped": skipped}
        id_placeholders = ",".join("?" for _ in ids)
        c.execute(f"DELETE FROM account_pool_events WHERE item_id IN ({id_placeholders})", ids)
        cur = c.execute(f"DELETE FROM account_pool_items WHERE id IN ({id_placeholders})", ids)
    return {"requested": requested, "deleted": int(cur.rowcount), "statuses": allowed, "skipped": skipped}


def claim_unused_emails(limit: int, *, task_id: str = "", round_id: str = "") -> list[dict]:
    limit = max(1, min(int(limit or 1), 500))
    now = time.time()
    claimed: list[dict] = []
    with get_db()._conn() as c:
        rows = c.execute(
            """
            SELECT id, pool_status FROM account_pool_items
            WHERE pool_status = 'email_unused'
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            c.execute(
                """
                UPDATE account_pool_items
                SET pool_status='in_progress', task_id=?, round_id=?, reserved_at=?,
                    attempt_count=attempt_count+1, last_stage='claimed', updated_at=?
                WHERE id=? AND pool_status='email_unused'
                """,
                (task_id, round_id, now, now, int(row["id"])),
            )
            updated = c.execute(
                f"SELECT {', '.join(_ITEM_COLUMNS)} FROM account_pool_items WHERE id = ?",
                (int(row["id"]),),
            ).fetchone()
            if updated:
                _event(
                    c,
                    int(row["id"]),
                    from_status=row["pool_status"],
                    to_status="in_progress",
                    stage="claimed",
                    reason="task claim",
                    task_id=task_id,
                    round_id=round_id,
                )
                claimed.append(_public_item(dict(updated), reveal=True))
    return claimed


def transition_item_by_email(email: str, *, to_status: str, stage: str, reason: str = "",
                             payload: dict | None = None, **fields: Any) -> bool:
    target = _email(email)
    if not target or to_status not in POOL_STATUS_LABELS:
        return False
    now = time.time()
    allowed = {col for col in _WRITE_COLUMNS if col not in {"created_at", "updated_at", "pool_status"}}
    updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
    updates["pool_status"] = to_status
    updates["last_stage"] = stage
    updates["last_error"] = reason[:500]
    updates["updated_at"] = now
    if to_status == "registered_pending_plus":
        updates.setdefault("registered_at", now)
    if to_status in ("plus_missing_rt", "plus_with_rt"):
        updates.setdefault("activated_at", now)
    if to_status == "plus_with_rt":
        updates.setdefault("rt_obtained_at", now)
    if to_status == "registration_failed":
        updates.setdefault("failed_at", now)

    with get_db()._conn() as c:
        row = c.execute(
            "SELECT * FROM account_pool_items WHERE lower(email) = ? OR lower(chatgpt_email) = ? LIMIT 1",
            (target, target),
        ).fetchone()
        if not row:
            item = {"email": target, **updates, "created_at": now}
            result = _insert_or_update_item(c, item, reason=reason or stage, stage=stage)
            return result in ("created", "updated")
        set_sql = ", ".join(f"{key} = ?" for key in updates)
        c.execute(f"UPDATE account_pool_items SET {set_sql} WHERE id = ?", [*updates.values(), row["id"]])
        _event(
            c,
            int(row["id"]),
            from_status=row["pool_status"],
            to_status=to_status,
            stage=stage,
            reason=reason,
            task_id=_text(updates.get("task_id") or row["task_id"]),
            round_id=_text(updates.get("round_id") or row["round_id"]),
            payload=payload,
        )
    return True


def migrate_from_legacy_inventory() -> dict:
    db = get_db()
    registered = db.iter_registered_accounts()
    cards = db.iter_card_results()
    outlook_by_email = {}
    with db._conn() as c:
        outlook_rows = c.execute(
            """
            SELECT id, email, password, client_id, refresh_token, status, last_error
            FROM outlook_mail_accounts
            ORDER BY id ASC
            """
        ).fetchall()
    for row in outlook_rows:
        outlook_by_email[_email(row["email"])] = dict(row)

    latest_card: dict[str, dict] = {}
    for card in cards:
        email = _email(card.get("chatgpt_email") or card.get("email"))
        if email:
            latest_card[email] = card

    counts = {"created": 0, "updated": 0, "skipped": 0, "invalid": 0}
    seen: set[str] = set()
    with db._conn() as c:
        for acc in registered:
            email = _email(acc.get("email"))
            if not email or email in seen:
                continue
            seen.add(email)
            card = latest_card.get(email, {})
            outlook = outlook_by_email.get(email, {})
            rt = _text(acc.get("refresh_token") or card.get("refresh_token"))
            paid = _text(card.get("status")).lower() == "succeeded" or bool(card.get("team_account_id"))
            if paid and rt:
                pool_status = "plus_with_rt"
            elif paid:
                pool_status = "plus_missing_rt"
            else:
                pool_status = "registered_pending_plus"
            item = {
                "email": email,
                "email_password": outlook.get("password") or acc.get("password") or "",
                "mail_client_id": outlook.get("client_id") or "",
                "mail_refresh_token": outlook.get("refresh_token") or "",
                "email_source": "legacy_migration",
                "chatgpt_email": email,
                "account_password": acc.get("password") or outlook.get("password") or "",
                "session_token": acc.get("session_token") or "",
                "access_token": acc.get("access_token") or "",
                "id_token": acc.get("id_token") or "",
                "device_id": acc.get("device_id") or "",
                "csrf_token": acc.get("csrf_token") or "",
                "cookie_header": acc.get("cookie_header") or "",
                "refresh_token": rt,
                "team_account_id": card.get("team_account_id") or "",
                "team_gpt_account_pk": card.get("team_gpt_account_pk") or "",
                "invite_permission": card.get("invite_permission") or "",
                "plan_type": "team" if card.get("team_account_id") else ("plus" if paid else "free"),
                "payment_status": card.get("status") or "",
                "payment_channel": card.get("channel") or "",
                "payment_session_id": card.get("session_id") or "",
                "email_domain": card.get("email_domain") or "",
                "pool_status": pool_status,
                "source_registered_account_id": int(acc.get("id") or 0),
                "source_card_result_id": int(card.get("id") or 0),
                "source_outlook_mail_id": int(outlook.get("id") or 0),
                "registered_at": time.time() if acc.get("ts") else 0,
                "activated_at": time.time() if paid else 0,
                "rt_obtained_at": time.time() if rt else 0,
            }
            result = _insert_or_update_item(c, item, reason="manual legacy migration", stage="legacy_migration")
            counts[result] = counts.get(result, 0) + 1

        for email, outlook in outlook_by_email.items():
            if not email or email in seen:
                continue
            item = {
                "email": email,
                "email_password": outlook.get("password") or "",
                "mail_client_id": outlook.get("client_id") or "",
                "mail_refresh_token": outlook.get("refresh_token") or "",
                "email_source": "legacy_outlook_migration",
                "pool_status": "registration_failed" if outlook.get("status") == "failed" else "email_unused",
                "last_error": outlook.get("last_error") or "",
                "source_outlook_mail_id": int(outlook.get("id") or 0),
            }
            result = _insert_or_update_item(c, item, reason="manual outlook migration", stage="legacy_migration")
            counts[result] = counts.get(result, 0) + 1
    counts["total"] = count_items()
    return counts


def get_rotation_config() -> dict:
    cfg = get_db().get_runtime_json(ROTATION_CONFIG_KEY, {}) or {}
    interval = int(cfg.get("interval") or 100)
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "interval": max(1, interval),
    }


def set_rotation_config(*, enabled: bool, interval: int = 100) -> dict:
    cfg = {"enabled": bool(enabled), "interval": max(1, int(interval or 100))}
    get_db().set_runtime_json(ROTATION_CONFIG_KEY, cfg)
    return cfg


def pending_activation_count() -> int:
    with get_db()._conn() as c:
        return int(
            c.execute(
                "SELECT COUNT(*) FROM account_pool_items WHERE pool_status = 'registered_pending_plus'"
            ).fetchone()[0]
        )


def claim_pending_activation_account(*, task_id: str = "", round_id: str = "") -> dict:
    now = time.time()
    with get_db()._conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            f"""
            SELECT {', '.join(_ITEM_COLUMNS)}
            FROM account_pool_items
            WHERE pool_status = 'registered_pending_plus'
              AND (session_token != '' OR access_token != '')
            ORDER BY updated_at ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            c.execute("COMMIT")
            return {}
        c.execute(
            """
            UPDATE account_pool_items
            SET pool_status='in_progress', task_id=?, round_id=?,
                reserved_at=?, attempt_count=attempt_count+1,
                last_stage='rotation_claimed', last_error='', updated_at=?
            WHERE id=? AND pool_status='registered_pending_plus'
            """,
            (task_id, round_id, now, now, int(row["id"])),
        )
        _event(
            c,
            int(row["id"]),
            from_status="registered_pending_plus",
            to_status="in_progress",
            stage="rotation_claimed",
            reason="rotation picked pending activation account",
            task_id=task_id,
            round_id=round_id,
        )
        updated = c.execute(
            f"SELECT {', '.join(_ITEM_COLUMNS)} FROM account_pool_items WHERE id=?",
            (int(row["id"]),),
        ).fetchone()
        c.execute("COMMIT")
    return _public_item(dict(updated), reveal=True) if updated else {}
