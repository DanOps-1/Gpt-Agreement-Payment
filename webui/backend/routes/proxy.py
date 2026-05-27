"""出口代理控制（Webshare API 触发整池 IP 轮换）。

用于解决 `app.midtrans.com` 这种 Cloudflare 边缘节流（429 + 空 body）—
按 IP 限流，换 IP 即解。复用 `pipeline._rotate_webshare_ip`：
  1. POST Webshare /proxy/list/refresh/ 触发整池替换
  2. 轮询新 IP 直到不同于旧 IP
  3. swap gost 本地 relay 上游
  4. 可选：同步 team 全局代理（默认关）
"""
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
    """读取当前 Webshare 池中的第一条代理（不触发轮换）。"""
    cfg = _read_pay_config()
    ws_cfg = (cfg.get("webshare") or {})
    if not ws_cfg.get("enabled"):
        raise HTTPException(status_code=400, detail="webshare 未启用")
    api_key = (ws_cfg.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="webshare.api_key 为空")

    # 用 sys.path hack 而不是直接 import pipeline，避免触发 pipeline 顶部的
    # 副作用（建 OUTPUT_DIR/logs 等）。WebshareClient 只是个轻量 HTTP wrapper。
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
    """触发整池 IP 轮换。同步换 gost 上游。Team 同步默认沿用配置中的开关。

    返回新 proxy 元信息。失败:
      400 - 配置不全 / webshare.enabled=false
      402 - quota 耗尽
      502 - Webshare API 异常
    """
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

    # 拿当前 IP 作为 prev_ip 参考，让 wait_for_fresh_proxy 能正确识别新 IP
    prev_ip = ""
    try:
        cur = WebshareClient(ws_cfg["api_key"]).get_current_proxy()
        prev_ip = cur.get("proxy_address", "") or ""
    except Exception:
        pass

    try:
        # 用户手动按按钮 → 跳过 _rotate_webshare_ip 的冷却（明确意图覆盖节流）
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
