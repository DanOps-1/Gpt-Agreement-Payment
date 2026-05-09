"""邮箱服务（CF Email Routing 路径）。

历史上这个模块走 IMAP 拉 QQ 邮箱接 OTP（5s 轮询 + 转发链路 30–90s 延迟）。
现在彻底切到 Cloudflare Email Worker → KV 路径：

    寄件人 → CF MX (catch-all) → otp-relay Worker → KV
                                                       ↓
                                            cf_kv_otp_provider 读

OTP 提取由 Worker 端做（见 scripts/otp_email_worker.js），
本模块只剩两件事：
  1. 用 catch-all 域名生成随机收件地址 (`create_mailbox`)
  2. 委托 `CloudflareKVOtpProvider` 阻塞拿 OTP (`wait_for_otp`)

KV 凭证读取顺序：环境变量 `CF_API_TOKEN/CF_ACCOUNT_ID/CF_OTP_KV_NAMESPACE_ID`
→ SQLite runtime_meta[secrets] 的 cloudflare 段。详见 cf_kv_otp_provider.py。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import requests

if os.name == "nt":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)


# —— 真人风邮箱前缀生成 ——
# 与 browser_register._gen_name 保持同款英美常见名池；OpenAI 反欺诈系统对
# "随机字符串前缀"评分较低，用 first/last 组合更接近真实新用户分布。
_FIRST_NAMES = [
    "james", "john", "emily", "sophia", "michael", "oliver", "emma",
    "william", "amelia", "lucas", "mia", "ethan", "noah", "ava", "liam",
    "isabella", "mason", "charlotte", "logan", "harper", "elijah", "evelyn",
    "benjamin", "abigail", "jacob", "ella", "alexander", "scarlett", "henry",
    "grace", "daniel", "chloe", "matthew", "lily", "samuel", "zoe",
    "david", "hannah", "joseph", "aria", "ryan", "nora",
]
_LAST_NAMES = [
    "smith", "johnson", "williams", "brown", "jones", "garcia",
    "miller", "davis", "rodriguez", "martinez", "wilson", "anderson",
    "taylor", "thomas", "moore", "jackson", "martin", "lee", "walker",
    "hall", "allen", "young", "king", "wright", "scott", "green",
    "baker", "adams", "nelson", "carter",
]


def _humanlike_local_part(rng: random.Random | None = None) -> str:
    """生成像真人的邮箱前缀，例如 emma.davis、jsmith92、liam_wilson03。

    采样模式（权重）：
      - first.last                       (常见专业邮箱)
      - firstlast                        (无分隔)
      - first_last                       (下划线)
      - first.last + 1-2 位数字
      - firstlast + 2-4 位数字（含年份）
      - first 首字母 + last + 数字 (jsmith92)
      - first + last 首字母 + 数字 (emmas01)
      - first + 出生年（1985-2003）

    所有结果只含 [a-z0-9._]，长度 5-22，符合 RFC + 多数邮件服务的本地部要求。
    """
    r = rng or random
    first = r.choice(_FIRST_NAMES)
    last = r.choice(_LAST_NAMES)

    pattern = r.choices(
        population=[
            "first.last", "firstlast", "first_last",
            "first.last+num", "firstlast+num",
            "f.last+num", "first.l+num", "first+year",
        ],
        weights=[14, 10, 6, 18, 16, 14, 10, 12],
        k=1,
    )[0]

    if pattern == "first.last":
        local = f"{first}.{last}"
    elif pattern == "firstlast":
        local = f"{first}{last}"
    elif pattern == "first_last":
        local = f"{first}_{last}"
    elif pattern == "first.last+num":
        n = r.randint(1, 99)
        local = f"{first}.{last}{n:02d}"
    elif pattern == "firstlast+num":
        # 偏向 4 位年份样式（更像真人）
        if r.random() < 0.55:
            n = r.randint(1985, 2003)
            local = f"{first}{last}{n}"
        else:
            n = r.randint(1, 999)
            local = f"{first}{last}{n}"
    elif pattern == "f.last+num":
        n = r.randint(1, 99)
        local = f"{first[0]}{last}{n:02d}"
    elif pattern == "first.l+num":
        n = r.randint(1, 99)
        local = f"{first}{last[0]}{n:02d}"
    else:  # first+year
        n = r.randint(1985, 2003)
        local = f"{first}{n}"

    # 兜底长度（极个别长姓如 rodriguez+full year 会到 22）
    if len(local) > 22:
        local = local[:22]
    return local


class OutlookAccount:
    def __init__(self, email: str, password: str, client_id: str, refresh_token: str):
        self.email = email.strip().lower()
        self.password = password.strip()
        self.client_id = client_id.strip()
        self.refresh_token = refresh_token.strip()
        self.access_token = ""
        self.expires_at = 0.0


def _parse_outlook_account_line(line: str) -> Optional[OutlookAccount]:
    parts = [p.strip() for p in str(line or "").strip().split("----", 3)]
    if len(parts) != 4 or not parts[0] or not parts[2] or not parts[3]:
        return None
    return OutlookAccount(parts[0], parts[1], parts[2], parts[3])


def _extract_otp6(text: str) -> str:
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text or "")
    return m.group(1) if m else ""


def _parse_graph_time(value: str) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _default_webui_db_path() -> Path:
    root = Path(__file__).resolve().parents[1]
    return Path(os.environ.get("WEBUI_DB_PATH") or os.environ.get("WEBUI_DATABASE_PATH") or root / "output" / "webui.db")


class MailProvider:
    """生成 catch-all 子域随机邮箱 + 委托 CF KV provider 取 OTP。

    `last_persona` 暴露最近一次 `create_mailbox()` 产生的完整 persona
    （邮箱 / first / last / 密码），供 `browser_register` 复用，
    确保「邮箱 first-name 与注册显示姓名一致」——OpenAI 反欺诈系统
    会对二者不一致打负分。
    """

    def __init__(self, catch_all_domain: str = "", *, cfg: Any = None):
        self.cfg = cfg
        self.provider = str(getattr(cfg, "provider", "") or "").strip().lower() or "cf"
        self.catch_all_domain = getattr(cfg, "catch_all_domain", catch_all_domain) if cfg is not None else catch_all_domain
        self._reuse_email: Optional[str] = None  # 兼容 register-only resume
        self._outlook_accounts: list[OutlookAccount] = self._load_outlook_accounts(cfg)
        self._outlook_cursor = 0
        self._current_outlook: Optional[OutlookAccount] = None
        self._current_outlook_db_id = 0
        self._outlook_state_path = self._resolve_outlook_state_path(cfg)
        self._outlook_source = str(getattr(cfg, "outlook_source", "") or "").strip().lower()
        self._outlook_db_path = _default_webui_db_path()
        # 算法化 persona 生成器（音节合成法，详见 persona.py）
        from persona import PersonaGenerator, Persona
        self._persona_gen = PersonaGenerator(catch_all_domain)
        self.last_persona: Optional[Persona] = None

    @classmethod
    def from_config(cls, mail_cfg: Any) -> "MailProvider":
        return cls(getattr(mail_cfg, "catch_all_domain", "") or "", cfg=mail_cfg)

    @staticmethod
    def _load_outlook_accounts(cfg: Any) -> list[OutlookAccount]:
        if cfg is None:
            return []
        rows: list[str] = []
        raw_accounts = getattr(cfg, "outlook_accounts", None) or []
        if isinstance(raw_accounts, list):
            for item in raw_accounts:
                if isinstance(item, str):
                    rows.append(item)
                elif isinstance(item, dict):
                    rows.append("----".join([
                        str(item.get("email") or ""),
                        str(item.get("password") or ""),
                        str(item.get("client_id") or ""),
                        str(item.get("refresh_token") or item.get("token") or ""),
                    ]))
        path = str(getattr(cfg, "outlook_accounts_path", "") or "").strip()
        if path:
            p = Path(path).expanduser()
            if not p.is_absolute():
                p = (Path.cwd() / p).resolve()
            try:
                rows.extend(p.read_text(encoding="utf-8").splitlines())
            except Exception as e:
                raise RuntimeError(f"读取 Outlook 账号池失败: {p}: {e}") from e
        return [acc for acc in (_parse_outlook_account_line(row) for row in rows) if acc]

    @staticmethod
    def _resolve_outlook_state_path(cfg: Any) -> Optional[Path]:
        if cfg is None:
            return None
        path = str(getattr(cfg, "outlook_accounts_path", "") or "").strip()
        if not path:
            return None
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        return p.with_suffix(p.suffix + ".state.json")

    @staticmethod
    def _random_name() -> str:
        # 保留旧 API 兼容；新流程走 persona generator
        return _humanlike_local_part()

    def _next_outlook_account(self) -> OutlookAccount:
        if self._outlook_source == "db":
            return self._reserve_outlook_account_from_db()
        if not self._outlook_accounts:
            raise RuntimeError("mail.provider=outlook 但未配置 outlook_accounts_path/outlook_accounts")
        if self._outlook_state_path:
            idx = self._reserve_outlook_index()
            return self._outlook_accounts[idx % len(self._outlook_accounts)]
        acc = self._outlook_accounts[self._outlook_cursor % len(self._outlook_accounts)]
        self._outlook_cursor += 1
        return acc

    def _reserve_outlook_index(self) -> int:
        assert self._outlook_state_path is not None
        self._outlook_state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._outlook_state_path, "a+", encoding="utf-8") as f:
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read().strip()
                try:
                    state = json.loads(raw) if raw else {}
                except Exception:
                    state = {}
                idx = int(state.get("next_index") or 0)
                state["next_index"] = idx + 1
                state["updated_at"] = time.time()
                f.seek(0)
                f.truncate()
                f.write(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
                f.flush()
                return idx
            finally:
                try:
                    if os.name == "nt":
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass

    def _reserve_outlook_account_from_db(self) -> OutlookAccount:
        if not self._outlook_db_path.exists():
            raise RuntimeError(f"Outlook 数据库不存在: {self._outlook_db_path}")
        now = time.time()
        retry_after_s = float(
            getattr(self.cfg, "outlook_reserved_retry_after_s", 0)
            or os.environ.get("OUTLOOK_RESERVED_RETRY_AFTER_S", "1800")
            or 1800
        )
        stale_reserved_before = now - max(0.0, retry_after_s)
        with sqlite3.connect(self._outlook_db_path, isolation_level=None, timeout=15) as c:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout = 15000")
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                """
                SELECT id, email, password, client_id, refresh_token
                FROM outlook_mail_accounts
                WHERE status='available'
                   OR (status='reserved' AND reserved_at <= ?)
                ORDER BY CASE status WHEN 'available' THEN 0 ELSE 1 END, id ASC
                LIMIT 1
                """,
                (stale_reserved_before,),
            ).fetchone()
            if not row:
                c.execute("COMMIT")
                raise RuntimeError("Outlook 邮箱池已耗尽：没有可复用账号（available 或已过期 reserved）")
            c.execute(
                """
                UPDATE outlook_mail_accounts
                SET status='reserved', reserved_at=?, updated_at=?, last_error=''
                WHERE id=?
                """,
                (now, now, int(row["id"])),
            )
            c.execute("COMMIT")
        self._current_outlook_db_id = int(row["id"])
        return OutlookAccount(row["email"], row["password"], row["client_id"], row["refresh_token"])

    def _mark_outlook_db_account(self, status: str, error: str = "") -> None:
        if not self._current_outlook_db_id:
            return
        now = time.time()
        with sqlite3.connect(self._outlook_db_path, isolation_level=None, timeout=15) as c:
            if status == "used":
                c.execute(
                    """
                    UPDATE outlook_mail_accounts
                    SET status='used', used_at=?, updated_at=?, last_error=''
                    WHERE id=?
                    """,
                    (now, now, self._current_outlook_db_id),
                )
            elif status == "failed":
                c.execute(
                    """
                    UPDATE outlook_mail_accounts
                    SET status='failed', updated_at=?, last_error=?
                    WHERE id=?
                    """,
                    (now, str(error or "")[:500], self._current_outlook_db_id),
                )

    def mark_current_mailbox_used(self) -> None:
        """Keep Outlook mailbox reserved after registration.

        Outlook rows are consumed only after payment succeeds.  Registration
        success alone must remain retryable because a later payment step may
        fail or the task may be interrupted.
        """
        return None

    def create_mailbox(self) -> str:
        """生成 random@catch_all 邮箱地址（也可复用 _reuse_email）。

        同时将算法生成的完整 persona 缓存到 `self.last_persona`，
        `browser_register` 通过该字段读取与邮箱同源的姓名 / 密码。
        """
        if self._reuse_email:
            addr = self._reuse_email
            self._reuse_email = None
            logger.info(f"复用邮箱: {addr}")
            self.last_persona = None  # resume 路径无法回推 first/last
            return addr
        if self.provider == "outlook":
            acc = self._next_outlook_account()
            self._current_outlook = acc
            self.last_persona = None
            logger.info(f"邮箱已取用: {acc.email} (路径: Outlook Graph API)")
            return acc.email
        if not self.catch_all_domain:
            raise RuntimeError(
                "MailProvider.create_mailbox: catch_all_domain 未配置；"
                "CF Email Worker 路径需要 catch-all 子域（在 zone 内）"
            )
        persona = self._persona_gen.next()
        self.last_persona = persona
        logger.info(
            f"邮箱已创建: {persona.email} | persona={persona.first} {persona.last} "
            f"(路径: CF Email Worker → KV)"
        )
        return persona.email

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """阻塞等 OTP。直接走 CF KV，不再有 IMAP fallback。

        失败抛 TimeoutError 或 RuntimeError。原 IMAP 路径已删除——
        QQ 邮箱 / auth_code 这些参数全部废弃。
        """
        if self.provider == "outlook":
            return self._wait_for_outlook_otp(
                email_addr, timeout=timeout, issued_after=issued_after
            )
        from cf_kv_otp_provider import CloudflareKVOtpProvider

        logger.info(
            f"[mail] 走 CF KV 取 OTP -> {email_addr} (timeout={timeout}s)"
        )
        provider = CloudflareKVOtpProvider.from_env_or_secrets()
        return provider.wait_for_otp(
            email_addr, timeout=timeout, issued_after=issued_after
        )

    def _outlook_account_for_email(self, email_addr: str) -> OutlookAccount:
        target = str(email_addr or "").strip().lower()
        if self._current_outlook and self._current_outlook.email.lower() == target:
            return self._current_outlook
        for acc in self._outlook_accounts:
            if acc.email.lower() == target:
                self._current_outlook = acc
                return acc
        if self._outlook_source == "db":
            if not self._outlook_db_path.exists():
                raise RuntimeError(f"Outlook 数据库不存在: {self._outlook_db_path}")
            with sqlite3.connect(self._outlook_db_path, isolation_level=None, timeout=15) as c:
                c.row_factory = sqlite3.Row
                row = c.execute(
                    """
                    SELECT id, email, password, client_id, refresh_token
                    FROM outlook_mail_accounts
                    WHERE lower(email) = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (target,),
                ).fetchone()
            if row:
                self._current_outlook_db_id = int(row["id"])
                self._current_outlook = OutlookAccount(
                    row["email"],
                    row["password"],
                    row["client_id"],
                    row["refresh_token"],
                )
                return self._current_outlook
        raise RuntimeError(f"Outlook 账号池找不到邮箱: {email_addr}")

    def _outlook_access_token(self, acc: OutlookAccount) -> str:
        now = time.time()
        if acc.access_token and acc.expires_at > now + 60:
            return acc.access_token
        resp = requests.post(
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
            data={
                "client_id": acc.client_id,
                "grant_type": "refresh_token",
                "refresh_token": acc.refresh_token,
                "scope": "https://graph.microsoft.com/Mail.Read offline_access",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Outlook refresh_token 失败: http={resp.status_code} body={resp.text[:240]}")
        data = resp.json()
        acc.access_token = str(data.get("access_token") or "")
        if not acc.access_token:
            raise RuntimeError(f"Outlook refresh_token 未返回 access_token: {json.dumps(data, ensure_ascii=False)[:240]}")
        if data.get("refresh_token"):
            acc.refresh_token = str(data.get("refresh_token"))
        try:
            acc.expires_at = now + float(data.get("expires_in") or 3600)
        except Exception:
            acc.expires_at = now + 3600
        return acc.access_token

    def _wait_for_outlook_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        acc = self._outlook_account_for_email(email_addr)
        interval = max(1.0, float(getattr(self.cfg, "outlook_poll_interval_s", 3.0) or 3.0))
        folder = str(getattr(self.cfg, "outlook_folder", "Inbox") or "Inbox").strip() or "Inbox"
        deadline = time.time() + max(10, int(timeout))
        issued_after = float(issued_after or 0)
        logger.info(f"[mail] 走 Outlook Graph 取 OTP -> {email_addr} (timeout={timeout}s)")
        last_error = ""
        while time.time() < deadline:
            try:
                token = self._outlook_access_token(acc)
                url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages"
                resp = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "$top": "10",
                        "$select": "subject,bodyPreview,receivedDateTime,from",
                        "$orderby": "receivedDateTime desc",
                    },
                    timeout=20,
                )
                if resp.status_code in (401, 403):
                    acc.access_token = ""
                    last_error = f"http={resp.status_code} {resp.text[:160]}"
                    time.sleep(interval)
                    continue
                if resp.status_code != 200:
                    last_error = f"http={resp.status_code} {resp.text[:160]}"
                    time.sleep(interval)
                    continue
                for msg in (resp.json().get("value") or []):
                    received = _parse_graph_time(str(msg.get("receivedDateTime") or ""))
                    if issued_after and received and received + 60 < issued_after:
                        continue
                    text = f"{msg.get('subject') or ''}\n{msg.get('bodyPreview') or ''}"
                    code = _extract_otp6(text)
                    if code:
                        logger.info(f"[mail] Outlook 收到 OTP={code} key={email_addr}")
                        return code
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:160]}"
            time.sleep(interval)
        detail = f"; last_error={last_error}" if last_error else ""
        if bool(getattr(self.cfg, "outlook_mark_failed_on_otp_timeout", True)):
            self._mark_outlook_db_account("failed", detail or "otp timeout")
        raise TimeoutError(f"等待 Outlook OTP 超时 ({timeout}s): {email_addr}{detail}")
