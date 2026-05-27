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
  last_check_message TEXT DEFAULT '',
  last_plan_type TEXT DEFAULT ''
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

-- Outlook 账号池（接码买的 4 段格式：email----password----client_id----refresh_token）
-- Run 时从池里 claim 一个 available outlook，注册到 ChatGPT 后 mark used
CREATE TABLE IF NOT EXISTS outlook_accounts (
  email TEXT PRIMARY KEY COLLATE NOCASE,
  password TEXT DEFAULT '',
  client_id TEXT DEFAULT '',
  refresh_token TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'available',  -- available / in_use / used / dead
  imported_at REAL DEFAULT 0,
  claimed_at REAL DEFAULT 0,
  used_at REAL DEFAULT 0,
  chatgpt_email TEXT DEFAULT '',  -- 注册成功后等于自己 (现在 outlook 邮箱注册 ChatGPT 用同 email)
  fail_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_outlook_accounts_status ON outlook_accounts(status, imported_at);

-- 优惠长链接池 (mode=promo_link 抓的): 注册/登录账号 → 调 ChatGPT checkout API
-- 拿 promo 命中的 hosted long URL (https://checkout.stripe.com/c/pay/cs_live_...) 存这.
CREATE TABLE IF NOT EXISTS promo_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL COLLATE NOCASE,
  checkout_url TEXT NOT NULL,             -- 长链接 (hosted, 优惠命中)
  cs_id TEXT DEFAULT '',                  -- cs_live_xxx
  processor_entity TEXT DEFAULT '',       -- openai_llc / openai_ie / ...
  plan_name TEXT DEFAULT '',              -- chatgptplusplan / chatgptteamplan
  promo_campaign_id TEXT DEFAULT '',      -- plus-1-month-free 等
  billing_country TEXT DEFAULT '',
  billing_currency TEXT DEFAULT '',
  amount_due_cents INTEGER DEFAULT 0,     -- 命中 promo 应该 ≤ 100 (1 currency unit)
  status TEXT NOT NULL DEFAULT 'fresh',   -- fresh / used / expired
  created_at REAL NOT NULL,
  used_at REAL DEFAULT 0,
  raw_response TEXT DEFAULT ''            -- ChatGPT checkout API 完整 response (debug)
);
CREATE INDEX IF NOT EXISTS idx_promo_links_email_id ON promo_links(email, id);
CREATE INDEX IF NOT EXISTS idx_promo_links_status_created ON promo_links(status, created_at);
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
        if "last_plan_type" not in existing_acc:
            c.execute("ALTER TABLE registered_accounts ADD COLUMN last_plan_type TEXT DEFAULT ''")
        # 并发跑 no_card_plus 时多 worker 抢占同一 promo_link 的原子化锁字段
        existing_pl = {row["name"] for row in c.execute("PRAGMA table_info(promo_links)").fetchall()}
        if "claimed_by" not in existing_pl:
            c.execute("ALTER TABLE promo_links ADD COLUMN claimed_by TEXT DEFAULT ''")
        if "claimed_at" not in existing_pl:
            c.execute("ALTER TABLE promo_links ADD COLUMN claimed_at REAL DEFAULT 0")

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

    def add_promo_link(self, row: dict) -> int:
        """写一条 promo 长链接记录, 返新 row id."""
        email = _email(row.get("email"))
        if not email or not row.get("checkout_url"):
            return 0
        import json as _json
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO promo_links(
                  email, checkout_url, cs_id, processor_entity,
                  plan_name, promo_campaign_id, billing_country, billing_currency,
                  amount_due_cents, status, created_at, raw_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    _text(row.get("checkout_url")),
                    _text(row.get("cs_id")),
                    _text(row.get("processor_entity")),
                    _text(row.get("plan_name")),
                    _text(row.get("promo_campaign_id")),
                    _text(row.get("billing_country")),
                    _text(row.get("billing_currency")),
                    int(row.get("amount_due_cents") or 0),
                    _text(row.get("status") or "fresh"),
                    time.time(),
                    _json.dumps(row.get("raw_response") or {}, ensure_ascii=False)
                        if isinstance(row.get("raw_response"), dict)
                        else _text(row.get("raw_response")),
                ),
            )
            return int(cur.lastrowid or 0)

    def list_promo_links(self, status: str = "", limit: int = 200) -> list[dict]:
        with self._conn() as c:
            if status:
                rows = c.execute(
                    """SELECT id, email, checkout_url, cs_id, processor_entity,
                       plan_name, promo_campaign_id, billing_country, billing_currency,
                       amount_due_cents, status, created_at, used_at
                       FROM promo_links WHERE status=? ORDER BY id DESC LIMIT ?""",
                    (status, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT id, email, checkout_url, cs_id, processor_entity,
                       plan_name, promo_campaign_id, billing_country, billing_currency,
                       amount_due_cents, status, created_at, used_at
                       FROM promo_links ORDER BY id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def promo_links_stats(self) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM promo_links GROUP BY status"
            ).fetchall()
        out = {"fresh": 0, "in_use": 0, "used": 0, "expired": 0, "total": 0}
        for r in rows:
            out[r["status"]] = r["n"]
            out["total"] += r["n"]
        return out

    def mark_promo_link_used(self, link_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE promo_links SET status='used', used_at=?, claimed_by='', claimed_at=0 "
                "WHERE id=? AND status IN ('fresh','in_use')",
                (time.time(), int(link_id)),
            )
            return cur.rowcount > 0

    def mark_promo_link_status(self, link_id: int, new_status: str) -> bool:
        """通用 status 更新, 支持 fresh / used / expired / in_use.

        no_card_paypal_plus.py 检测到 stripe due > 0 (promo 不命中) 时调
        mark_promo_link_status(id, 'expired') 标该 link 废, 防止下次 auto pick
        再选中跑同样浪费几分钟.
        """
        valid = {"fresh", "used", "expired", "in_use"}
        if new_status not in valid:
            return False
        with self._conn() as c:
            cur = c.execute(
                "UPDATE promo_links SET status=? WHERE id=?",
                (new_status, int(link_id)),
            )
            return cur.rowcount > 0

    def claim_next_fresh_promo_link(
        self,
        worker_id: str,
        plan_like: str = "plus",
        email: str = "",
        max_due_cents: int = 100,
        exclude_ids: list[int] | None = None,
    ) -> dict | None:
        """原子地占用一条 fresh promo_link：UPDATE...WHERE status='fresh' 一次性把
        最早匹配的行翻到 status='in_use' + claimed_by=worker_id，并 RETURNING 行内容。

        SQLite 3.35+ 支持 RETURNING；老版本走 SELECT+UPDATE in IMMEDIATE 事务兜底。
        多个 worker 并发调时 SQLite 行级 BUSY 重试 + WHERE status='fresh' 二次校验
        保证不会两个 worker 拿到同一行。
        """
        worker_id = _text(worker_id).strip() or "anon"
        plan_like = _text(plan_like).strip() or "plus"
        email = _text(email).strip()
        now = time.time()
        # 先选最匹配 id 的 fresh 行：lower(plan_name) LIKE '%plus%'，可选 email 过滤
        where = ["status='fresh'", "lower(plan_name) LIKE ?"]
        params: list = [f"%{plan_like.lower()}%"]
        if max_due_cents and max_due_cents > 0:
            where.append("amount_due_cents <= ?")
            params.append(int(max_due_cents))
        if email:
            where.append("lower(email) = lower(?)")
            params.append(email)
        excl = [int(x) for x in (exclude_ids or []) if int(x) > 0]
        if excl:
            placeholders = ",".join("?" * len(excl))
            where.append(f"id NOT IN ({placeholders})")
            params.extend(excl)
        with self._conn() as c:
            # SQLite 3.35+: 用 RETURNING + 子查询 LIMIT 1 一句话原子完成
            try:
                row = c.execute(
                    f"""
                    UPDATE promo_links
                       SET status='in_use', claimed_by=?, claimed_at=?
                     WHERE id = (
                       SELECT id FROM promo_links
                        WHERE {' AND '.join(where)}
                        ORDER BY id DESC
                        LIMIT 1
                     )
                    RETURNING id, email, checkout_url, cs_id, processor_entity,
                              plan_name, promo_campaign_id, billing_country,
                              billing_currency, amount_due_cents, status
                    """,
                    (worker_id, now, *params),
                ).fetchone()
                return dict(row) if row else None
            except sqlite3.OperationalError:
                pass
            # 兜底：BEGIN IMMEDIATE → SELECT → UPDATE
            c.execute("BEGIN IMMEDIATE")
            try:
                sel = c.execute(
                    f"SELECT id, email, checkout_url, cs_id, processor_entity, plan_name, "
                    f"       promo_campaign_id, billing_country, billing_currency, "
                    f"       amount_due_cents, status "
                    f"FROM promo_links WHERE {' AND '.join(where)} "
                    f"ORDER BY id DESC LIMIT 1",
                    tuple(params),
                ).fetchone()
                if not sel:
                    c.execute("COMMIT")
                    return None
                upd = c.execute(
                    "UPDATE promo_links SET status='in_use', claimed_by=?, claimed_at=? "
                    "WHERE id=? AND status='fresh'",
                    (worker_id, now, int(sel["id"])),
                )
                if upd.rowcount <= 0:
                    c.execute("ROLLBACK")
                    return None
                c.execute("COMMIT")
                return dict(sel)
            except Exception:
                c.execute("ROLLBACK")
                raise

    def claim_promo_link_by_id(self, worker_id: str, link_id: int) -> dict | None:
        """显式 --promo-link-id 模式：atomic claim 指定 id 的 fresh 行，
        被别的 worker 抢了就返 None。"""
        worker_id = _text(worker_id).strip() or "anon"
        with self._conn() as c:
            try:
                row = c.execute(
                    """
                    UPDATE promo_links
                       SET status='in_use', claimed_by=?, claimed_at=?
                     WHERE id=? AND status='fresh'
                    RETURNING id, email, checkout_url, cs_id, processor_entity,
                              plan_name, promo_campaign_id, billing_country,
                              billing_currency, amount_due_cents, status
                    """,
                    (worker_id, time.time(), int(link_id)),
                ).fetchone()
                return dict(row) if row else None
            except sqlite3.OperationalError:
                pass
            c.execute("BEGIN IMMEDIATE")
            try:
                sel = c.execute(
                    "SELECT id, email, checkout_url, cs_id, processor_entity, plan_name, "
                    "       promo_campaign_id, billing_country, billing_currency, "
                    "       amount_due_cents, status FROM promo_links "
                    "WHERE id=? AND status='fresh'",
                    (int(link_id),),
                ).fetchone()
                if not sel:
                    c.execute("COMMIT")
                    return None
                c.execute(
                    "UPDATE promo_links SET status='in_use', claimed_by=?, claimed_at=? WHERE id=?",
                    (worker_id, time.time(), int(link_id)),
                )
                c.execute("COMMIT")
                return dict(sel)
            except Exception:
                c.execute("ROLLBACK")
                raise

    def release_promo_link(self, link_id: int, new_status: str = "fresh") -> bool:
        """worker 失败时把 in_use 状态退回 fresh（或标 expired）让其它 worker 复用。"""
        if new_status not in ("fresh", "expired"):
            return False
        with self._conn() as c:
            cur = c.execute(
                "UPDATE promo_links SET status=?, claimed_by='', claimed_at=0 "
                "WHERE id=? AND status='in_use'",
                (new_status, int(link_id)),
            )
            return cur.rowcount > 0

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
                       last_check_at, last_check_status, last_check_message,
                       last_plan_type
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
                       last_check_at, last_check_status, last_check_message,
                       last_plan_type
                FROM registered_accounts WHERE id = ?
                """,
                (int(account_id),),
            ).fetchone()
        return dict(row) if row else {}

    def update_account_check(self, account_id: int, status: str, message: str = "",
                              plan_type: str = "") -> bool:
        """Record validity probe outcome (status: 'valid' | 'invalid' | 'unknown').

        ``plan_type`` 可选: 当 caller 从实时 API (/backend-api/accounts/check)
        拿到了订阅状态时一并写入,避免下次 inventory 渲染读到 stale JWT claim。
        空字符串表示不更新 plan_type 字段(向后兼容)。
        """
        sets = [
            "last_check_at = ?",
            "last_check_status = ?",
            "last_check_message = ?",
        ]
        args: list[Any] = [time.time(), _text(status), _text(message)[:500]]
        if plan_type:
            sets.append("last_plan_type = ?")
            args.append(_text(plan_type)[:80])
        args.append(int(account_id))
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE registered_accounts SET {', '.join(sets)} WHERE id = ?",
                args,
            )
        return cur.rowcount > 0

    def update_account_rt_status(
        self,
        account_id: int,
        *,
        status: str,
        message: str = "",
        plan_type: str = "",
        access_token: str = "",
        refresh_token: str = "",
        id_token: str = "",
    ) -> bool:
        """Persist the result of a refresh_token based status refresh.

        Successful refreshes may rotate the access/refresh token and expose the
        current ChatGPT plan in the access-token claims.  Keep the raw token
        update and the derived status in one DB write so inventory never shows
        a new plan with stale credentials, or vice versa.
        """
        sets = [
            "last_check_at = ?",
            "last_check_status = ?",
            "last_check_message = ?",
        ]
        args: list[Any] = [time.time(), _text(status), _text(message)[:500]]
        if plan_type:
            sets.append("last_plan_type = ?")
            args.append(_text(plan_type)[:80])
        if access_token:
            sets.append("access_token = ?")
            args.append(_text(access_token))
        if refresh_token:
            sets.append("refresh_token = ?")
            args.append(_text(refresh_token))
        if id_token:
            sets.append("id_token = ?")
            args.append(_text(id_token))
        args.append(int(account_id))
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE registered_accounts SET {', '.join(sets)} WHERE id = ?",
                args,
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
