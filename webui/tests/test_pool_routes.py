def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


def test_pool_retry_uses_requested_workers(client, monkeypatch):
    _login(client)

    from webui.backend.routes import pool as pool_route

    starts = []

    monkeypatch.setattr(
        pool_route,
        "prepare_retry_items",
        lambda ids, *, retry_type, task_id="": {
            "requested": len(ids),
            "prepared": 5,
            "retry_type": retry_type,
        },
    )

    def fake_start(**kwargs):
        starts.append(kwargs)
        return {"running": True, "mode": kwargs.get("mode"), "cmd": []}

    monkeypatch.setattr(pool_route.runner, "start", fake_start)

    r = client.post(
        "/api/pool/accounts/retry",
        json={
            "ids": [1, 2, 3, 4, 5],
            "retry_type": "payment",
            "max_workers": 4,
        },
    )

    assert r.status_code == 200
    assert starts == [{
        "mode": "batch",
        "paypal": True,
        "batch": 5,
        "workers": 4,
        "gopay": False,
        "count": 5,
        "register_only": False,
        "pay_only": True,
        "push_server": False,
    }]


def test_pool_retry_single_worker_keeps_singlexn(client, monkeypatch):
    _login(client)

    from webui.backend.routes import pool as pool_route

    starts = []
    monkeypatch.setattr(
        pool_route,
        "prepare_retry_items",
        lambda ids, *, retry_type, task_id="": {
            "requested": len(ids),
            "prepared": 3,
            "retry_type": retry_type,
        },
    )
    monkeypatch.setattr(
        pool_route.runner,
        "start",
        lambda **kwargs: starts.append(kwargs) or {"running": True},
    )

    r = client.post(
        "/api/pool/accounts/retry",
        json={
            "ids": [1, 2, 3],
            "retry_type": "registration",
            "max_workers": 1,
        },
    )

    assert r.status_code == 200
    assert starts[0]["mode"] == "singlexn"
    assert starts[0]["batch"] == 0
    assert starts[0]["workers"] == 1
    assert starts[0]["count"] == 3
    assert starts[0]["pay_only"] is False
