from __future__ import annotations

from typing import Literal
from pydantic import BaseModel

Status = Literal["ok", "warn", "fail"]


class CheckResult(BaseModel):
    name: str
    status: Status
    message: str
    details: str | None = None


class PreflightResult(BaseModel):
    status: Status
    message: str
    details: str | None = None
    checks: list[CheckResult] = []


def aggregate(checks: list[CheckResult]) -> PreflightResult:
    if any(c.status == "fail" for c in checks):
        agg = "fail"
    elif any(c.status == "warn" for c in checks):
        agg = "warn"
    else:
        agg = "ok"
    msg = f"{sum(1 for c in checks if c.status == 'ok')}/{len(checks)} ok"
    return PreflightResult(status=agg, message=msg, checks=checks)
