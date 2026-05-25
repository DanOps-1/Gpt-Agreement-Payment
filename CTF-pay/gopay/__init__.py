"""GoPay tokenization payment package.

Wave D-1 (2026-05-18): Original CTF-pay/gopay.py (1749 lines) moved entirely into `_monolith`,
old sibling files (gopay_sign / gopay_sign_v2 / gopay_protocol_pay /
gopay_protocol_client_v2 / gopay_client) all moved into sign/ + protocol/ subpackages.
__init__.py here re-exports all external symbols used by (qris.py / card.py / pipeline),
to keep existing `from gopay import X` from breaking."""

from gopay._monolith import (  # noqa: F401
    DEFAULT_MIDTRANS_CLIENT_ID,
    DEFAULT_STRIPE_PK,
    DEFAULT_TIMEOUT,
    DEFAULT_OTP_REGEX,
    GoPayCharger,
    GoPayError,
    OTPCancelled,
    GoPayPINRejected,
    _new_session,
    _build_chatgpt_session,
    _load_cfg,
    _extract_otp_from_payload,
    _extract_otp_from_text,
    _parse_payload_timestamp,
    _json_path_get,
    cli_otp_provider,
    file_watch_otp_provider,
    whatsapp_file_otp_provider,
    whatsapp_http_otp_provider,
    command_otp_provider,
    build_configured_otp_provider,
    main,
)
