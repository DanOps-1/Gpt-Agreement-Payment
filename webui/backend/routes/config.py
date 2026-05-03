from fastapi import APIRouter
from pydantic import BaseModel
from ..auth import CurrentUser
from ..config_health import build_config_health
from ..config_writer import write_configs

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


@router.post("/export")
def export(req: ExportRequest, user: str = CurrentUser):
    return write_configs(req.answers)


@router.post("/health")
def health(req: HealthRequest, user: str = CurrentUser):
    return build_config_health(req.model_dump())


@router.get("/health")
def health_get(user: str = CurrentUser):
    return build_config_health({})
