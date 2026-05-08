from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .gopay_signer import signed_headers


DROP_REQUEST_HEADERS = {
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
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


def _text_preview(value: Any, max_chars: int = 3000) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    elif value is None:
        text = ""
    else:
        text = str(value)
    if len(text) > max_chars:
        return text[:max_chars] + "...<truncated>"
    return text


def _link_id_from_unlink_url(unlink_url: str) -> str:
    raw = (unlink_url or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    query_link_id = (parse_qs(parsed.query).get("link_id") or [""])[0].strip()
    if query_link_id:
        return query_link_id
    marker = "/v1/links/"
    if marker in parsed.path:
        return parsed.path.split(marker, 1)[1].strip("/")
    return ""


def extract_gopay_link_entries(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    services = data.get("linked_services")
    if not isinstance(services, list):
        return []
    entries: list[dict[str, Any]] = []
    for service in services:
        if not isinstance(service, dict):
            continue
        service_unlink_url = str(service.get("unlink_service_url") or "").strip()
        accounts = service.get("linked_accounts")
        if not isinstance(accounts, list):
            continue
        for account in accounts:
            if not isinstance(account, dict):
                continue
            account_unlink_url = str(account.get("unlink_url") or "").strip()
            link_id = str(account.get("link_id") or "").strip()
            if not link_id:
                link_id = _link_id_from_unlink_url(account_unlink_url) or _link_id_from_unlink_url(service_unlink_url)
            entries.append({
                "service_id": service.get("service_id") or "",
                "service_name": service.get("service_name") or "",
                "service_unlink_url": service_unlink_url,
                "allow_service_unlink": service.get("allow_service_unlink"),
                "link_id": link_id,
                "association_name": account.get("association_name") or "",
                "activation_date": account.get("activation_date") or "",
                "is_active": account.get("is_active"),
                "unlink_url": account_unlink_url,
                "allow_account_unlink": account.get("allow_account_unlink"),
            })
    return entries


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


def _count_linked_services(payload: Any) -> tuple[int, int]:
    data = payload.get("data") if isinstance(payload, dict) else None
    services = data.get("linked_services") if isinstance(data, dict) else None
    if not isinstance(services, list):
        return 0, 0
    account_count = 0
    for service in services:
        accounts = service.get("linked_accounts") if isinstance(service, dict) else None
        if isinstance(accounts, list):
            account_count += len(accounts)
    return len(services), account_count


def _path_for_signature(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return parsed.netloc, path


def _signed_request_headers(parsed: dict[str, Any], *, method: str | None = None, url: str | None = None, body: str | None = None) -> dict[str, str]:
    req_method = (method or parsed["method"]).upper()
    req_url = url or parsed["url"]
    req_body = parsed["body"] if body is None else body
    host, path = _path_for_signature(req_url)
    return signed_headers(
        parsed["headers"],
        method=req_method,
        host=host,
        path=path,
        body=req_body or "",
    )


def fetch_linkedapps(raw_request: str, base_url: str = "", timeout: float = 20.0) -> dict[str, Any]:
    parsed = parse_raw_http_request(raw_request, base_url)
    headers = _signed_request_headers(parsed)
    with httpx.Client(timeout=max(1.0, min(float(timeout or 20.0), 60.0)), follow_redirects=False) as client:
        resp = client.request(
            parsed["method"],
            parsed["url"],
            headers=headers,
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
        "request_headers": headers,
    }


def _resolve_unlink_target(parsed_source_url: str, base_url: str, unlink_url: str, *, normalize: bool = True) -> str:
    target_url = normalize_unlink_target(unlink_url) if normalize else (unlink_url or "").strip()
    if not target_url:
        raise ValueError("unlink_url is empty")
    parsed_source = urlsplit(parsed_source_url)
    origin = f"{parsed_source.scheme}://{parsed_source.netloc}"
    if not target_url.startswith("http://") and not target_url.startswith("https://"):
        target_url = urljoin(normalize_base_url(base_url or origin), target_url.lstrip("/"))
    return target_url


def patch_unlink(
    raw_request: str,
    base_url: str,
    unlink_url: str,
    timeout: float = 20.0,
    *,
    normalize: bool = True,
    unlink_raw_request: str = "",
) -> dict[str, Any]:
    request_template = unlink_raw_request if (unlink_raw_request or "").strip() else raw_request
    parsed = parse_raw_http_request(request_template, base_url)
    target_url = _resolve_unlink_target(parsed["url"], base_url, unlink_url, normalize=normalize)
    body_text = parsed["body"]
    body = body_text.encode("utf-8") if body_text else b""
    headers = _signed_request_headers(parsed, method="PATCH", url=target_url, body=body_text)
    with httpx.Client(timeout=max(1.0, min(float(timeout or 20.0), 60.0)), follow_redirects=False) as client:
        resp = client.request("PATCH", target_url, headers=headers, content=body)
    body_json = None
    try:
        body_json = resp.json()
    except Exception:
        pass
    business_success = None
    if isinstance(body_json, dict) and "success" in body_json:
        business_success = bool(body_json.get("success"))
    return {
        "ok": 200 <= resp.status_code < 300 and business_success is not False,
        "url": str(resp.request.url),
        "status_code": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "body": resp.text,
        "body_json": body_json,
        "success": business_success,
        "used_unlink_raw_request": bool((unlink_raw_request or "").strip()),
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


def _is_same_link(entry: dict[str, Any], link_id: str) -> bool:
    if not link_id:
        return False
    if str(entry.get("link_id") or "").strip() == link_id:
        return True
    return (
        _link_id_from_unlink_url(str(entry.get("unlink_url") or "")) == link_id
        or _link_id_from_unlink_url(str(entry.get("service_unlink_url") or "")) == link_id
    )


def _select_unlink_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not entries:
        return None

    def score(entry: dict[str, Any]) -> tuple[int, str]:
        service_id = str(entry.get("service_id") or "").lower()
        service_name = str(entry.get("service_name") or "").lower()
        association = str(entry.get("association_name") or "").lower()
        text = " ".join([service_id, service_name, association])
        preferred = int("openai" in text or "midtrans" in text or service_id == "checkout_midtrans")
        active = int(entry.get("is_active") is not False)
        allowed = int(entry.get("allow_account_unlink") is not False or entry.get("allow_service_unlink") is not False)
        return (preferred * 100 + active * 10 + allowed, str(entry.get("activation_date") or ""))

    return sorted(entries, key=score, reverse=True)[0]


def _unlink_candidates(entry: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, bool]] = set()

    def add(label: str, url: str, normalize: bool) -> None:
        url = (url or "").strip()
        key = (url, normalize)
        if not url or key in seen:
            return
        seen.add(key)
        candidates.append({"label": label, "url": url, "normalize": normalize})

    service_url = str(entry.get("service_unlink_url") or "")
    account_url = str(entry.get("unlink_url") or "")
    link_id = str(entry.get("link_id") or "").strip()
    add("service_unlink_url", service_url, True)
    add("linked_account_unlink_url_normalized", account_url, True)
    add("linked_account_unlink_url_raw", account_url, False)
    if link_id:
        add("link_id_path", f"/v1/links/{link_id}", False)
    return candidates


def _load_auto_unbind_from_gopay(gp: dict[str, Any]) -> tuple[str, str, str, dict[str, Any] | None]:
    auto = gp.get("auto_unbind") if isinstance(gp, dict) else {}
    if not isinstance(auto, dict):
        return "", "", "", {"ok": False, "skipped": True, "reason": "auto_unbind_not_configured"}
    base_url = str(auto.get("base_url") or "")
    raw_request = str(auto.get("raw_request") or "")
    unlink_raw_request = str(auto.get("unlink_raw_request") or "")
    if not raw_request.strip():
        return base_url, raw_request, unlink_raw_request, {"ok": False, "skipped": True, "reason": "raw_request_empty"}
    return base_url, raw_request, unlink_raw_request, None


def _load_auto_unbind_config(config_path: Path) -> tuple[str, str, str, dict[str, Any] | None]:
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "", "", "", {"ok": False, "skipped": True, "reason": "config_not_found"}
    except Exception as e:
        return "", "", "", {"ok": False, "skipped": True, "reason": f"config_read_failed: {e}"}
    gp = cfg.get("gopay") if isinstance(cfg, dict) else {}
    return _load_auto_unbind_from_gopay(gp if isinstance(gp, dict) else {})


def fetch_linkedapps_from_config(config_path: Path, timeout: float = 20.0) -> dict[str, Any]:
    base_url, raw_request, unlink_raw_request, error = _load_auto_unbind_config(config_path)
    if error:
        return error
    linked = fetch_linkedapps(raw_request, base_url, timeout=timeout)
    entries = extract_gopay_link_entries(linked.get("body_json"))
    service_count, account_count = _count_linked_services(linked.get("body_json"))
    return {
        "ok": bool(linked.get("has_data")),
        "skipped": False,
        "reason": "" if linked.get("has_data") else "linkedapps_data_missing",
        "method": linked.get("method"),
        "url": linked.get("url"),
        "status_code": linked.get("status_code"),
        "content_type": linked.get("content_type"),
        "has_data": linked.get("has_data"),
        "services_count": service_count,
        "accounts_count": account_count,
        "entries": entries,
        "unlink_urls": linked.get("unlink_urls") or [],
        "unlink_raw_request_configured": bool(unlink_raw_request.strip()),
        "body": linked.get("body"),
        "body_json": linked.get("body_json"),
    }


def unlink_entry_from_config(
    config_path: Path,
    *,
    unlink_url: str = "",
    service_unlink_url: str = "",
    link_id: str = "",
    timeout: float = 20.0,
    unlink_raw_request: str = "",
    log=lambda _m: None,
) -> dict[str, Any]:
    base_url, raw_request, configured_unlink_raw_request, error = _load_auto_unbind_config(config_path)
    if error:
        return error
    unlink_raw_request = unlink_raw_request if (unlink_raw_request or "").strip() else configured_unlink_raw_request
    entry = {
        "unlink_url": unlink_url,
        "service_unlink_url": service_unlink_url,
        "link_id": link_id or _link_id_from_unlink_url(unlink_url) or _link_id_from_unlink_url(service_unlink_url),
        "service_id": "",
        "service_name": "",
        "association_name": "",
    }
    candidates = _unlink_candidates(entry)
    if not candidates:
        return {"ok": False, "skipped": False, "reason": "unlink_url_empty"}
    source = parse_raw_http_request(raw_request, base_url)
    seen_target_urls: set[str] = set()
    last_patch: dict[str, Any] | None = None
    last_verify: dict[str, Any] | None = None
    link_id = str(entry.get("link_id") or "").strip()
    attempts: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates, start=1):
        target_url = _resolve_unlink_target(
            source["url"],
            base_url,
            str(candidate.get("url") or ""),
            normalize=bool(candidate.get("normalize")),
        )
        if target_url in seen_target_urls:
            continue
        seen_target_urls.add(target_url)
        log(f"[webui] GoPay manual-unbind PATCH attempt={idx} label={candidate.get('label')} url={target_url}")
        patched = patch_unlink(
            raw_request,
            base_url,
            str(candidate.get("url") or ""),
            timeout=timeout,
            normalize=bool(candidate.get("normalize")),
            unlink_raw_request=unlink_raw_request,
        )
        last_patch = patched
        attempt_info = {
            "label": candidate.get("label"),
            "url": target_url,
            "status_code": patched.get("status_code"),
            "success": patched.get("success"),
            "ok": patched.get("ok"),
            "content_type": patched.get("content_type"),
            "body": patched.get("body"),
            "body_json": patched.get("body_json"),
            "used_unlink_raw_request": patched.get("used_unlink_raw_request"),
        }
        attempts.append(attempt_info)
        log(
            "[webui] GoPay manual-unbind PATCH result "
            f"status={patched.get('status_code')} success={patched.get('success')} ok={patched.get('ok')}"
        )
        if not patched.get("ok"):
            continue
        verify = fetch_linkedapps(raw_request, base_url, timeout=timeout)
        last_verify = verify
        verify_entries = extract_gopay_link_entries(verify.get("body_json"))
        still_present = bool(link_id and any(_is_same_link(item, link_id) for item in verify_entries))
        log(
            "[webui] GoPay manual-unbind verify "
            f"status={verify.get('status_code')} has_data={verify.get('has_data')} still_present={still_present}"
        )
        if verify.get("has_data") and (not link_id or not still_present):
            return {
                "ok": True,
                "skipped": False,
                "unlink_target_url": target_url,
                "unlink_status_code": patched.get("status_code"),
                "unlink_body": patched.get("body"),
                "unlink_body_json": patched.get("body_json"),
                "verify_status_code": verify.get("status_code"),
                "verify_still_present": still_present,
                "verify_body": verify.get("body"),
                "verify_body_json": verify.get("body_json"),
                "attempts": attempts,
            }
    return {
        "ok": False,
        "skipped": False,
        "reason": "unlink_still_present" if last_patch and last_patch.get("ok") else "unlink_request_failed",
        "unlink_status_code": last_patch.get("status_code") if last_patch else None,
        "unlink_body": last_patch.get("body") if last_patch else "",
        "unlink_body_json": last_patch.get("body_json") if last_patch else None,
        "verify_status_code": last_verify.get("status_code") if last_verify else None,
        "verify_body": last_verify.get("body") if last_verify else "",
        "verify_body_json": last_verify.get("body_json") if last_verify else None,
        "attempts": attempts,
    }


def run_from_gopay_config(gopay_cfg: dict[str, Any], log=lambda _m: None) -> dict[str, Any]:
    base_url, raw_request, unlink_raw_request, error = _load_auto_unbind_from_gopay(gopay_cfg or {})
    if error:
        return error

    log("[webui] GoPay auto-unbind start")
    log("[webui] GoPay LinksApp GET request starting")
    linked = fetch_linkedapps(raw_request, base_url)
    log(
        "[webui] GoPay LinksApp GET "
        f"method={linked.get('method')} url={linked.get('url')} "
        f"status={linked.get('status_code')} content_type={linked.get('content_type')}"
    )
    log(f"[webui] GoPay LinksApp body={_text_preview(linked.get('body_json') or linked.get('body'))}")
    if not linked.get("has_data"):
        return {
            "ok": False,
            "skipped": False,
            "reason": "linkedapps_data_missing",
            "linkedapps_status_code": linked.get("status_code"),
        }

    entries = extract_gopay_link_entries(linked.get("body_json"))
    service_count, account_count = _count_linked_services(linked.get("body_json"))
    log(
        "[webui] GoPay LinksApp parsed "
        f"has_data={linked.get('has_data')} services={service_count} "
        f"accounts={account_count} unlink_candidates={len(entries)}"
    )
    entry = _select_unlink_entry(entries)
    if not entry:
        return {
            "ok": False,
            "skipped": False,
            "reason": "unlink_url_missing",
            "linkedapps_status_code": linked.get("status_code"),
        }

    link_id = str(entry.get("link_id") or "").strip()
    unlink_url = str(entry.get("unlink_url") or "").strip()
    log(
        "[webui] GoPay auto-unbind selected "
        f"service_id={entry.get('service_id')} service_name={entry.get('service_name')} "
        f"association={entry.get('association_name')} link_id={link_id} "
        f"unlink_url={unlink_url} service_unlink_url={entry.get('service_unlink_url')}"
    )

    source = parse_raw_http_request(raw_request, base_url)
    candidates = _unlink_candidates(entry)
    last_patch: dict[str, Any] | None = None
    last_verify: dict[str, Any] | None = None
    seen_target_urls: set[str] = set()
    for idx, candidate in enumerate(candidates, start=1):
        target_url = _resolve_unlink_target(
            source["url"],
            base_url,
            str(candidate.get("url") or ""),
            normalize=bool(candidate.get("normalize")),
        )
        if target_url in seen_target_urls:
            continue
        seen_target_urls.add(target_url)
        log(
            "[webui] GoPay auto-unbind PATCH "
            f"attempt={idx}/{len(candidates)} label={candidate.get('label')} url={target_url}"
        )
        patched = patch_unlink(
            raw_request,
            base_url,
            str(candidate.get("url") or ""),
            normalize=bool(candidate.get("normalize")),
            unlink_raw_request=unlink_raw_request,
        )
        last_patch = patched
        log(
            "[webui] GoPay auto-unbind PATCH result "
            f"status={patched.get('status_code')} success={patched.get('success')} "
            f"ok={patched.get('ok')} content_type={patched.get('content_type')} "
            f"unlink_raw={patched.get('used_unlink_raw_request')} "
            f"body={_text_preview(patched.get('body_json') or patched.get('body'))}"
        )
        if not patched.get("ok"):
            continue

        for verify_attempt in range(1, 3):
            if verify_attempt > 1:
                time.sleep(2)
            verify = fetch_linkedapps(raw_request, base_url)
            last_verify = verify
            verify_entries = extract_gopay_link_entries(verify.get("body_json"))
            still_present = bool(link_id and any(_is_same_link(item, link_id) for item in verify_entries))
            log(
                "[webui] GoPay auto-unbind verify "
                f"attempt={verify_attempt}/2 status={verify.get('status_code')} "
                f"has_data={verify.get('has_data')} still_present={still_present}"
            )
            log(f"[webui] GoPay auto-unbind verify body={_text_preview(verify.get('body_json') or verify.get('body'))}")
            if verify.get("has_data") and link_id and not still_present:
                return {
                    "ok": True,
                    "skipped": False,
                    "linkedapps_status_code": linked.get("status_code"),
                    "unlink_url": unlink_url,
                    "unlink_target_url": target_url,
                    "unlink_status_code": patched.get("status_code"),
                    "unlink_body": patched.get("body"),
                    "verify_status_code": verify.get("status_code"),
                }
            if verify.get("has_data") and not link_id:
                return {
                    "ok": True,
                    "skipped": False,
                    "linkedapps_status_code": linked.get("status_code"),
                    "unlink_url": unlink_url,
                    "unlink_target_url": target_url,
                    "unlink_status_code": patched.get("status_code"),
                    "unlink_body": patched.get("body"),
                    "verify_status_code": verify.get("status_code"),
                }

        log(
            "[webui] GoPay auto-unbind target still present after PATCH; "
            "trying next URL form if available"
        )

    return {
        "ok": False,
        "skipped": False,
        "reason": "unlink_still_present" if last_patch and last_patch.get("ok") else "unlink_request_failed",
        "linkedapps_status_code": linked.get("status_code"),
        "unlink_url": unlink_url,
        "unlink_status_code": last_patch.get("status_code") if last_patch else None,
        "unlink_body": last_patch.get("body") if last_patch else "",
        "verify_status_code": last_verify.get("status_code") if last_verify else None,
    }


def run_from_config(config_path: Path, log=lambda _m: None) -> dict[str, Any]:
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": False, "skipped": True, "reason": "config_not_found"}
    except Exception as e:
        return {"ok": False, "skipped": True, "reason": f"config_read_failed: {e}"}
    gp = cfg.get("gopay") if isinstance(cfg, dict) else {}
    return run_from_gopay_config(gp if isinstance(gp, dict) else {}, log=log)
