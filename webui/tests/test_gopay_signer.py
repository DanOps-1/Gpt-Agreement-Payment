from __future__ import annotations

from webui.backend.gopay_signer import DEFAULT_X_E2, nonce_marker_from_x_e1, sign_x_e1, signed_headers


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
    captured_x_e1 = (
        "34a45c2cfa97540affba7683d8bd2dbd3109a364ae31c505961c2de6a07f29a2:"
        "029ecdb91c58f1314ddadd0654007ec9fc8fa55dabec4a988d45dbbe8a1afa45"
        "000000000000000000000000000000009826a64441fe4119f016491efd7f0000"
        "c38392edf1b34cc84d1e7d496b67a4fa:D:1778385895260"
    )
    headers = signed_headers(
        {**BASELINE_HEADERS, "x-e1": captured_x_e1, "x-e2": "captured-e2"},
        method="POST",
        host="customer.gopayapi.com",
        path="/v1/explore",
        body='{"type":"QR_CODE","data":"000201010212QRISDATA"}',
    )

    assert headers["x-e2"] == "captured-e2"
    assert headers["x-e1"] != captured_x_e1
    assert headers["host"] == "customer.gopayapi.com"
    nonce_hex = headers["x-e1"].split(":")[1]
    assert nonce_hex[64:96] == "0" * 32
    assert nonce_hex[96:128] == "9826a64441fe4119f016491efd7f0000"


def test_signed_headers_falls_back_to_default_x_e2_when_missing():
    headers = signed_headers(
        BASELINE_HEADERS,
        method="GET",
        host="customer.gopayapi.com",
        path="/v1/linkedapps",
        body="",
    )

    assert headers["x-e2"] == DEFAULT_X_E2


def test_nonce_marker_from_x_e1_rejects_random_nonce_shape():
    assert nonce_marker_from_x_e1("old-e1") is None
    assert nonce_marker_from_x_e1(
        "53b5001cfe7e197004cf5e2799bee9334055ca6dd0d1ec2979d048173e2c0d76:"
        "d5e9e9400e35b417e239eff76cd2578d4512d11eb9601f1b7d1a7f59a3a77410"
        "39821973bc00bcec509f89eafdf94769d80243dc4cf66576d2df47304f8f540a"
        "d7f5ebe0cb78fb2f2354448eb3b9088d:D:1778384866396"
    ) is None
    assert nonce_marker_from_x_e1(
        "31069f3b2f7502377d693c16d0f59ef0fa335e66a7df7c389fad061f32e024c5:"
        "20b7e7474bffe5294b7639c211607ea81451894ba41747711c537c62fd1138c0"
        "46cb0ac9fd7fc2d5e8b0a21725219048b9841720d902afe9ecd0a56db98da6f3"
        "a73939f68e4b8a59c11d84313bb2c8ee:D:1778386407023"
    ) is None


def test_nonce_marker_from_x_e1_extracts_huawei_app_nonce_marker():
    assert nonce_marker_from_x_e1(
        "32840d8db8d0a21e349d9e259b8e3015dfb31b8a737766b10eb03a3f5c7d7c79:"
        "4d0b56e657649db0b53b0ee6d9fd988d3e9921bfe25d864233e395f371391775"
        "0000000000000000000000000000000071cf9b42ccb56d0ee0047d38fc7f0000"
        "5168f68a16231bd86dbec8b8206896d1:D:1778386476175"
    ) == "71cf9b42ccb56d0ee0047d38fc7f0000"


def test_signed_headers_explicit_nonce_marker_overrides_captured_x_e1_marker():
    captured_oppo_x_e1 = (
        "34a45c2cfa97540affba7683d8bd2dbd3109a364ae31c505961c2de6a07f29a2:"
        "029ecdb91c58f1314ddadd0654007ec9fc8fa55dabec4a988d45dbbe8a1afa45"
        "000000000000000000000000000000009826a64441fe4119f016491efd7f0000"
        "c38392edf1b34cc84d1e7d496b67a4fa:D:1778385895260"
    )
    headers = signed_headers(
        {**BASELINE_HEADERS, "x-e1": captured_oppo_x_e1},
        method="POST",
        host="customer.gopayapi.com",
        path="/v1/explore",
        body='{"type":"QR_CODE","data":"000201010212QRISDATA"}',
        nonce_marker_hex="71cf9b42ccb56d0ee0047d38fc7f0000",
    )

    nonce_hex = headers["x-e1"].split(":")[1]
    assert nonce_hex[64:96] == "0" * 32
    assert nonce_hex[96:128] == "71cf9b42ccb56d0ee0047d38fc7f0000"


def test_sign_x_e1_can_use_empty_body_with_actual_body_md5():
    kwargs = {
        "method": "POST",
        "host": "customer.gopayapi.com",
        "path": "/v1/explore",
        "body": '{"type":"QR_CODE","data":"000201010212QRISDATA"}',
        "nonce_hex": "1" * 160,
        "timestamp_ms": 1778387903112,
    }

    full_body_signature = sign_x_e1(BASELINE_HEADERS, **kwargs)
    empty_body_signature = sign_x_e1(
        BASELINE_HEADERS,
        **kwargs,
        body_for_signature="",
        body_text=kwargs["body"],
    )

    assert empty_body_signature != full_body_signature
    assert empty_body_signature.split(":", 1)[1] == full_body_signature.split(":", 1)[1]

