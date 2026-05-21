"""GoPay Protocol Client V2 — bypasses official app, uses reversed protocol.

Flow (steps 14a-14d of pipeline) — given a tref from Midtrans QRIS charge:
  14a. GET  gwa.gopayapi.com/v1/payment/validate?reference_id={tref}     (poll until ready)
  14b. POST gwa.gopayapi.com/v1/payment/confirm?reference_id={tref}      (get challenge_id, client_id)
  14c. POST customer.gopayapi.com/api/v1/users/pin/tokens/nb             (PIN tokenize, X-E1 SIGNED)
  14d. POST gwa.gopayapi.com/v1/payment/process?reference_id={tref}      (settle with pin_token)

Only step 14c requires the X-E1 v2 signature; others use Origin/Referer only.

Usage:
    client = GoPayProtocolClientV2(K, sso, pin, template_path)
    result = client.settle_merchanttransfer(deeplink_or_tref)
"""
from __future__ import annotations

import gzip
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CTF_PAY = _HERE.parent.parent  # CTF-pay/ 加进 sys.path 让 `import gopay` 解析
if str(_CTF_PAY) not in sys.path:
    sys.path.insert(0, str(_CTF_PAY))

from gopay.sign.v2 import load_template, build_sign_headers


DEFAULT_X_M1 = (
    "3:1778725895447-8854231793310991186,"
    "4:131072,"
    "5:universal990|1800|8,"
    "6:74:05:A5:09:9C:61,"
    "7:TPLINK-VNE9CI,"
    "8:1080x1920,"
    "10:1,"
    "11:CmfRoRt6Gzl3aiWRsbaczQgJFq0vuhETrNMRljTT700=,"
    "15:724629431c694ee54c615f884bd17527,"
    "16:f4105d22-bc01-41b4-90c6-5be29f6c2eac"
)


@dataclass
class StepResult:
    name: str
    code: int
    body: bytes
    json_body: dict | None = None
    error: str = ''


def parse_tref(deeplink_or_tref: str) -> str:
    if 'tref=' in deeplink_or_tref:
        return urllib.parse.parse_qs(urllib.parse.urlparse(deeplink_or_tref).query).get('tref', [''])[0]
    return deeplink_or_tref


def _http(method: str, url: str, headers: dict | None = None,
          body: bytes | dict | None = None, timeout: int = 15) -> tuple[int, bytes]:
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        data = resp.read()
        if resp.headers.get('content-encoding') == 'gzip':
            data = gzip.decompress(data)
        return resp.status, data
    except urllib.error.HTTPError as e:
        data = e.read()
        if e.headers.get('content-encoding') == 'gzip':
            try: data = gzip.decompress(data)
            except: pass
        return e.code, data
    except Exception as e:
        return -1, str(e).encode()


class GoPayProtocolClientV2:
    def __init__(self, K: bytes | str, sso: str, pin: str,
                 template_path: str = '/tmp/big_msg_1867.bin',
                 device_id: str = 'b66aedfffc4c1068',
                 x_m1: str = DEFAULT_X_M1,
                 log=print):
        if isinstance(K, str): K = K.encode()
        self.K = K
        self.sso = sso
        self.pin = pin
        self.device_id = device_id
        self.x_m1 = x_m1
        self.template = load_template(template_path)
        self.log = log

    # ─── Step 14a: validate ────────────────────────────────────────
    def step_validate(self, tref: str, retries: int = 8, delay: float = 1.5) -> StepResult:
        url = f"https://gwa.gopayapi.com/v1/payment/validate?reference_id={tref}"
        headers = {
            'Origin': 'https://merchants-gws-app.gopayapi.com',
            'Referer': 'https://merchants-gws-app.gopayapi.com/',
            'Accept': 'application/json',
        }
        for i in range(retries):
            code, body = _http('GET', url, headers)
            try:
                jb = json.loads(body)
            except Exception:
                jb = None
            self.log(f"  [14a] validate #{i+1}: {code} {body[:120]!r}")
            if code == 200 and jb and jb.get('success'):
                return StepResult('validate', code, body, jb)
            time.sleep(delay)
        return StepResult('validate', code, body, jb, error=f'failed after {retries} retries')

    # ─── Step 14b: confirm (get PIN challenge) ─────────────────────
    def step_confirm(self, tref: str) -> StepResult:
        url = f"https://gwa.gopayapi.com/v1/payment/confirm?reference_id={tref}"
        headers = {
            'Origin': 'https://merchants-gws-app.gopayapi.com',
            'Referer': 'https://merchants-gws-app.gopayapi.com/',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        code, body = _http('POST', url, headers, body={'payment_instructions': []})
        try:
            jb = json.loads(body)
        except Exception:
            jb = None
        self.log(f"  [14b] confirm: {code} {body[:200]!r}")
        return StepResult('confirm', code, body, jb)

    # ─── Step 14c: PIN tokenize (X-E1 SIGNED) ──────────────────────
    def step_pin_tokenize(self, challenge_id: str, client_id: str) -> StepResult:
        host = 'customer.gopayapi.com'
        path = '/api/v1/users/pin/tokens/nb'
        url_sign = f"{host}{path}"
        sig = build_sign_headers(self.template, self.K, url=url_sign, method='POST')
        headers = {
            'accept-encoding': 'gzip',
            'authorization': f'Bearer {self.sso}',
            'x-location-accuracy': '14.16100025177002',
            'gojek-service-area': '1',
            'country-code': 'ID',
            'support-request-id': str(uuid.uuid4()),
            'x-appversion': '2.8.0',
            'x-location': '35.6763787,139.649962',
            'x-m1': self.x_m1,
            'gojek-country-code': 'ID',
            'x-uniqueid': self.device_id,
            'x-phonemake': 'samsung',
            'x-help-version': '2.8.0',
            'user-agent': 'GoPay/2.8.0 (com.gojek.gopay; build:2080; Android, 12)',
            'x-deviceos': 'Android, 12',
            'x-user-type': 'customer',
            'x-appid': 'com.gojek.gopay',
            'gojek-timezone': 'Asia/Jakarta',
            'content-type': 'application/json',
            'x-apptype': 'GOPAY',
            'x-user-locale': 'en_ID',
        }
        headers.update(sig)
        body = {
            'challenge_id': challenge_id,
            'client_id': client_id,
            'pin': self.pin,
        }
        code, resp = _http('POST', f"https://{host}{path}", headers, body=body)
        try:
            jb = json.loads(resp)
        except Exception:
            jb = None
        self.log(f"  [14c] pin_tokenize: {code} {resp[:200]!r}")
        return StepResult('pin_tokenize', code, resp, jb)

    # ─── Step 14d: process (settle) ────────────────────────────────
    def step_process(self, tref: str, pin_token: str) -> StepResult:
        url = f"https://gwa.gopayapi.com/v1/payment/process?reference_id={tref}"
        headers = {
            'Origin': 'https://merchants-gws-app.gopayapi.com',
            'Referer': 'https://merchants-gws-app.gopayapi.com/',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        body = {
            'challenge': {
                'type': 'GOPAY_PIN_CHALLENGE',
                'value': {'pin_token': pin_token},
            },
        }
        code, resp = _http('POST', url, headers, body=body)
        try:
            jb = json.loads(resp)
        except Exception:
            jb = None
        self.log(f"  [14d] process: {code} {resp[:200]!r}")
        return StepResult('process', code, resp, jb)

    # ─── Full settlement flow ──────────────────────────────────────
    def settle_merchanttransfer(self, deeplink_or_tref: str) -> dict:
        tref = parse_tref(deeplink_or_tref)
        self.log(f"[client] tref={tref}, pin=*** (len={len(self.pin)})")

        # 14a
        r_validate = self.step_validate(tref)
        if r_validate.error or r_validate.code != 200:
            return {'state': 'failed_validate', 'tref': tref, 'detail': r_validate.body[:300].decode('utf-8', errors='replace')}

        # 14b
        r_confirm = self.step_confirm(tref)
        if r_confirm.code != 200:
            return {'state': 'failed_confirm', 'tref': tref, 'detail': r_confirm.body[:500].decode('utf-8', errors='replace')}
        jb = r_confirm.json_body or {}
        ch = jb.get('data', {}).get('challenge', {}).get('action', {}).get('value', {})
        challenge_id = ch.get('challenge_id', '')
        client_id = ch.get('client_id', '')
        if not challenge_id:
            return {'state': 'no_challenge', 'tref': tref, 'response': jb}

        # 14c
        r_pin = self.step_pin_tokenize(challenge_id, client_id)
        if r_pin.code != 200:
            return {'state': 'failed_pin', 'tref': tref, 'detail': r_pin.body[:500].decode('utf-8', errors='replace')}
        pin_token = (r_pin.json_body or {}).get('data', {}).get('pin_token') or \
                    (r_pin.json_body or {}).get('pin_token', '')
        if not pin_token:
            return {'state': 'no_pin_token', 'tref': tref, 'response': r_pin.json_body}

        # 14d
        r_process = self.step_process(tref, pin_token)
        if r_process.code != 200:
            return {'state': 'failed_process', 'tref': tref, 'detail': r_process.body[:500].decode('utf-8', errors='replace')}

        return {'state': 'settled', 'tref': tref, 'pin_token_len': len(pin_token)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--deeplink', help='GoPay merchanttransfer deeplink or just tref')
    ap.add_argument('--tref', help='Alternative: just the tref string')
    ap.add_argument('--K', default='1V79g&FZMB#zQ9:[T+8*xr1FXYVJ#%J)LiKl?c?=JG8dc{cX?d?p-u&Ti)$<vJC')
    ap.add_argument('--sso-file', default='/tmp/sso_token.txt')
    ap.add_argument('--pin', default='870657')
    ap.add_argument('--template', default='/tmp/big_msg_1867.bin')
    args = ap.parse_args()

    input_ref = args.deeplink or args.tref
    if not input_ref:
        ap.error('--deeplink or --tref required')

    sso = Path(args.sso_file).read_text().strip()
    client = GoPayProtocolClientV2(K=args.K, sso=sso, pin=args.pin, template_path=args.template)
    result = client.settle_merchanttransfer(input_ref)
    print("\n=== RESULT ===")
    print(json.dumps(result, indent=2, default=str))
