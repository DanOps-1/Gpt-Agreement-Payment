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

import logging
import random
import string
from typing import Optional

logger = logging.getLogger(__name__)


class MailProvider:
    """生成 catch-all 子域随机邮箱 + 委托 CF KV provider 取 OTP。"""

    def __init__(self, catch_all_domain: str = ""):
        self.catch_all_domain = catch_all_domain
        self._reuse_email: Optional[str] = None  # 兼容 register-only resume

    @staticmethod
    def _random_name() -> str:
        letters1 = "".join(random.choices(string.ascii_lowercase, k=5))
        numbers = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))
        return letters1 + numbers + letters2

    def create_mailbox(self) -> str:
        """生成 random@catch_all 邮箱地址（也可复用 _reuse_email）。"""
        if self._reuse_email:
            addr = self._reuse_email
            self._reuse_email = None
            logger.info(f"复用邮箱: {addr}")
            return addr
        if not self.catch_all_domain:
            raise RuntimeError(
                "MailProvider.create_mailbox: catch_all_domain 未配置；"
                "CF Email Worker 路径需要 catch-all 子域（在 zone 内）"
            )
        addr = f"{self._random_name()}@{self.catch_all_domain}"
        logger.info(f"邮箱已创建: {addr} (路径: CF Email Worker → KV)")
        return addr

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
        from cf_kv_otp_provider import CloudflareKVOtpProvider

        logger.info(
            f"[mail] 走 CF KV 取 OTP -> {email_addr} (timeout={timeout}s)"
        )
        provider = CloudflareKVOtpProvider.from_env_or_secrets()
        return provider.wait_for_otp(
            email_addr, timeout=timeout, issued_after=issued_after
        )
