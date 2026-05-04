import shutil
import sys
from ._common import CheckResult, PreflightResult, aggregate
from .. import runtime_env


def check() -> PreflightResult:
    checks: list[CheckResult] = []

    # Python
    if sys.version_info >= (3, 10):
        checks.append(CheckResult(name="python", status="ok",
                                  message=f"Python {sys.version.split()[0]}"))
    else:
        checks.append(CheckResult(name="python", status="fail",
                                  message=f"Python {sys.version.split()[0]} < 3.10"))

    # Binaries
    camoufox_path = shutil.which("camoufox")
    checks.append(CheckResult(
        name="camoufox",
        status="ok" if camoufox_path else "fail",
        message=camoufox_path or "camoufox not found in PATH",
    ))

    xvfb_status, xvfb_message = runtime_env.xvfb_check()
    checks.append(CheckResult(
        name="xvfb-run",
        status=xvfb_status,
        message=xvfb_message,
    ))

    # Playwright import
    try:
        import playwright  # noqa: F401
        checks.append(CheckResult(name="playwright", status="ok",
                                  message="playwright importable"))
    except ImportError as e:
        checks.append(CheckResult(name="playwright", status="fail",
                                  message=str(e)))

    return aggregate(checks)
