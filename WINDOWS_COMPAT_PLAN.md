# Windows 兼容性改造计划

## 🎯 目标

将 Gpt-Agreement-Payment 项目改造为完全支持 Windows 的版本，同时保持与上游同步的能力。

---

## 📋 需要修改的问题清单

### 1. 硬编码 Linux 路径（高优先级）

| 文件 | 问题路径 | 影响 | 修复方案 |
|------|---------|------|---------|
| `CTF-pay/card.py` | `/tmp/` | 临时文件存储 | 使用 `tempfile.gettempdir()` |
| `CTF-pay/card.py` | `~/.venvs/ctfml/bin/python` | hCaptcha solver 调用 | 跨平台路径检测 |
| `CTF-pay/card.py` | `/tmp/.X11-unix/X{display}` | X11 显示检测 | Windows 跳过此检查 |
| 多个文件 | `/dev/null` | 空设备 | Windows 用 `NUL` 或 `os.devnull` |

### 2. xvfb-run 依赖（中优先级）

| 位置 | 问题 | 修复方案 |
|------|------|---------|
| 所有文档 | 命令示例都用 `xvfb-run -a` | Windows 直接运行，Linux 保留 |
| `CTF-pay/card.py` | 检测 xvfb-run 可用性 | Windows 跳过虚拟显示 |

### 3. Shell 脚本（低优先级）

| 文件 | 问题 | 修复方案 |
|------|------|---------|
| `scripts/*.sh` | Bash 脚本 | 提供 `.bat` 或 `.ps1` 版本 |

---

## 🛠️ 实施步骤

### 阶段 1：创建跨平台工具层（1-2 天）

```bash
# 创建工具模块
mkdir -p utils
touch utils/__init__.py
```

**创建 `utils/platform_compat.py`**：

```python
"""
跨平台兼容性工具
支持 Windows / Linux / macOS
"""
import os
import platform
import tempfile
from pathlib import Path
from typing import Optional

def get_platform() -> str:
    """获取当前平台"""
    return platform.system()  # 'Windows', 'Linux', 'Darwin'

def is_windows() -> bool:
    """是否为 Windows"""
    return get_platform() == "Windows"

def is_linux() -> bool:
    """是否为 Linux"""
    return get_platform() == "Linux"

def get_temp_dir() -> str:
    """
    获取临时目录
    Windows: C:\\Users\\xxx\\AppData\\Local\\Temp
    Linux: /tmp
    """
    return tempfile.gettempdir()

def get_null_device() -> str:
    """
    获取空设备
    Windows: NUL
    Linux: /dev/null
    """
    return os.devnull

def get_venv_python(venv_name: str = "ctfml") -> Path:
    """
    获取虚拟环境 Python 路径
    Windows: C:\\Users\\xxx\\.venvs\\ctfml\\Scripts\\python.exe
    Linux: ~/.venvs/ctfml/bin/python
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
    """
    return is_linux() and not os.environ.get("DISPLAY")

def get_display_wrapper() -> Optional[list]:
    """
    获取显示包装命令
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
    """
    return os.path.expanduser(os.path.expandvars(path))

def ensure_dir(path: str) -> Path:
    """
    确保目录存在
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
    "rt_screenshots": "rt_screenshots",
}

def get_temp_path(key: str) -> Path:
    """
    获取临时文件路径（跨平台）
    """
    temp_base = Path(get_temp_dir())
    if key in TEMP_PATHS:
        return temp_base / TEMP_PATHS[key]
    return temp_base / key
```

### 阶段 2：修改核心文件（3-5 天）

#### 2.1 修改 `CTF-pay/card.py`

```python
# 在文件开头添加
from utils.platform_compat import (
    get_temp_dir, get_venv_python, get_null_device,
    get_temp_path, needs_xvfb, is_windows
)

# 替换所有硬编码路径
# 原代码：
dir="/tmp"
# 改为：
dir=get_temp_dir()

# 原代码：
solver_path = "~/.venvs/ctfml/bin/python"
# 改为：
solver_path = str(get_venv_python("ctfml"))

# 原代码：
bridge_meta_path = "/tmp/stripe_hcaptcha_bridge_latest.json"
# 改为：
bridge_meta_path = str(get_temp_path("hcaptcha_bridge_meta"))

# 原代码：
if display_suffix.isdigit() and os.path.exists(f"/tmp/.X11-unix/X{display_suffix}"):
# 改为：
if not is_windows() and display_suffix.isdigit() and os.path.exists(f"/tmp/.X11-unix/X{display_suffix}"):

# 原代码：
xvfb_run = shutil.which("xvfb-run")
# 改为：
if needs_xvfb():
    xvfb_run = shutil.which("xvfb-run")
else:
    xvfb_run = None
```

#### 2.2 修改 `CTF-reg/browser_register.py`

```python
# 同样添加导入和替换路径
from utils.platform_compat import get_temp_dir, get_temp_path
```

#### 2.3 修改 `pipeline.py`

```python
# 添加跨平台支持
from utils.platform_compat import get_display_wrapper, is_windows

# 在调用 subprocess 时处理 xvfb
def run_with_display(cmd: list):
    """带显示包装运行命令"""
    wrapper = get_display_wrapper()
    if wrapper:
        cmd = wrapper + cmd
    return subprocess.run(cmd, ...)
```

### 阶段 3：更新文档（1 天）

#### 3.1 创建 `docs/windows-setup.md`

```markdown
# Windows 安装指南

## 系统要求

- Windows 10/11
- Python 3.11+
- Git for Windows

## 安装步骤

### 1. 安装依赖

```powershell
# 使用 pip 安装
pip install requests curl_cffi playwright camoufox browserforge mitmproxy pybase64
playwright install firefox
camoufox fetch

# 安装 ML 依赖（独立虚拟环境）
python -m venv $env:USERPROFILE\.venvs\ctfml
& "$env:USERPROFILE\.venvs\ctfml\Scripts\python.exe" -m pip install torch transformers opencv-python pillow numpy
```

### 2. 配置文件

```powershell
# 复制配置模板
Copy-Item CTF-pay\config.paypal.example.json CTF-pay\config.paypal.json
Copy-Item CTF-reg\config.paypal-proxy.example.json CTF-reg\config.paypal-proxy.json
```

### 3. 运行

```powershell
# Windows 不需要 xvfb-run
python pipeline.py --config CTF-pay\config.paypal.json --paypal
```

## 常见问题

### Q: 为什么不需要 xvfb-run？
A: xvfb 是 Linux 的虚拟显示服务器，Windows 不需要。

### Q: 路径问题
A: Windows 使用反斜杠 `\`，但 Python 的 `Path` 对象会自动处理。
```

#### 3.2 更新 `README.md`

在安装部分添加 Windows 说明：

```markdown
### Windows 用户

Windows 用户请参考 [Windows 安装指南](docs/windows-setup.md)。

主要区别：
- 不需要 `xvfb-run` 命令
- 虚拟环境路径：`%USERPROFILE%\.venvs\ctfml\Scripts\python.exe`
- 临时文件路径：`%TEMP%`
```

### 阶段 4：测试（2-3 天）

```bash
# 创建测试脚本
cat > test_windows_compat.py << 'EOF'
"""
Windows 兼容性测试
"""
import sys
from utils.platform_compat import *

def test_paths():
    print(f"Platform: {get_platform()}")
    print(f"Temp dir: {get_temp_dir()}")
    print(f"Null device: {get_null_device()}")
    print(f"VEnv Python: {get_venv_python()}")
    print(f"Needs xvfb: {needs_xvfb()}")
    print(f"Display wrapper: {get_display_wrapper()}")

def test_temp_paths():
    for key in ["hcaptcha_solver_out", "hcaptcha_bridge_meta"]:
        print(f"{key}: {get_temp_path(key)}")

if __name__ == "__main__":
    test_paths()
    test_temp_paths()
EOF

python test_windows_compat.py
```

---

## 📊 工作量估算

| 阶段 | 工作量 | 优先级 |
|------|--------|--------|
| 创建跨平台工具层 | 1-2 天 | ⭐⭐⭐ 高 |
| 修改核心文件 | 3-5 天 | ⭐⭐⭐ 高 |
| 更新文档 | 1 天 | ⭐⭐ 中 |
| 测试验证 | 2-3 天 | ⭐⭐⭐ 高 |
| **总计** | **7-11 天** | |

---

## 🔄 与上游同步策略

### 每周检查上游更新

```bash
# 1. 拉取上游
git fetch upstream

# 2. 查看新提交
git log HEAD..upstream/main --oneline

# 3. 重点关注这些文件的改动
git diff HEAD..upstream/main -- CTF-pay/card.py
git diff HEAD..upstream/main -- CTF-pay/hcaptcha_auto_solver.py
git diff HEAD..upstream/main -- CTF-reg/browser_register.py

# 4. 如果有路径相关改动，需要手动适配
```

### 自动化检测脚本

```python
# check_upstream_paths.py
"""
检测上游更新中是否有新的硬编码路径
"""
import subprocess
import re

def check_new_hardcoded_paths():
    # 获取上游新增的代码
    result = subprocess.run(
        ["git", "diff", "HEAD..upstream/main"],
        capture_output=True, text=True
    )
    
    # 检测硬编码路径模式
    patterns = [
        r'"/tmp/',
        r"'/tmp/",
        r'~/.venvs',
        r'/dev/null',
        r'xvfb-run',
    ]
    
    issues = []
    for line in result.stdout.split('\n'):
        if line.startswith('+'):  # 新增的行
            for pattern in patterns:
                if re.search(pattern, line):
                    issues.append(line)
    
    if issues:
        print("⚠️  发现上游新增了硬编码路径：")
        for issue in issues:
            print(f"  {issue}")
        print("\n需要手动适配这些改动！")
    else:
        print("✅ 上游更新没有新的路径问题")

if __name__ == "__main__":
    check_new_hardcoded_paths()
```

---

## 🎁 额外优化建议

### 1. 配置文件路径也跨平台化

```python
# config_loader.py
from pathlib import Path
from utils.platform_compat import normalize_path

def load_config(config_path: str):
    # 支持 ~ 和环境变量
    path = Path(normalize_path(config_path))
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    # ...
```

### 2. 日志路径统一管理

```python
# utils/paths.py
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
LOGS_DIR = OUTPUT_DIR / "logs"

def ensure_output_dirs():
    OUTPUT_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
```

### 3. 创建 Windows 启动脚本

```powershell
# run_pipeline.ps1
param(
    [string]$Mode = "single",
    [string]$Config = "CTF-pay\config.paypal.json"
)

$ErrorActionPreference = "Stop"

# 检查 Python
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python not found!"
    exit 1
}

# 运行 pipeline
switch ($Mode) {
    "single" {
        python pipeline.py --config $Config --paypal
    }
    "batch" {
        python pipeline.py --config $Config --paypal --batch 5
    }
    "daemon" {
        python pipeline.py --config $Config --paypal --daemon
    }
    default {
        Write-Error "Unknown mode: $Mode"
    }
}
```

---

## ✅ 验收标准

- [ ] 所有硬编码 Linux 路径已替换
- [ ] Windows 10/11 上可以正常运行单次 pipeline
- [ ] 临时文件正确存储在 Windows TEMP 目录
- [ ] hCaptcha solver 可以在 Windows 虚拟环境中调用
- [ ] 文档包含 Windows 安装和使用说明
- [ ] 可以正常同步上游更新
- [ ] 所有测试通过

---

## 📞 需要帮助？

如果在改造过程中遇到问题：
1. 检查 `utils/platform_compat.py` 是否正确导入
2. 使用 `test_windows_compat.py` 验证路径
3. 查看 `docs/windows-setup.md` 的常见问题
4. 在你的 Fork 仓库开 Issue 记录问题
