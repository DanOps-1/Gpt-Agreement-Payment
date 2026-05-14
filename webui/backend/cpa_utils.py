from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_cpa_base_url(raw: str) -> str:
    """Return the CPA API base URL used by /v0/management/auth-files.

    Users often paste the browser management URL, for example
    http://host:8318/management.html#/.  The API lives under /api, while URL
    fragments are never sent to the server.  Normalize that common input so
    health checks and real pushes hit the same endpoint.
    """
    value = str(raw or "").strip().rstrip("/")
    if not value:
        return ""
    parts = urlsplit(value)
    path = (parts.path or "").rstrip("/")
    if path.endswith("/management.html") or path == "/management.html":
        path = "/api"
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")
