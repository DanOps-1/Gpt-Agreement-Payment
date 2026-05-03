"""
跨平台兼容性工具
支持 Windows / Linux / macOS

使用示例:
    from utils.platform_compat import get_temp_dir, get_venv_python

    temp_dir = get_temp_dir()  # Windows: C:\\Users\\xxx\\AppData\\Local\\Temp
    python_path = get_venv_python("ctfml")  # 自动检测平台
"""
import os
import platform
import tempfile
from pathlib import Path
from typing import Optional, List

def get_platform() -> str:
    """
    获取当前平台

    Returns:
        'Windows', 'Linux', 'Darwin' (macOS)
    """
    return platform.system()

def is_windows() -> bool:
    """是否为 Windows 平台"""
    return get_platform() == "Windows"

def is_linux() -> bool:
    """是否为 Linux 平台"""
    return get_platform() == "Linux"

def is_macos() -> bool:
    """是否为 macOS 平台"""
    return get_platform() == "Darwin"

def get_temp_dir() -> str:
    """
    获取临时目录（跨平台）

    Returns:
        Windows: C:\\Users\\xxx\\AppData\\Local\\Temp
        Linux: /tmp
        macOS: /var/folders/...
    """
    return tempfile.gettempdir()

def get_null_device() -> str:
    """
    获取空设备（跨平台）

    Returns:
        Windows: NUL
        Linux/macOS: /dev/null
    """
    return os.devnull

def get_venv_python(venv_name: str = "ctfml") -> Path:
    """
    获取虚拟环境 Python 路径（跨平台）

    Args:
        venv_name: 虚拟环境名称，默认 "ctfml"

    Returns:
        Windows: C:\\Users\\xxx\\.venvs\\ctfml\\Scripts\\python.exe
        Linux/macOS: ~/.venvs/ctfml/bin/python
    """
    venv_base = Path.home() / ".venvs" / venv_name

    if is_windows():
        return venv_base / "Scripts" / "python.exe"
    else:
        return venv_base / "bin" / "python"

def needs_xvfb() -> bool:
    """
    是否需要 xvfb 虚拟显示

    仅 Linux 且无 DISPLAY 环境变量时需要

    Returns:
        True: 需要 xvfb-run
        False: 不需要（Windows/macOS 或已有 DISPLAY）
    """
    return is_linux() and not os.environ.get("DISPLAY")

def get_display_wrapper() -> Optional[List[str]]:
    """
    获取显示包装命令

    Returns:
        Linux 无 DISPLAY: ['xvfb-run', '-a']
        其他: None
    """
    if needs_xvfb():
        import shutil
        if shutil.which("xvfb-run"):
            return ["xvfb-run", "-a"]
    return None

def normalize_path(path: str) -> str:
    """
    规范化路径（处理 ~ 和环境变量）

    Args:
        path: 原始路径，可包含 ~ 或 $VAR

    Returns:
        展开后的绝对路径

    Example:
        >>> normalize_path("~/data")
        'C:\\Users\\admin\\data'  # Windows
        '/home/user/data'         # Linux
    """
    return os.path.expanduser(os.path.expandvars(path))

def ensure_dir(path: str) -> Path:
    """
    确保目录存在（不存在则创建）

    Args:
        path: 目录路径

    Returns:
        Path 对象
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p

# 临时文件路径映射
TEMP_PATHS = {
    "hcaptcha_solver_out": "hcaptcha_auto_solver_live",
    "hcaptcha_bridge_meta": "stripe_hcaptcha_bridge_latest.json",
    "paypal_pwd_resp": "paypal_pwd_resp.html",
    "paypal_ddc_slider": "paypal_ddc_slider.png",
    "paypal_ddc_detected": "paypal_ddc_detected.png",
    "rt_pwd_skip": "rt_pwd_skip.png",
    "rt_after_pwd": "rt_after_pwd.png",
    "rt_addphone": "rt_addphone.png",
    "rt_consent": "rt_consent.png",
}

def get_temp_path(key: str, create_dir: bool = False) -> Path:
    """
    获取临时文件路径（跨平台）

    Args:
        key: 路径键名（见 TEMP_PATHS）
        create_dir: 是否创建父目录

    Returns:
        完整的临时文件路径

    Example:
        >>> get_temp_path("hcaptcha_solver_out")
        WindowsPath('C:/Users/admin/AppData/Local/Temp/hcaptcha_auto_solver_live')
    """
    temp_base = Path(get_temp_dir())

    if key in TEMP_PATHS:
        path = temp_base / TEMP_PATHS[key]
    else:
        path = temp_base / key

    if create_dir and not path.suffix:  # 如果是目录
        path.mkdir(parents=True, exist_ok=True)
    elif create_dir:  # 如果是文件，创建父目录
        path.parent.mkdir(parents=True, exist_ok=True)

    return path

def check_x11_display(display_suffix: str) -> bool:
    """
    检查 X11 显示是否存在（仅 Linux）

    Args:
        display_suffix: 显示编号，如 "99"

    Returns:
        True: 显示存在
        False: 显示不存在或非 Linux
    """
    if not is_linux():
        return False

    if not display_suffix.isdigit():
        return False

    x11_socket = Path(f"/tmp/.X11-unix/X{display_suffix}")
    return x11_socket.exists()

def get_path_separator() -> str:
    """
    获取路径分隔符

    Returns:
        Windows: '\\'
        Linux/macOS: '/'
    """
    return os.sep

def convert_to_native_path(path: str) -> str:
    """
    转换为本地路径格式

    Args:
        path: 路径字符串（可能包含 / 或 \\）

    Returns:
        本地格式的路径

    Example:
        >>> convert_to_native_path("output/logs/daemon.log")
        'output\\logs\\daemon.log'  # Windows
        'output/logs/daemon.log'    # Linux
    """
    return str(Path(path))

# 项目路径管理
def get_project_root() -> Path:
    """
    获取项目根目录

    Returns:
        项目根目录的 Path 对象
    """
    # 假设 utils 在项目根目录下
    return Path(__file__).parent.parent

def get_output_dir() -> Path:
    """获取 output 目录"""
    return get_project_root() / "output"

def get_logs_dir() -> Path:
    """获取 logs 目录"""
    return get_output_dir() / "logs"

def ensure_project_dirs():
    """确保项目必需的目录存在"""
    ensure_dir(get_output_dir())
    ensure_dir(get_logs_dir())

# 命令行工具
def wrap_command_with_display(cmd: List[str]) -> List[str]:
    """
    为命令添加显示包装（如果需要）

    Args:
        cmd: 原始命令列表

    Returns:
        可能添加了 xvfb-run 的命令列表

    Example:
        >>> wrap_command_with_display(["python", "script.py"])
        ['xvfb-run', '-a', 'python', 'script.py']  # Linux 无 DISPLAY
        ['python', 'script.py']                     # Windows
    """
    wrapper = get_display_wrapper()
    if wrapper:
        return wrapper + cmd
    return cmd

def get_platform_info() -> dict:
    """
    获取平台详细信息（用于调试）

    Returns:
        包含平台信息的字典
    """
    return {
        "system": get_platform(),
        "is_windows": is_windows(),
        "is_linux": is_linux(),
        "is_macos": is_macos(),
        "temp_dir": get_temp_dir(),
        "null_device": get_null_device(),
        "needs_xvfb": needs_xvfb(),
        "display_wrapper": get_display_wrapper(),
        "path_separator": get_path_separator(),
        "python_version": platform.python_version(),
        "machine": platform.machine(),
    }

if __name__ == "__main__":
    # 测试模块
    print("=== 平台兼容性工具测试 ===\n")

    info = get_platform_info()
    for key, value in info.items():
        print(f"{key:20s}: {value}")

    print("\n=== 路径测试 ===")
    print(f"VEnv Python: {get_venv_python()}")
    print(f"Temp path (solver): {get_temp_path('hcaptcha_solver_out')}")
    print(f"Project root: {get_project_root()}")
    print(f"Output dir: {get_output_dir()}")

    print("\n=== 命令包装测试 ===")
    test_cmd = ["python", "pipeline.py", "--help"]
    wrapped = wrap_command_with_display(test_cmd)
    print(f"Original: {test_cmd}")
    print(f"Wrapped:  {wrapped}")
