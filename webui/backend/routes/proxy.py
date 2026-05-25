"""Export proxy control (Webshare API triggers full pool IP rotation).

Used to resolve `app.midtrans.com` type Cloudflare edge rate limiting (429 + empty body) —
rate limited by IP, changing IP solves it. Reuses `pipeline._rotate_webshare_ip`:
  1. POST Webshare /proxy/list/refresh/ to trigger full pool replacement
  2. Poll new IP until different from old IP
  3. swap gost local relay upstream
  4. Optional: sync team global proxy (disabled by default)"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..auth import CurrentUser
from .. import settings as s


router = APIRouter(prefix="/api/proxy", tags=["proxy"])


def _read_pay_config() -> dict:
    try:
        return json.loads(Path(s.PAY_CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


@router.get("/current")
def get_current(user: str = CurrentUser):
    """Read the first proxy in the current Webshare pool (does not trigger rotation)."""
    cfg = _read_pay_config()
    ws_cfg = (cfg.get("webshare") or {})
    if not ws_cfg.get("enabled"):
        raise HTTPException(status_code=400, detail="webshare 未启用")
    api_key = (ws_cfg.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="webshare.api_key 为空")

    # Use sys.path hack instead of directly importing pipeline to avoid triggering
    # side effects at the top of pipeline (creating OUTPUT_DIR/logs, etc.). WebshareClient is just a lightweight HTTP wrapper.
    import sys
    sys.path.insert(0, str(s.ROOT))
    try:
        from pipeline import WebshareClient
    finally:
        try:
            sys.path.remove(str(s.ROOT))
        except ValueError:
            pass

    try:
        client = WebshareClient(api_key)
        px = client.get_current_proxy()
        quota = client.get_replacement_quota()
        return {
            "ip": px.get("proxy_address"),
            "port": int(px.get("port", 0)),
            "country": px.get("country_code"),
            "asn": px.get("asn_name"),
            "valid": px.get("valid"),
            "quota": quota,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"webshare 查询失败: {e}")


@router.post("/rotate-ip")
def rotate_ip(user: str = CurrentUser):
    """Trigger full pool IP rotation. Sync gost upstream. Team sync defaults to using the configuration switch.

    Returns new proxy metadata. Failure:
      400 - configuration incomplete / webshare.enabled=false
      402 - quota exhausted
      502 - Webshare API exception"""
    cfg = _read_pay_config()
    ws_cfg = (cfg.get("webshare") or {})
    if not ws_cfg.get("enabled"):
        raise HTTPException(status_code=400, detail="webshare 未启用，先在配置里 enable")
    if not (ws_cfg.get("api_key") or "").strip():
        raise HTTPException(status_code=400, detail="webshare.api_key 为空")

    import sys
    sys.path.insert(0, str(s.ROOT))
    try:
        from pipeline import _rotate_webshare_ip, WebshareQuotaExhausted, WebshareClient
    finally:
        try:
            sys.path.remove(str(s.ROOT))
        except ValueError:
            pass

    # Use current IP as prev_ip reference so wait_for_fresh_proxy can correctly identify new IP
    prev_ip = ""
    try:
        cur = WebshareClient(ws_cfg["api_key"]).get_current_proxy()
        prev_ip = cur.get("proxy_address", "") or ""
    except Exception:
        pass

    try:
        # User manually presses button → skip _rotate_webshare_ip cooldown (explicit intent overrides rate limiting)
        new_px = _rotate_webshare_ip(cfg, team_client=None, prev_ip=prev_ip, force=True)
    except WebshareQuotaExhausted as e:
        raise HTTPException(status_code=402, detail=f"Webshare 替换额度耗尽: {e}")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"IP 轮换失败: {e}")

    return {
        "ok": True,
        "prev_ip": prev_ip,
        "new_ip": new_px.get("proxy_address"),
        "port": int(new_px.get("port", 0)),
        "country": new_px.get("country_code"),
        "asn": new_px.get("asn_name"),
        "valid": new_px.get("valid"),
    }
