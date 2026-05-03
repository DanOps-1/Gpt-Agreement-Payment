"""Preflight test for the Cloudflare KV-backed OTP path.

Mocks `urllib.request.build_opener(...)` so we never make real HTTP
requests; matches the behaviour of webui/backend/preflight/cloudflare_kv.py
which builds an opener with no proxy and calls 3 endpoints in order.
"""
import io
import json
from unittest.mock import patch


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


class _FakeResp:
    def __init__(self, status, body, ctype="application/json"):
        self.status = status
        self._body = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return self._body


class _FakeOpener:
    def __init__(self, route_map):
        self.route_map = route_map

    def open(self, req, timeout=None):
        url = req.full_url
        # 优先 endswith 精确匹配；不命中再用 contains 兜底（避免 /accounts/X
        # 把 /accounts/X/workers/... 截胡）
        for needle, resp in self.route_map.items():
            if url.endswith(needle):
                return self._reply(resp)
        for needle, resp in self.route_map.items():
            if needle in url:
                return self._reply(resp)
        return _FakeResp(404, {"success": False, "errors": [{"message": "no route"}]})

    def _reply(self, resp):
        if isinstance(resp, Exception):
            raise resp
        return _FakeResp(*resp) if isinstance(resp, tuple) else resp


def _patch_opener(monkeypatch, route_map):
    import urllib.request

    def fake_build(*args, **kwargs):
        return _FakeOpener(route_map)

    monkeypatch.setattr(urllib.request, "build_opener", fake_build)


def test_cf_kv_all_ok(client, monkeypatch):
    _login(client)
    _patch_opener(monkeypatch, {
        "/accounts/acct-1": (200, {"success": True, "result": {"name": "MyAccount"}}),
        "/storage/kv/namespaces/kv-1": (200, {"success": True, "result": {"title": "OTP_KV"}}),
        "/workers/scripts?per_page=100": (200, {"success": True, "result": [{"id": "otp-relay"}]}),
    })
    r = client.post("/api/preflight/cloudflare_kv", json={
        "api_token": "tok",
        "account_id": "acct-1",
        "kv_namespace_id": "kv-1",
        "worker_name": "otp-relay",
    })
    assert r.json()["status"] == "ok"


def test_cf_kv_missing_kv(client, monkeypatch):
    _login(client)
    _patch_opener(monkeypatch, {
        "/accounts/acct-1": (200, {"success": True, "result": {"name": "MyAccount"}}),
        "/storage/kv/namespaces/kv-bad": (200, {"success": False, "errors": [{"code": 10013, "message": "namespace not found"}]}),
        "/workers/scripts?per_page=100": (200, {"success": True, "result": [{"id": "otp-relay"}]}),
    })
    r = client.post("/api/preflight/cloudflare_kv", json={
        "api_token": "tok",
        "account_id": "acct-1",
        "kv_namespace_id": "kv-bad",
        "worker_name": "otp-relay",
    })
    assert r.json()["status"] == "fail"


def test_cf_kv_bad_token(client, monkeypatch):
    _login(client)
    _patch_opener(monkeypatch, {
        "/accounts/acct-1": (403, {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}),
    })
    r = client.post("/api/preflight/cloudflare_kv", json={
        "api_token": "bad",
        "account_id": "acct-1",
        "kv_namespace_id": "kv-1",
        "worker_name": "otp-relay",
    })
    assert r.json()["status"] == "fail"


def test_cf_kv_worker_missing_warns(client, monkeypatch):
    """worker 不存在不应硬 fail（用户可能晚点再 deploy），降级 warn。"""
    _login(client)
    _patch_opener(monkeypatch, {
        "/accounts/acct-1": (200, {"success": True, "result": {"name": "MyAccount"}}),
        "/storage/kv/namespaces/kv-1": (200, {"success": True, "result": {"title": "OTP_KV"}}),
        "/workers/scripts?per_page=100": (200, {"success": True, "result": [{"id": "some-other-script"}]}),
    })
    r = client.post("/api/preflight/cloudflare_kv", json={
        "api_token": "tok",
        "account_id": "acct-1",
        "kv_namespace_id": "kv-1",
        "worker_name": "otp-relay",
    })
    body = r.json()
    # account + kv 都 ok，worker warn → 整体应该是 warn 或 ok（取 aggregate 规则）
    assert body["status"] in ("warn", "ok")
