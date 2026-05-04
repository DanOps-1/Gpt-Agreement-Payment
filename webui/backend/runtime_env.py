import os
import shutil
import sys


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def has_display(env: dict[str, str] | None = None) -> bool:
    env = env or os.environ
    return bool(str(env.get("DISPLAY") or "").strip() or str(env.get("WAYLAND_DISPLAY") or "").strip())


def pipeline_cmd_prefix() -> list[str]:
    if is_linux() and shutil.which("xvfb-run"):
        return ["xvfb-run", "-a"]
    return []


def xvfb_check() -> tuple[str, str]:
    path = shutil.which("xvfb-run")
    if path:
        return "ok", path
    if is_macos():
        return "ok", "macOS 不需要 xvfb-run；将直接启动 python"
    if is_linux() and has_display():
        return "ok", "未安装 xvfb-run；检测到 DISPLAY/WAYLAND_DISPLAY，将直接使用当前桌面会话"
    return "warn", "xvfb-run not found in PATH；将直接启动 python，Linux 无桌面时建议安装 xvfb"
