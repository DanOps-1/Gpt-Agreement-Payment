"""邮箱服务（outlook 池 + CF Email Routing fallback）。

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

import logging
import random
import time
from typing import Optional

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


class MailProvider:
    """生成 catch-all 子域随机邮箱 + 委托 CF KV provider 取 OTP。

    `last_persona` 暴露最近一次 `create_mailbox()` 产生的完整 persona
    （邮箱 / first / last / 密码），供 `browser_register` 复用，
    确保「邮箱 first-name 与注册显示姓名一致」——OpenAI 反欺诈系统
    会对二者不一致打负分。
    """

    def __init__(self, catch_all_domain: str = ""):
        self.catch_all_domain = catch_all_domain
        self._reuse_email: Optional[str] = None  # 兼容 register-only resume
        # 算法化 persona 生成器（音节合成法，详见 persona.py）
        from persona import PersonaGenerator, Persona
        self._persona_gen = PersonaGenerator(catch_all_domain)
        self.last_persona: Optional[Persona] = None
        # outlook 池 claim 后保存这三段，wait_for_otp 走 IMAP OAuth2
        self._outlook_creds: Optional[dict] = None  # {email, refresh_token, client_id}

    @staticmethod
    def _random_name() -> str:
        # 保留旧 API 兼容；新流程走 persona generator
        return _humanlike_local_part()

    def create_mailbox(self) -> str:
        """二选一: outlook 池 / domain catch-all. 严格互斥, 不混合不 fallback.

        env 透传 (webui runner 写, CLI 用户用 cfg.mail.source 默认):
          WEBUI_MAIL_SOURCE = "outlook" | "catch_all"
          WEBUI_OUTLOOK_EMAIL = "<email>" (仅 source=outlook 时, 空 = claim_next)

        失败行为 (设计意图: 让 user 立刻知道该补哪个池):
          - source=outlook 池空 → 抛错 ("去 /outlook 页 import 邮箱")
          - source=outlook 指定 email 不在池 → 抛错
          - source=catch_all 没配 domain → 抛错 ("去 wizard 配 catch_all_domain")
        """
        if self._reuse_email:
            addr = self._reuse_email
            self._reuse_email = None
            logger.info(f"复用邮箱: {addr}")
            self.last_persona = None
            return addr

        import os as _os
        # source 来源优先级: env > cfg.mail.source > 默认 catch_all (历史行为兼容 CLI)
        _source = (_os.environ.get("WEBUI_MAIL_SOURCE", "") or "").strip().lower()
        if not _source:
            # 兼容旧 env: WEBUI_MAIL_MODE=outlook 等价于 source=outlook
            _legacy = (_os.environ.get("WEBUI_MAIL_MODE", "") or "").strip().lower()
            if _legacy == "outlook":
                _source = "outlook"
            else:
                # 默认 catch_all: catch_all_domain 配了就用, 没配抛错
                _source = "catch_all"
        _outlook_email = (_os.environ.get("WEBUI_OUTLOOK_EMAIL", "") or "").strip()

        if _source == "outlook":
            return self._create_outlook_mailbox(_outlook_email)
        elif _source == "catch_all":
            return self._create_catchall_mailbox()
        else:
            raise RuntimeError(
                f"未知 mail source: {_source!r} (合法: outlook / catch_all)"
            )

    def _create_outlook_mailbox(self, target_email: str = "") -> str:
        """从 outlook 池 claim 一个号. 池空 / 指定号不可用 → 抛错, 不 fallback."""
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _root = _Path(__file__).resolve().parents[2]  # 仓库根
            if str(_root) not in _sys.path:
                _sys.path.insert(0, str(_root))
            from webui.backend import outlook_pool
        except Exception as e:
            raise RuntimeError(f"outlook_pool 模块不可用: {e}") from e

        if target_email:
            claimed = outlook_pool.claim_email(target_email)
            if not claimed:
                raise RuntimeError(
                    f"outlook 模式指定账号 {target_email} 不可用 "
                    f"(已 in_use/used/dead 或不在池子里)"
                )
            origin = "指定"
        else:
            claimed = outlook_pool.claim_next()
            if not claimed:
                raise RuntimeError(
                    "outlook 模式池空 (无 available 账号). "
                    "去 /outlook 页粘贴 4 段格式批量导入 (Thunderbird client_id 才能自动 IMAP)."
                )
            origin = "claim_next"

        self._outlook_creds = claimed
        self.last_persona = None  # outlook 不用算法 persona
        logger.info(
            f"邮箱已创建: {claimed['email']} | (源=outlook池 {origin}, IMAP OAuth2 收 OTP)"
        )
        return claimed["email"]

    def _create_catchall_mailbox(self) -> str:
        """从 catch_all_domain 算法生成 persona@domain. domain 没配 → 抛错."""
        if not self.catch_all_domain:
            raise RuntimeError(
                "catch_all 模式但 catch_all_domain 没配. "
                "去 webui /wizard 邮箱段配置 catch_all_domain + CF Email Worker."
            )
        persona = self._persona_gen.next()
        self.last_persona = persona
        logger.info(
            f"邮箱已创建: {persona.email} | persona={persona.first} {persona.last} "
            f"(源=catch_all → CF Email Worker → KV 收 OTP)"
        )
        return persona.email

    def mark_outlook_dead(self, reason: str = "") -> None:
        """auth_flow 检测到 OpenAI '已有账号' 分支时调用：当前 outlook 已被 OpenAI 注册过，
        mark dead 防止下次再被 claim。"""
        creds = self._outlook_creds
        if not creds:
            return
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _root = _Path(__file__).resolve().parents[2]  # Wave H bug: mail/provider.py 多沉一层, parents[1] 现在指 CTF-reg/ 而非仓库根
            if str(_root) not in _sys.path:
                _sys.path.insert(0, str(_root))
            from webui.backend import outlook_pool as _op
            _op.mark_dead(creds["email"], reason=reason or "OpenAI 已识别为已注册")
            logger.info(f"[mail] outlook 已 mark dead: {creds['email']}  reason={reason!r}")
        except Exception as e:
            logger.warning(f"[mail] mark_outlook_dead 失败 (不致命): {e}")

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """阻塞等 OTP。outlook 池 claim 的账号走 IMAP OAuth2；
        catch-all fallback 走 CF KV。失败抛 TimeoutError / RuntimeError。
        """
        # 当前邮箱来自 outlook 池 → IMAP OAuth2 fetch
        creds = self._outlook_creds
        if creds and creds.get("email", "").lower() == (email_addr or "").lower():
            try:
                import sys as _sys
                from pathlib import Path as _Path
                _root = _Path(__file__).resolve().parents[2]  # Wave H bug: mail/provider.py 多沉一层, parents[1] 现在指 CTF-reg/ 而非仓库根
                if str(_root) not in _sys.path:
                    _sys.path.insert(0, str(_root))
                from webui.backend import outlook_pool as _op
            except Exception as e:
                raise RuntimeError(f"outlook_pool 模块不可用: {e}")
            # protocol 路径下 outlook 池"已有账号"分支需要 60-120s 内见结果——
            # OpenAI 真发邮件通常 5-30s 到达；超过 90s 没邮件 = OpenAI 反欺诈静默拒绝。
            # 给 caller 主权（OTP_TIMEOUT），floor 90s 保留邮件投递抖动余地。
            timeout = max(int(timeout), 90)
            # 严格 threshold：只接受 issued_after 之后到达的邮件, 避免 retry resend 后
            # 抓 server 端已失效的"旧 X 邮件" → verify 401 wrong_email_otp_code.
            # 5s grace 仅容忍 NTP 偏差; OpenAI → outlook 邮件投递延迟通常 < 5s, 测过.
            strict_threshold = (issued_after - 5) if issued_after else (time.time() - 5)
            # 自动化优先级根据 client_id 动态决定:
            #   - Thunderbird 公开 client_id (注册时声明了 v2 IMAP scope) → IMAP 优先 (秒级, 扫 INBOX/Junk/Spam)
            #   - 其它 supplier 自家 client_id (常 v1 wl.imap 限制, IMAP 拒) → web scrape 优先
            # 后备永远是 manual file 兜底.
            THUNDERBIRD_IMAP_OK = {
                "9e5f94bc-e8a4-4e73-b8be-63364c29d753",  # 新 Thunderbird
                "08162f7c-0fd2-4200-a84a-f25a4db0b584",  # 老 Thunderbird
            }
            imap_first = (creds.get("client_id") in THUNDERBIRD_IMAP_OK)
            password = creds.get("password", "")
            otp = None

            def _try_imap():
                logger.info(
                    f"[mail] outlook IMAP OAuth2 取 OTP -> {email_addr} "
                    f"(timeout={timeout}s threshold>={int(strict_threshold)})"
                )
                try:
                    return _op.fetch_otp_via_imap(
                        creds["email"], creds["refresh_token"], creds["client_id"],
                        timeout=timeout, threshold_ts=strict_threshold,
                    )
                except Exception as _e:
                    logger.warning(f"[mail] IMAP XOAUTH2 fail: {_e}")
                    return None

            def _try_web():
                if not password:
                    return None
                logger.info(
                    f"[mail] outlook WEB scrape OTP -> {email_addr} "
                    f"(timeout={timeout}s threshold>={int(strict_threshold)})"
                )
                try:
                    from mail.outlook import scrape_otp as _ows  # Wave H
                    return _ows(email_addr, password, timeout=timeout, threshold_ts=strict_threshold)
                except Exception as _e:
                    logger.warning(f"[mail] web scrape fail: {_e}")
                    return None

            if imap_first:
                logger.info(f"[mail] client_id={creds['client_id'][:8]} 已知 IMAP-OK → IMAP 优先")
                otp = _try_imap() or _try_web()
            else:
                logger.info(f"[mail] client_id={creds['client_id'][:8]} 非 Thunderbird → web 优先")
                otp = _try_web() or _try_imap()

            if not otp:
                # web + IMAP 双 fail → mark_dead + raise. 不能返 None
                # (verify_otp(None) 触发 OpenAI 400 "expected a string, but got null").
                try:
                    self.mark_outlook_dead("web + IMAP 双 fail (OpenAI 静默拒发 OTP)")
                except Exception:
                    pass
                # 设 flag 让 drivers/protocol 跳过 retry resend (已经 web+IMAP 都试了, 再等 180s 浪费)
                self.outlook_exhausted = True
                raise TimeoutError(
                    f"outlook OTP timeout (web + IMAP 双 fail, fast-fail) for {email_addr}"
                )
            # OpenAI 真发了 OTP 收到了 → 邮箱已被 OpenAI 认识, mark used 防 reuse.
            try:
                _op.mark_used(creds["email"], chatgpt_email=creds["email"])
            except Exception as e:
                logger.warning(f"[mail] outlook mark_used 失败（不致命）: {e}")
            return otp

        # fallback：catch-all 邮箱走 CF KV
        from mail.cf_kv import CloudflareKVOtpProvider  # Wave H: cf_kv_otp_provider.py → mail/cf_kv.py
        logger.info(
            f"[mail] 走 CF KV 取 OTP -> {email_addr} (timeout={timeout}s)"
        )
        provider = CloudflareKVOtpProvider.from_env_or_secrets()
        return provider.wait_for_otp(
            email_addr, timeout=timeout, issued_after=issued_after
        )
