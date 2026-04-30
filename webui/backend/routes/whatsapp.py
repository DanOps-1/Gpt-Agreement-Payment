"""WhatsApp Web sidecar control + status."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from .. import wa_relay


router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


class StartRequest(BaseModel):
    mode: str = Field(pattern="^(qr|pairing)$", default="qr")
    phone: str = ""  # only used in pairing mode


@router.get("/status")
def get_status(user: str = CurrentUser):
    return wa_relay.status()


@router.post("/start")
def start(req: StartRequest, user: str = CurrentUser):
    try:
        return wa_relay.start(mode=req.mode, pairing_phone=req.phone)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/stop")
def stop(user: str = CurrentUser):
    return wa_relay.stop()


@router.post("/logout")
def logout(user: str = CurrentUser):
    """Stop sidecar + clear stored WhatsApp Web session (forces re-scan)."""
    return wa_relay.logout()
