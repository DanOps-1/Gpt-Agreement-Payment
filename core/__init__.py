"""Shared layer: pure utilities extracted from CTF-pay/CTF-reg that are independent of specific payment/registration workflows.

Modules:
- otp_extractor: parse OTP codes from text/JSON payload (regex + timestamp validation)
- otp_providers: four types of OTP providers - CLI / file polling / HTTP polling / subcommand
- jwt_decode: OAuth JWT decoding + expiration validation + email/plan field extraction

Future Wave will also extract http_session, chatgpt_auth, logging to this layer."""
