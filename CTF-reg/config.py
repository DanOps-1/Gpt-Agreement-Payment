"""Automated card binding payment - configuration file"""
import os
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MailConfig:
    """Email service configuration (CF Email Worker → KV path).

    OTP goes through Cloudflare Email Routing → otp-relay Worker → KV. Original IMAP/SMTP
    fields (imap_server/imap_port/smtp_*/email/auth_code) are completely deprecated; residual
    these fields in old config files will be silently ignored by Config.from_file.

    KV credentials (api_token / account_id / kv_namespace_id) go in SQLite runtime_meta[secrets]
    cloudflare section or environment variables, not in MailConfig."""
    catch_all_domain: str = ""
    # Domain pool: pipeline runtime picks one from it as catch_all_domain (round-robin + burn based on invite probe results)
    catch_all_domains: list = field(default_factory=list)
    # Cloudflare provisions subdomains on-demand (read and used by pipeline, CTF-reg itself does not handle)
    auto_provision: dict = field(default_factory=dict)


@dataclass
class CardInfo:
    """Credit card information"""
    number: str = ""
    cvc: str = ""
    exp_month: str = ""
    exp_year: str = ""


@dataclass
class BillingInfo:
    """Billing information"""
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
    """Team/Plus plan configuration"""
    plan_name: str = "chatgptteamplan"
    workspace_name: str = "MyWorkspace"
    price_interval: str = "month"
    seat_quantity: int = 5
    promo_campaign_id: str = "team-1-month-free"
    is_coupon_from_query_param: bool = False
    checkout_ui_mode: str = "custom"
    output_url_mode: str = ""
    # The following fields are written by webui wizard; CTF-reg does not directly consume but needs to support loading
    plan_type: str = "team"           # team | plus
    entry_point: str = ""             # team_workspace_purchase_modal | all_plans_pricing_modal
    billing_country: str = ""
    billing_currency: str = ""


@dataclass
class CaptchaConfig:
    """CAPTCHA solving service configuration"""
    api_url: str = ""  # CAPTCHA platform API base URL compatible with createTask/getTaskResult protocol
    client_key: str = ""


@dataclass
class Config:
    """Global configuration"""
    mail: MailConfig = field(default_factory=MailConfig)
    card: CardInfo = field(default_factory=CardInfo)
    billing: BillingInfo = field(default_factory=BillingInfo)
    team_plan: TeamPlanConfig = field(default_factory=TeamPlanConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    proxy: Optional[str] = None
    # Existing credentials (optional, used when skipping registration and paying directly)
    session_token: Optional[str] = None
    access_token: Optional[str] = None
    device_id: Optional[str] = None
    # Stripe
    stripe_build_hash: str = "f197c9c0f0"

    @classmethod
    def from_file(cls, path: str) -> "Config":
        """Load configuration from JSON file"""
        import dataclasses

        def filtered_kwargs(dataclass_type, raw: dict | None) -> dict:
            # WebUI and CTF-pay will gradually add configuration fields; CTF-reg only consumes part of them.
            # Filter unknown keys during loading to avoid interrupting registration flow due to "payment fields unused in registration phase".
            valid_keys = {f.name for f in dataclasses.fields(dataclass_type)}
            return {k: v for k, v in (raw or {}).items() if k in valid_keys}

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = cls()
        if "mail" in data:
            # Filter deprecated IMAP/SMTP fields (imap_server, imap_port, smtp_*,
            # email, auth_code), allowing old config to still work without throwing unexpected
            # keyword errors. New code should only configure catch_all_domain(s) + auto_provision.
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
