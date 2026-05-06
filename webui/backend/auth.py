from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException
from .db import get_db


def current_user(session_id: str | None = Cookie(default=None)) -> str:
    if not session_id:
        raise HTTPException(status_code=401, detail="not authenticated")
    user = get_db().lookup_session(session_id)
    if not user:
        raise HTTPException(status_code=401, detail="session expired")
    return user


CurrentUser = Depends(current_user)
