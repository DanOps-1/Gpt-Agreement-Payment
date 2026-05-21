"""并发跑 no_card_plus: N worker subprocess, 各自独立 phone+sms_url."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from .. import parallel_runner


router = APIRouter(prefix="/api/run/parallel", tags=["run-parallel"])


class WorkerCfg(BaseModel):
    phone: str = Field(..., min_length=4, max_length=24)
    sms_url: str = Field(default="")  # 空 = 用 common.default_sms_url
    tag: str = Field(default="", max_length=40)
    worker_id: str = Field(default="", max_length=32)


class ParallelStartRequest(BaseModel):
    # N worker. Phone 池 (前端 slot 行) 可以少于 N, 多 worker 共享同 phone, OTP 阶段 phone-lock 排队.
    workers: list[WorkerCfg] = Field(..., min_length=1, max_length=20)
    # 公共参数 (与单 run no_card_plus 模式一致)
    config: str = ""
    paypal_country: str = "US"
    paypal_lang: str = "en"
    signup_retries: int = 3
    otp_timeout: int = 240
    node_rpa_timeout: int = 900
    max_due: int = 100
    promo_link_id: int = 0  # 0 = 各 worker 自动 claim
    allow_already_paid: bool = False
    allow_full_price: bool = False
    inventory_mail_source: str = Field(
        default="any", pattern="^(any|outlook|catch_all)$"
    )
    default_sms_url: str = ""  # 兜底, 当 worker 未单独配 sms_url
    stagger_s: float = 1.0  # 错开启动避免同一秒触发 PayPal/gost


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


# Phone OTP 临界区互斥锁: Node RPA 在 form submit (触发 SMS) 前 try-acquire,
# OTP fill 完 release. 无 auth 守护是故意的 — Node 在容器内部 loopback 自调用,
# 同 webui 已经被 reverse-proxy 框起来, 外部访问不到 :8765 直连; 而且 acquire 失败
# 不会泄露任何敏感信息 (只回 holder worker_id).
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
