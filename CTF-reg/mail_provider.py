"""邮箱服务。

默认路径仍是 Cloudflare Email Worker → KV：

    寄件人 → CF MX (catch-all) → otp-relay Worker → KV
                                                       ↓
                                            cf_kv_otp_provider 读

也支持 LuckMail OpenAPI Mode A 的 Microsoft Graph 邮箱接码：

    LuckMail 创建 ms_graph 订单 → 返回 outlook/hotmail 地址
    OpenAI 发 OTP → LuckMail order/code 轮询验证码

OTP 提取由 Worker 端做（见 scripts/otp_email_worker.js），
或由 LuckMail 项目规则提取。上层只依赖两个动作：
  1. 创建可收码邮箱 (`create_mailbox`)
  2. 阻塞拿 OTP (`wait_for_otp`)

KV 凭证读取顺序：环境变量 `CF_API_TOKEN/CF_ACCOUNT_ID/CF_OTP_KV_NAMESPACE_ID`
→ SQLite runtime_meta[secrets] 的 cloudflare 段。详见 cf_kv_otp_provider.py。

LuckMail 凭证读取顺序：环境变量 `LUCKMAIL_API_KEY`
→ SQLite runtime_meta[secrets] 的 luckmail 段 → mail.luckmail（兼容独立运行）。
"""
from __future__ import annotations

import logging
import random
from typing import Any, Optional

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
    """统一邮件接码 provider。

    `last_persona` 暴露最近一次 `create_mailbox()` 产生的完整 persona
    （邮箱 / first / last / 密码），供 `browser_register` 复用，
    确保「邮箱 first-name 与注册显示姓名一致」——OpenAI 反欺诈系统
    会对二者不一致打负分。
    """

    def __init__(
        self,
        catch_all_domain: str = "",
        *,
        config: Any = None,
        provider: str = "",
    ):
        raw_cfg = self._raw_config(config)
        if raw_cfg and not catch_all_domain:
            catch_all_domain = str(raw_cfg.get("catch_all_domain") or "")
        self.catch_all_domain = catch_all_domain
        self.provider = self._normalize_provider(
            provider or str(raw_cfg.get("provider") or "")
        )
        self._raw_mail_config = raw_cfg
        self._reuse_email: Optional[str] = None  # 兼容 register-only resume
        self._luckmail_orders_by_email: dict[str, Any] = {}
        self._last_luckmail_order: Any = None
        # 算法化 persona 生成器（音节合成法，详见 persona.py）
        from persona import PersonaGenerator, Persona
        self._persona_gen = PersonaGenerator(catch_all_domain)
        self.last_persona: Optional[Persona] = None

    @classmethod
    def from_config(cls, mail_config: Any) -> "MailProvider":
        raw = cls._raw_config(mail_config)
        return cls(
            str(raw.get("catch_all_domain") or ""),
            config=mail_config,
            provider=str(raw.get("provider") or ""),
        )

    @staticmethod
    def _raw_config(config: Any) -> dict:
        if config is None:
            return {}
        if isinstance(config, dict):
            return dict(config)
        data = getattr(config, "__dict__", None)
        return dict(data or {})

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        p = (provider or "").strip().lower()
        if p in ("", "cf", "cloudflare", "cloudflare_kv", "cf_kv"):
            return "cloudflare_kv"
        if p in ("luckmail", "luckyous", "luckyous_ms_graph", "luckmail_ms_graph", "ms_graph"):
            return "luckmail_ms_graph"
        return p

    def _is_luckmail(self) -> bool:
        return self.provider == "luckmail_ms_graph"

    @staticmethod
    def _random_name() -> str:
        # 保留旧 API 兼容；新流程走 persona generator
        return _humanlike_local_part()

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
            self.prepare_for_otp(addr, reason="reuse_email")
            return addr
        if self._is_luckmail():
            from luckmail_provider import LuckMailOpenAPIClient, resolve_luckmail_config

            cfg = resolve_luckmail_config(self._raw_mail_config)
            client = LuckMailOpenAPIClient(
                api_key=cfg["api_key"],
                base_url=cfg["base_url"],
            )
            order = client.create_order(
                project_code=cfg["project_code"],
                email_type=cfg["email_type"],
                domain=cfg["domain"],
                specified_email=cfg["specified_email"],
            )
            email = order.email_address.strip().lower()
            self._luckmail_orders_by_email[email] = order
            self._last_luckmail_order = order
            self.last_persona = None
            logger.info(
                "[mail] LuckMail ms_graph 订单已创建: email=%s order=%s project=%s",
                email,
                order.order_no,
                order.project or cfg["project_code"],
            )
            return email
        if not self.catch_all_domain:
            raise RuntimeError(
                "MailProvider.create_mailbox: catch_all_domain 未配置；"
                "Cloudflare Email Worker 路径需要 catch-all 子域（在 zone 内）。"
                "如需 LuckMail，请配置 mail.provider=luckmail_ms_graph"
            )
        persona = self._persona_gen.next()
        self.last_persona = persona
        logger.info(
            f"邮箱已创建: {persona.email} | persona={persona.first} {persona.last} "
            f"(路径: CF Email Worker → KV)"
        )
        return persona.email

    def prepare_for_otp(self, email_addr: str, reason: str = "") -> None:
        """Hook called before an upstream service sends an OTP.

        Cloudflare catch-all needs no preparation. LuckMail needs an active
        order before the OTP email arrives; for already-known accounts we create
        a `specified_email` order for that address.
        """
        if not self._is_luckmail():
            return
        key = email_addr.strip().lower()
        if not key:
            return
        if key in self._luckmail_orders_by_email:
            return

        from luckmail_provider import LuckMailOpenAPIClient, resolve_luckmail_config

        cfg = resolve_luckmail_config(self._raw_mail_config)
        client = LuckMailOpenAPIClient(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
        )
        order = client.create_order(
            project_code=cfg["project_code"],
            email_type=cfg["email_type"],
            domain=cfg["domain"],
            specified_email=key,
        )
        self._luckmail_orders_by_email[key] = order
        self._last_luckmail_order = order
        logger.info(
            "[mail] LuckMail 指定邮箱订单已创建: email=%s order=%s reason=%s",
            key,
            order.order_no,
            reason or "otp",
        )

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """阻塞等 OTP。按当前 provider 走 CF KV 或 LuckMail。

        失败抛 TimeoutError 或 RuntimeError。原 IMAP 路径已删除——
        QQ 邮箱 / auth_code 这些参数全部废弃。
        """
        if self._is_luckmail():
            from luckmail_provider import LuckMailOpenAPIClient, resolve_luckmail_config

            key = email_addr.strip().lower()
            self.prepare_for_otp(key, reason="wait_for_otp")
            order = self._luckmail_orders_by_email.get(key) or self._last_luckmail_order
            if not order:
                raise RuntimeError(f"LuckMail 未找到可轮询订单: {email_addr}")
            cfg = resolve_luckmail_config(self._raw_mail_config)
            client = LuckMailOpenAPIClient(
                api_key=cfg["api_key"],
                base_url=cfg["base_url"],
            )
            wait_timeout = max(int(timeout or 0), int(cfg["timeout_seconds"] or 0), 30)
            logger.info(
                "[mail] 走 LuckMail ms_graph 取 OTP -> %s order=%s timeout=%ss",
                key,
                order.order_no,
                wait_timeout,
            )
            try:
                code = client.poll_code(
                    order.order_no,
                    timeout=wait_timeout,
                    poll_interval_s=cfg["poll_interval_s"],
                )
                self._luckmail_orders_by_email.pop(key, None)
                return code
            except Exception:
                # Timeout/异常时取消未完成订单，避免占用库存；成功订单不能取消。
                try:
                    client.cancel_order(order.order_no)
                except Exception:
                    pass
                self._luckmail_orders_by_email.pop(key, None)
                raise

        from cf_kv_otp_provider import CloudflareKVOtpProvider

        logger.info(
            f"[mail] 走 CF KV 取 OTP -> {email_addr} (timeout={timeout}s)"
        )
        provider = CloudflareKVOtpProvider.from_env_or_secrets()
        return provider.wait_for_otp(
            email_addr, timeout=timeout, issued_after=issued_after
        )
