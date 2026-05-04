# Installation Guide

[← Back to README](../README.md)

## System Requirements

- **OS**: Linux (Ubuntu 22.04+ / Debian 11+ / Kali / Any systemd-based distribution)
- **Python**: 3.11+
- **Memory**: At least 2 GB (Running hCaptcha solver + Camoufox simultaneously)
- **Disk**: Core approx. 500 MB, total 5 GB including ML venv
- **Network**: Access to OpenAI / Stripe / PayPal / Cloudflare APIs
- **Optional**: xvfb (for headless mode), gost (for SOCKS5 with auth)

---

## Step 1: System Packages

```bash
# Kali / Debian / Ubuntu
sudo apt update && sudo apt install -y \
    python3 python3-pip python3-venv \
    xvfb \
    curl wget git \
    sqlite3 jq

# Install gost (SOCKS5-with-auth → SOCKS5-no-auth relay, as Camoufox doesn't support SOCKS5 auth)
sudo curl -sSfL \
    https://github.com/go-gost/gost/releases/latest/download/gost-linux-amd64 \
    -o /usr/local/bin/gost && sudo chmod +x /usr/local/bin/gost
```

---

## Step 2: Core Python Dependencies

```bash
git clone https://github.com/DanOps-1/Gpt-Agreement-Payment
cd Gpt-Agreement-Payment

pip install requests curl_cffi playwright camoufox browserforge mitmproxy pybase64

# Playwright + Camoufox browser binaries
playwright install firefox
camoufox fetch
```

---

## Step 3: Optional ML venv (hCaptcha Visual Solver)

The ML dependencies for the solver are quite heavy; it's recommended to install them in a separate venv:

```bash
python -m venv ~/.venvs/ctfml
~/.venvs/ctfml/bin/pip install \
    torch transformers \
    opencv-python pillow numpy
```

It runs without these, but the solver will skip heuristic fallbacks and rely solely on VLM. See [`configuration.md`](configuration.md#vlm-endpoint) for VLM endpoint configuration.

---

## Step 4: Copy Configuration Templates

```bash
cp CTF-pay/config.paypal.example.json       CTF-pay/config.paypal.json
cp CTF-reg/config.paypal-proxy.example.json CTF-reg/config.paypal-proxy.json
cp CTF-reg/config.example.json              CTF-reg/config.noproxy.json
```

Refer to [`configuration.md`](configuration.md) for field definitions.

---

## Step 5: CF API Token (For Automated Subdomains)

If you're using a multi-zone domain pool to automatically create catch-all subdomains, a Cloudflare API token is required:

1. Log in to [https://dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens)
2. **Create Token** → **Custom token**
3. Permissions:
   - `Zone` → `DNS` → `Edit`
   - `Zone` → `Zone` → `Read`
4. Select the zones you want to manage under Zone Resources.
5. After creation, add it to `SQLite runtime_meta[secrets]`:

```json
{
  "cloudflare": {
    "api_token": "YOUR_TOKEN",
    "forward_to": "admin@example.com"
  }
}
```

The `output/` directory is ignored by git and won't be committed.

---

## Step 6: Initial PayPal Run

The first run will launch a Camoufox window asking you to log in to PayPal. This step **must be done manually** once to pass OTP 2FA:

```bash
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal
```

> ⚠️ **If using a remote server**: Use VNC / X11 forwarding or temporarily disable `xvfb-run` to view the display. Alternatively, copy the entire `paypal_cf_persist/` directory from a machine where you've already logged in.

After a successful login, cookies are persisted to `CTF-pay/paypal_cf_persist/` (gitignored), and subsequent runs will reuse the trusted device state.

> ⚠️ **Important: Disable mobile push notifications in PayPal settings** (Settings → Login management → Remove mobile devices). Otherwise, it will prioritize push notifications over email OTP, which will stall the automation.

---

## Verifying Installation

```bash
# Check core packages
python -c "import camoufox, playwright, curl_cffi; print('core ok')"

# Check ML venv
~/.venvs/ctfml/bin/python -c "import torch, transformers, cv2; print('ml ok')"

# Check gost
gost -V

# Check xvfb
which xvfb-run
```

If all four return OK, you're ready to start.

---

## Common Installation Issues

### `camoufox fetch` stalls or fails to download

If the connection to GitHub's release page is slow, use a proxy:

```bash
HTTPS_PROXY=http://127.0.0.1:7890 camoufox fetch
```

Alternatively, manually download from [https://github.com/daijro/camoufox/releases](https://github.com/daijro/camoufox/releases) and place it in `~/.cache/camoufox/`.

### `playwright install firefox` fails

```bash
# Use a mirror (e.g., in China)
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    playwright install firefox
```

### Slow or failing Torch installation

```bash
# CPU-only version (smaller)
~/.venvs/ctfml/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
```

If your machine has a GPU and you want to use CUDA:

```bash
~/.venvs/ctfml/bin/pip install torch --index-url https://download.pytorch.org/whl/cu121
```

### Camoufox reports `socks5 auth not supported`

This is expected behavior. Configure a gost relay:

```bash
gost -L=socks5://:18898 -F=socks5://USER:PASS@PROXY_HOST:PORT &
```

Then point the proxy in the config to `socks5://127.0.0.1:18898`. The daemon mode has a built-in gost watchdog to manage this process automatically.

### `cannot open display` error

This happens if xvfb isn't running or the environment variable isn't passed:

```bash
# Wrap it with xvfb-run (Recommended)
xvfb-run -a python pipeline.py ...

# Or start Xvfb manually
Xvfb :99 -screen 0 1920x1080x24 &
DISPLAY=:99 python pipeline.py ...
```

---

## Next Steps

- Detailed Configuration → [`configuration.md`](configuration.md)
- Running the App → [`operating-modes.md`](operating-modes.md)
- Troubleshooting → [`debugging.md`](debugging.md)
