"""HTTP routes for the auto-loop runner."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from .. import auto_loop


router = APIRouter(prefix="/api/auto-loop", tags=["auto-loop"])


class StartRequest(BaseModel):
    target_success: int = Field(ge=1, le=10000, default=5)
    max_consec_fail: int = Field(ge=1, le=100, default=5)
    paypal: bool = False
    gopay: bool = True
    pay_only: bool = False
    register_only: bool = False
    register_mode: str = Field(default="browser", pattern="^(browser|protocol)$")


@router.post("/start")
def start(req: StartRequest, user: str = CurrentUser):
    try:
        return auto_loop.start(**req.model_dump())
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/stop")
def stop(user: str = CurrentUser):
    return auto_loop.stop()


@router.get("/status")
def status(user: str = CurrentUser):
    return auto_loop.status()
