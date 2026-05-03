from fastapi import APIRouter, HTTPException
from ..auth import CurrentUser
from ..preflight import system as system_check
from ..preflight import cloudflare as cf_check
from ..preflight import cloudflare_kv as cf_kv_check
from ..preflight import proxy as proxy_check
from ..preflight import webshare as ws_check
from ..preflight import card as card_check
from ..preflight import captcha as captcha_check
from ..preflight import vlm as vlm_check
from ..preflight import team_system as ts_check
from ..preflight import cpa as cpa_check

router = APIRouter(prefix="/api/preflight", tags=["preflight"])

_REGISTRY = {
    "system": lambda body: system_check.check(),
    "cloudflare": cf_check.check,
    "cloudflare_kv": cf_kv_check.check,
    "proxy": proxy_check.check,
    "webshare": ws_check.check,
    "card": card_check.check,
    "captcha": captcha_check.check,
    "vlm": vlm_check.check,
    "team_system": ts_check.check,
    "cpa": cpa_check.check,
}


@router.post("/{name}")
def run_check(name: str, body: dict, user: str = CurrentUser):
    fn = _REGISTRY.get(name)
    if not fn:
        raise HTTPException(status_code=404, detail=f"unknown check: {name}")
    return fn(body)
