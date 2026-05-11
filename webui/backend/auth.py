from fastapi import Cookie, Depends, HTTPException
from .db import get_db


def current_user(session_id: str | None = Cookie(default=None)) -> str:
    if not session_id:
        raise HTTPException(status_code=401, detail="not authenticated")
    user = get_db().lookup_session(session_id)
    if not user:
        raise HTTPException(status_code=401, detail="session expired")
    return user


def current_user_optional(session_id: str | None = Cookie(default=None)) -> str | None:
    """Return the logged-in user if any, otherwise None (no exception).

    Used by endpoints that accept either a session cookie OR a separate token
    auth path; the route then decides which paths are acceptable.
    """
    if not session_id:
        return None
    return get_db().lookup_session(session_id)


CurrentUser = Depends(current_user)
