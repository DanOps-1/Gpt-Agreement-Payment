"""WhatsApp Web sidecar control + status."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from .. import wa_relay


router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


class StartRequest(BaseModel):
    mode: str = Field(pattern="^(qr|pairing)$", default="qr")
    phone: str = ""
    engine: str = ""


class SettingsRequest(BaseModel):
    engine: str = ""


def _check_relay_token(token: str = "", x_wa_relay_token: str = "") -> None:
    got = token or x_wa_relay_token or ""
    expected = wa_relay.relay_token()
    if not got or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=403, detail="invalid relay token")


@router.get("/status")
def get_status(user: str = CurrentUser):
    return wa_relay.status()


@router.post("/start")
def start(req: StartRequest, user: str = CurrentUser):
    try:
        return wa_relay.start(mode=req.mode, pairing_phone=req.phone, engine=req.engine)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/settings")
def update_settings(req: SettingsRequest, user: str = CurrentUser):
    try:
        return wa_relay.set_preferred_engine(req.engine)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/stop")
def stop(user: str = CurrentUser):
    return wa_relay.stop()


@router.post("/logout")
def logout(user: str = CurrentUser):
    return wa_relay.logout()


@router.post("/sidecar/state")
def sidecar_state(
    payload: dict,
    token: str = "",
    x_wa_relay_token: str = Header(default=""),
):
    _check_relay_token(token=token, x_wa_relay_token=x_wa_relay_token)
    return {"ok": True, "state": wa_relay.apply_sidecar_state(payload)}


@router.get("/latest-otp")
def latest_otp(
    response: Response,
    since: float = 0.0,
    token: str = "",
    x_wa_relay_token: str = Header(default=""),
):
    _check_relay_token(token=token, x_wa_relay_token=x_wa_relay_token)
    item = wa_relay.latest_otp(since=since)
    if not item:
        response.status_code = 204
        return None
    return item
