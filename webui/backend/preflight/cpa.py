import httpx
from pydantic import BaseModel
from ._common import CheckResult, PreflightResult, aggregate
from ..cpa_utils import normalize_cpa_base_url


class CPAInput(BaseModel):
    base_url: str
    admin_key: str


def check(body: dict) -> PreflightResult:
    cfg = CPAInput.model_validate(body)
    base = normalize_cpa_base_url(cfg.base_url)
    headers = {"Authorization": f"Bearer {cfg.admin_key}"}
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(f"{base}/v0/management/auth-files", headers=headers)
    except httpx.HTTPError as e:
        return aggregate([CheckResult(name="management", status="fail",
                                      message=str(e))])
    if r.status_code == 200:
        ctype = r.headers.get("content-type", "")
        if "json" not in ctype.lower():
            return aggregate([CheckResult(
                name="management",
                status="fail",
                message="auth-files 返回的不是 JSON；base_url 可能填成了管理页面地址",
                details=f"normalized_base_url={base}\ncontent-type={ctype}\n{r.text[:500]}",
            )])
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
