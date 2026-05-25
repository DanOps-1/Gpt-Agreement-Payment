"""QRIS Payment Package (Indonesia Central Bank Unified QR Code Standard, without OTP/PIN/binding).

Wave E-1: Original CTF-pay/qris.py (887 lines) integrated into _monolith.py as a whole, subsequent Waves will split into
charger / card_bridge / mock / render / cli submodules."""

from qris._monolith import QrisCharger, main  # noqa: F401
