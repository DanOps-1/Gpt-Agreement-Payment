from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_browser_register():
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "CTF-reg" / "browser_register.py"
    spec = importlib.util.spec_from_file_location("browser_register", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakePage:
    url = "https://chatgpt.com/"

    def title(self):
        return "Just a moment..."

    def inner_text(self, selector, timeout=0):
        assert selector == "body"
        return "Verifying you are human. This may take a few seconds. Cloudflare Ray ID"

    def screenshot(self, path):
        Path(path).write_bytes(b"png")


def test_raise_if_blocking_challenge_reports_cloudflare(tmp_path):
    browser_register = _load_browser_register()
    screenshot_path = tmp_path / "challenge.png"

    try:
        browser_register._raise_if_blocking_challenge(
            FakePage(),
            stage="before email form",
            screenshot_path=screenshot_path,
        )
    except RuntimeError as exc:
        msg = str(exc)
    else:
        raise AssertionError("expected Cloudflare challenge to raise")

    assert "Cloudflare" in msg
    assert "before email form" in msg
    assert str(screenshot_path) in msg
    assert screenshot_path.exists()
