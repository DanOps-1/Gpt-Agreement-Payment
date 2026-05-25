#!/usr/bin/env python3
"""GoPay "multiple accounts from one credential" protocol batch runner.

Link a set of GoPay credentials (phone, pin, country_code) at protocol layer to N
ChatGPT accounts. Each attempt uses a different device fingerprint (sign rotator) + different
user-agent + different correlation-id, so GoPay server treats each request as a different device
session, avoiding cumulative risk score.

Flow per ChatGPT account:
  1. fresh_checkout (ChatGPT) → cs_live + Stripe pm + redirect_url
  2. midtrans linking (new reference_id)
  3. GoPay validate-reference (browser, no signing)
  4. GoPay user-consent → OTP sent (WhatsApp/SMS to phone)
  5. user provides OTP → write to /tmp/gopay_otp_<idx>.txt
  6. validate-otp → pin tokenize (/nb, PIN plaintext) → validate-pin
  7. midtrans/charge:
     - SUCCESS → payment/process settle → Stripe webhook → Plus upgrade
     - DENIED (fraud)  → linking_only state, Stripe webhook async may still upgrade
  8. wait verify_delay seconds then re-fetch ChatGPT plan_type to check if plus

CLI:
  python -m scripts.gopay_oneshot_multi \
      --gopay-config /path/to/runtime.json \
      --target-emails a@x.com,b@x.com,c@x.com \
      --otp-base /tmp/gopay_otp \
      --verify-delay 60

per-account artifact: ./output/gopay_multi_<email>_<ts>.json"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Allow this script to run as both `python -m scripts.gopay_oneshot_multi` and `python scripts/...`
_HERE = Path(__file__).resolve().parent
_CTF_PAY = _HERE.parent
if str(_CTF_PAY) not in sys.path:
    sys.path.insert(0, str(_CTF_PAY))


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s)


def _now_ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def run_one(*, target_email: str, config_path: str, otp_file: str,
            verify_delay: int, outdir: Path, idx: int, total: int) -> dict:
    """Run GoPay protocol linking flow for a single ChatGPT account."""
    print(f"\n{'#'*72}\n# [{idx+1}/{total}] target={target_email}\n{'#'*72}")
    # Clean up old OTP files (avoid misreading previous OTP)
    if os.path.exists(otp_file):
        os.unlink(otp_file)
        print(f"  cleared {otp_file}")

    # spawn pipeline.py --gopay --pay-only --target-emails <email>
    cmd = [
        sys.executable, "-u",
        str(_CTF_PAY.parent / "pipeline.py"),
        "--config", config_path,
        "--gopay",
        "--pay-only",
        "--target-emails", target_email,
        "--gopay-otp-file", otp_file,
    ]
    log_file = outdir / f"gopay_multi_{_slug(target_email)}_{_now_ts()}.log"
    art_file = outdir / f"gopay_multi_{_slug(target_email)}_{_now_ts()}.json"
    print(f"  log: {log_file}")
    print(f"  art: {art_file}")
    print(f"  >>> [user] OTP 来时写到 {otp_file} (linking step 会 echo 'OTP sent')")

    started = time.time()
    state = "spawn_error"
    err = ""
    pipe_result = None
    try:
        with open(log_file, "w") as lf:
            proc = subprocess.Popen(
                cmd, stdout=lf, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        proc.wait(timeout=900)
        rc = proc.returncode
        state = "ok" if rc == 0 else f"exit_{rc}"
        # Find CARD_RESULT_JSON
        for line in log_file.read_text(errors="replace").splitlines()[::-1]:
            line = line.strip()
            if line.startswith("CARD_RESULT_JSON="):
                try:
                    pipe_result = json.loads(line.split("=", 1)[1])
                except Exception:
                    pass
                break
    except subprocess.TimeoutExpired:
        proc.kill()
        state = "timeout"
        err = "subprocess > 900s"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    # verify phase: wait for webhook async processing, then check plan_type
    plan_now = ""
    if verify_delay > 0:
        print(f"  ⏳ wait {verify_delay}s for Stripe webhook → ChatGPT plan_type")
        time.sleep(verify_delay)
    try:
        plan_now = _probe_plan(target_email)
        print(f"  plan_type after: {plan_now}")
    except Exception as e:
        plan_now = f"probe_err:{type(e).__name__}"

    artifact = {
        "target_email": target_email,
        "started_at": started,
        "duration_s": round(time.time() - started, 1),
        "state": state,
        "pipeline_result": pipe_result,
        "plan_after_wait": plan_now,
        "error": err,
        "log_file": str(log_file),
    }
    art_file.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))
    print(f"  done state={state} plan_after={plan_now}")
    return artifact


def _probe_plan(email: str) -> str:
    """Fetch new access_token using session_token from webui DB email, parse chatgpt_plan_type."""
    import sqlite3
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return "no_curl_cffi"
    db = Path("/root/Gpt-Agreement-Payment/output/webui.db")
    if not db.exists():
        return "no_db"
    c = sqlite3.connect(str(db))
    r = c.execute(
        "SELECT session_token FROM registered_accounts WHERE email=? ORDER BY id DESC LIMIT 1",
        (email,),
    ).fetchone()
    if not r or not r[0]:
        return "no_session_token"
    s = creq.Session(impersonate="chrome136")
    s.proxies = {
        "https": os.environ.get("CHATGPT_PROXY", "socks5h://127.0.0.1:18898"),
        "http": os.environ.get("CHATGPT_PROXY", "socks5h://127.0.0.1:18898"),
    }
    s.cookies.set("__Secure-next-auth.session-token", r[0], domain="chatgpt.com")
    try:
        rr = s.get("https://chatgpt.com/api/auth/session", timeout=15)
        if rr.status_code != 200:
            return f"http_{rr.status_code}"
        return str((rr.json().get("account") or {}).get("planType") or "")
    except Exception as e:
        return f"err:{type(e).__name__}"


def main():
    p = argparse.ArgumentParser(
        description="GoPay 一号多开 protocol batch runner",
    )
    p.add_argument("--gopay-config", required=True,
                   help="含 gopay.{phone_number,pin,country_code,otp,protocol} 段的 JSON")
    p.add_argument("--target-emails", required=True,
                   help="ChatGPT 账号 emails, 逗号分隔, 顺序 batch")
    p.add_argument("--otp-base", default="/tmp/gopay_otp",
                   help="OTP file 前缀; 第 i 次用 <base>_<i>.txt")
    p.add_argument("--verify-delay", type=int, default=60,
                   help="每次 linking 完成后等待 Stripe webhook 秒数 (default 60)")
    p.add_argument("--outdir", default="./output/gopay_multi",
                   help="per-attempt artifact 落盘目录")
    args = p.parse_args()

    emails = [e.strip() for e in args.target_emails.split(",") if e.strip()]
    if not emails:
        print("[gopay-multi] target-emails empty", file=sys.stderr)
        sys.exit(2)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    summary = []
    for i, em in enumerate(emails):
        otp_file = f"{args.otp_base}_{i}.txt"
        r = run_one(
            target_email=em,
            config_path=args.gopay_config,
            otp_file=otp_file,
            verify_delay=args.verify_delay,
            outdir=outdir,
            idx=i,
            total=len(emails),
        )
        summary.append(r)

    plus_n = sum(1 for r in summary if r.get("plan_after_wait") == "plus")
    print(f"\n{'='*72}")
    print(f"SUMMARY: {plus_n}/{len(summary)} ChatGPT accounts upgraded to plus")
    for r in summary:
        print(f"  {r['target_email']}: state={r['state']} plan={r['plan_after_wait']}")
    print(f"  artifacts in: {outdir}")
    sys.exit(0 if plus_n > 0 else 1)


if __name__ == "__main__":
    main()
