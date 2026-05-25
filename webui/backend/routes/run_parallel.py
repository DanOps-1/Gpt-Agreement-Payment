"""Concurrent run no_card_plus: N worker subprocess, each independently with phone+sms_url."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from .. import parallel_runner


router = APIRouter(prefix="/api/run/parallel", tags=["run-parallel"])


class WorkerCfg(BaseModel):
    phone: str = Field(..., min_length=4, max_length=24)
    sms_url: str = Field(default="")  # Empty = use common.default_sms_url
    tag: str = Field(default="", max_length=40)
    worker_id: str = Field(default="", max_length=32)


class ParallelStartRequest(BaseModel):
    # N worker. Phone pool (frontend slot row) can be less than N, multiple workers share same phone, OTP phase phone-lock queuing.
    workers: list[WorkerCfg] = Field(..., min_length=1, max_length=20)
    # Common parameters (consistent with single run no_card_plus mode)
    config: str = ""
    paypal_country: str = "US"
    paypal_lang: str = "en"
    signup_retries: int = 3
    otp_timeout: int = 240
    node_rpa_timeout: int = 900
    max_due: int = 100
    promo_link_id: int = 0  # 0 = each worker auto-claim
    allow_already_paid: bool = False
    allow_full_price: bool = False
    inventory_mail_source: str = Field(
        default="any", pattern="^(any|outlook|catch_all)$"
    )
    default_sms_url: str = ""  # Fallback, when worker not separately configured sms_url
    stagger_s: float = 1.0  # Stagger startup to avoid triggering PayPal/gost in same second


@router.post("/start")
def start(req: ParallelStartRequest, user: str = CurrentUser):
    common = req.model_dump(exclude={"workers"})
    workers = [w.model_dump() for w in req.workers]
    try:
        return parallel_runner.start_workers(workers, common)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/stop")
def stop(user: str = CurrentUser):
    return parallel_runner.stop_all()


@router.post("/clear")
def clear(user: str = CurrentUser):
    return parallel_runner.clear_finished()


@router.get("/status")
def status(user: str = CurrentUser):
    return parallel_runner.batch_summary()


@router.get("/logs")
def logs(worker_id: str, since: int = 0, user: str = CurrentUser):
    if not worker_id:
        raise HTTPException(status_code=400, detail="worker_id required")
    return parallel_runner.get_worker_log(worker_id, since_seq=int(since))


# Phone OTP critical section mutex lock: Node RPA try-acquire before form submit (triggers SMS),
# Release after OTP fill. No auth guardian is intentional — Node self-calls within container loopback,
# Same as webui already wrapped by reverse-proxy, external cannot directly access :8765; moreover acquire failure
# will not leak any sensitive information (only return holder worker_id).
@router.post("/phone-lock/acquire")
def phone_lock_acquire(phone: str, worker: str):
    r = parallel_runner.acquire_phone_lock(phone, worker)
    if not r.get("ok"):
        raise HTTPException(status_code=409, detail=r)
    return r


@router.post("/phone-lock/release")
def phone_lock_release(phone: str, worker: str = ""):
    return parallel_runner.release_phone_lock(phone, worker)


@router.get("/phone-lock/list")
def phone_lock_list(user: str = CurrentUser):
    return {"locks": parallel_runner.list_phone_locks()}
