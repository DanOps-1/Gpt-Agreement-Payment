import httpx
from pydantic import BaseModel
from ._common import CheckResult, PreflightResult, aggregate


class CPAInput(BaseModel):
    base_url: str
    admin_key: str


def check(body: dict) -> PreflightResult:
    cfg = CPAInput.model_validate(body)
    base = cfg.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {cfg.admin_key}"}
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(f"{base}/v0/management/auth-files", headers=headers)
    except httpx.HTTPError as e:
        return aggregate([CheckResult(name="management", status="fail",
                                      message=str(e))])
    if r.status_code == 200:
        try:
            data = r.json()
            n = len(data) if isinstance(data, list) else (
                data.get("count") if isinstance(data, dict) else "?")
        except Exception:
            n = "?"
        return aggregate([CheckResult(name="management", status="ok",
                                      message=f"auth-files reachable ({n} entries)")])
    if r.status_code in (401, 403):
        return aggregate([CheckResult(name="management", status="fail",
                                      message=f"HTTP {r.status_code} — admin_key 无效或被拒",
                                      details=(r.text[:500] +
                                               "\n⚠ 该服务对错误 key 会限频/封 IP，请勿连续重试"))])
    return aggregate([CheckResult(name="management", status="fail",
                                  message=f"HTTP {r.status_code}",
                                  details=r.text[:1000])])
