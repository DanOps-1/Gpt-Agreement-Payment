# 🚀 Windows 二次开发快速开始指南

## 📌 你的情况总结

- ✅ 项目已克隆：`D:\DevSpace\G01_GptProject\Gpt-Agreement-Payment`
- ✅ 跨平台工具已创建：`utils/platform_compat.py`
- ✅ 工具测试通过（Windows 环境）
- 🎯 目标：基于此项目做 Windows 适配的二次开发，同时能跟踪上游更新

---

## 🎯 推荐方案：Fork + 定期同步

基于你的需求，我推荐 **方案 1（Fork + 定期同步）**，原因：
1. 保留原有功能和逻辑
2. 只需要做 Windows 路径适配
3. 可以随时获取上游的新功能和 bug 修复
4. 工作量适中（7-11 天）

---

## 📋 立即开始的步骤

### 第 1 步：在 GitHub 上 Fork 项目

```bash
# 1. 访问原项目
https://github.com/DanOps-1/Gpt-Agreement-Payment

# 2. 点击右上角 "Fork" 按钮
# 3. Fork 到你的账号下
```

### 第 2 步：重新克隆你的 Fork（或添加远程仓库）

**选项 A：重新克隆（推荐，干净）**

```bash
# 备份当前目录
cd D:\DevSpace\G01_GptProject
mv Gpt-Agreement-Payment Gpt-Agreement-Payment.backup

# 克隆你的 Fork
git clone https://github.com/YOUR_USERNAME/Gpt-Agreement-Payment.git
cd Gpt-Agreement-Payment

# 添加上游仓库
git remote add upstream https://github.com/DanOps-1/Gpt-Agreement-Payment.git
git fetch upstream
```

**选项 B：修改当前仓库（快速）**

```bash
cd D:\DevSpace\G01_GptProject\Gpt-Agreement-Payment

# 查看当前远程仓库
git remote -v

# 重命名 origin 为 upstream
git remote rename origin upstream

# 添加你的 Fork 为 origin
git remote add origin https://github.com/YOUR_USERNAME/Gpt-Agreement-Payment.git

# 验证
git remote -v
# origin    https://github.com/YOUR_USERNAME/Gpt-Agreement-Payment.git (fetch)
# origin    https://github.com/YOUR_USERNAME/Gpt-Agreement-Payment.git (push)
# upstream  https://github.com/DanOps-1/Gpt-Agreement-Payment.git (fetch)
# upstream  https://github.com/DanOps-1/Gpt-Agreement-Payment.git (push)
```

### 第 3 步：创建 Windows 适配分支

```bash
# 创建并切换到新分支
git checkout -b feature/windows-compat

# 复制已创建的工具文件（如果用选项 A）
# 如果用选项 B，这些文件已经存在了
```

### 第 4 步：提交跨平台工具模块

```bash
# 添加文件
git add utils/
git add GIT_WORKFLOW_GUIDE.md
git add WINDOWS_COMPAT_PLAN.md
git add CLAUDE_SKILLS_GUIDE.md

# 提交
git commit -m "feat: add cross-platform compatibility layer for Windows

- Add utils/platform_compat.py for path handling
- Support Windows temp directories and venv paths
- Auto-detect xvfb requirement (Linux only)
- Add comprehensive documentation"

# 推送到你的 Fork
git push origin feature/windows-compat
```

### 第 5 步：开始修改核心文件

按照 `WINDOWS_COMPAT_PLAN.md` 的计划，逐步修改：

```bash
# 1. 修改 CTF-pay/card.py（最重要）
# 在文件开头添加：
# from utils.platform_compat import get_temp_dir, get_venv_python, ...

# 2. 搜索所有硬编码路径
grep -n '"/tmp/' CTF-pay/card.py
grep -n '~/.venvs' CTF-pay/card.py

# 3. 逐个替换并测试
```

---

## 🔄 日常工作流程

### 开发新功能

```bash
# 1. 确保在功能分支
git checkout feature/windows-compat

# 2. 修改代码
# 编辑文件...

# 3. 测试
python utils/platform_compat.py  # 测试工具模块
python pipeline.py --help         # 测试主程序

# 4. 提交
git add .
git commit -m "fix: replace hardcoded /tmp paths in card.py"
git push origin feature/windows-compat
```

### 每周同步上游更新

```bash
# 1. 切换到主分支
git checkout main

# 2. 拉取上游更新
git fetch upstream
git merge upstream/main

# 3. 推送到你的 Fork
git push origin main

# 4. 合并到功能分支
git checkout feature/windows-compat
git merge main

# 5. 如果有冲突，解决后提交
git add .
git commit -m "merge: sync with upstream"
git push origin feature/windows-compat
```

### 检查上游是否有新的路径问题

```bash
# 查看上游新增的代码
git fetch upstream
git diff HEAD..upstream/main | grep -E '"/tmp/|~/.venvs|/dev/null'

# 如果发现新的硬编码路径，需要手动适配
```

---

## 📝 修改优先级

### 🔴 高优先级（必须改）

1. **CTF-pay/card.py** - 核心支付逻辑
   - 替换 `/tmp/` → `get_temp_dir()`
   - 替换 `~/.venvs/ctfml/bin/python` → `get_venv_python()`
   - 处理 xvfb 检测

2. **CTF-reg/browser_register.py** - 注册逻辑
   - 替换临时路径

3. **pipeline.py** - 主编排器
   - 添加 `wrap_command_with_display()`

### 🟡 中优先级（建议改）

4. **文档更新**
   - 创建 `docs/windows-setup.md`
   - 更新 `README.md` 添加 Windows 说明
   - 更新 `CLAUDE.md` 添加 Windows 命令

### 🟢 低优先级（可选）

5. **Shell 脚本**
   - 提供 PowerShell 版本
   - 创建 Windows 批处理文件

---

## 🧪 测试清单

每次修改后运行这些测试：

```bash
# 1. 测试跨平台工具
python utils/platform_compat.py

# 2. 测试配置加载
python pipeline.py --help

# 3. 测试注册（如果配置好了）
python pipeline.py --register-only --cardw-config CTF-reg/config.paypal-proxy.json

# 4. 测试完整流程（需要完整配置）
python pipeline.py --config CTF-pay/config.paypal.json --paypal
```

---

## 📊 进度追踪

创建一个 TODO 列表：

```markdown
## Windows 适配进度

### 阶段 1：基础设施 ✅
- [x] 创建 utils/platform_compat.py
- [x] 测试跨平台工具
- [x] 创建文档

### 阶段 2：核心文件修改 🚧
- [ ] 修改 CTF-pay/card.py
  - [ ] 替换 /tmp/ 路径 (约 15 处)
  - [ ] 替换 venv 路径 (约 3 处)
  - [ ] 处理 xvfb 检测 (约 2 处)
- [ ] 修改 CTF-reg/browser_register.py
- [ ] 修改 pipeline.py

### 阶段 3：文档更新 ⏳
- [ ] 创建 docs/windows-setup.md
- [ ] 更新 README.md
- [ ] 更新 CLAUDE.md

### 阶段 4：测试验证 ⏳
- [ ] 单元测试
- [ ] 集成测试
- [ ] Windows 10 测试
- [ ] Windows 11 测试
```

---

## 🆘 常见问题

### Q1: 我应该从头重写吗？

**A:** 不建议。原因：
- 原项目逻辑复杂（8000 行 card.py）
- 协议细节很多（Stripe + PayPal + OAuth）
- 只是路径问题，不是架构问题
- 重写工作量太大（可能需要 1-2 个月）

**建议**：先做 Windows 适配，如果后续发现架构问题再考虑重构。

### Q2: 如何处理上游的大改动？

**A:** 分情况处理：
- **小改动**（bug 修复、新功能）：直接 merge
- **路径相关改动**：手动适配后 merge
- **架构重构**：评估后决定是否跟进

### Q3: 我的改动能合并回上游吗？

**A:** 可以尝试！步骤：
1. 确保你的改动不破坏 Linux 兼容性
2. 在你的 Fork 上创建 Pull Request 到上游
3. 说明这是 Windows 兼容性改进
4. 等待作者审核

但注意：作者可能不接受（项目说明中提到主要针对 Linux）。

### Q4: 如果上游不接受我的 PR 怎么办？

**A:** 没关系！继续维护你的 Fork：
- 你的 Fork 就是 "Windows 版本"
- 定期同步上游更新
- 在你的 README 中说明这是 Windows 适配版

---

## 🎁 额外资源

### 已创建的文件

1. **GIT_WORKFLOW_GUIDE.md** - Git 工作流详细指南
2. **WINDOWS_COMPAT_PLAN.md** - Windows 适配详细计划
3. **utils/platform_compat.py** - 跨平台工具模块
4. **CLAUDE_SKILLS_GUIDE.md** - Claude Code 技能使用指南

### 推荐工具

- **Git GUI**: GitHub Desktop / GitKraken / SourceTree
- **代码编辑器**: VS Code（推荐）/ PyCharm
- **对比工具**: Beyond Compare / WinMerge
- **终端**: Windows Terminal（推荐）/ PowerShell

---

## 🚀 下一步行动

1. **立即执行**：
   ```bash
   # Fork 项目（在 GitHub 网页上操作）
   # 然后运行：
   git remote add upstream https://github.com/DanOps-1/Gpt-Agreement-Payment.git
   git checkout -b feature/windows-compat
   git add utils/ *.md
   git commit -m "feat: add Windows compatibility layer"
   git push origin feature/windows-compat
   ```

2. **本周完成**：
   - 修改 `CTF-pay/card.py` 的路径问题
   - 测试基本功能

3. **下周完成**：
   - 修改其他核心文件
   - 更新文档
   - 完整测试

---

## 💡 最后的建议

1. **小步快跑**：每次只改一个文件，改完就测试
2. **频繁提交**：每个小改动都提交，方便回滚
3. **写好注释**：说明为什么这样改（方便以后维护）
4. **保持同步**：每周检查上游更新
5. **记录问题**：遇到问题记录在 GitHub Issues

---

**祝你开发顺利！有问题随时问我。** 🎉
