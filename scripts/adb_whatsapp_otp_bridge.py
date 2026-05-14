#!/usr/bin/env python3
"""Forward WhatsApp OTP notifications from an Android device/emulator to WebUI.

The bridge polls `adb shell dumpsys notification --noredact`, extracts GoPay /
WhatsApp verification codes from com.whatsapp notification blocks, and POSTs
new codes to `/api/whatsapp/ingest-otp`.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from hashlib import sha256
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_PACKAGE = "com.whatsapp"
DEFAULT_OTP_REGEX = r"(?<!\d)(\d{6})(?!\d)"


def _run_adb(adb: str, serial: str, args: list[str], timeout: float) -> str:
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return subprocess.check_output(
        cmd,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def _notification_dump(adb: str, serial: str, timeout: float) -> str:
    try:
        return _run_adb(adb, serial, ["shell", "dumpsys", "notification", "--noredact"], timeout)
    except subprocess.CalledProcessError as exc:
        output = exc.output or str(exc)
        raise RuntimeError(output.strip() or "adb dumpsys notification failed") from exc


def _whatsapp_blocks(dump: str, package: str) -> Iterable[str]:
    lines = dump.splitlines()
    seen: set[str] = set()
    for idx, line in enumerate(lines):
        if package not in line:
            continue
        start = max(0, idx - 10)
        end = min(len(lines), idx + 90)
        block = "\n".join(lines[start:end])
        key = sha256(block.encode("utf-8", errors="ignore")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        yield block


def _clean_otp(value: str) -> str:
    code = re.sub(r"\D", "", value or "")
    if 4 <= len(code) <= 8:
        return code
    return ""


def _extract_otp(text: str, code_regex: str) -> str:
    patterns = [
        r"(?:otp|one[-\s]*time|verification|verify|code|kode|verifikasi|gopay|whatsapp|验证码|驗證碼)[^\d]{0,100}(\d{4,8})(?!\d)",
        r"(?<!\d)(\d{4,8})(?!\d)[^\n\r]{0,100}(?:otp|one[-\s]*time|verification|verify|code|kode|verifikasi|gopay|验证码|驗證碼)",
        code_regex or DEFAULT_OTP_REGEX,
    ]
    for pattern in patterns:
        for match in reversed(list(re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL))):
            groups = match.groups() or (match.group(0),)
            for group in reversed(groups):
                code = _clean_otp(group)
                if code:
                    return code
    return ""


def _fingerprint(code: str, text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip())
    return sha256(f"{code}\n{normalized[:1000]}".encode("utf-8", errors="ignore")).hexdigest()


def _push(server: str, token: str, payload: dict, timeout: float) -> dict:
    url = server.rstrip("/") + "/api/whatsapp/ingest-otp?" + urlencode({"token": token})
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if not body:
            return {}
        return json.loads(body)


def _candidates(dump: str, package: str, code_regex: str) -> Iterable[tuple[str, str, str]]:
    for block in _whatsapp_blocks(dump, package):
        code = _extract_otp(block, code_regex)
        if not code:
            continue
        yield code, block, _fingerprint(code, block)


def main() -> int:
    parser = argparse.ArgumentParser(description="ADB WhatsApp OTP bridge for WebUI GoPay")
    parser.add_argument("--server", default="http://127.0.0.1:8765", help="WebUI backend base URL")
    parser.add_argument("--token", required=True, help="WebUI WhatsApp relay token")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial", default="", help="adb device serial, e.g. emulator-5554")
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--interval", type=float, default=0.7)
    parser.add_argument("--adb-timeout", type=float, default=8.0)
    parser.add_argument("--http-timeout", type=float, default=8.0)
    parser.add_argument("--code-regex", default=DEFAULT_OTP_REGEX)
    parser.add_argument("--once", action="store_true", help="push one new OTP and exit")
    parser.add_argument("--push-existing", action="store_true", help="also push notifications already present at startup")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    seen: set[str] = set()
    try:
        initial = _notification_dump(args.adb, args.serial, args.adb_timeout)
        if not args.push_existing:
            seen.update(fp for _code, _text, fp in _candidates(initial, args.package, args.code_regex))
            print(f"[adb-wa] baseline ignored {len(seen)} existing OTP notification(s)", flush=True)
    except Exception as exc:
        print(f"[adb-wa] adb not ready: {exc}", file=sys.stderr, flush=True)

    while True:
        try:
            dump = _notification_dump(args.adb, args.serial, args.adb_timeout)
            for code, text, fp in _candidates(dump, args.package, args.code_regex):
                if fp in seen:
                    continue
                seen.add(fp)
                payload = {
                    "otp": code,
                    "text": text,
                    "source": "adb_notification",
                    "package": args.package,
                    "ts": time.time(),
                }
                if args.dry_run:
                    print(f"[adb-wa] dry-run otp={code}", flush=True)
                else:
                    _push(args.server, args.token, payload, args.http_timeout)
                    print(f"[adb-wa] pushed otp={code}", flush=True)
                if args.once:
                    return 0
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"[adb-wa] {exc}", file=sys.stderr, flush=True)
        time.sleep(max(0.2, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
