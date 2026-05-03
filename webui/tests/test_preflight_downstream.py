import respx
from httpx import Response


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


@respx.mock
def test_team_system_ok(client):
    _login(client)
    respx.post("http://127.0.0.1:3000/api/user/login").mock(
        return_value=Response(200, json={"success": True, "data": {"token": "t"}})
    )
    r = client.post("/api/preflight/team_system", json={
        "base_url": "http://127.0.0.1:3000",
        "username": "admin",
        "password": "p",
    })
    assert r.json()["status"] == "ok"


@respx.mock
def test_team_system_bad_creds(client):
    _login(client)
    respx.post("http://127.0.0.1:3000/api/user/login").mock(
        return_value=Response(200, json={"success": False, "message": "bad password"})
    )
    r = client.post("/api/preflight/team_system", json={
        "base_url": "http://127.0.0.1:3000",
        "username": "admin",
        "password": "p",
    })
    assert r.json()["status"] == "fail"


@respx.mock
def test_cpa_ok(client):
    _login(client)
    respx.get("https://cpa.example.com/api/v0/management/auth-files").mock(
        return_value=Response(200, json=[])
    )
    r = client.post("/api/preflight/cpa", json={
        "base_url": "https://cpa.example.com/api",
        "admin_key": "k",
    })
    assert r.json()["status"] == "ok"
