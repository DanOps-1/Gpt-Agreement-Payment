from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, Cookie
from pydantic import BaseModel
from ..db import get_db
from ..auth import CurrentUser

router = APIRouter(prefix="/api", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(req: LoginRequest, response: Response):
    db = get_db()
    if not db.verify_user(req.username, req.password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    sid = db.create_session(req.username)
    response.set_cookie(
        "session_id",
        sid,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response, session_id: str | None = Cookie(default=None)):
    if session_id:
        get_db().delete_session(session_id)
    response.delete_cookie("session_id", path="/")
    return {"ok": True}


@router.get("/me")
def me(user: str = CurrentUser):
    return {"username": user}
