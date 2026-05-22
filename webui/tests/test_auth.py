def _setup(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})


def test_login_sets_session_cookie(client):
    _setup(client)
    r = client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert "session_id" in r.cookies


def test_login_webui_prefixed_api(client):
    client.post("/webui/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    r = client.post("/webui/api/login", json={"username": "admin", "password": "hunter2hunter2"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert "session_id" in r.cookies

    r = client.get("/webui/api/me")
    assert r.status_code == 200
    assert r.json() == {"username": "admin"}


def test_login_wrong_password(client):
    _setup(client)
    r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_me_requires_session(client):
    _setup(client)
    r = client.get("/api/me")
    assert r.status_code == 401

    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"username": "admin"}


def test_logout_clears_cookie(client):
    _setup(client)
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/logout")
    r = client.get("/api/me")
    assert r.status_code == 401
