import respx
from httpx import Response


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


@respx.mock
def test_proxy_ok_country_match(client, monkeypatch):
    _login(client)
    respx.get("https://api.ipify.org").mock(return_value=Response(200, text="1.2.3.4"))
    respx.get("http://ip-api.com/json/1.2.3.4").mock(
        return_value=Response(200, json={"status": "success", "countryCode": "US", "country": "United States"})
    )
    # 镜像里装了真 gost binary：socks5+auth 分支会 spawn 真进程往 127.0.0.1:1080
    # 转发，目标根本不存在，gost forward 失败让整体被拉成 fail。test 关心的是
    # IP/country 检测，把 gost 那段 stub 掉。
    from webui.backend.preflight import proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_port_listening", lambda port: True)
    monkeypatch.setattr(proxy_mod, "_spawn_gost_relay", lambda *a, **k: (True, "test-stub"))
    r = client.post("/api/preflight/proxy", json={
        "mode": "manual",
        "url": "socks5://user:pw@127.0.0.1:1080",
        "expected_country": "US",
    })
    assert r.json()["status"] == "ok"


@respx.mock
def test_proxy_country_mismatch(client):
    _login(client)
    respx.get("https://api.ipify.org").mock(return_value=Response(200, text="1.2.3.4"))
    respx.get("http://ip-api.com/json/1.2.3.4").mock(
        return_value=Response(200, json={"status": "success", "countryCode": "DE", "country": "Germany"})
    )
    r = client.post("/api/preflight/proxy", json={
        "mode": "manual",
        "url": "socks5://user:pw@127.0.0.1:1080",
        "expected_country": "US",
    })
    assert r.json()["status"] == "warn"


def test_proxy_mode_none(client):
    _login(client)
    r = client.post("/api/preflight/proxy", json={"mode": "none"})
    assert r.json()["status"] == "ok"
