# 安装指南

[← 回到 README](../README.md)

## 系统要求

- **OS**：Linux（Ubuntu 22.04+ / Debian 11+ / Kali / 任何 systemd 发行版）
- **Python**：3.11+
- **内存**：至少 2 GB（hCaptcha solver + Camoufox 同时跑）
- **磁盘**：核心约 500 MB，加上 ML venv 总共 5 GB
- **网络**：能访问 OpenAI / Stripe / PayPal / Cloudflare API
- **可选**：xvfb（跑无头时用）、gost（带 auth 的 socks5 用）

---

## 第一步：系统包

```bash
# Kali / Debian / Ubuntu
sudo apt update && sudo apt install -y \
    python3 python3-pip python3-venv \
    xvfb \
    curl wget git \
    sqlite3 jq

# 装 gost（SOCKS5-with-auth → SOCKS5-no-auth 中继，Camoufox 不吃 socks5 auth）
sudo curl -sSfL \
    https://github.com/go-gost/gost/releases/latest/download/gost-linux-amd64 \
    -o /usr/local/bin/gost && sudo chmod +x /usr/local/bin/gost
```

---

## 第二步：核心 Python 依赖

```bash
git clone https://github.com/DanOps-1/Gpt-Agreement-Payment
cd Gpt-Agreement-Payment

pip install requests curl_cffi playwright camoufox browserforge mitmproxy pybase64

# Playwright + Camoufox 浏览器二进制
playwright install firefox
camoufox fetch
```

---

## 第三步：可选 ML venv（hCaptcha 视觉求解器）

solver 的 ML 依赖比较重，建议单独装到独立 venv：

```bash
python -m venv ~/.venvs/ctfml
~/.venvs/ctfml/bin/pip install \
    torch transformers \
    opencv-python pillow numpy
```

不装也能跑，只是 solver 会跳过启发式回退，全靠 VLM。VLM endpoint 配置看 [`configuration.md`](configuration.md#vlm-endpoint)。

---

## 第四步：拷模板配置

```bash
cp CTF-pay/config.paypal.example.json       CTF-pay/config.paypal.json
cp CTF-reg/config.paypal-proxy.example.json CTF-reg/config.paypal-proxy.json
cp CTF-reg/config.example.json              CTF-reg/config.noproxy.json
```

每个字段含义看 [`configuration.md`](configuration.md)。

---

## 第五步：CF API token（开新子域用）

如果要走多 zone 域池自动开 catch-all 子域，需要 Cloudflare API token：

1. 登 [https://dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens)
2. **Create Token** → **Custom token**
3. 权限：
   - `Zone` → `DNS` → `Edit`
   - `Zone` → `Zone` → `Read`
4. Zone Resources 选你要管的 zone
5. 创建后写到 `SQLite runtime_meta[secrets]`：

```json
{
  "cloudflare": {
    "api_token": "你的 token",
    "forward_to": "admin@example.com"
  }
}
```

`output/` 目录已经被 gitignore，不会进 commit。

---

## 第六步：第一次跑 PayPal

第一次跑会弹 Camoufox 让你登 PayPal。这一步**必须人肉过一次** OTP 2FA：

```bash
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal
```

> ⚠️ **如果是远程服务器**：用 VNC / X11 forwarding 或者临时取消 `xvfb-run` 直接显示。或者把 `paypal_cf_persist/` 整个目录从已经登过 PayPal 的机器拷过来。

登成功后 cookies 持久化到 `CTF-pay/paypal_cf_persist/`（gitignored），后面跑都复用受信任设备状态。

> ⚠️ **PayPal 一定要在它后台关掉手机推送**（Settings → Login management → 移除手机设备），否则它会优先发推送而不是邮箱 OTP，自动化会卡住。

---

## 验证安装

```bash
# 看核心包
python -c "import camoufox, playwright, curl_cffi; print('core ok')"

# 看 ML venv
~/.venvs/ctfml/bin/python -c "import torch, transformers, cv2; print('ml ok')"

# 看 gost
gost -V

# 看 xvfb
which xvfb-run
```

四条都返 OK 就可以开跑了。

---

## 常见安装问题

### `camoufox fetch` 卡住或下载失败

国内网络拉 GitHub release 慢，可以加代理：

```bash
HTTPS_PROXY=http://127.0.0.1:7890 camoufox fetch
```

或者手动从 [https://github.com/daijro/camoufox/releases](https://github.com/daijro/camoufox/releases) 下载放到 `~/.cache/camoufox/`。

### `playwright install firefox` 失败

```bash
# 用国内镜像
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    playwright install firefox
```

### Torch 安装慢 / 装不上

```bash
# CPU only 版本（小很多）
~/.venvs/ctfml/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
```

如果你的机器有 GPU 想用 CUDA：

```bash
~/.venvs/ctfml/bin/pip install torch --index-url https://download.pytorch.org/whl/cu121
```

### Camoufox 报 `socks5 auth not supported`

这是预期行为，配 gost 中继：

```bash
gost -L=socks5://:18898 -F=socks5://USER:PASS@PROXY_HOST:PORT &
```

然后在 config 里把 proxy 指向 `socks5://127.0.0.1:18898`。daemon 模式有内置 gost 看门狗，会自动管理这个进程。

### 跑起来报 `cannot open display`

xvfb 没跑或者环境变量没传：

```bash
# 用 xvfb-run 包一层（推荐）
xvfb-run -a python pipeline.py ...

# 或者手动起 Xvfb
Xvfb :99 -screen 0 1920x1080x24 &
DISPLAY=:99 python pipeline.py ...
```

---

## 下一步

- 配置详解 → [`configuration.md`](configuration.md)
- 跑起来 → [`operating-modes.md`](operating-modes.md)
- 排错 → [`debugging.md`](debugging.md)
