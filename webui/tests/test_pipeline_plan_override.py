"""验证 pipeline.py 的 --plan 临时覆盖逻辑 + card.py abcard payload 在 Plus 下
不再带 workspace/seat。两个修复都是为了让 PayPal 协议支付走 Plus 订阅时不被
ChatGPT 后端拒绝（plan_name=plus 但带 team_plan_data → 400）。"""

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
    assert patched != str(cfg_path)  # 临时文件，不动用户原文件

    patched_cfg = json.loads(open(patched, encoding="utf-8").read())
    plan = patched_cfg["fresh_checkout"]["plan"]
    assert plan["plan_name"] == "chatgptplusplan"
    assert plan["entry_point"] == "all_plans_pricing_modal"
    # 关键：team-only 字段必须剥掉
    assert "workspace_name" not in plan
    assert "seat_quantity" not in plan
    # 用户配的 billing_country 不能被吞掉
    assert plan["billing_country"] == "IE"

    # 原文件没动
    orig = json.loads(cfg_path.read_text())
    assert orig["fresh_checkout"]["plan"]["plan_name"] == "chatgptteamplan"
    assert orig["fresh_checkout"]["plan"]["workspace_name"] == "MyWorkspace"


def test_apply_plan_override_team_fills_defaults(tmp_path):
    """Team 反向：plan_name 还是 team，但补全 example 漏配的 promo/entry_point。"""
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
    # team 模式不动 workspace/seat
    assert plan["workspace_name"] == "MyTeam"
    assert plan["seat_quantity"] == 3


def test_apply_plan_override_preserves_user_promo(tmp_path):
    """用户已显式配 promo_campaign_id 时不能被默认值覆盖。"""
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
    """abcard 路径在 Plus 下不能带 workspace_name / seat_quantity，否则 ChatGPT
    后端会按 team_plan 字段校验直接 400。"""
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
            # 故意塞进 team-only 字段：模拟 example skeleton 残留 / 老 config
            "workspace_name": "ShouldNotShowUp",
            "seat_quantity": 5,
        }
    })
    assert plus_payload["plan_type"] == "chatgptplusplan"
    assert plus_payload["billing_country_code"] == "IE"
    assert plus_payload["promo_campaign_id"] == "plus-1-month-free"
    # 关键断言
    assert "workspace_name" not in plus_payload
    assert "seat_quantity" not in plus_payload
    # processor_entity 由 billing_country 非 US 触发
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
