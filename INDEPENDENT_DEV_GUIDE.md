# 独立 Windows 版本开发说明

## 📌 当前状态

- **分支**: `independent-windows-version`
- **远程仓库**: 仅保留你的 Fork (https://github.com/kyger104/Gpt-Agreement-Payment.git)
- **上游连接**: 已断开（不自动同步原项目）

## 🎯 开发策略

### 为什么选择独立开发？

1. **完全自主控制**：不受上游更新影响，按自己的节奏开发
2. **大胆重构**：可以自由改变架构，不用担心合并冲突
3. **专注 Windows**：针对 Windows 平台深度优化
4. **观察后决定**：先看原项目如何演进，再决定是否采纳

### 与原项目的关系

- ✅ **保留原始代码**：作为参考基础
- ✅ **手动跟踪更新**：定期查看原项目，选择性采纳有价值的改动
- ✅ **独立演进**：不自动合并，避免冲突
- ✅ **可以随时重新连接**：如果需要，可以重新添加 upstream

---

## 🔄 如何跟踪原项目更新

### 方法 1：在 GitHub 网页上查看

```
1. 访问原项目：https://github.com/DanOps-1/Gpt-Agreement-Payment
2. 查看 Commits 页面
3. 查看 Pull Requests 和 Issues
4. 手动对比有价值的改动
```

### 方法 2：临时添加 upstream 查看（推荐）

```bash
# 临时添加上游（不会自动同步）
git remote add upstream https://github.com/DanOps-1/Gpt-Agreement-Payment.git

# 拉取上游信息（不合并）
git fetch upstream

# 查看上游新提交
git log HEAD..upstream/main --oneline

# 查看具体改动
git diff HEAD..upstream/main

# 查看某个文件的改动
git diff HEAD..upstream/main -- CTF-pay/card.py

# 查看完后，可以删除 upstream（保持独立）
git remote remove upstream
```

### 方法 3：使用 Git 对比工具

```bash
# 对比两个项目的差异
git diff upstream/main..HEAD

# 生成补丁文件
git format-patch upstream/main..HEAD

# 查看某个提交的详细内容
git show <commit-hash>
```

---

## 📋 开发计划

### 阶段 1：Windows 基础适配（当前）

- [x] 创建跨平台工具模块 `utils/platform_compat.py`
- [ ] 修改 `CTF-pay/card.py` 的路径问题
- [ ] 修改 `CTF-reg/browser_register.py`
- [ ] 修改 `pipeline.py`
- [ ] 测试基本功能

### 阶段 2：深度 Windows 优化

- [ ] 优化 Windows 下的浏览器自动化
- [ ] 改进临时文件管理
- [ ] 添加 Windows 特定的错误处理
- [ ] 创建 PowerShell 脚本

### 阶段 3：架构改进（可选）

- [ ] 重构配置管理
- [ ] 模块化代码结构
- [ ] 改进日志系统
- [ ] 添加单元测试

### 阶段 4：定期审查原项目

- [ ] 每月查看原项目更新
- [ ] 评估有价值的改动
- [ ] 选择性地手动移植功能
- [ ] 记录不采纳的原因

---

## 🛠️ 日常工作流程

### 开发新功能

```bash
# 1. 确保在独立分支
git checkout independent-windows-version

# 2. 修改代码
# 编辑文件...

# 3. 测试
python utils/platform_compat.py
python pipeline.py --help

# 4. 提交
git add .
git commit -m "feat: your feature description"

# 5. 推送到你的 GitHub
git push origin independent-windows-version
```

### 查看原项目更新（每月一次）

```bash
# 1. 临时添加上游
git remote add upstream https://github.com/DanOps-1/Gpt-Agreement-Payment.git

# 2. 拉取信息
git fetch upstream

# 3. 查看新提交
git log HEAD..upstream/main --oneline --graph

# 4. 查看感兴趣的改动
git show <commit-hash>

# 5. 如果有价值，手动应用
# 方式 A：cherry-pick 单个提交
git cherry-pick <commit-hash>

# 方式 B：手动复制代码
# 查看改动，手动修改你的代码

# 6. 删除上游连接
git remote remove upstream
```

### 手动移植原项目的改动

```bash
# 1. 查看原项目某个文件的改动
git remote add upstream https://github.com/DanOps-1/Gpt-Agreement-Payment.git
git fetch upstream
git diff HEAD..upstream/main -- CTF-pay/hcaptcha_auto_solver.py > /tmp/upstream_changes.patch

# 2. 查看补丁内容
cat /tmp/upstream_changes.patch

# 3. 手动应用（不是自动 apply，而是参考着改）
# 打开文件，参考补丁内容，手动修改

# 4. 测试并提交
git add .
git commit -m "feat: port hCaptcha solver improvements from upstream"

# 5. 清理
git remote remove upstream
```

---

## 📊 优势与劣势

### ✅ 优势

1. **完全自主**：不受上游影响，按自己节奏开发
2. **避免冲突**：不会因为上游改动导致合并冲突
3. **大胆重构**：可以自由改变架构
4. **专注目标**：专注 Windows 平台优化

### ⚠️ 劣势

1. **手动同步**：需要手动跟踪和移植上游改动
2. **可能错过**：可能错过上游的重要 bug 修复
3. **工作量**：手动移植比自动合并费时
4. **分叉风险**：长期可能与上游差异很大

### 💡 建议

- **定期查看**：每月至少查看一次原项目
- **选择性移植**：只移植真正有价值的改动
- **记录决策**：记录为什么采纳或不采纳某个改动
- **保持文档**：维护好你的改动文档

---

## 🔗 如果需要重新连接上游

如果将来你想重新建立与原项目的连接：

```bash
# 1. 添加上游
git remote add upstream https://github.com/DanOps-1/Gpt-Agreement-Payment.git

# 2. 拉取上游
git fetch upstream

# 3. 创建新分支来测试合并
git checkout -b test-merge-upstream
git merge upstream/main

# 4. 解决冲突后，决定是否保留
# 如果满意：
git checkout independent-windows-version
git merge test-merge-upstream

# 如果不满意：
git checkout independent-windows-version
git branch -D test-merge-upstream
```

---

## 📝 版本说明

- **基于版本**: DanOps-1/Gpt-Agreement-Payment @ commit 1693844
- **分支时间**: 2026-05-03
- **独立开发开始**: 2026-05-03
- **最后同步**: 从未（完全独立）

---

## 📞 需要帮助？

如果在开发过程中需要：
1. 查看原项目的某个改动
2. 决定是否移植某个功能
3. 解决技术问题

随时问我！我会帮你分析和决策。

---

**祝开发顺利！** 🚀
