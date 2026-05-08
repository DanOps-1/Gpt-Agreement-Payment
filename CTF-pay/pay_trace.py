from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from datetime import datetime
from typing import Any

import requests


_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUTPUT_DIR = os.path.join(_REPO_DIR, "output")
_LOG_DIR = os.path.join(_OUTPUT_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
PAY_TRACE_FILE = os.path.join(_LOG_DIR, "pay_out.log")
PAY_TRACE_ENABLED = str(os.environ.get("PAY_TRACE_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")

_WRITE_LOCK = threading.Lock()
_REQUESTS_PATCHED = False
_ORIGINAL_REQUESTS_REQUEST = None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    try:
        return str(value)
    except Exception:
        return repr(value)


def _dump(value: Any) -> str:
    try:
        return json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=False)
    except Exception:
        return repr(value)


def _headers_to_dict(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        items = headers.items()
    except Exception:
        return out
    for key, value in items:
        out[str(key)] = str(value)
    return out


def _cookies_to_dict(cookies: Any) -> Any:
    if cookies is None:
        return {}
    if isinstance(cookies, dict):
        return cookies
    try:
        return cookies.get_dict()
    except Exception:
        return str(cookies)


def _response_text(resp: Any) -> str:
    try:
        return resp.text
    except Exception:
        pass
    try:
        content = resp.content
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return str(content)
    except Exception:
        return "<unable to read response body>"


def _request_headers(session_obj: Any, kwargs: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    headers.update(_headers_to_dict(getattr(session_obj, "headers", {}) or {}))
    headers.update(_headers_to_dict(kwargs.get("headers") or {}))
    return headers


def _request_cookies(session_obj: Any, kwargs: dict[str, Any]) -> Any:
    cookies: dict[str, Any] = {}
    session_cookies = _cookies_to_dict(getattr(session_obj, "cookies", None))
    if isinstance(session_cookies, dict):
        cookies.update(session_cookies)
    else:
        cookies["session_cookies"] = session_cookies
    request_cookies = _cookies_to_dict(kwargs.get("cookies"))
    if isinstance(request_cookies, dict):
        cookies.update(request_cookies)
    elif request_cookies:
        cookies["request_cookies"] = request_cookies
    return cookies


def _write_block(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    with _WRITE_LOCK:
        with open(PAY_TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(text)


def log_pay_event(title: str, payload: Any = None) -> None:
    if not PAY_TRACE_ENABLED:
        return
    lines = [
        "===启动===",
        f"time={datetime.now().isoformat()}",
        f"event={title}",
    ]
    if payload is not None:
        lines.append("payload=" + _dump(payload))
    lines.append("===结束===")
    _write_block(lines)


def _trace_call(label: str, session_obj: Any, original_request, method: str, url: str, kwargs: dict[str, Any]):
    trace_id = uuid.uuid4().hex
    started = time.time()
    req_lines = [
        "===启动===",
        f"trace_id={trace_id}",
        f"time={datetime.now().isoformat()}",
        f"label={label}",
        f"thread={threading.current_thread().name}",
        f"method={str(method).upper()}",
        f"url={url}",
        "request_headers=" + _dump(_request_headers(session_obj, kwargs)),
        "request_cookies=" + _dump(_request_cookies(session_obj, kwargs)),
    ]
    for key in (
        "params",
        "data",
        "json",
        "files",
        "auth",
        "timeout",
        "allow_redirects",
        "proxies",
        "proxy",
        "verify",
        "cert",
    ):
        if key in kwargs:
            req_lines.append(f"request_{key}=" + _dump(kwargs.get(key)))
    try:
        resp = original_request(method, url, **kwargs)
    except Exception as exc:
        req_lines.extend([
            f"elapsed_ms={int((time.time() - started) * 1000)}",
            f"error_type={type(exc).__name__}",
            "error=" + str(exc),
            "traceback=" + traceback.format_exc(),
            "===结束===",
        ])
        _write_block(req_lines)
        raise

    req_obj = getattr(resp, "request", None)
    if req_obj is not None:
        prepared_headers = _headers_to_dict(getattr(req_obj, "headers", {}) or {})
        if prepared_headers:
            req_lines.append("prepared_request_headers=" + _dump(prepared_headers))
        prepared_body = getattr(req_obj, "body", None)
        if prepared_body is not None:
            req_lines.append("prepared_request_body=" + _dump(prepared_body))

    req_lines.extend([
        f"elapsed_ms={int((time.time() - started) * 1000)}",
        f"response_url={getattr(resp, 'url', '')}",
        f"response_status={getattr(resp, 'status_code', '')}",
        "response_headers=" + _dump(_headers_to_dict(getattr(resp, "headers", {}) or {})),
        "response_cookies=" + _dump(_cookies_to_dict(getattr(resp, "cookies", None))),
        "response_body=" + _response_text(resp),
        "===结束===",
    ])
    _write_block(req_lines)
    return resp


def trace_session(session_obj: Any, label: str = "session") -> Any:
    if not PAY_TRACE_ENABLED:
        return session_obj
    if session_obj is None or getattr(session_obj, "_pay_trace_wrapped", False):
        return session_obj
    original_request = getattr(session_obj, "request", None)
    if not callable(original_request):
        return session_obj

    def traced_request(method: str, url: str, **kwargs: Any):
        return _trace_call(label, session_obj, original_request, method, url, kwargs)

    try:
        setattr(session_obj, "request", traced_request)
        setattr(session_obj, "_pay_trace_wrapped", True)
    except Exception:
        return session_obj
    return session_obj


def install_requests_trace(label: str = "requests") -> None:
    global _REQUESTS_PATCHED, _ORIGINAL_REQUESTS_REQUEST
    if not PAY_TRACE_ENABLED:
        return
    if _REQUESTS_PATCHED:
        return
    _ORIGINAL_REQUESTS_REQUEST = requests.sessions.Session.request

    def traced_request(self, method: str, url: str, **kwargs: Any):
        if getattr(self, "_pay_trace_wrapped", False):
            return _ORIGINAL_REQUESTS_REQUEST(self, method, url, **kwargs)
        return _trace_call(label, self, lambda m, u, **kw: _ORIGINAL_REQUESTS_REQUEST(self, m, u, **kw), method, url, kwargs)

    requests.sessions.Session.request = traced_request
    _REQUESTS_PATCHED = True
