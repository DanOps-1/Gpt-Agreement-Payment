# 本地测试环境

这套本地环境只覆盖 Web UI、后端接口测试，以及 `CTF-pay` 的本地 mock 回放。它不会访问真实支付、真实账号、验证码平台或第三方服务，适合做功能联调和回归测试。

## 准备

需要安装：

- Windows PowerShell
- Python 3.11+
- Node.js 20+，建议开启 corepack，因为前端使用 pnpm

如果 PowerShell 阻止脚本运行，可以先在当前窗口执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

## 一键初始化

在仓库根目录执行：

```powershell
.\scripts\setup_local_env.ps1
```

脚本会创建 `.venv`，安装 `webui/requirements.txt`，安装前端依赖并构建 `webui/frontend/dist`。

如果你的 pip 默认镜像不可用，可以临时切到官方源：

```powershell
.\scripts\setup_local_env.ps1 -PipIndexUrl https://pypi.org/simple
```

如果本机同时装了多个 Python，可以指定 3.11+ 路径：

```powershell
.\scripts\setup_local_env.ps1 -PythonPath C:\Python311\python.exe
```

如果之前误用低版本 Python 创建过 `.venv`，先删除 `.venv`，再用 Python 3.11+ 重新初始化。

如果 pip 在你的网络下需要代理：

```powershell
.\scripts\setup_local_env.ps1 -PipProxy http://127.0.0.1:7890
```

如果公司或本地网络做了 HTTPS 拦截，临时信任指定源也可以这样跑：

```powershell
.\scripts\setup_local_env.ps1 `
  -PipIndexUrl https://pypi.org/simple `
  -PipTrustedHost pypi.org,files.pythonhosted.org
```

只装后端依赖：

```powershell
.\scripts\setup_local_env.ps1 -SkipFrontend
```

装前端依赖但跳过构建：

```powershell
.\scripts\setup_local_env.ps1 -SkipBuild
```

## 启动 Web UI

```powershell
.\scripts\start_webui.ps1
```

打开：

```text
http://127.0.0.1:8765
```

首次访问会进入 `/setup` 创建管理员账号。本地数据默认写到 `output/webui.db`。

换端口：

```powershell
.\scripts\start_webui.ps1 -Port 8766
```

后端热重载：

```powershell
.\scripts\start_webui.ps1 -Reload
```

前端热重载开发模式可以另开一个终端：

```powershell
cd webui\frontend
pnpm dev
```

然后打开 `http://127.0.0.1:5173`。Vite 会把 `/api` 代理到 `127.0.0.1:8765`。

## 跑本地 mock 回放

```powershell
.\scripts\run_local_mock.ps1
```

这个命令会使用 `CTF-pay/config.local-mock.json`，启动仅监听 `127.0.0.1` 的 mock gateway，回放 checkout、challenge、3DS、poll 的状态机。

输出位置：

- `output/logs/card.log`
- `output/ctf_local_mock_latest.json`
- `output/local-mock.config.json`

切换场景：

```powershell
.\scripts\run_local_mock.ps1 -Scenario direct_decline
.\scripts\run_local_mock.ps1 -Scenario challenge_failed
.\scripts\run_local_mock.ps1 -Scenario challenge_pass_then_decline
```

## 跑测试

后端和前端全部跑：

```powershell
.\scripts\run_local_tests.ps1
```

只跑后端：

```powershell
.\scripts\run_local_tests.ps1 -BackendOnly
```

只跑前端：

```powershell
.\scripts\run_local_tests.ps1 -FrontendOnly
```

## 常见问题

`Missing .venv`：先执行 `.\scripts\setup_local_env.ps1`。

`pnpm was not found`：安装 Node.js 20+ 后执行 `corepack enable`，再重新运行初始化脚本。

`address already in use`：8765 端口被占用，使用 `.\scripts\start_webui.ps1 -Port 8766`。

前端页面 404 或资源缺失：重新执行 `.\scripts\setup_local_env.ps1`，或进入 `webui\frontend` 后执行 `pnpm build`。
