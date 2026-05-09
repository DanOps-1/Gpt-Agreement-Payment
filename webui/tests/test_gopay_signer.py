from __future__ import annotations

from webui.backend.gopay_signer import DEFAULT_X_E2, signed_headers


BASELINE_HEADERS = {
    "authorization": "Bearer token",
    "x-m1": "m1",
    "x-uniqueid": "unique",
    "x-phonemake": "Google",
    "x-phonemodel": "Pixel",
    "x-deviceos": "Android, 12",
    "x-appversion": "2.8.0",
    "x-appid": "com.gojek.gopay",
    "x-apptype": "GOPAY",
    "x-platform": "Android",
}


def test_signed_headers_preserves_captured_x_e2_and_refreshes_x_e1():
    headers = signed_headers(
        {**BASELINE_HEADERS, "x-e1": "old-e1", "x-e2": "captured-e2"},
        method="POST",
        host="customer.gopayapi.com",
        path="/v1/explore",
        body='{"type":"QR_CODE","data":"000201010212QRISDATA"}',
    )

    assert headers["x-e2"] == "captured-e2"
    assert headers["x-e1"] != "old-e1"
    assert headers["host"] == "customer.gopayapi.com"


def test_signed_headers_falls_back_to_default_x_e2_when_missing():
    headers = signed_headers(
        BASELINE_HEADERS,
        method="GET",
        host="customer.gopayapi.com",
        path="/v1/linkedapps",
        body="",
    )

    assert headers["x-e2"] == DEFAULT_X_E2
