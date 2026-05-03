def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


def test_whatsapp_status_requires_auth(client):
    r = client.get("/api/whatsapp/status")
    assert r.status_code == 401


def test_whatsapp_status_authed(client, monkeypatch):
    _login(client)

    from webui.backend import wa_relay
    monkeypatch.setattr(wa_relay, "status", lambda: {"running": False, "status": "stopped"})

    r = client.get("/api/whatsapp/status")
    assert r.status_code == 200
    assert r.json()["status"] == "stopped"


def test_whatsapp_start_calls_relay(client, monkeypatch):
    _login(client)

    from webui.backend import wa_relay
    calls = []

    def fake_start(mode="qr", pairing_phone="", engine=""):
        calls.append((mode, pairing_phone, engine))
        return {"running": True, "status": "awaiting_qr_scan"}

    monkeypatch.setattr(wa_relay, "start", fake_start)

    r = client.post("/api/whatsapp/start", json={"mode": "qr", "engine": "wwebjs"})
    assert r.status_code == 200
    assert r.json()["running"] is True
    assert calls == [("qr", "", "wwebjs")]


def test_whatsapp_start_rejects_bad_engine(client):
    _login(client)

    r = client.post("/api/whatsapp/start", json={"mode": "qr", "engine": "nope"})
    assert r.status_code == 400
    assert "engine must be baileys or wwebjs" in r.json()["detail"]


def test_whatsapp_engine_aliases():
    from webui.backend import wa_relay

    assert wa_relay._normalize_engine("baileys") == "baileys"
    assert wa_relay._normalize_engine("wwebjs") == "wwebjs"
    assert wa_relay._normalize_engine("whatsapp-web.js") == "wwebjs"


def test_whatsapp_preferred_engine_persists(client, tmp_path, monkeypatch):
    _login(client)

    from webui.backend import wa_relay

    monkeypatch.setenv("WEBUI_DATA_DIR", str(tmp_path))
    wa_relay.set_preferred_engine("wwebjs")

    assert wa_relay._read_preferred_engine() == "wwebjs"
    status = wa_relay.status()
    assert status["preferred_engine"] == "wwebjs"
    assert status["engine"] == "wwebjs"


def test_whatsapp_settings_route_persists_engine(client):
    _login(client)

    r = client.post("/api/whatsapp/settings", json={"engine": "wwebjs"})
    assert r.status_code == 200
    body = r.json()
    assert body["preferred_engine"] == "wwebjs"
    assert body["engine"] == "wwebjs"


def test_whatsapp_session_snapshot_roundtrip(tmp_path, monkeypatch):
    from webui.backend import wa_relay
    from webui.backend.db import get_db

    monkeypatch.setenv("WEBUI_DATA_DIR", str(tmp_path))
    db = get_db()
    db.clear_runtime_data()

    session_dir = tmp_path / "wa_session"
    nested = session_dir / "baileys-gopay"
    nested.mkdir(parents=True)
    (nested / "creds.json").write_text('{"registered":true}', encoding="utf-8")

    wa_relay._persist_session_snapshot()
    assert not session_dir.exists()
    assert db.has_runtime_key("wa_session_snapshot")

    assert wa_relay._restore_session_snapshot() is True
    assert (nested / "creds.json").read_text(encoding="utf-8") == '{"registered":true}'

    wa_relay._clear_session_snapshot()
    assert not session_dir.exists()
    assert not db.has_runtime_key("wa_session_snapshot")


def test_whatsapp_start_error_returns_400(client, monkeypatch):
    _login(client)

    from webui.backend import wa_relay
    monkeypatch.setattr(wa_relay, "start", lambda **_: (_ for _ in ()).throw(RuntimeError("boom")))

    r = client.post("/api/whatsapp/start", json={"mode": "qr"})
    assert r.status_code == 400
    assert "boom" in r.json()["detail"]
