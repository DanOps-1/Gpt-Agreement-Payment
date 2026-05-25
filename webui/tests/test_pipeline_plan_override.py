"""Verify --plan temporary override logic in pipeline.py + card.py abcard payload under Plus
no longer carries workspace/seat. Both fixes ensure PayPal protocol payments under Plus subscription
are not rejected by ChatGPT backend (plan_name=plus but with team_plan_data → 400)."""

import json
import sys

import pipeline


def test_apply_plan_override_plus_strips_team_fields(tmp_path):
    cfg_path = tmp_path / "config.paypal.json"
    cfg_path.write_text(json.dumps({
        "fresh_checkout": {
            "plan": {
                "plan_name": "chatgptteamplan",
                "workspace_name": "MyWorkspace",
                "seat_quantity": 5,
                "promo_campaign_id": "team-1-month-free",
                "billing_country": "IE",
            }
        }
    }), encoding="utf-8")

    patched = pipeline._apply_plan_override(str(cfg_path), "plus")
    assert patched != str(cfg_path)  # Temporary file, do not touch user's original file

    patched_cfg = json.loads(open(patched, encoding="utf-8").read())
    plan = patched_cfg["fresh_checkout"]["plan"]
    assert plan["plan_name"] == "chatgptplusplan"
    assert plan["entry_point"] == "all_plans_pricing_modal"
    # Critical: team-only fields must be stripped
    assert "workspace_name" not in plan
    assert "seat_quantity" not in plan
    # User-configured billing_country must not be dropped
    assert plan["billing_country"] == "IE"

    # Original file unchanged
    orig = json.loads(cfg_path.read_text())
    assert orig["fresh_checkout"]["plan"]["plan_name"] == "chatgptteamplan"
    assert orig["fresh_checkout"]["plan"]["workspace_name"] == "MyWorkspace"


def test_apply_plan_override_team_fills_defaults(tmp_path):
    """Team reverse: plan_name is still team, but complete the promo/entry_point missing in example configuration."""
    cfg_path = tmp_path / "config.paypal.json"
    cfg_path.write_text(json.dumps({
        "fresh_checkout": {
            "plan": {
                "workspace_name": "MyTeam",
                "seat_quantity": 3,
            }
        }
    }), encoding="utf-8")

    patched = pipeline._apply_plan_override(str(cfg_path), "team")
    plan = json.loads(open(patched, encoding="utf-8").read())["fresh_checkout"]["plan"]
    assert plan["plan_name"] == "chatgptteamplan"
    assert plan["entry_point"] == "team_workspace_purchase_modal"
    assert plan["promo_campaign_id"] == "team-1-month-free"
    # team mode does not touch workspace/seat
    assert plan["workspace_name"] == "MyTeam"
    assert plan["seat_quantity"] == 3


def test_apply_plan_override_preserves_user_promo(tmp_path):
    """User explicitly configured promo_campaign_id must not be overridden by default values."""
    cfg_path = tmp_path / "config.paypal.json"
    cfg_path.write_text(json.dumps({
        "fresh_checkout": {
            "plan": {
                "plan_name": "chatgptteamplan",
                "promo_campaign_id": "my-custom-campaign",
            }
        }
    }), encoding="utf-8")

    patched = pipeline._apply_plan_override(str(cfg_path), "plus")
    plan = json.loads(open(patched, encoding="utf-8").read())["fresh_checkout"]["plan"]
    assert plan["plan_name"] == "chatgptplusplan"
    assert plan["promo_campaign_id"] == "my-custom-campaign"


def test_card_abcard_payload_plus_omits_team_fields():
    """abcard path under Plus cannot carry workspace_name / seat_quantity, otherwise ChatGPT
backend will validate by team_plan field and return 400 directly."""
    sys.path.insert(0, str(pipeline.CARD_DIR))
    try:
        from card import _build_abcard_checkout_payload
    finally:
        try:
            sys.path.remove(str(pipeline.CARD_DIR))
        except ValueError:
            pass

    plus_payload = _build_abcard_checkout_payload({
        "plan": {
            "plan_name": "chatgptplusplan",
            "billing_country": "IE",
            "billing_currency": "EUR",
            "promo_campaign_id": "plus-1-month-free",
            # Intentionally insert team-only fields: simulate example skeleton residue / old config
            "workspace_name": "ShouldNotShowUp",
            "seat_quantity": 5,
        }
    })
    assert plus_payload["plan_type"] == "chatgptplusplan"
    assert plus_payload["billing_country_code"] == "IE"
    assert plus_payload["promo_campaign_id"] == "plus-1-month-free"
    # Critical assertion
    assert "workspace_name" not in plus_payload
    assert "seat_quantity" not in plus_payload
    # processor_entity triggered by billing_country non-US
    assert plus_payload["processor_entity"] == "openai_ie"

    team_payload = _build_abcard_checkout_payload({
        "plan": {
            "plan_name": "chatgptteamplan",
            "workspace_name": "MyTeam",
            "seat_quantity": 3,
        }
    })
    assert team_payload["plan_type"] == "chatgptteamplan"
    assert team_payload["workspace_name"] == "MyTeam"
    assert team_payload["seat_quantity"] == 3
