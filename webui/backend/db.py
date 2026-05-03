import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

import bcrypt

from .settings import get_data_dir


_DUMMY_PW_HASH = bcrypt.hashpw(b"dummy-password-for-timing", bcrypt.gensalt(rounds=12))
_CLEANED_DATA_DIRS: set[str] = set()
_LEGACY_RUNTIME_FILES = (
    "daemon_state.json",
    "email_domain_state.json",
    "secrets.json",
    "wa_state.json",
    "webui_wizard_state.json",
    "registered_accounts.jsonl",
    "results.jsonl",
    "wa_otp_legacy.txt",
)


def _purge_legacy_runtime_files(data_dir: Path, *, force: bool = False) -> None:
    key = str(Path(data_dir).resolve())
    if not force and key in _CLEANED_DATA_DIRS:
        return
    _CLEANED_DATA_DIRS.add(key)
    for name in _LEGACY_RUNTIME_FILES:
        path = Path(data_dir) / name
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY,
  pw_hash BLOB NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS runtime_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS registered_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL COLLATE NOCASE,
  ts TEXT NOT NULL,
  password TEXT DEFAULT '',
  session_token TEXT DEFAULT '',
  access_token TEXT DEFAULT '',
  device_id TEXT DEFAULT '',
  csrf_token TEXT DEFAULT '',
  id_token TEXT DEFAULT '',
  refresh_token TEXT DEFAULT '',
  cookie_header TEXT DEFAULT '',
  created_at REAL NOT NULL,
  last_check_at REAL DEFAULT 0,
  last_check_status TEXT DEFAULT '',
  last_check_message TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_registered_accounts_email_id
  ON registered_accounts(email, id);

CREATE TABLE IF NOT EXISTS pipeline_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  mode TEXT DEFAULT '',
  status TEXT DEFAULT '',
  error TEXT DEFAULT '',
  registration_status TEXT DEFAULT '',
  registration_email TEXT DEFAULT '',
  registration_error TEXT DEFAULT '',
  payment_status TEXT DEFAULT '',
  payment_email TEXT DEFAULT '',
  payment_error TEXT DEFAULT '',
  domain TEXT DEFAULT '',
  proxy TEXT DEFAULT '',
  cpa_import TEXT DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipeline_results_registration_email_id
  ON pipeline_results(registration_email, id);
CREATE INDEX IF NOT EXISTS idx_pipeline_results_payment_email_id
  ON pipeline_results(payment_email, id);

CREATE TABLE IF NOT EXISTS card_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  status TEXT DEFAULT '',
  chatgpt_email TEXT DEFAULT '',
  email TEXT DEFAULT '',
  session_id TEXT DEFAULT '',
  channel TEXT DEFAULT '',
  entity TEXT DEFAULT '',
  config TEXT DEFAULT '',
  error TEXT DEFAULT '',
  refresh_token TEXT DEFAULT '',
  team_account_id TEXT DEFAULT '',
  invite_permission TEXT DEFAULT '',
  team_gpt_account_pk TEXT DEFAULT '',
  email_domain TEXT DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_card_results_email_session_id
  ON card_results(chatgpt_email, session_id, id);

CREATE TABLE IF NOT EXISTS oauth_status (
  email TEXT PRIMARY KEY COLLATE NOCASE,
  status TEXT NOT NULL,
  ts TEXT NOT NULL,
  fail_reason TEXT DEFAULT ''
);
"""


_CARD_RESULT_COLUMNS = {
    "refresh_token",
    "team_account_id",
    "invite_permission",
    "team_gpt_account_pk",
    "email_domain",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _email(value: Any) -> str:
    return _text(value).strip().lower()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _purge_legacy_runtime_files(self.path.parent)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode = WAL")
            c.executescript(_SCHEMA)
            self._ensure_columns(c)

    def _conn(self):
        c = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA busy_timeout = 10000")
        return c

    def _ensure_columns(self, c: sqlite3.Connection) -> None:
        """Lightweight forward migration for DBs created by older webui builds."""
        existing = {row["name"] for row in c.execute("PRAGMA table_info(card_results)").fetchall()}
        for name in ("invite_permission", "team_gpt_account_pk", "email_domain"):
            if name not in existing:
                c.execute(f"ALTER TABLE card_results ADD COLUMN {name} TEXT DEFAULT ''")
        existing_acc = {row["name"] for row in c.execute("PRAGMA table_info(registered_accounts)").fetchall()}
        if "last_check_at" not in existing_acc:
            c.execute("ALTER TABLE registered_accounts ADD COLUMN last_check_at REAL DEFAULT 0")
        if "last_check_status" not in existing_acc:
            c.execute("ALTER TABLE registered_accounts ADD COLUMN last_check_status TEXT DEFAULT ''")
        if "last_check_message" not in existing_acc:
            c.execute("ALTER TABLE registered_accounts ADD COLUMN last_check_message TEXT DEFAULT ''")

    # ──────────────────────────────────────────
    # Runtime data store. SQLite is the only source of truth for runtime data.
    # Config files remain JSON because they are user-editable configuration, not
    # mutable account/payment state.
    # ──────────────────────────────────────────

    def clear_runtime_data(self) -> None:
        """Delete account/payment/oauth rows and transient run state.

        Do not wipe durable WebUI configuration kept in runtime_meta, such as
        Cloudflare secrets, wizard answers, WhatsApp engine preference, relay
        token, or WhatsApp session snapshot.  Those are database-backed config /
        auth cache; clearing old account inventory must not break the next run's
        OTP provider.
        """
        with self._conn() as c:
            for table in ("registered_accounts", "pipeline_results", "card_results", "oauth_status"):
                c.execute(f"DELETE FROM {table}")
            for key in ("daemon_state", "email_domain_state", "wa_state"):
                c.execute("DELETE FROM runtime_meta WHERE key = ?", (key,))
        _purge_legacy_runtime_files(self.path.parent, force=True)

    def runtime_counts(self) -> dict[str, int]:
        with self._conn() as c:
            return {
                table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("registered_accounts", "pipeline_results", "card_results", "oauth_status")
            }

    def set_runtime_value(self, key: str, value: str) -> bool:
        key = _text(key).strip()
        if not key:
            return False
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO runtime_meta(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value=excluded.value,
                  updated_at=excluded.updated_at
                """,
                (key, _text(value), time.time()),
            )
        return True

    def get_runtime_value(self, key: str, default: str = "") -> str:
        key = _text(key).strip()
        if not key:
            return default
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM runtime_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return row["value"] if row else default

    def set_runtime_json(self, key: str, value: Any) -> bool:
        return self.set_runtime_value(
            key,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )

    def get_runtime_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_runtime_value(key, "")
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def delete_runtime_key(self, key: str) -> bool:
        key = _text(key).strip()
        if not key:
            return False
        with self._conn() as c:
            c.execute("DELETE FROM runtime_meta WHERE key = ?", (key,))
        return True

    def has_runtime_key(self, key: str) -> bool:
        key = _text(key).strip()
        if not key:
            return False
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM runtime_meta WHERE key = ? LIMIT 1",
                (key,),
            ).fetchone()
        return row is not None

    def add_registered_account(self, row: dict) -> bool:
        email = _email(row.get("email"))
        if not email:
            return False
        ts = _text(row.get("ts")) or time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO registered_accounts(
                  email, ts, password, session_token, access_token, device_id,
                  csrf_token, id_token, refresh_token, cookie_header, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    ts,
                    _text(row.get("password")),
                    _text(row.get("session_token")),
                    _text(row.get("access_token")),
                    _text(row.get("device_id")),
                    _text(row.get("csrf_token")),
                    _text(row.get("id_token")),
                    _text(row.get("refresh_token")),
                    _text(row.get("cookie_header")),
                    time.time(),
                ),
            )
        return True

    def iter_registered_accounts(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT id, email, ts, password, session_token, access_token, device_id,
                       csrf_token, id_token, refresh_token, cookie_header,
                       last_check_at, last_check_status, last_check_message
                FROM registered_accounts
                ORDER BY id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_registered_account(self, account_id: int) -> dict:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT id, email, ts, password, session_token, access_token, device_id,
                       csrf_token, id_token, refresh_token, cookie_header,
                       last_check_at, last_check_status, last_check_message
                FROM registered_accounts WHERE id = ?
                """,
                (int(account_id),),
            ).fetchone()
        return dict(row) if row else {}

    def update_account_check(self, account_id: int, status: str, message: str = "") -> bool:
        """Record validity probe outcome (status: 'valid' | 'invalid' | 'unknown')."""
        with self._conn() as c:
            cur = c.execute(
                """
                UPDATE registered_accounts
                SET last_check_at = ?, last_check_status = ?, last_check_message = ?
                WHERE id = ?
                """,
                (time.time(), _text(status), _text(message)[:500], int(account_id)),
            )
        return cur.rowcount > 0

    def delete_registered_accounts(self, ids: list[int]) -> int:
        """Hard-delete accounts by id. Returns number of rows deleted.
        Associated rows in pipeline_results / card_results / oauth_status are
        intentionally kept for audit (lookup by email still works)."""
        clean = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
        if not clean:
            return 0
        placeholders = ",".join("?" * len(clean))
        with self._conn() as c:
            cur = c.execute(
                f"DELETE FROM registered_accounts WHERE id IN ({placeholders})",
                clean,
            )
        return cur.rowcount

    def find_latest_registered_account(self, email: str) -> dict:
        target = _email(email)
        if not target:
            return {}
        with self._conn() as c:
            row = c.execute(
                """
                SELECT email, ts, password, session_token, access_token, device_id,
                       csrf_token, id_token, refresh_token, cookie_header
                FROM registered_accounts
                WHERE email = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (target,),
            ).fetchone()
        return dict(row) if row else {}

    def add_pipeline_result(self, record: dict) -> bool:
        reg = record.get("registration") if isinstance(record.get("registration"), dict) else {}
        pay = record.get("payment") if isinstance(record.get("payment"), dict) else {}
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO pipeline_results(
                  ts, mode, status, error,
                  registration_status, registration_email, registration_error,
                  payment_status, payment_email, payment_error,
                  domain, proxy, cpa_import, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _text(record.get("ts")) or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    _text(record.get("mode")),
                    _text(record.get("status")),
                    _text(record.get("error")),
                    _text(reg.get("status")),
                    _email(reg.get("email")),
                    _text(reg.get("error")),
                    _text(pay.get("status")),
                    _email(pay.get("email") or record.get("chatgpt_email") or record.get("email")),
                    _text(pay.get("error")),
                    _text(record.get("domain")),
                    _text(record.get("proxy")),
                    _text(record.get("cpa_import")),
                    time.time(),
                ),
            )
        return True

    def iter_pipeline_results(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT ts, mode, status, error,
                       registration_status, registration_email, registration_error,
                       payment_status, payment_email, payment_error,
                       domain, proxy, cpa_import
                FROM pipeline_results
                ORDER BY id ASC
                """
            ).fetchall()
        out: list[dict] = []
        for row in rows:
            d = {
                "ts": row["ts"],
                "mode": row["mode"],
                "status": row["status"],
                "error": row["error"],
                "registration": {
                    "status": row["registration_status"],
                    "email": row["registration_email"],
                    "error": row["registration_error"],
                },
                "payment": {
                    "status": row["payment_status"],
                    "email": row["payment_email"],
                    "error": row["payment_error"],
                },
                "domain": row["domain"],
                "proxy": row["proxy"],
            }
            if row["cpa_import"]:
                d["cpa_import"] = row["cpa_import"]
            out.append(d)
        return out

    def add_card_result(self, record: dict) -> bool:
        chatgpt_email = _email(record.get("chatgpt_email") or record.get("email"))
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO card_results(
                  ts, status, chatgpt_email, email, session_id, channel, entity,
                  config, error, refresh_token, team_account_id, invite_permission,
                  team_gpt_account_pk, email_domain, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _text(record.get("ts")) or time.strftime("%Y-%m-%d %H:%M:%S"),
                    _text(record.get("status") or record.get("state")),
                    chatgpt_email,
                    _email(record.get("email")),
                    _text(record.get("session_id")),
                    _text(record.get("channel")),
                    _text(record.get("entity")),
                    _text(record.get("config")),
                    _text(record.get("error"))[:500],
                    _text(record.get("refresh_token")),
                    _text(record.get("team_account_id")),
                    _text(record.get("invite_permission")),
                    _text(record.get("team_gpt_account_pk")),
                    _text(record.get("email_domain")),
                    time.time(),
                ),
            )
        return True

    def iter_card_results(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT ts, status, chatgpt_email, email, session_id, channel, entity,
                       config, error, refresh_token, team_account_id,
                       invite_permission, team_gpt_account_pk, email_domain
                FROM card_results
                ORDER BY id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_refresh_token_for_email(self, email: str, session_id: str = "") -> str:
        target = _email(email)
        if not target:
            return ""
        params: list[Any] = [target]
        sid_clause = ""
        if session_id:
            sid_clause = "AND session_id = ?"
            params.append(session_id)
        with self._conn() as c:
            row = c.execute(
                f"""
                SELECT refresh_token
                FROM card_results
                WHERE chatgpt_email = ? {sid_clause}
                  AND refresh_token != ''
                ORDER BY id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return row["refresh_token"] if row else ""

    def augment_card_result_last_match(self, email: str, session_id: str, extra_fields: dict) -> bool:
        target = _email(email)
        if not target:
            return False
        params: list[Any] = [target]
        sid_clause = ""
        if session_id:
            sid_clause = "AND session_id = ?"
            params.append(session_id)
        with self._conn() as c:
            row = c.execute(
                f"""
                SELECT id FROM card_results
                WHERE chatgpt_email = ? {sid_clause}
                ORDER BY id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if not row:
                return False
            updates = {
                key: _text(value)
                for key, value in (extra_fields or {}).items()
                if key in _CARD_RESULT_COLUMNS and value is not None
            }
            if not updates:
                return True
            set_sql = ", ".join(f"{key} = ?" for key in updates)
            c.execute(
                f"UPDATE card_results SET {set_sql} WHERE id = ?",
                [*updates.values(), row["id"]],
            )
        return True

    def find_team_id_from_results(self, email: str) -> str:
        target = _email(email)
        if not target:
            return ""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT team_account_id
                FROM card_results
                WHERE chatgpt_email = ? AND team_account_id != ''
                ORDER BY id DESC
                LIMIT 1
                """,
                (target,),
            ).fetchone()
        return row["team_account_id"] if row else ""

    def set_oauth_status(self, email: str, status: str, fail_reason: str = "", ts: str = "") -> bool:
        target = _email(email)
        if not target or not status:
            return False
        if not ts:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO oauth_status(email, status, ts, fail_reason)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                  status=excluded.status,
                  ts=excluded.ts,
                  fail_reason=excluded.fail_reason
                """,
                (target, status, ts, fail_reason),
            )
        return True

    def load_oauth_status_map(self) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT email, status, ts, fail_reason FROM oauth_status ORDER BY email ASC"
            ).fetchall()
        return {
            row["email"]: {
                "status": row["status"],
                "ts": row["ts"],
                "fail_reason": row["fail_reason"],
            }
            for row in rows
        }

    def user_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, username: str, password: str) -> None:
        h = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12))
        with self._conn() as c:
            c.execute(
                "INSERT INTO users(username, pw_hash, created_at) VALUES (?, ?, ?)",
                (username, h, time.time()),
            )

    def verify_user(self, username: str, password: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT pw_hash FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            # Burn equivalent CPU to mask user-existence timing
            bcrypt.checkpw(password.encode(), _DUMMY_PW_HASH)
            return False
        return bcrypt.checkpw(password.encode(), row["pw_hash"])

    def create_session(self, username: str, ttl_s: int = 7 * 24 * 3600) -> str:
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO sessions(id, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (sid, username, now, now + ttl_s),
            )
        return sid

    def lookup_session(self, sid: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT username, expires_at FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
        if not row:
            return None
        username, expires_at = row["username"], row["expires_at"]
        if time.time() >= expires_at:
            self.delete_session(sid)
            return None
        return username

    def delete_session(self, sid: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE id = ?", (sid,))


def get_db() -> Database:
    """Database instance pointing to the configured webui.db path.
    Reads WEBUI_DATA_DIR at call time (test isolation)."""
    return Database(get_data_dir() / "webui.db")
