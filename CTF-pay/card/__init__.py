"""Stripe Checkout / 3D Secure / hCaptcha 自动化支付包。

Wave F-1 (2026-05-18): 原 CTF-pay/card.py (9050 行) 整体进 _monolith.py,
后续 Wave 再拆 cli / flow / checkout / stripe_3ds / fingerprint / datadome /
oauth_exchange / errors 等子模块。

__init__.py 这里 re-export 整个 _monolith 命名空间, 让 pipeline._monolith
的三处 `from card import (...)` / `import card as card_mod` 都仍可用。
"""

from card._monolith import *  # noqa: F401, F403
from card._monolith import (  # 显式 re-export 确保 wildcard 不漏 _ 开头符号
    _exchange_refresh_token_with_session,
    _build_proxy_url_from_cfg,
    _codex_oauth_client_id_from_config,
    _create_chatgpt_http_session,
    _decode_jwt_payload,
    _extract_email_from_access_token,
    _extract_plan_type_from_access_token,
    _is_access_token_expired,
    ChallengeReconfirmRequired,
    CheckoutSessionInactive,
    FreshCheckoutAuthError,
    main,
)
