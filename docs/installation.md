# Installation Guide

[← Back to README](../README.md)

## System Requirements

- **OS**: Linux (Ubuntu 22.04+ / Debian 11+ / Kali / any systemd distribution)
- **Python**: 3.11+
- **Memory**: At least 2 GB (hCaptcha solver + Camoufox running simultaneously)
- **Disk**: Core ~500 MB, plus 5 GB total with ML venv
- **Network**: Access to OpenAI / Stripe / PayPal / Cloudflare API
- **Optional**: xvfb (for headless execution), gost (for socks5 with auth)

---

## Step 1: System Packages```bash
# Kali / Debian / Ubuntu
sudo apt update && sudo apt install -y \
    python3 python3-pip python3-venv \
    xvfb \
    curl wget git \
    sqlite3 jq

# Install gost (SOCKS5-with-auth → SOCKS5-no-auth relay, Camoufox doesn't support socks5 auth)
sudo curl -sSfL \
    https://github.com/go-gost/gost/releases/latest/download/gost-linux-amd64 \
    -o /usr/local/bin/gost && sudo chmod +x /usr/local/bin/gost
```---

## Step 2: Core Python Dependencies```bash
git clone https://github.com/DanOps-1/Gpt-Agreement-Payment
cd Gpt-Agreement-Payment

pip install requests curl_cffi playwright camoufox browserforge mitmproxy pybase64

# Playwright + Camoufox browser binary
playwright install firefox
camoufox fetch
```## Step 3: Optional ML venv (hCaptcha Visual Solver)

The ML dependencies for the solver are quite heavy, so it's recommended to install them in a separate venv:```bash
python -m venv ~/.venvs/ctfml
~/.venvs/ctfml/bin/pip install \
    torch transformers \
    opencv-python pillow numpy
```It can run without installation, it's just that the solver will skip heuristic fallback and rely entirely on VLM. For VLM endpoint configuration, see [`configuration.md`](configuration.md#vlm-endpoint).

---

## Step 4: Copy Template Configuration```bash
cp CTF-pay/config.paypal.example.json       CTF-pay/config.paypal.json
cp CTF-reg/config.paypal-proxy.example.json CTF-reg/config.paypal-proxy.json
cp CTF-reg/config.example.json              CTF-reg/config.noproxy.json
```See [`configuration.md`](configuration.md) for the meaning of each field.

---

## Step 5: CF API Token (for creating new subdomains)

If you want to use multiple zone domain pools with automatic catch-all subdomain creation, you need a Cloudflare API token:

1. Log in to [https://dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens)
2. **Create Token** → **Custom token**
3. Permissions:
   - `Zone` → `DNS` → `Edit`
   - `Zone` → `Zone` → `Read`
4. Select the zones you want to manage in Zone Resources
5. After creation, write it to `SQLite runtime_meta[secrets]`:```json
{
  "cloudflare": {
    "api_token": "your token",
    "forward_to": "admin@example.com"
  }
}
````output/` directory is already in gitignore and won't be committed.

---

## Step 6: First Run of PayPal

The first run will launch Camoufox and prompt you to log in to PayPal. This step **requires manual handling of OTP 2FA at least once**:```bash
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal
```> ⚠️ **If using a remote server**: Use VNC / X11 forwarding or temporarily disable `xvfb-run` to display directly. Alternatively, copy the entire `paypal_cf_persist/` directory from a machine that has already logged into PayPal.

After successful login, cookies are persisted to `CTF-pay/paypal_cf_persist/` (gitignored), and subsequent runs will reuse the trusted device state.

> ⚠️ **You must disable push notifications on PayPal's backend** (Settings → Login management → Remove mobile device), otherwise it will prioritize sending push notifications instead of email OTP, and automation will get stuck.

---

## Verifying Installation```bash
# Check core packages
python -c "import camoufox, playwright, curl_cffi; print('core ok')"

# Check ML venv
~/.venvs/ctfml/bin/python -c "import torch, transformers, cv2; print('ml ok')"

# Check gost
gost -V

# Check xvfb
which xvfb-run
```All four returning OK means you're ready to go.

---

## Common Installation Issues

### `camoufox fetch` hangs or download fails

Domestic networks have slow GitHub release downloads, you can add a proxy:```bash
HTTPS_PROXY=http://127.0.0.1:7890 camoufox fetch
```Or manually download from [https://github.com/daijro/camoufox/releases](https://github.com/daijro/camoufox/releases) and place it in `~/.cache/camoufox/`.

### `playwright install firefox` fails```bash
# Use domestic mirror
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    playwright install firefox
```### Torch Installation is Slow / Won't Install```bash
# CPU only version (much smaller)
~/.venvs/ctfml/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
```# If your machine has a GPU and wants to use CUDA:```bash
~/.venvs/ctfml/bin/pip install torch --index-url https://download.pytorch.org/whl/cu121
```### Camoufox Reports `socks5 auth not supported`

This is expected behavior. Configure a gost relay:```bash
gost -L=socks5://:18898 -F=socks5://USER:PASS@PROXY_HOST:PORT &
```Then in config, point the proxy to `socks5://127.0.0.1:18898`. Daemon mode has a built-in gost watchdog that will automatically manage this process.

### Getting `cannot open display` error when running

xvfb is not running or the environment variable is not set:```bash
# Wrap with xvfb-run (recommended)
xvfb-run -a python pipeline.py ...

# Or manually start Xvfb
Xvfb :99 -screen 0 1920x1080x24 &
DISPLAY=:99 python pipeline.py ...
```---

## Next Steps

- Configuration Details → [`configuration.md`](configuration.md)
- Getting Started → [`operating-modes.md`](operating-modes.md)
- Troubleshooting → [`debugging.md`](debugging.md)