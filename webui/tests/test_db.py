import pytest
from webui.backend.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


def test_create_and_verify_user(db):
    db.create_user("admin", "secret")
    assert db.verify_user("admin", "secret") is True
    assert db.verify_user("admin", "wrong") is False
    assert db.verify_user("nobody", "secret") is False


def test_user_count_distinguishes_uninitialized(db):
    assert db.user_count() == 0
    db.create_user("admin", "secret")
    assert db.user_count() == 1


def test_session_create_lookup_delete(db):
    db.create_user("admin", "secret")
    sid = db.create_session("admin")
    assert db.lookup_session(sid) == "admin"
    db.delete_session(sid)
    assert db.lookup_session(sid) is None


def test_session_expires(db, monkeypatch):
    import webui.backend.db as db_mod
    db.create_user("admin", "secret")
    times = [1000.0]
    monkeypatch.setattr(db_mod.time, "time", lambda: times[0])
    sid = db.create_session("admin", ttl_s=60)
    times[0] = 1061.0  # past TTL
    assert db.lookup_session(sid) is None


def test_clear_runtime_data_preserves_durable_runtime_config(db):
    db.set_runtime_json("secrets", {"cloudflare": {"api_token": "tok"}})
    db.set_runtime_json("wizard_state", {"current_step": 4, "answers": {}})
    db.set_runtime_json("wa_settings", {"engine": "baileys"})
    db.set_runtime_json("wa_session_snapshot", {"data": "snapshot"})
    db.set_runtime_json("daemon_state", {"old": True})
    db.set_runtime_json("wa_state", {"latest": {"otp": "123456"}})
    db.add_registered_account({"email": "a@example.com", "session_token": "sess"})
    db.add_card_result({"chatgpt_email": "a@example.com", "status": "succeeded"})

    db.clear_runtime_data()

    assert db.iter_registered_accounts() == []
    assert db.iter_card_results() == []
    assert db.get_runtime_json("secrets", {})["cloudflare"]["api_token"] == "tok"
    assert db.get_runtime_json("wizard_state", {})["current_step"] == 4
    assert db.get_runtime_json("wa_settings", {})["engine"] == "baileys"
    assert db.get_runtime_json("wa_session_snapshot", {})["data"] == "snapshot"
    assert db.get_runtime_json("daemon_state", {}) == {}
    assert db.get_runtime_json("wa_state", {}) == {}
