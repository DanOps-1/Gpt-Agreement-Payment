#!/usr/bin/env python3
"""走 card.py 完整 fresh_checkout 路径拿到 chatgpt approve + pm-redirects URL，
然后 monkey-patch 替换 card._drive_gopay_from_redirect，从 redirect 接管做
**untokenized** GoPay charge → 出 QR + deeplink，**不要 phone OTP / WhatsApp**。

card.py auto --gopay 路径已知能稳定过 chatgpt approve（含完整 sentinel /
warm-up / manual_approval beta 流程，500+ 行就 port 不动了）。这里复用它。

用法：
    python scripts/qris_via_card.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "CTF-pay"))

import card  # noqa: E402  card.py 必须 in-process import 才能 monkey-patch
import gopay as _gopay  # noqa: E402
from qris import QrisCharger  # noqa: E402  复用 charge / artifacts / wait

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


class _QrisHookSuccess(Exception):
    """sentinel：从 _drive_gopay_from_redirect 接管完成后抛出，阻断 card.run 后续 poll。"""
    def __init__(self, ref: str):
        super().__init__(ref)
        self.ref = ref


def _qris_drive_from_redirect(redirect_url: str, cfg: dict, otp_file: str = "", session_id: str = ""):
    """替代 card._drive_gopay_from_redirect：从 pm-redirects URL 接管走 untokenized
    midtrans charge（payment_type=gopay tokenization=false）→ 拿 deeplink_url + 渲染 QR。
    """
    logger.info(f"[qris-via-card] 接管 redirect: {redirect_url[:100]}...")
    auth_cfg = (cfg.get("fresh_checkout") or {}).get("auth") or {}
    cs_session = _gopay._build_chatgpt_session(auth_cfg)
    proxy = (cfg.get("proxy") or "").strip() or None
    qris_cfg = cfg.get("qris") or {}
    if not qris_cfg.get("output_dir"):
        qris_cfg = {**qris_cfg, "output_dir": str(ROOT / "qris_artifacts")}

    charger = QrisCharger(
        cs_session, qris_cfg,
        proxy=proxy,
        runtime_cfg=cfg.get("runtime"),
    )
    # pm-redirects → midtrans snap_token
    snap_token = charger._fetch_pm_redirect_snap_token(redirect_url)
    logger.info(f"[qris-via-card] midtrans snap_token={snap_token}")
    charger._midtrans_load_transaction(snap_token)
    parsed = charger._midtrans_create_qris_charge(snap_token)
    artifacts = charger._save_qr_artifacts(parsed)
    ref = parsed["charge_ref"]
    logger.info("─" * 64)
    logger.info(f"[qris-via-card] QR 已生成 reference={ref}")
    if artifacts.get("qr_png_path"):
        logger.info(f"[qris-via-card] PNG: {artifacts['qr_png_path']}")
    if artifacts.get("qr_image_url"):
        logger.info(f"[qris-via-card] 远端预览: {artifacts['qr_image_url']}")
    if parsed.get("deeplink_url"):
        logger.info(f"[qris-via-card] DEEPLINK: {parsed['deeplink_url']}")
    if parsed.get("expiry_time"):
        logger.info(f"[qris-via-card] 过期: {parsed['expiry_time']}")
    logger.info("─" * 64)
    # 不调 wait_for_settlement（card.run 后续会 poll，不要重复 poll）
    # 把结果写到一个 sentinel 文件让 card.run 退出后能读
    out_meta = ROOT / "qris_artifacts" / f"latest_charge_{ref}.json"
    out_meta.parent.mkdir(parents=True, exist_ok=True)
    out_meta.write_text(json.dumps({
        "charge_ref": ref,
        "snap_token": snap_token,
        "qr_png_path": artifacts.get("qr_png_path", ""),
        "qr_image_url": artifacts.get("qr_image_url", ""),
        "deeplink_url": parsed.get("deeplink_url", ""),
        "expiry_time": parsed.get("expiry_time", ""),
        "session_id": session_id,
    }, indent=2), encoding="utf-8")
    logger.info(f"[qris-via-card] 写 sentinel: {out_meta}")
    raise _QrisHookSuccess(ref)


def main():
    config_path = str(ROOT / "CTF-pay" / "config.paypal.json")
    logger.info(f"[qris-via-card] config: {config_path}")

    # gost 保活
    from pipeline import _read_card_cfg, _ensure_gost_alive
    pay_cfg = _read_card_cfg(config_path)
    _ensure_gost_alive(pay_cfg)

    # 从 webui DB 拿最新一个未消耗的注册账号，写到 config.fresh_checkout.auth.access_token，
    # 让 card.run 走 access_token 模式（不再 auto_register 注册新账号）。
    from webui.backend.db import get_db
    db = get_db()
    con = db._conn()
    cur = con.execute(
        "SELECT email, session_token, access_token, device_id, cookie_header "
        "FROM registered_accounts ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("DB 没注册账号；先 register-only 一个再来")
    acct = dict(row)
    logger.info(f"[qris-via-card] 复用账号: {acct['email']}  session={bool(acct['session_token'])}  access={bool(acct['access_token'])}")

    # 临时把账号 token 注入 config，让 card.py 走 access_token 模式
    tmp_cfg_path = ROOT / "CTF-pay" / "config.paypal-tmp-qris.json"
    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    auth = raw.setdefault("fresh_checkout", {}).setdefault("auth", {})
    auth["mode"] = "access_token"
    auth["session_token"] = acct["session_token"] or ""
    auth["access_token"] = acct["access_token"] or ""
    auth["device_id"] = acct["device_id"] or ""
    auth["cookie_header"] = acct["cookie_header"] or ""
    auth["prefer_session_refresh"] = True
    if "auto_register" in auth:
        auth["auto_register"]["enabled"] = False
    tmp_cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    config_path = str(tmp_cfg_path)
    logger.info(f"[qris-via-card] 临时 config: {config_path}")

    # ★ 替换 _drive_gopay_from_redirect → 我们的 untokenized handler
    card._drive_gopay_from_redirect = _qris_drive_from_redirect
    logger.info("[qris-via-card] monkey-patched card._drive_gopay_from_redirect → qris untokenized")

    # in-process 调 card.run，走完整 fresh_checkout + manual_approval。
    # checkout_input='auto' 让它 fresh_checkout=True 自己创建 cs；use_gopay=True
    # 走 manual_approval beta + 走到 redirect 时调 _drive_gopay_from_redirect (我们替换的)
    try:
        result = card.run(
            checkout_input="auto",
            card_index=0,
            config_path=config_path,
            use_gopay=True,
        )
    except _QrisHookSuccess as e:
        # 我们的 hook 抛这个 sentinel 阻断 card.run 后续 polling
        logger.info(f"[qris-via-card] hook 接管成功 ref={e.ref}")
        return
    except SystemExit as e:
        # card.run 跑完最后会 SystemExit；这是预期
        logger.info(f"[qris-via-card] card.run 完成 SystemExit({e.code})")
    except Exception as e:
        logger.error(f"[qris-via-card] card.run 异常: {e}")
        # 看 sentinel 文件是否被我们 hook 写了
        sentinel_dir = ROOT / "qris_artifacts"
        latest = sorted(sentinel_dir.glob("latest_charge_*.json"))
        if latest:
            logger.info(f"[qris-via-card] 但 hook 已写 sentinel: {latest[-1]}")
            print(f"=== QR READY ===\n{latest[-1].read_text()}")
            return
        raise


if __name__ == "__main__":
    main()
