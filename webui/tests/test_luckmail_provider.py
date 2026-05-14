import json
import os
import subprocess
import sys
from pathlib import Path

from webui.backend.db import get_db


def test_luckmail_provider_reads_sqlite_secrets_from_ctf_reg_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBUI_DATA_DIR", str(tmp_path))
    get_db().set_runtime_json("secrets", {"luckmail": {"api_key": "lk-subprocess-key"}})

    repo = Path(__file__).resolve().parents[2]
    script = """
import json
import os
import sys

sys.path.insert(0, os.getcwd())
from luckmail_provider import resolve_luckmail_config

cfg = resolve_luckmail_config({
    "provider": "luckmail_ms_graph",
    "luckmail": {"project_code": "openai"},
})
print(json.dumps({
    "api_key": cfg["api_key"],
    "project_code": cfg["project_code"],
}))
"""
    env = dict(os.environ)
    env.pop("LUCKMAIL_API_KEY", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(repo / "CTF-reg"),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=True,
    )

    parsed = json.loads(proc.stdout)
    assert parsed == {"api_key": "lk-subprocess-key", "project_code": "openai"}
