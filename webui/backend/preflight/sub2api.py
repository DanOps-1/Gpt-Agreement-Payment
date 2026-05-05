import httpx
from pydantic import BaseModel
from ._common import CheckResult, PreflightResult, aggregate


class Sub2APIInput(BaseModel):
    base_url: str
    admin_token: str = ""
    admin_key: str = ""


def _count_accounts(data) -> str:
    if isinstance(data, list):
        return str(len(data))
    if not isinstance(data, dict):
        return "?"
    for key in ("count", "total", "total_count"):
        if key in data:
            return str(data.get(key))
    nested = data.get("data")
    if isinstance(nested, list):
        return str(len(nested))
    if isinstance(nested, dict):
        for key in ("count", "total", "total_count"):
            if key in nested:
                return str(nested.get(key))
        for key in ("items", "accounts", "list"):
            value = nested.get(key)
            if isinstance(value, list):
                return str(len(value))
    return "?"


def check(body: dict) -> PreflightResult:
    cfg = Sub2APIInput.model_validate(body)
    base = cfg.base_url.rstrip("/")
    token = (cfg.admin_token or cfg.admin_key or "").strip()
    if not base or not token:
        return aggregate([CheckResult(name="admin_accounts", status="fail",
                                      message="base_url/admin_token required")])
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(f"{base}/api/v1/admin/accounts", headers=headers)
    except httpx.HTTPError as e:
        return aggregate([CheckResult(name="admin_accounts", status="fail",
                                      message=str(e))])
    if r.status_code == 200:
        try:
            n = _count_accounts(r.json())
        except Exception:
            n = "?"
        return aggregate([CheckResult(name="admin_accounts", status="ok",
                                      message=f"accounts reachable ({n} entries)")])
    if r.status_code in (401, 403):
        return aggregate([CheckResult(name="admin_accounts", status="fail",
                                      message=f"HTTP {r.status_code} - admin_token invalid",
                                      details=r.text[:500])])
    return aggregate([CheckResult(name="admin_accounts", status="fail",
                                  message=f"HTTP {r.status_code}",
                                  details=r.text[:1000])])
