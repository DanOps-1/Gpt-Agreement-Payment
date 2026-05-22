def test_setup_status_uninitialized(client):
    r = client.get("/api/setup/status")
    assert r.status_code == 200
    assert r.json() == {"initialized": False}


def test_setup_creates_admin_then_status_initialized(client):
    r = client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    r = client.get("/api/setup/status")
    assert r.json() == {"initialized": True}


def test_setup_webui_prefixed_api(client):
    r = client.get("/webui/api/setup/status")
    assert r.status_code == 200
    assert r.json() == {"initialized": False}

    r = client.post("/webui/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    r = client.get("/webui/api/setup/status")
    assert r.status_code == 200
    assert r.json() == {"initialized": True}


def test_setup_rejects_second_call(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    r = client.post("/api/setup", json={"username": "user", "password": "yyyyyyyyy"})
    assert r.status_code == 409


def test_setup_rejects_short_password(client):
    r = client.post("/api/setup", json={"username": "admin", "password": "short"})
    assert r.status_code == 422
