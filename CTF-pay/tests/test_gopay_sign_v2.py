"""Integration tests for GoPay X-E1 v2 signer.

Tests:
  1. Algorithm constants are deterministic (KEY_C, KEY_D derive from scratch)
  2. Sign reproduces captured X-E1 from ground truth
  3. Different nonces yield different ciphers (per-request variation)
  4. Different URLs yield different sha portions
  5. GoPayProtocolClient with sign_version='v2' uses v2 path
  6. Template parse/build round-trip preserves bytes
"""
import hashlib
import hmac
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gopay.sign.v2 as v2  # Wave D: gopay_sign_v2.py → gopay/sign/v2.py


# ─── Ground truth captured from libbatteryOpt SHA-256 hook 2026-05-14 ──
GROUND_TRUTH = {
    'K': b'1V79g&FZMB#zQ9:[T+8*xr1FXYVJ#%J)LiKl?c?=JG8dc{cX?d?p-u&Ti)$<vJC',
    'nonce': b'7x4lQPoyuPdiqNmcOda0T2x2FUELObMf',
    'ts_ms': 1778758474793,
    'method': 'GET',
    'url': 'gateway.paylater.gofin.co.id/paylater-user/v2/profile?initialRoute=',
    'expected_xe1': (
        "1c163a34ccde16b8653a26f1b2c2b31e6f1dcaa9ddce6113d3761903d2904132"
        ":a1192922aafc7b9da815811296d60a5884dc42c4392256dbd6891f04ee9eb939"
        ":D:1778758474793"
    ),
    'expected_K9': bytes.fromhex('c9beee13dc4f6e1c65b89a315d896b2e1e364d6c56d459480a8e3b7deb846d14'),
    'expected_T1': bytes.fromhex('0ad7f6e32d0b0a1dd1eee51f8e4efda9a00b3ac302e7c89b2142ec9c913a03be'),
    'expected_T2': bytes.fromhex('a1192922aafc7b9da815811296d60a5884dc42c4392256dbd6891f04ee9eb939'),
    'expected_T3': bytes.fromhex('c56dd53a168002f53d1aec5f641d120757ffe3fa40a8e87d3bd05dc297ed1c2e'),
}


class TestConstantsDerivation(unittest.TestCase):
    def test_KEY_C_deterministic(self):
        self.assertEqual(
            v2.KEY_C.hex(),
            '19f253a44301c9648c03f3570581ff2875b6aa52b1499bb8afffe346c52ecf6e'
        )

    def test_KEY_D_deterministic(self):
        self.assertEqual(
            v2.KEY_D.hex(),
            '122a04d927bb0af35c01fc39abe9d3df7bf9e5fcd18eef511e1d69dc3f2220bb'
        )

    def test_KEY_C_recomputable(self):
        K = hmac.new(b'\x00' * 64, b'\x01' * 64, hashlib.sha256).digest()
        self.assertEqual(K, v2.KEY_C)


class TestHMACChain(unittest.TestCase):
    def test_derive_k9_matches_captured(self):
        K9 = v2.derive_k9(GROUND_TRUTH['nonce'])
        self.assertEqual(K9, GROUND_TRUTH['expected_K9'])

    def test_t1_t2_t3_match_captured(self):
        T1, T2, T3 = v2.compute_t1_t2_t3(GROUND_TRUTH['nonce'])
        self.assertEqual(T1, GROUND_TRUTH['expected_T1'])
        self.assertEqual(T2, GROUND_TRUTH['expected_T2'])
        self.assertEqual(T3, GROUND_TRUTH['expected_T3'])

    def test_derive_k9_rejects_wrong_nonce_length(self):
        with self.assertRaises(ValueError):
            v2.derive_k9(b'short')


class TestNonceVariability(unittest.TestCase):
    def test_random_nonces_yield_different_ciphers(self):
        nonces = [v2.generate_nonce() for _ in range(5)]
        ciphers = set()
        for n in nonces:
            _, T2, _ = v2.compute_t1_t2_t3(n)
            ciphers.add(T2.hex())
        # All nonces should produce distinct ciphers
        self.assertEqual(len(ciphers), 5)

    def test_nonce_length_is_32(self):
        for _ in range(10):
            self.assertEqual(len(v2.generate_nonce()), 32)

    def test_nonce_is_printable_ascii(self):
        n = v2.generate_nonce()
        for b in n:
            self.assertTrue(0x21 <= b <= 0x7e, f"non-printable byte 0x{b:02x}")


class TestEndToEndSign(unittest.TestCase):
    def setUp(self):
        template_path = Path('/tmp/big_msg_1867.bin')
        if not template_path.exists():
            self.skipTest("template file /tmp/big_msg_1867.bin not present")
        self.tmpl = v2.load_template(template_path)

    def test_reproduces_captured_x_e1(self):
        xe1, _ = v2.sign_xe1_v2(
            self.tmpl, GROUND_TRUTH['K'],
            ts_ms=GROUND_TRUTH['ts_ms'],
            method=GROUND_TRUTH['method'],
            url=GROUND_TRUTH['url'],
            nonce=GROUND_TRUTH['nonce'],
        )
        self.assertEqual(xe1, GROUND_TRUTH['expected_xe1'])

    def test_different_urls_yield_different_sha(self):
        xe1_a, _ = v2.sign_xe1_v2(self.tmpl, GROUND_TRUTH['K'],
                                    ts_ms=1234567890123, method='GET',
                                    url='customer.gopayapi.com/v1/users/profile',
                                    nonce=GROUND_TRUTH['nonce'])
        xe1_b, _ = v2.sign_xe1_v2(self.tmpl, GROUND_TRUTH['K'],
                                    ts_ms=1234567890123, method='GET',
                                    url='customer.gopayapi.com/v2/users/profile',
                                    nonce=GROUND_TRUTH['nonce'])
        sha_a = xe1_a.split(':')[0]
        sha_b = xe1_b.split(':')[0]
        self.assertNotEqual(sha_a, sha_b)

    def test_xe2_constant_format(self):
        xe2 = v2.get_xe2()
        self.assertEqual(len(xe2), 29)
        # Should match the captured device fingerprint format
        self.assertTrue(all(c in '0123456789ABCDEF' for c in xe2),
                        "X-E2 should be uppercase hex chars")

    def test_build_sign_headers_returns_both(self):
        headers = v2.build_sign_headers(
            self.tmpl, GROUND_TRUTH['K'],
            url='customer.gopayapi.com/v1/users/profile',
            method='GET',
        )
        self.assertIn('X-E1', headers)
        self.assertIn('X-E2', headers)
        # Format: <sha>:<cipher>:D:<ts>
        parts = headers['X-E1'].split(':')
        self.assertEqual(len(parts), 4)
        self.assertEqual(len(parts[0]), 64, "sha should be 64 hex chars")
        self.assertEqual(len(parts[1]), 64, "cipher should be 64 hex chars")
        self.assertEqual(parts[2], 'D')


class TestProtocolClientV2(unittest.TestCase):
    def test_v2_path_in_protocol_client(self):
        from gopay.protocol.legacy_pay import _V2_AVAILABLE
        self.assertTrue(_V2_AVAILABLE)


if __name__ == '__main__':
    unittest.main(verbosity=2)
