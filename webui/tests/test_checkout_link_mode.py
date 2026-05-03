from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("card_mod", ROOT / "CTF-pay" / "card.py")
card = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(card)  # type: ignore[union-attr]


def test_hosted_checkout_defaults_to_provider_long_url():
    selected = card._select_fresh_checkout_url(
        provider_url="https://pay.openai.com/c/pay/cs_live_long#frag",
        canonical_url="https://chatgpt.com/checkout/openai_llc/cs_live_long",
        fresh_cfg={"plan": {"checkout_ui_mode": "hosted"}},
        checkout_payload={"checkout_ui_mode": "hosted"},
    )
    assert selected == "https://pay.openai.com/c/pay/cs_live_long#frag"


def test_custom_checkout_defaults_to_canonical_short_url():
    selected = card._select_fresh_checkout_url(
        provider_url="https://pay.openai.com/c/pay/cs_live_short#frag",
        canonical_url="https://chatgpt.com/checkout/openai_llc/cs_live_short",
        fresh_cfg={"plan": {"checkout_ui_mode": "custom"}},
        checkout_payload={"checkout_ui_mode": "custom"},
    )
    assert selected == "https://chatgpt.com/checkout/openai_llc/cs_live_short"


def test_explicit_output_url_mode_overrides_checkout_ui_mode():
    selected = card._select_fresh_checkout_url(
        provider_url="https://pay.openai.com/c/pay/cs_live_forced#frag",
        canonical_url="https://chatgpt.com/checkout/openai_llc/cs_live_forced",
        fresh_cfg={"plan": {"checkout_ui_mode": "custom", "output_url_mode": "provider"}},
        checkout_payload={"checkout_ui_mode": "custom"},
    )
    assert selected == "https://pay.openai.com/c/pay/cs_live_forced#frag"
