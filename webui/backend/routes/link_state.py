"""GoPay phone link-state HTTP API.

Endpoints:

* ``GET  /api/gopay/link-state``                       — list all tracked phones
* ``GET  /api/gopay/link-state/{phone}``               — query one phone
* ``POST /api/gopay/link-state/unlink``                — body ``{"phone": "..."}``,
  flips the phone from linked to unlinked. Authoritative source for "external
  service has cleared the link on GoPay's side, please continue".

Auth model:
* GET endpoints: either a logged-in WebUI session OR the relay token (so external
  services can poll without a session cookie).
* POST /unlink: relay token only (mutation by an external worker).
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .. import link_state, wa_relay
from ..auth import current_user_optional


router = APIRouter(prefix="/api/gopay/link-state", tags=["gopay-link"])


class UnlinkRequest(BaseModel):
    phone: str
    source: str = "external"


class SetStateRequest(BaseModel):
    phone: str
    linked: bool
    source: str = "manual"
    payment_ref: str = ""


def _has_valid_relay_token(token: str, header_token: str) -> bool:
    got = token or header_token or ""
    if not got:
        return False
    return secrets.compare_digest(got, wa_relay.relay_token())


def _require_session_or_token(
    user: str | None = Depends(current_user_optional),
    token: str = "",
    x_wa_relay_token: str = Header(default=""),
) -> str:
    if user:
        return f"session:{user}"
    if _has_valid_relay_token(token, x_wa_relay_token):
        return "token"
    raise HTTPException(status_code=401, detail="auth required (session or relay token)")


def _require_token(
    token: str = "",
    x_wa_relay_token: str = Header(default=""),
) -> str:
    if not _has_valid_relay_token(token, x_wa_relay_token):
        raise HTTPException(status_code=403, detail="invalid relay token")
    return "token"


@router.get("")
def list_states(_auth: str = Depends(_require_session_or_token)):
    return {"items": link_state.list_all()}


@router.get("/{phone}")
def get_state(phone: str, _auth: str = Depends(_require_session_or_token)):
    return link_state.get_status(phone)


@router.post("/unlink")
def unlink(req: UnlinkRequest, _auth: str = Depends(_require_token)):
    try:
        return link_state.mark_unlinked(req.phone, source=req.source or "external")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/set")
def set_state(req: SetStateRequest, _auth: str = Depends(_require_session_or_token)):
    """双向写入：linked=True 强制标记为 linked，False 标记 unlinked。

    session 鉴权（WebUI 手动覆盖）或 relay token（外部服务）都可。
    """
    try:
        return link_state.set_status(
            req.phone,
            req.linked,
            source=req.source or "manual",
            payment_ref=req.payment_ref or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
