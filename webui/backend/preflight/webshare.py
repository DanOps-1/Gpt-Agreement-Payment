import httpx
from pydantic import BaseModel
from ._common import CheckResult, PreflightResult, aggregate

WS_API = "https://proxy.webshare.io/api/v2"


class WebshareInput(BaseModel):
    api_key: str
    register_country: str | None = None
    payment_country: str | None = None


def check(body: dict) -> PreflightResult:
    cfg = WebshareInput.model_validate(body)
    headers = {"Authorization": f"Token {cfg.api_key}"}
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(f"{WS_API}/proxy/list/", headers=headers,
                      params={"mode": "direct", "page_size": 1})
    except httpx.HTTPError as e:
        return aggregate([CheckResult(name="api", status="fail",
                                      message=str(e))])
    if r.status_code == 200:
        data = r.json()
        countries = ", ".join(
            x.strip().upper()
            for x in (cfg.register_country or "", cfg.payment_country or "")
            if x and x.strip()
        )
        suffix = f"; stage countries={countries}" if countries else ""
        return aggregate([CheckResult(name="api", status="ok",
                                      message=f"{data.get('count', '?')} proxies available{suffix}")])
    return aggregate([CheckResult(name="api", status="fail",
                                  message=f"HTTP {r.status_code}",
                                  details=r.text[:1000])])
