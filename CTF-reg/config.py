"""
自动化绑卡支付 - 配置文件
"""
import os
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MailConfig:
    """邮箱服务配置（CF Email Worker → KV 路径）。

    OTP 走 Cloudflare Email Routing → otp-relay Worker → KV。原 IMAP/SMTP
    字段（imap_server/imap_port/smtp_*/email/auth_code）已彻底废弃；旧
    config 文件里残留这些字段会被 Config.from_file 静默忽略。

    KV 凭证（api_token / account_id / kv_namespace_id）放 SQLite runtime_meta[secrets]
    的 cloudflare 段或环境变量，不在 MailConfig 里。
    """
    catch_all_domain: str = ""
    # 域名池：pipeline 运行时从中挑一个作为 catch_all_domain（轮换 + 根据 invite 探测结果烧掉）
    catch_all_domains: list = field(default_factory=list)
    # Cloudflare 按需开通子域（被 pipeline 读取使用，CTF-reg 自身不处理）
    auto_provision: dict = field(default_factory=dict)


@dataclass
class CardInfo:
    """信用卡信息"""
    number: str = ""
    cvc: str = ""
    exp_month: str = ""
    exp_year: str = ""


@dataclass
class BillingInfo:
    """账单信息"""
    name: str = "John Smith"
    email: str = ""
    country: str = "US"
    currency: str = "USD"
    address_line1: str = "123 Main St"
    address_line2: str = ""
    address_city: str = "San Francisco"
    address_state: str = "CA"
    postal_code: str = "94105"


@dataclass
class TeamPlanConfig:
    """团队/Plus 计划配置"""
    plan_name: str = "chatgptteamplan"
    workspace_name: str = "MyWorkspace"
    price_interval: str = "month"
    seat_quantity: int = 5
    promo_campaign_id: str = "team-1-month-free"
    is_coupon_from_query_param: bool = False
    checkout_ui_mode: str = "custom"
    output_url_mode: str = ""
    # 以下字段由 webui wizard 写入，CTF-reg 不直接消费但需要兼容加载
    plan_type: str = "team"           # team | plus
    entry_point: str = ""             # team_workspace_purchase_modal | all_plans_pricing_modal
    billing_country: str = ""
    billing_currency: str = ""


@dataclass
class CaptchaConfig:
    """验证码打码服务配置"""
    api_url: str = ""  # 兼容 createTask/getTaskResult 协议的打码平台 API base URL
    client_key: str = ""


@dataclass
class Config:
    """总配置"""
    mail: MailConfig = field(default_factory=MailConfig)
    card: CardInfo = field(default_factory=CardInfo)
    billing: BillingInfo = field(default_factory=BillingInfo)
    team_plan: TeamPlanConfig = field(default_factory=TeamPlanConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    proxy: Optional[str] = None
    # 已有凭证（可选，跳过注册直接支付时使用）
    session_token: Optional[str] = None
    access_token: Optional[str] = None
    device_id: Optional[str] = None
    # Stripe
    stripe_build_hash: str = "f197c9c0f0"

    @classmethod
    def from_file(cls, path: str) -> "Config":
        """从 JSON 文件加载配置"""
        import dataclasses

        def filtered_kwargs(dataclass_type, raw: dict | None) -> dict:
            # WebUI 与 CTF-pay 会逐步增加配置字段；CTF-reg 只消费其中一部分。
            # 加载时过滤未知 key，避免因为“注册阶段不用的支付字段”中断注册流程。
            valid_keys = {f.name for f in dataclasses.fields(dataclass_type)}
            return {k: v for k, v in (raw or {}).items() if k in valid_keys}

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = cls()
        if "mail" in data:
            # 过滤已废弃的 IMAP/SMTP 字段（imap_server, imap_port, smtp_*,
            # email, auth_code），让旧 config 仍然能跑而不抛 unexpected
            # keyword 错。新代码请只配 catch_all_domain(s) + auto_provision。
            cfg.mail = MailConfig(**filtered_kwargs(MailConfig, data["mail"]))
        if "card" in data:
            cfg.card = CardInfo(**filtered_kwargs(CardInfo, data["card"]))
        if "billing" in data:
            cfg.billing = BillingInfo(**filtered_kwargs(BillingInfo, data["billing"]))
        if "team_plan" in data:
            cfg.team_plan = TeamPlanConfig(**filtered_kwargs(TeamPlanConfig, data["team_plan"]))
        if "captcha" in data:
            cfg.captcha = CaptchaConfig(**filtered_kwargs(CaptchaConfig, data["captcha"]))
        cfg.proxy = data.get("proxy")
        cfg.session_token = data.get("session_token")
        cfg.access_token = data.get("access_token")
        cfg.device_id = data.get("device_id")
        cfg.stripe_build_hash = data.get("stripe_build_hash", cfg.stripe_build_hash)
        return cfg

    def to_dict(self) -> dict:
        return {
            "mail": self.mail.__dict__,
            "card": self.card.__dict__,
            "billing": self.billing.__dict__,
            "team_plan": self.team_plan.__dict__,
            "captcha": self.captcha.__dict__,
            "proxy": self.proxy,
            "session_token": self.session_token,
            "access_token": self.access_token,
            "device_id": self.device_id,
            "stripe_build_hash": self.stripe_build_hash,
        }
