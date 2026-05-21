"""GoPay HTTP client — sends authenticated requests using v2 signer.

Verified working against `customer.gopayapi.com/v1/support/customer/activity` (HTTP 200).

Usage:
    client = GoPayClient(
        K=b'<displayEncoderKey>',
        sso='<JWE SSO bearer token>',
        template_path='/tmp/big_msg_1867.bin',
    )
    status, body = client.get('customer.gopayapi.com', '/v1/users/profile')
    status, body = client.post('customer.gopayapi.com', '/v1/support/customer/activity', body=b'{}')
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

from gopay.sign.v2 import load_template, build_sign_headers


# Captured device fingerprint from samsung SM-G780F test device
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
class GoPayClient:
    K: bytes
    sso: str
    template_path: str
    device_id: str = 'b66aedfffc4c1068'
    app_version: str = '2.8.0'
    x_m1: str = DEFAULT_X_M1
    location: str = '35.6763787,139.649962'
    location_accuracy: str = '14.16100025177002'
    locale: str = 'en_ID'
    timezone: str = 'Asia/Jakarta'
    country_code: str = 'ID'
    service_area: str = '1'
    phone_make: str = 'samsung'
    device_os: str = 'Android, 12'
    user_agent: str = 'GoPay/2.8.0 (com.gojek.gopay; build:2080; Android, 12)'
    template: object = field(init=False)

    def __post_init__(self):
        self.template = load_template(self.template_path)

    def _build_headers(self, host: str, path: str, method: str,
                       body_len: int = 0,
                       extra: dict | None = None) -> dict:
        url_for_sign = f"{host}{path}"
        sig = build_sign_headers(self.template, self.K, url=url_for_sign, method=method)
        headers = {
            'accept-encoding': 'gzip',
            'authorization': f'Bearer {self.sso}',
            'x-location-accuracy': self.location_accuracy,
            'gojek-service-area': self.service_area,
            'country-code': self.country_code,
            'support-request-id': str(uuid.uuid4()),
            'x-appversion': self.app_version,
            'x-location': self.location,
            'x-m1': self.x_m1,
            'gojek-country-code': self.country_code,
            'x-uniqueid': self.device_id,
            'x-phonemake': self.phone_make,
            'x-help-version': self.app_version,
            'user-agent': self.user_agent,
            'x-deviceos': self.device_os,
            'x-user-type': 'customer',
            'x-appid': 'com.gojek.gopay',
            'gojek-timezone': self.timezone,
            'content-type': 'application/json',
            'x-apptype': 'GOPAY',
            'x-user-locale': self.locale,
        }
        headers.update(sig)
        if body_len:
            headers['content-length'] = str(body_len)
        if extra:
            headers.update(extra)
        return headers

    def request(self, method: str, host: str, path: str,
                body: bytes | None = None,
                extra_headers: dict | None = None,
                timeout: int = 15) -> tuple[int, bytes]:
        headers = self._build_headers(host, path, method,
                                       body_len=len(body) if body else 0,
                                       extra=extra_headers)
        req = urllib.request.Request(
            f"https://{host}{path}",
            method=method, headers=headers, data=body
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            data = resp.read()
            # Handle gzip
            if resp.headers.get('content-encoding') == 'gzip':
                import gzip
                data = gzip.decompress(data)
            return resp.status, data
        except urllib.error.HTTPError as e:
            data = e.read()
            if e.headers.get('content-encoding') == 'gzip':
                import gzip
                try: data = gzip.decompress(data)
                except: pass
            return e.code, data

    def get(self, host: str, path: str, **kwargs) -> tuple[int, bytes]:
        return self.request('GET', host, path, **kwargs)

    def post(self, host: str, path: str, body: bytes | dict | None = None, **kwargs):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        return self.request('POST', host, path, body=body, **kwargs)


# ─── CLI ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser()
    ap.add_argument('method', choices=['GET', 'POST'])
    ap.add_argument('url', help='https://host/path or just host/path')
    ap.add_argument('--body', help='JSON body (POST)', default=None)
    ap.add_argument('--K', default='1V79g&FZMB#zQ9:[T+8*xr1FXYVJ#%J)LiKl?c?=JG8dc{cX?d?p-u&Ti)$<vJC')
    ap.add_argument('--sso-file', default='/tmp/sso_token.txt')
    ap.add_argument('--template', default='/tmp/big_msg_1867.bin')
    args = ap.parse_args()

    url = args.url.replace('https://', '').replace('http://', '')
    host, _, path = url.partition('/')
    path = '/' + path

    sso = open(args.sso_file).read().strip()
    client = GoPayClient(K=args.K.encode(), sso=sso, template_path=args.template)
    code, body = client.request(args.method, host, path,
                                  body=args.body.encode() if args.body else None)
    print(f"HTTP {code}")
    try:
        parsed = json.loads(body)
        print(json.dumps(parsed, indent=2))
    except:
        print(body.decode('utf-8', errors='replace')[:2000])
    sys.exit(0 if 200 <= code < 300 else 1)
