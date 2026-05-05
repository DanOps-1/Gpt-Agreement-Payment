import httpx


def looks_like_api_key(token: str) -> bool:
    return (token or "").strip().startswith("admin-")


def resolve_admin_jwt(base_url: str, cfg: dict, timeout: float = 15.0) -> str:
    """Return a sub2api Admin JWT from config.

    sub2api's /api/v1/admin/* endpoints expect the login JWT. The `admin-...`
    tokens are API keys for downstream access, so they cannot be used here.
    """
    base = (base_url or "").rstrip("/")
    token = (
        cfg.get("admin_jwt")
        or cfg.get("admin_token")
        or cfg.get("jwt")
        or cfg.get("token")
        or ""
    ).strip()
    if token and not looks_like_api_key(token):
        return token

    username = (cfg.get("admin_email") or cfg.get("username") or cfg.get("email") or "").strip()
    password = (cfg.get("admin_password") or cfg.get("password") or "").strip()
    if not base or not username or not password:
        return ""

    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{base}/api/v1/auth/login", json={
            "email": username,
            "password": password,
        })
        if r.status_code == 404:
            r = c.post(f"{base}/api/v1/login", json={
                "email": username,
                "password": password,
            })
        r.raise_for_status()
        data = r.json()
    for key in ("access_token", "token", "jwt"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    nested = data.get("data")
    if isinstance(nested, dict):
        for key in ("access_token", "token", "jwt"):
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value
    return ""
