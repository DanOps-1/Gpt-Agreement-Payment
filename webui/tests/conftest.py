import pytest
from fastapi.testclient import TestClient
from webui.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBUI_DATA_DIR", str(tmp_path))
    app = create_app()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_runner():
    yield
    import webui.backend.runner as r
    r._proc = None
    r._started_at = None
    r._ended_at = None
    r._exit_code = None
    r._cmd = None
    r._mode = None
    r._log_lines = []
    r._seq_counter = 0


@pytest.fixture(autouse=True)
def _reset_wa_relay():
    yield
    import webui.backend.wa_relay as w
    w._proc = None
    w._mode = ""
    w._started_at = None
