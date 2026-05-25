"""Stripe Checkout / 3D Secure / hCaptcha automated payment package.

Wave F-1 (2026-05-18): Original CTF-pay/card.py (9050 lines) moved entirely into _monolith.py,
subsequent Waves will split into cli / flow / checkout / stripe_3ds / fingerprint / datadome /
oauth_exchange / errors and other submodules.

__init__.py re-exports the entire _monolith namespace here, so that the three places in pipeline._monolith
`from card import (...)` / `import card as card_mod` remain usable."""

from card._monolith import *  # noqa: F401, F403
from card._monolith import (  # Explicit re-export ensures wildcard does not miss symbols starting with _
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
