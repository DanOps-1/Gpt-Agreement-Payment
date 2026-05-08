import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..auth import CurrentUser
from ..config_health import build_config_health
from ..config_writer import write_configs
from ..db import get_db
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


class AccountImportServerRequest(BaseModel):
    url: str = "http://127.0.0.1:8787/api/import"
    token: str = "dev-import-token"
    timeout_s: float = 30


class OutlookMailPoolImportRequest(BaseModel):
    text: str = ""
    enable: bool = True


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


@router.get("/account-import-server")
def get_account_import_server(user: str = CurrentUser):
    data = _load_pay_config()
    cfg = data.get("account_import_server") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "url": str(cfg.get("url") or cfg.get("import_url") or "http://127.0.0.1:8787/api/import"),
        "token": str(cfg.get("token") or cfg.get("import_token") or "dev-import-token"),
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
        raise HTTPException(status_code=400, detail="Bearer token 不能为空")
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
    return {"ok": True, **get_db().outlook_mail_pool_stats(), "path": str(s.DB_PATH)}


@router.post("/outlook-mail-pool/import")
def import_outlook_mail_pool(req: OutlookMailPoolImportRequest, user: str = CurrentUser):
    lines = [line.strip() for line in (req.text or "").splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="Outlook 账号内容为空")
    result = get_db().import_outlook_mail_accounts(lines)
    reg_path = _resolve_reg_config_path()
    if req.enable:
        cfg, reg_path = _load_reg_config()
        mail = cfg.setdefault("mail", {})
        if not isinstance(mail, dict):
            mail = {}
            cfg["mail"] = mail
        mail["provider"] = "outlook"
        mail["outlook_source"] = "db"
        mail["outlook_poll_interval_s"] = float(mail.get("outlook_poll_interval_s") or 3)
        mail["outlook_folder"] = str(mail.get("outlook_folder") or "Inbox")
        _save_reg_config(cfg, reg_path)
    return {
        "ok": True,
        "enabled": bool(req.enable),
        "result": result,
        "path": str(s.DB_PATH),
        "reg_config_path": str(reg_path),
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


@router.post("/health")
def health(req: HealthRequest, user: str = CurrentUser):
    return build_config_health(req.model_dump())


@router.get("/health")
def health_get(user: str = CurrentUser):
    return build_config_health({})
