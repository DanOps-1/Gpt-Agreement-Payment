import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..auth import CurrentUser
from ..config_health import build_config_health
from ..config_writer import write_configs
from .. import settings as s
from .. import gopay_auto_unbind

router = APIRouter(prefix="/api/config", tags=["config"])


class ExportRequest(BaseModel):
    answers: dict


class HealthRequest(BaseModel):
    mode: str = "single"
    paypal: bool = True
    gopay: bool = False
    pay_only: bool = False
    register_only: bool = False
    batch: int = 0
    workers: int = 3
    self_dealer: int = 0
    count: int = 0


class GoPayAutoUnbindRequest(BaseModel):
    base_url: str = ""
    raw_request: str = ""


class GoPayAutoUnbindFetchRequest(BaseModel):
    base_url: str = ""
    raw_request: str
    timeout: float = 20.0


@router.post("/export")
def export(req: ExportRequest, user: str = CurrentUser):
    return write_configs(req.answers)


@router.post("/gopay/auto-unbind")
def save_gopay_auto_unbind(req: GoPayAutoUnbindRequest, user: str = CurrentUser):
    path = s.PAY_CONFIG_PATH
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    gopay = data.setdefault("gopay", {})
    if not isinstance(gopay, dict):
        gopay = {}
        data["gopay"] = gopay
    auto_unbind = {}
    if req.base_url.strip():
        auto_unbind["base_url"] = req.base_url.strip()
    if req.raw_request.strip():
        auto_unbind["raw_request"] = req.raw_request
    if auto_unbind:
        gopay["auto_unbind"] = auto_unbind
    else:
        gopay.pop("auto_unbind", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(path), "configured": bool(auto_unbind)}


@router.post("/gopay/auto-unbind/fetch-body")
def fetch_gopay_auto_unbind_body(req: GoPayAutoUnbindFetchRequest, user: str = CurrentUser):
    try:
        result = gopay_auto_unbind.fetch_linkedapps(req.raw_request, req.base_url, timeout=req.timeout)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"request failed: {e}")
    result.pop("request_headers", None)
    return result


@router.post("/health")
def health(req: HealthRequest, user: str = CurrentUser):
    return build_config_health(req.model_dump())


@router.get("/health")
def health_get(user: str = CurrentUser):
    return build_config_health({})
