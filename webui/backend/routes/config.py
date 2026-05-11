import json
import importlib.util
import re
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import requests
from ..auth import CurrentUser
from ..config_health import build_config_health
from ..config_writer import write_configs
from ..db import get_db
from ..account_pool import import_email_lines
from .. import settings as s
from .. import gopay_auto_unbind

router = APIRouter(prefix="/api/config", tags=["config"])


class ExportRequest(BaseModel):
    answers: dict


class HealthRequest(BaseModel):
    mode: str = "single"
    paypal: bool = True
    gopay: bool = False
    pay_only: bool = False
    register_only: bool = False
    batch: int = 0
    workers: int = 3
    self_dealer: int = 0
    count: int = 0


class GoPayAutoUnbindRequest(BaseModel):
    base_url: str = ""
    raw_request: str = ""
    unlink_raw_request: str = ""


class GoPayAutoLoginTokenRequest(BaseModel):
    country_code: str = "62"
    phone_number: str = ""
    otp_poll_url: str = ""
    gopay: dict = Field(default_factory=dict)
    timeout: float = 300.0


class GoPayAutoLoginPinRequest(BaseModel):
    country_code: str = "62"
    phone_number: str = ""
    pin: str = ""
    token_path: str = ""
    otp_poll_url: str = ""
    gopay: dict = Field(default_factory=dict)
    timeout: float = 300.0


class AccountImportServerRequest(BaseModel):
    url: str = "https://mail.shfjkqhk.site/api/email-data"
    token: str = "sakuya1.2.3."
    timeout_s: float = 30


class OutlookMailPoolImportRequest(BaseModel):
    text: str = ""


class OutlookLatestMailRequest(BaseModel):
    line: str = ""
    folder: str = "Inbox"
    top: int = 5


class GoPayAutoUnbindFetchRequest(BaseModel):
    base_url: str = ""
    raw_request: str
    timeout: float = 20.0


class GoPayAutoUnbindInspectRequest(BaseModel):
    timeout: float = 20.0


class GoPayManualUnbindRequest(BaseModel):
    unlink_url: str = ""
    service_unlink_url: str = ""
    link_id: str = ""
    unlink_raw_request: str = ""
    timeout: float = 20.0


@router.post("/export")
def export(req: ExportRequest, user: str = CurrentUser):
    return write_configs(req.answers)


def _load_pay_config() -> dict:
    path = s.PAY_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_pay_config(data: dict) -> None:
    path = s.PAY_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _load_gopay_module() -> Any:
    path = s.CTF_PAY_DIR / "gopay.py"
    if not path.exists():
        raise HTTPException(status_code=500, detail=f"gopay.py not found: {path}")
    module_name = "ctf_pay_gopay_webui"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=500, detail=f"cannot load gopay module: {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        if previous is not None:
            sys.modules[module_name] = previous
        else:
            sys.modules.pop(module_name, None)
        raise HTTPException(status_code=500, detail=f"load gopay module failed: {e}") from e
    return module


def _first_gopay_account(gopay_cfg: dict) -> dict:
    accounts = gopay_cfg.get("accounts") if isinstance(gopay_cfg, dict) else None
    if not isinstance(accounts, list):
        return {}
    for item in accounts:
        if isinstance(item, dict):
            return dict(item)
    return {}


def _build_auto_login_gopay_cfg(
    gopay_cfg: dict,
    *,
    country_code: str,
    phone_number: str,
    otp_poll_url: str = "",
    pin: str = "",
) -> dict:
    base = dict(gopay_cfg or {})
    first = _first_gopay_account(base)
    cfg = {**base, **first}
    cfg.pop("accounts", None)
    cfg["country_code"] = country_code
    cfg["phone_number"] = phone_number
    cfg["pin"] = pin or str(cfg.get("pin") or "000000")
    cfg["auto_login_country_code"] = country_code
    cfg["auto_login_phone"] = phone_number
    cfg["auto_login_pin"] = pin
    cfg["auto_login_otp_poll_url"] = otp_poll_url
    cfg["sms_otp_poll_url"] = otp_poll_url
    cfg["auto_login_otp_filter_phone"] = True
    cfg["auto_login_otp_timeout"] = float(base.get("auto_login_otp_timeout") or base.get("otp_timeout") or 300)
    cfg["auto_login_otp_interval"] = float(base.get("auto_login_otp_interval") or 1)
    cfg.setdefault("auto_login_token_dir", str(s.ROOT / "output" / "gopay_tokens"))
    return cfg


def _safe_token_result(result: dict) -> dict:
    token_result = result.get("token_result") if isinstance(result.get("token_result"), dict) else {}
    safe_token = {
        key: value
        for key, value in token_result.items()
        if key not in {"access_token", "refresh_token", "token"}
    }
    safe = {
        key: value
        for key, value in result.items()
        if key not in {"access_token", "refresh_token", "token_result", "token"}
    }
    safe["token_result"] = safe_token
    if token_result.get("token_path"):
        safe["token_path"] = token_result.get("token_path")
    return safe


def _resolve_gopay_token_path(raw: str) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="token_path is empty")
    path = Path(text)
    if not path.is_absolute():
        path = s.ROOT / path
    try:
        resolved = path.resolve()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid token_path: {e}") from e
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=400, detail=f"token file not found: {resolved}")
    return resolved


@router.get("/account-import-server")
def get_account_import_server(user: str = CurrentUser):
    data = _load_pay_config()
    cfg = data.get("account_import_server") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "url": str(cfg.get("url") or cfg.get("import_url") or "https://mail.shfjkqhk.site/api/email-data"),
        "token": str(cfg.get("token") or cfg.get("import_token") or "sakuya1.2.3."),
        "timeout_s": float(cfg.get("timeout_s") or 30),
        "path": str(s.PAY_CONFIG_PATH),
    }


@router.post("/account-import-server")
def save_account_import_server(req: AccountImportServerRequest, user: str = CurrentUser):
    url = req.url.strip()
    token = req.token.strip()
    if not url:
        raise HTTPException(status_code=400, detail="导入接口 URL 不能为空")
    if not token:
        raise HTTPException(status_code=400, detail="推送服务器凭证不能为空")
    data = _load_pay_config()
    data["account_import_server"] = {
        "url": url,
        "token": token,
        "timeout_s": float(req.timeout_s or 30),
    }
    _save_pay_config(data)
    return {"ok": True, "path": str(s.PAY_CONFIG_PATH), "account_import_server": data["account_import_server"]}


def _resolve_reg_config_path() -> Path:
    pay_cfg = _load_pay_config()
    raw = ""
    try:
        raw = str(
            (((pay_cfg.get("fresh_checkout") or {}).get("auth") or {}).get("auto_register") or {}).get("config_path")
            or ""
        ).strip()
    except Exception:
        raw = ""
    if not raw:
        return s.REG_CONFIG_PATH
    path = Path(raw)
    return path if path.is_absolute() else (s.ROOT / path)


def _load_reg_config() -> tuple[dict, Path]:
    path = _resolve_reg_config_path()
    if not path.exists():
        return {}, path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, path
    return (data if isinstance(data, dict) else {}), path


def _save_reg_config(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("/outlook-mail-pool")
def outlook_mail_pool_stats(user: str = CurrentUser):
    return {"ok": True, **get_db().outlook_mail_pool_stats(), "path": str(get_db().path)}


@router.post("/outlook-mail-pool/import")
def import_outlook_mail_pool(req: OutlookMailPoolImportRequest, user: str = CurrentUser):
    lines = [line.strip() for line in (req.text or "").splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="Outlook 账号内容为空")
    result = get_db().import_outlook_mail_accounts(lines)
    pool_result = import_email_lines(
        lines,
        source="outlook_mail_pool_import",
        import_batch="outlook_mail_pool",
    )
    reg_path = _resolve_reg_config_path()
    return {
        "ok": True,
        "result": result,
        "pool_result": pool_result,
        "path": str(get_db().path),
        "reg_config_path": str(reg_path),
    }


def _parse_outlook_line(line: str) -> tuple[str, str, str, str]:
    parts = [p.strip() for p in str(line or "").strip().split("----", 3)]
    if len(parts) != 4 or not parts[0] or not parts[2] or not parts[3]:
        raise HTTPException(status_code=400, detail="格式错误：邮箱----密码----client_id----refresh_token")
    return parts[0].lower(), parts[1], parts[2], parts[3]


def _extract_otp(text: str) -> str:
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text or "")
    return m.group(1) if m else ""


@router.post("/outlook-mail/latest")
def read_outlook_latest_mail(req: OutlookLatestMailRequest, user: str = CurrentUser):
    email, _password, client_id, refresh_token = _parse_outlook_line(req.line)
    folder = (req.folder or "Inbox").strip() or "Inbox"
    top = min(max(int(req.top or 5), 1), 20)
    try:
        token_resp = requests.post(
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "https://graph.microsoft.com/Mail.Read offline_access",
            },
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Outlook token 请求失败: {e}") from e
    if token_resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Outlook refresh_token 失败: http={token_resp.status_code} {token_resp.text[:240]}",
        )
    token_data = token_resp.json()
    access_token = str(token_data.get("access_token") or "")
    if not access_token:
        raise HTTPException(status_code=400, detail="Outlook token 响应未返回 access_token")
    try:
        mail_resp = requests.get(
            f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "$top": str(top),
                "$select": "subject,bodyPreview,receivedDateTime,from",
                "$orderby": "receivedDateTime desc",
            },
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Graph 读取邮件失败: {e}") from e
    if mail_resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Graph 读取邮件失败: http={mail_resp.status_code} {mail_resp.text[:300]}",
        )
    messages = []
    for msg in mail_resp.json().get("value") or []:
        from_addr = ""
        try:
            from_addr = str(((msg.get("from") or {}).get("emailAddress") or {}).get("address") or "")
        except Exception:
            from_addr = ""
        subject = str(msg.get("subject") or "")
        preview = str(msg.get("bodyPreview") or "")
        messages.append({
            "receivedDateTime": str(msg.get("receivedDateTime") or ""),
            "from": from_addr,
            "subject": subject,
            "otp": _extract_otp(f"{subject}\n{preview}"),
            "preview": preview[:500],
        })
    return {
        "ok": True,
        "email": email,
        "scope": token_data.get("scope") or "",
        "count": len(messages),
        "latest": messages[0] if messages else None,
        "messages": messages,
    }


@router.post("/gopay/auto-unbind")
def save_gopay_auto_unbind(req: GoPayAutoUnbindRequest, user: str = CurrentUser):
    path = s.PAY_CONFIG_PATH
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    gopay = data.setdefault("gopay", {})
    if not isinstance(gopay, dict):
        gopay = {}
        data["gopay"] = gopay
    auto_unbind = {}
    if req.base_url.strip():
        auto_unbind["base_url"] = req.base_url.strip()
    if req.raw_request.strip():
        auto_unbind["raw_request"] = req.raw_request
    if req.unlink_raw_request.strip():
        auto_unbind["unlink_raw_request"] = req.unlink_raw_request
    if auto_unbind:
        gopay["auto_unbind"] = auto_unbind
    else:
        gopay.pop("auto_unbind", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(path), "configured": bool(auto_unbind)}


@router.post("/gopay/auto-unbind/fetch-body")
def fetch_gopay_auto_unbind_body(req: GoPayAutoUnbindFetchRequest, user: str = CurrentUser):
    try:
        result = gopay_auto_unbind.fetch_linkedapps(req.raw_request, req.base_url, timeout=req.timeout)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"request failed: {e}")
    result.pop("request_headers", None)
    return result


@router.post("/gopay/auto-unbind/linkedapps")
def inspect_gopay_linkedapps(req: GoPayAutoUnbindInspectRequest, user: str = CurrentUser):
    try:
        result = gopay_auto_unbind.fetch_linkedapps_from_config(s.PAY_CONFIG_PATH, timeout=req.timeout)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"request failed: {e}")
    if result.get("skipped"):
        raise HTTPException(status_code=400, detail=result.get("reason", "auto_unbind_not_configured"))
    return result


@router.post("/gopay/auto-unbind/manual")
def manual_gopay_unbind(req: GoPayManualUnbindRequest, user: str = CurrentUser):
    try:
        result = gopay_auto_unbind.unlink_entry_from_config(
            s.PAY_CONFIG_PATH,
            unlink_url=req.unlink_url,
            service_unlink_url=req.service_unlink_url,
            link_id=req.link_id,
            unlink_raw_request=req.unlink_raw_request,
            timeout=req.timeout,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"request failed: {e}")
    if result.get("skipped"):
        raise HTTPException(status_code=400, detail=result.get("reason", "auto_unbind_not_configured"))
    return result


@router.post("/gopay/auto-login/token")
def run_gopay_auto_login_token(req: GoPayAutoLoginTokenRequest, user: str = CurrentUser):
    country_code = _digits(req.country_code) or "62"
    phone_number = _digits(req.phone_number)
    otp_poll_url = str(req.otp_poll_url or "").strip()
    if not phone_number:
        raise HTTPException(status_code=400, detail="GoPay phone_number is empty")
    if not otp_poll_url:
        raise HTTPException(status_code=400, detail="GoPay OTP poll URL is empty")
    timeout = max(float(req.timeout or 300), 1.0)
    gopay_mod = _load_gopay_module()
    cfg = _build_auto_login_gopay_cfg(
        req.gopay,
        country_code=country_code,
        phone_number=phone_number,
        otp_poll_url=otp_poll_url,
    )
    cfg["auto_login_setup_pin"] = False
    cfg["auto_login_otp_timeout"] = timeout
    logs: list[str] = []
    try:
        charger = gopay_mod.GoPayCharger(
            requests.Session(),
            cfg,
            otp_provider=lambda: "",
            log=lambda line: logs.append(str(line)),
        )
        result = charger._gopay_auto_login_if_configured()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GoPay auto-login token failed: {e}") from e
    safe = _safe_token_result(result if isinstance(result, dict) else {})
    token_path = str(safe.get("token_path") or "")
    if not token_path:
        token_result = safe.get("token_result") if isinstance(safe.get("token_result"), dict) else {}
        token_path = str(token_result.get("token_path") or "")
    if not token_path:
        raise HTTPException(status_code=502, detail="GoPay auto-login finished without token_path")
    return {
        "ok": True,
        "country_code": country_code,
        "phone_number": phone_number,
        "token_path": token_path,
        "result": safe,
        "logs": logs[-12:],
    }


@router.post("/gopay/auto-login/pin")
def run_gopay_auto_login_pin(req: GoPayAutoLoginPinRequest, user: str = CurrentUser):
    country_code = _digits(req.country_code) or "62"
    phone_number = _digits(req.phone_number)
    pin = _digits(req.pin)
    otp_poll_url = str(req.otp_poll_url or "").strip()
    if not phone_number:
        raise HTTPException(status_code=400, detail="GoPay phone_number is empty")
    if len(pin) != 6:
        raise HTTPException(status_code=400, detail="GoPay PIN must be 6 digits")
    if not otp_poll_url:
        raise HTTPException(status_code=400, detail="GoPay OTP poll URL is empty")
    timeout = max(float(req.timeout or 300), 1.0)
    token_path = _resolve_gopay_token_path(req.token_path)
    try:
        saved = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"read token file failed: {e}") from e
    if not isinstance(saved, dict):
        raise HTTPException(status_code=400, detail="token file payload is invalid")
    token_data = saved.get("token") if isinstance(saved.get("token"), dict) else {}
    access_token = str(token_data.get("access_token") or "")
    if not access_token:
        raise HTTPException(status_code=400, detail="token file missing access_token")
    gopay_mod = _load_gopay_module()
    cfg = _build_auto_login_gopay_cfg(
        req.gopay,
        country_code=country_code,
        phone_number=phone_number,
        otp_poll_url=otp_poll_url,
        pin=pin,
    )
    cfg["auto_login_setup_pin"] = True
    cfg["auto_login_otp_timeout"] = timeout
    client_id = str(saved.get("client_id") or cfg.get("auto_login_client_id") or gopay_mod.GOPAY_LOGIN_CLIENT_ID)
    client_secret = str(cfg.get("auto_login_client_secret") or gopay_mod.GOPAY_LOGIN_CLIENT_SECRET)
    logs: list[str] = []
    try:
        charger = gopay_mod.GoPayCharger(
            requests.Session(),
            cfg,
            otp_provider=lambda: "",
            log=lambda line: logs.append(str(line)),
        )
        result = charger._gopay_setup_pin_after_login(
            phone=phone_number,
            country_code=country_code,
            access_token=access_token,
            client_id=client_id,
            client_secret=client_secret,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GoPay auto-login PIN failed: {e}") from e
    saved["pin_setup"] = result
    saved["pin_set_at"] = time.time()
    token_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "country_code": country_code,
        "phone_number": phone_number,
        "token_path": str(token_path),
        "pin_setup": result,
        "logs": logs[-12:],
    }


@router.post("/health")
def health(req: HealthRequest, user: str = CurrentUser):
    return build_config_health(req.model_dump())


@router.get("/health")
def health_get(user: str = CurrentUser):
    return build_config_health({})
