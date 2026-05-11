from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel

from ._common import CheckResult, PreflightResult, aggregate


_CTF_REG_DIR = Path(__file__).resolve().parents[3] / "CTF-reg"
if str(_CTF_REG_DIR) not in sys.path:
    sys.path.insert(0, str(_CTF_REG_DIR))

from luckmail_provider import LuckMailOpenAPIClient, DEFAULT_BASE_URL  # noqa: E402


class LuckMailInput(BaseModel):
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    project_code: str = ""
    email_type: str = "ms_graph"


def check(body: dict) -> PreflightResult:
    cfg = LuckMailInput.model_validate(body or {})
    checks: list[CheckResult] = []

    if not cfg.api_key.strip():
        checks.append(CheckResult(name="api_key", status="fail", message="缺 api_key"))
    else:
        checks.append(CheckResult(name="api_key", status="ok", message="已填写"))
    if not cfg.project_code.strip():
        checks.append(CheckResult(name="project_code", status="fail", message="缺 project_code"))
    else:
        checks.append(CheckResult(name="project_code", status="ok", message=cfg.project_code.strip()))
    if cfg.email_type != "ms_graph":
        checks.append(CheckResult(name="email_type", status="fail", message="微软 Graph 邮箱必须使用 ms_graph"))
    else:
        checks.append(CheckResult(name="email_type", status="ok", message="ms_graph"))
    if any(c.status == "fail" for c in checks):
        return aggregate(checks)

    try:
        client = LuckMailOpenAPIClient(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
        )
        balance = client.get_balance()
        amount = balance.get("balance")
        suffix = f"balance={amount}" if amount is not None else "balance ok"
        checks.append(CheckResult(name="balance", status="ok", message=suffix))
    except Exception as e:
        checks.append(CheckResult(name="balance", status="fail", message=str(e)[:240]))
    return aggregate(checks)
