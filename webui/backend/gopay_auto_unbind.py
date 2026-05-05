from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx


DROP_REQUEST_HEADERS = {
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "accept-encoding",
}


def normalize_base_url(base_url: str, host: str = "") -> str:
    base = (base_url or "").strip()
    if not base and host:
        base = f"https://{host.strip()}"
    if base and "://" not in base:
        base = f"https://{base}"
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("base_url must be an http(s) URL or raw request must include Host")
    return base.rstrip("/") + "/"


def parse_raw_http_request(raw: str, base_url: str = "") -> dict[str, Any]:
    if not (raw or "").strip():
        raise ValueError("raw_request is empty")
    marker = "\r\n\r\n" if "\r\n\r\n" in raw else "\n\n"
    if marker in raw:
        head, body = raw.split(marker, 1)
    else:
        head, body = raw, ""
    lines = [line.rstrip("\r") for line in head.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        raise ValueError("raw_request is missing request line")
    parts = lines[0].split()
    if len(parts) < 2:
        raise ValueError("invalid request line")
    method = parts[0].upper()
    target = parts[1]
    headers: dict[str, str] = {}
    raw_host = ""
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        key = name.strip()
        if not key:
            continue
        if key.lower() == "host":
            raw_host = value.strip()
        if key.lower() in DROP_REQUEST_HEADERS:
            continue
        headers[key] = value.strip()
    if target.startswith("http://") or target.startswith("https://"):
        url = target
    else:
        url = urljoin(normalize_base_url(base_url, raw_host), target.lstrip("/"))
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise ValueError("parsed request URL is invalid")
    return {
        "method": method,
        "url": url,
        "headers": headers,
        "body": body,
    }


def extract_gopay_unlink_urls(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    services = data.get("linked_services")
    if not isinstance(services, list):
        return []
    urls: list[str] = []
    for service in services:
        if not isinstance(service, dict):
            continue
        accounts = service.get("linked_accounts")
        if not isinstance(accounts, list):
            continue
        for account in accounts:
            if not isinstance(account, dict):
                continue
            unlink_url = str(account.get("unlink_url") or "").strip()
            if unlink_url:
                urls.append(unlink_url)
    return urls


def has_linkedapps_data(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("data"), dict)


def fetch_linkedapps(raw_request: str, base_url: str = "", timeout: float = 20.0) -> dict[str, Any]:
    parsed = parse_raw_http_request(raw_request, base_url)
    with httpx.Client(timeout=max(1.0, min(float(timeout or 20.0), 60.0)), follow_redirects=False) as client:
        resp = client.request(
            parsed["method"],
            parsed["url"],
            headers=parsed["headers"],
            content=parsed["body"].encode("utf-8") if parsed["body"] else None,
        )
    body_text = resp.text
    body_json = None
    unlink_urls: list[str] = []
    try:
        body_json = resp.json()
        unlink_urls = extract_gopay_unlink_urls(body_json)
    except Exception:
        pass
    return {
        "ok": True,
        "method": parsed["method"],
        "url": str(resp.request.url),
        "status_code": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "body": body_text,
        "body_json": body_json,
        "has_data": has_linkedapps_data(body_json),
        "unlink_urls": unlink_urls,
        "unlink_url": unlink_urls[0] if unlink_urls else "",
        "request_headers": parsed["headers"],
    }


def patch_unlink(raw_request: str, base_url: str, unlink_url: str, timeout: float = 20.0) -> dict[str, Any]:
    parsed = parse_raw_http_request(raw_request, base_url)
    target_url = normalize_unlink_target(unlink_url)
    if not target_url:
        raise ValueError("unlink_url is empty")
    parsed_source = urlsplit(parsed["url"])
    origin = f"{parsed_source.scheme}://{parsed_source.netloc}"
    if not target_url.startswith("http://") and not target_url.startswith("https://"):
        target_url = urljoin(normalize_base_url(base_url or origin), target_url.lstrip("/"))
    with httpx.Client(timeout=max(1.0, min(float(timeout or 20.0), 60.0)), follow_redirects=False) as client:
        resp = client.request("PATCH", target_url, headers=parsed["headers"])
    body_json = None
    try:
        body_json = resp.json()
    except Exception:
        pass
    return {
        "ok": 200 <= resp.status_code < 300,
        "url": str(resp.request.url),
        "status_code": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "body": resp.text,
        "body_json": body_json,
    }


def normalize_unlink_target(unlink_url: str) -> str:
    raw = (unlink_url or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    query = parse_qs(parsed.query)
    link_id = (query.get("link_id") or [""])[0].strip()
    if parsed.path.rstrip("/") == "/v1/links" and link_id:
        cleaned_query = {k: v for k, v in query.items() if k != "link_id"}
        return urlunsplit((
            parsed.scheme,
            parsed.netloc,
            f"/v1/links/{link_id}",
            urlencode(cleaned_query, doseq=True),
            parsed.fragment,
        ))
    return raw


def run_from_config(config_path: Path, log=lambda _m: None) -> dict[str, Any]:
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": False, "skipped": True, "reason": "config_not_found"}
    except Exception as e:
        return {"ok": False, "skipped": True, "reason": f"config_read_failed: {e}"}
    gp = cfg.get("gopay") if isinstance(cfg, dict) else {}
    auto = gp.get("auto_unbind") if isinstance(gp, dict) else {}
    if not isinstance(auto, dict):
        return {"ok": False, "skipped": True, "reason": "auto_unbind_not_configured"}
    base_url = str(auto.get("base_url") or "")
    raw_request = str(auto.get("raw_request") or "")
    if not raw_request.strip():
        return {"ok": False, "skipped": True, "reason": "raw_request_empty"}
    linked = fetch_linkedapps(raw_request, base_url)
    if not linked.get("has_data"):
        return {
            "ok": False,
            "skipped": False,
            "reason": "linkedapps_data_missing",
            "status_code": linked.get("status_code"),
        }
    unlink_url = linked.get("unlink_url") or ""
    if not unlink_url:
        return {
            "ok": False,
            "skipped": False,
            "reason": "unlink_url_missing",
            "status_code": linked.get("status_code"),
        }
    log(f"[webui] GoPay auto-unbind unlink_url={unlink_url}")
    patched = patch_unlink(raw_request, base_url, unlink_url)
    return {
        "ok": bool(patched.get("ok")),
        "skipped": False,
        "linkedapps_status_code": linked.get("status_code"),
        "unlink_url": unlink_url,
        "unlink_status_code": patched.get("status_code"),
        "unlink_body": patched.get("body"),
    }
