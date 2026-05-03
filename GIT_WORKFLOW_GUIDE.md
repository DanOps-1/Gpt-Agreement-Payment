# Git Fork 工作流指南

## 方案 1：Fork + 定期同步上游（推荐）

### 初始设置

```bash
# 1. 在 GitHub 上 Fork 原项目到你的账号
# https://github.com/DanOps-1/Gpt-Agreement-Payment -> Fork

# 2. 克隆你的 Fork
git clone https://github.com/YOUR_USERNAME/Gpt-Agreement-Payment.git
cd Gpt-Agreement-Payment

# 3. 添加原项目为上游仓库
git remote add upstream https://github.com/DanOps-1/Gpt-Agreement-Payment.git

# 4. 验证远程仓库
git remote -v
# origin    https://github.com/YOUR_USERNAME/Gpt-Agreement-Payment.git (fetch)
# origin    https://github.com/YOUR_USERNAME/Gpt-Agreement-Payment.git (push)
# upstream  https://github.com/DanOps-1/Gpt-Agreement-Payment.git (fetch)
# upstream  https://github.com/DanOps-1/Gpt-Agreement-Payment.git (push)
```

### 日常开发流程

```bash
# 1. 创建功能分支（基于 main）
git checkout main
git checkout -b feature/windows-support

# 2. 进行你的修改
# - 修复 Windows 路径问题
# - 添加新功能
# - 优化代码

# 3. 提交修改
git add .
git commit -m "feat: add Windows path compatibility"

# 4. 推送到你的 Fork
git push origin feature/windows-support
```

### 同步上游更新

```bash
# 1. 切换到主分支
git checkout main

# 2. 拉取上游更新
git fetch upstream

# 3. 合并上游的 main 分支
git merge upstream/main

# 4. 推送到你的 Fork
git push origin main

# 5. 将上游更新合并到你的功能分支
git checkout feature/windows-support
git merge main

# 如果有冲突，解决冲突后：
git add .
git commit -m "merge: resolve conflicts with upstream"
git push origin feature/windows-support
```

### 处理冲突的技巧

```bash
# 查看冲突文件
git status

# 使用 VS Code 或其他工具解决冲突
# 冲突标记：
# <<<<<<< HEAD
# 你的修改
# =======
# 上游的修改
# >>>>>>> upstream/main

# 解决后标记为已解决
git add <resolved-file>
git commit

# 如果想放弃合并
git merge --abort
```

### 定期检查上游更新

```bash
# 查看上游是否有新提交
git fetch upstream
git log HEAD..upstream/main --oneline

# 查看具体改动
git diff HEAD..upstream/main

# 只查看某个文件的改动
git diff HEAD..upstream/main -- CTF-pay/card.py
```

---

## 方案 2：独立分支开发

**适合场景**：你的改动很大，不打算合并回上游

```bash
# 1. 克隆原项目
git clone https://github.com/DanOps-1/Gpt-Agreement-Payment.git
cd Gpt-Agreement-Payment

# 2. 创建独立开发分支
git checkout -b windows-rewrite

# 3. 大刀阔斧地修改
# - 重构 Windows 兼容性
# - 改变架构
# - 添加新功能

# 4. 定期查看上游（手动挑选有用的更新）
git remote add upstream https://github.com/DanOps-1/Gpt-Agreement-Payment.git
git fetch upstream

# 5. 手动挑选上游的某个提交（cherry-pick）
git log upstream/main --oneline
git cherry-pick <commit-hash>

# 6. 或者只查看上游的某个文件改动，手动应用
git show upstream/main:CTF-pay/hcaptcha_auto_solver.py > /tmp/upstream_solver.py
# 然后手动对比和应用
```

---

## 方案 3：从头重写（适合大改）

**适合场景**：Windows 兼容性问题太多，想重新设计架构

```bash
# 1. 创建新项目
mkdir Gpt-Agreement-Payment-Windows
cd Gpt-Agreement-Payment-Windows
git init

# 2. 添加原项目为参考
git remote add reference https://github.com/DanOps-1/Gpt-Agreement-Payment.git
git fetch reference

# 3. 从头开始写，但可以参考原项目
# - 复制核心逻辑
# - 重写 Windows 兼容层
# - 改进架构

# 4. 定期查看原项目的更新（作为参考）
git fetch reference
git log reference/main --oneline
git show reference/main:CTF-pay/card.py | less
```

---

## 推荐的 Windows 适配改动

### 1. 路径兼容性

```python
# 原代码（硬编码 Linux 路径）
dir="/tmp"
solver_path = "~/.venvs/ctfml/bin/python"

# 改为跨平台
import tempfile
import os
from pathlib import Path

dir = tempfile.gettempdir()  # Windows: C:\Users\xxx\AppData\Local\Temp
solver_path = Path.home() / ".venvs" / "ctfml" / ("Scripts" if os.name == "nt" else "bin") / "python"
```

### 2. xvfb-run 处理

```python
# 原代码
xvfb_run = shutil.which("xvfb-run")

# 改为
import platform
if platform.system() == "Windows":
    # Windows 不需要 xvfb
    xvfb_run = None
else:
    xvfb_run = shutil.which("xvfb-run")
```

### 3. 创建跨平台工具模块

```python
# utils/platform_compat.py
import os
import platform
import tempfile
from pathlib import Path

def get_temp_dir():
    """获取临时目录（跨平台）"""
    return tempfile.gettempdir()

def get_venv_python(venv_name="ctfml"):
    """获取虚拟环境 Python 路径（跨平台）"""
    if platform.system() == "Windows":
        return Path.home() / ".venvs" / venv_name / "Scripts" / "python.exe"
    else:
        return Path.home() / ".venvs" / venv_name / "bin" / "python"

def needs_xvfb():
    """是否需要 xvfb（仅 Linux）"""
    return platform.system() == "Linux" and not os.environ.get("DISPLAY")
```

---

## 我的建议

### 如果你是初学者或想快速上手
👉 **选择方案 1（Fork + 同步）**
- 保留原有功能
- 只做 Windows 适配
- 可以随时获取上游更新

### 如果你想大改架构
👉 **选择方案 2（独立分支）**
- 自由度更高
- 可以选择性地合并上游更新
- 适合长期维护

### 如果原项目问题太多
👉 **选择方案 3（重写）**
- 完全控制代码质量
- 可以用更好的架构
- 但工作量最大

---

## 快速开始：Windows 适配 Fork

```bash
# 1. Fork 项目
# 在 GitHub 上点击 Fork 按钮

# 2. 克隆并设置
git clone https://github.com/YOUR_USERNAME/Gpt-Agreement-Payment.git
cd Gpt-Agreement-Payment
git remote add upstream https://github.com/DanOps-1/Gpt-Agreement-Payment.git

# 3. 创建 Windows 适配分支
git checkout -b feature/windows-compat

# 4. 创建跨平台工具模块
mkdir -p utils
cat > utils/platform_compat.py << 'EOF'
import os
import platform
import tempfile
from pathlib import Path

def get_temp_dir():
    return tempfile.gettempdir()

def get_venv_python(venv_name="ctfml"):
    if platform.system() == "Windows":
        return str(Path.home() / ".venvs" / venv_name / "Scripts" / "python.exe")
    return str(Path.home() / ".venvs" / venv_name / "bin" / "python")

def needs_xvfb():
    return platform.system() == "Linux" and not os.environ.get("DISPLAY")
EOF

# 5. 开始修改代码
# 逐步替换硬编码路径

# 6. 测试并提交
git add .
git commit -m "feat: add Windows compatibility layer"
git push origin feature/windows-compat
```

---

## 监控上游更新的自动化

```bash
# 创建一个脚本来检查上游更新
cat > check_upstream.sh << 'EOF'
#!/bin/bash
git fetch upstream
COMMITS=$(git log HEAD..upstream/main --oneline | wc -l)
if [ $COMMITS -gt 0 ]; then
    echo "⚠️  上游有 $COMMITS 个新提交："
    git log HEAD..upstream/main --oneline
    echo ""
    echo "运行以下命令同步："
    echo "  git checkout main"
    echo "  git merge upstream/main"
    echo "  git checkout feature/windows-compat"
    echo "  git merge main"
else
    echo "✅ 已是最新版本"
fi
EOF

chmod +x check_upstream.sh

# 定期运行（比如每周）
./check_upstream.sh
```
